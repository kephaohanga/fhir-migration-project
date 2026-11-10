"""
plant_procedure_errors.py
-------------------------
Phase 2 test data: plant known, realistic defects in the legacy procedure file.

  data/legacy/procedures.csv        clean Synthea export
  data/legacy_dirty/procedures.csv  the LEGACY EHR EXPORT (what this script writes)

One defect per chosen procedure, each on a different visit:
  1 x orphan visit       ENCOUNTER is a placeholder ID            -> expected R-PC01 Reject
  1 x patient mismatch   PATIENT is a different patient than the
                         visit's patient                          -> expected R-PC03 Reject
  1 x blank code         CODE removed                             -> expected R-PC04 Reject
  1 x stop before start  STOP one minute before START             -> expected R-PC05 Reject
  1 x duplicate row      an exact copy of a procedure row         -> expected R-PC06 Fix

Visits that Phase 1 already rejected or archived are avoided, so each defect
tests exactly one rule.

Output: data/legacy_dirty/procedures.csv
        docs/planted_procedure_errors_answer_key.csv
Run from the project folder:  python src\\plant_procedure_errors.py
"""

import csv
import random
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

SOURCE_FILE = Path("data/legacy/procedures.csv")
DIRTY_FILE = Path("data/legacy_dirty/procedures.csv")
DIRTY_ENCOUNTERS = Path("data/legacy_dirty/encounters.csv")
ANSWER_KEY = Path("docs/planted_procedure_errors_answer_key.csv")
SEED = 2028
PLACEHOLDER = "00000000-0000-0000-0000-000000000000"
DATE_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

rng = random.Random(SEED)


def key(df, i):
    """Same business key as apply_corrections.py: PATIENT_ENCOUNTER_CODE_START."""
    return "_".join(df.at[i, c] for c in ("PATIENT", "ENCOUNTER", "CODE", "START"))


def main():
    df = pd.read_csv(SOURCE_FILE, dtype=str, keep_default_na=False)

    # Avoid visits whose patient was planted as a placeholder in Phase 1
    avoid = set()
    if DIRTY_ENCOUNTERS.exists():
        enc = pd.read_csv(DIRTY_ENCOUNTERS, dtype=str, keep_default_na=False)
        avoid = set(enc.loc[(enc["PATIENT"] == PLACEHOLDER) | (enc["PATIENT"] == ""), "Id"])

    # Five procedures on five different visits, each with a STOP time
    first_per_visit = {}
    for i, r in df.iterrows():
        if r["ENCOUNTER"] not in avoid and r["STOP"] and r["CODE"]:
            first_per_visit.setdefault(r["ENCOUNTER"], i)
    rows = [first_per_visit[e] for e in rng.sample(sorted(first_per_visit), 5)]
    answer = []

    def note(i, error_type, column, original, planted, rule, action):
        answer.append({"record_id": key(df, i), "error_type": error_type, "column": column,
                       "original": original, "planted": planted,
                       "expected_rule": rule, "expected_action": action})

    # 1. Orphan visit
    i = rows[0]
    original = df.at[i, "ENCOUNTER"]
    df.at[i, "ENCOUNTER"] = PLACEHOLDER
    note(i, "orphan_visit", "ENCOUNTER", original, PLACEHOLDER, "R-PC01", "Reject")

    # 2. Patient mismatch: another real patient from the same file
    i = rows[1]
    original = df.at[i, "PATIENT"]
    other = rng.choice(sorted(set(df["PATIENT"]) - {original, PLACEHOLDER}))
    df.at[i, "PATIENT"] = other
    note(i, "patient_mismatch", "PATIENT", original, other, "R-PC03", "Reject")

    # 3. Blank code
    i = rows[2]
    original = df.at[i, "CODE"]
    df.at[i, "CODE"] = ""
    note(i, "blank_code", "CODE", original, "", "R-PC04", "Reject")

    # 4. STOP before START
    i = rows[3]
    original = df.at[i, "STOP"]
    planted = (datetime.strptime(df.at[i, "START"], DATE_FORMAT) - timedelta(minutes=1)).strftime(DATE_FORMAT)
    df.at[i, "STOP"] = planted
    note(i, "stop_before_start", "STOP", original, planted, "R-PC05", "Reject")

    # 5. Duplicate row (appended at the end, so the first copy is the one kept)
    i = rows[4]
    df = pd.concat([df, df.loc[[i]]], ignore_index=True)
    note(i, "duplicate_record", "(whole row)", "", "row appears twice", "R-PC06", "Fix")

    DIRTY_FILE.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(DIRTY_FILE, index=False)
    ANSWER_KEY.parent.mkdir(parents=True, exist_ok=True)
    with ANSWER_KEY.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(answer[0].keys()))
        writer.writeheader()
        writer.writerows(answer)

    print(f"Legacy export with planted defects: {DIRTY_FILE} ({len(df)} rows)")
    for a in answer:
        print(f"  {a['error_type']:<18} {a['record_id'][:24]}...  -> {a['expected_rule']} {a['expected_action']}")
    print(f"Answer key: {ANSWER_KEY}")


if __name__ == "__main__":
    main()
