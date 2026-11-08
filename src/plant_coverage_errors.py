"""
plant_coverage_errors.py
------------------------
Phase 2 test data: plant known, realistic defects in the legacy insurance file.

  data/legacy/payer_transitions.csv        clean Synthea export
  data/legacy_dirty/payer_transitions.csv  the LEGACY EHR EXPORT (what this script writes)

Defects are planted only in CURRENT, INSURED policies (the rows that will be
migrated under the "current coverage only" decision), one per patient:
  1 x orphan policy        PATIENT is a placeholder ID   -> expected R-CV01 Reject
  1 x unknown payer        PAYER is not in payers.csv    -> expected R-CV02 Reject
  1 x end before start     END_DATE before START_DATE    -> expected R-CV03 Reject
  1 x blank member ID      MEMBERID removed              -> expected R-CV05 Flag
                           (skipped if no current policy has a member ID)
  1 x overlapping row      a second current row for the same patient,
                           starting 30 days earlier      -> expected R-CV04 Fix (older row superseded)

Output: data/legacy_dirty/payer_transitions.csv
        docs/planted_coverage_errors_answer_key.csv
Run from the project folder:  python src\\plant_coverage_errors.py
"""

import csv
import random
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

SOURCE_FILE = Path("data/legacy/payer_transitions.csv")
PAYERS_FILE = Path("data/legacy/payers.csv")
DIRTY_FILE = Path("data/legacy_dirty/payer_transitions.csv")
ANSWER_KEY = Path("docs/planted_coverage_errors_answer_key.csv")
SEED = 2027
PLACEHOLDER_PATIENT = "00000000-0000-0000-0000-000000000000"
DATE_FORMAT = "%Y-%m-%dT%H:%M:%SZ"          # the format Synthea uses

rng = random.Random(SEED)


def dt(text):
    return datetime.strptime(text, DATE_FORMAT) if text else None


def main():
    df = pd.read_csv(SOURCE_FILE, dtype=str, keep_default_na=False)
    payers = pd.read_csv(PAYERS_FILE, dtype=str, keep_default_na=False)
    self_pay = set(payers.loc[(payers["OWNERSHIP"] == "NO_INSURANCE") | (payers["NAME"] == "NO_INSURANCE"), "Id"])

    # "Current" = the policy in force on the extract date. Synthea creates each yearly
    # policy in advance (END_DATE is often in the future), so the extract date is the
    # latest START_DATE in the file: the most recent policy activity recorded.
    # A policy covers the moment if it started at or before it and ends AFTER it
    # (a renewal ends at the exact moment the next policy starts: only the new one counts).
    as_of = max(dt(s) for s in df["START_DATE"] if s)
    first_current = {}
    for i, r in df.iterrows():
        start, end = dt(r["START_DATE"]), dt(r["END_DATE"])
        if (start and start <= as_of and (end is None or end > as_of)
                and r["PAYER"] not in self_pay):
            first_current.setdefault(r["PATIENT"], i)
    if len(first_current) < 5:
        raise SystemExit(f"Only {len(first_current)} patients have current insurance - need at least 5.")

    patients = rng.sample(sorted(first_current), 5)
    rows = [first_current[p] for p in patients]
    key = lambda i: f"{df.at[i, 'PATIENT']}_{df.at[i, 'START_DATE']}"
    answer = []

    def note(i, error_type, column, original, planted, rule, action):
        answer.append({"record_id": key(i), "error_type": error_type, "column": column,
                       "original": original, "planted": planted,
                       "expected_rule": rule, "expected_action": action})

    # 1. Orphan: the policy points to a patient that does not exist
    i = rows[0]
    original = df.at[i, "PATIENT"]
    df.at[i, "PATIENT"] = PLACEHOLDER_PATIENT
    note(i, "orphan_policy", "PATIENT", original, PLACEHOLDER_PATIENT, "R-CV01", "Reject")

    # 2. Unknown payer
    i = rows[1]
    original, fake = df.at[i, "PAYER"], str(uuid.UUID(int=rng.getrandbits(128)))
    df.at[i, "PAYER"] = fake
    note(i, "unknown_payer", "PAYER", original, fake, "R-CV02", "Reject")

    # 3. End date before start date
    i = rows[2]
    original = df.at[i, "END_DATE"]
    planted = (dt(df.at[i, "START_DATE"]) - timedelta(days=1)).strftime(DATE_FORMAT)
    df.at[i, "END_DATE"] = planted
    note(i, "end_before_start", "END_DATE", original, planted, "R-CV03", "Reject")

    # 4. Blank member ID (only if the policy has one)
    i = rows[3]
    if df.at[i, "MEMBERID"]:
        original = df.at[i, "MEMBERID"]
        df.at[i, "MEMBERID"] = ""
        note(i, "blank_member_id", "MEMBERID", original, "", "R-CV05", "Flag")
    else:
        print("  note: chosen policy has no MEMBERID to blank - defect 4 skipped")

    # 5. Overlapping row: a copy starting 30 days earlier (the older one must be superseded)
    i = rows[4]
    copy = df.loc[[i]].copy()
    copy.iloc[0, df.columns.get_loc("START_DATE")] = \
        (dt(df.at[i, "START_DATE"]) - timedelta(days=30)).strftime(DATE_FORMAT)
    df = pd.concat([df, copy], ignore_index=True)
    j = len(df) - 1
    note(j, "overlapping_policy", "(whole row)", "", "second current row, 30 days earlier", "R-CV04", "Fix")

    DIRTY_FILE.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(DIRTY_FILE, index=False)
    ANSWER_KEY.parent.mkdir(parents=True, exist_ok=True)
    with ANSWER_KEY.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(answer[0].keys()))
        writer.writeheader()
        writer.writerows(answer)

    print(f"Extract date (latest START_DATE): {as_of}")
    print(f"Patients with current insurance: {len(first_current)}")
    print(f"Legacy export with planted defects: {DIRTY_FILE} ({len(df)} rows)")
    for a in answer:
        print(f"  {a['error_type']:<20} {a['record_id'][:20]}...  -> {a['expected_rule']} {a['expected_action']}")
    print(f"Answer key: {ANSWER_KEY}")


if __name__ == "__main__":
    main()
