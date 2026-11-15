r"""
plant_referral_errors.py
------------------------
Phase 3 test data: plant known, realistic defects in the legacy referral file.

  data/legacy/referrals.csv        clean file from generate_phase3_legacy.py
  data/legacy_dirty/referrals.csv  the LEGACY EHR EXPORT (what this script writes)

One defect per chosen referral, each a different referral:
  1 x orphan patient     PATIENT is a placeholder ID                  -> expected R-RF01 Reject
  1 x unknown requester  REQUESTER is a placeholder ID                -> expected R-RF02 Flag
  1 x no diagnosis       REASON_CODE and REASON_DESCRIPTION blank     -> expected R-RF03 Flag
  1 x wrong diagnosis    REASON_CODE is not one of the patient's
                         conditions (taken from another patient)      -> expected R-RF03 Flag
  1 x messy status       OPEN written as "Open " (capitals, space)    -> expected R-RF04 Fix
  1 x unknown status     STATUS = WAITING (not a legacy status)       -> expected R-RF05 Reject
  1 x US date format     AUTHORED_DATE written as m/d/yyyy            -> expected R-RF06 Fix
  1 x duplicate row      an exact copy of a referral row              -> expected R-RF07 Fix

The two Reject defects are placed on referrals that no prior authorization points
to, so this step's rejects do not spill into the next step (prior authorizations).
Every defect can be corrected back from the clean file.

Output: data/legacy_dirty/referrals.csv
        docs/planted_referral_errors_answer_key.csv
Run from the project folder:  python src\plant_referral_errors.py
"""

import csv
import random
from pathlib import Path

import pandas as pd

SOURCE_FILE = Path("data/legacy/referrals.csv")
AUTH_FILE = Path("data/legacy/prior_auths.csv")
CONDITIONS_FILE = Path("data/legacy/conditions.csv")
DIRTY_FILE = Path("data/legacy_dirty/referrals.csv")
ANSWER_KEY = Path("docs/planted_referral_errors_answer_key.csv")
SEED = 2033
PLACEHOLDER = "00000000-0000-0000-0000-000000000000"

rng = random.Random(SEED)


def main():
    df = pd.read_csv(SOURCE_FILE, dtype=str, keep_default_na=False)
    auths = pd.read_csv(AUTH_FILE, dtype=str, keep_default_na=False)
    conditions = pd.read_csv(CONDITIONS_FILE, dtype=str, keep_default_na=False)
    linked = set(auths.loc[auths["REFERRAL_ID"] != "", "REFERRAL_ID"])

    order = list(df.index)
    rng.shuffle(order)
    used = set()

    def pick(test):
        """The first unused referral (in shuffled order) that passes the test."""
        for i in order:
            if i not in used and test(df.loc[i]):
                used.add(i)
                return i
        raise SystemExit("Not enough referrals to plant every defect.")

    answer = []

    def note(i, error_type, column, original, planted, rule, action):
        answer.append({"record_id": df.at[i, "REFERRAL_ID"], "error_type": error_type, "column": column,
                       "original": original, "planted": planted,
                       "expected_rule": rule, "expected_action": action})

    not_linked = lambda r: r["REFERRAL_ID"] not in linked

    # 1. Orphan patient (Reject) - a referral no authorization depends on
    i = pick(not_linked)
    original = df.at[i, "PATIENT"]
    df.at[i, "PATIENT"] = PLACEHOLDER
    note(i, "orphan_patient", "PATIENT", original, PLACEHOLDER, "R-RF01", "Reject")

    # 2. Unknown requester (Flag)
    i = pick(lambda r: True)
    original = df.at[i, "REQUESTER"]
    df.at[i, "REQUESTER"] = PLACEHOLDER
    note(i, "unknown_requester", "REQUESTER", original, PLACEHOLDER, "R-RF02", "Flag")

    # 3. No diagnosis (Flag)
    i = pick(lambda r: True)
    original = df.at[i, "REASON_CODE"]
    df.at[i, "REASON_CODE"] = ""
    df.at[i, "REASON_DESCRIPTION"] = ""
    note(i, "no_diagnosis", "REASON_CODE", original, "", "R-RF03", "Flag")

    # 4. Wrong diagnosis (Flag): a condition code this patient has never had
    i = pick(lambda r: True)
    patient = df.at[i, "PATIENT"]
    own = set(conditions.loc[conditions["PATIENT"] == patient, "CODE"])
    others = (conditions.loc[~conditions["CODE"].isin(own), ["CODE", "DESCRIPTION"]]
              .drop_duplicates("CODE").sort_values("CODE"))
    if others.empty:
        raise SystemExit("conditions.csv has no condition code this patient lacks.")
    wrong = others.iloc[rng.randrange(len(others))]
    original = df.at[i, "REASON_CODE"]
    df.at[i, "REASON_CODE"] = wrong["CODE"]
    df.at[i, "REASON_DESCRIPTION"] = wrong["DESCRIPTION"]
    note(i, "wrong_diagnosis", "REASON_CODE", original, wrong["CODE"], "R-RF03", "Flag")

    # 5. Messy status (Fix): OPEN -> "Open "
    i = pick(lambda r: r["STATUS"] == "OPEN")
    df.at[i, "STATUS"] = "Open "
    note(i, "messy_status", "STATUS", "OPEN", "Open ", "R-RF04", "Fix")

    # 6. Unknown status (Reject)
    i = pick(not_linked)
    original = df.at[i, "STATUS"]
    df.at[i, "STATUS"] = "WAITING"
    note(i, "unknown_status", "STATUS", original, "WAITING", "R-RF05", "Reject")

    # 7. US date format (Fix): 2026-08-04 -> 8/4/2026
    i = pick(lambda r: True)
    original = df.at[i, "AUTHORED_DATE"]
    y, m, d = original.split("-")
    planted = f"{int(m)}/{int(d)}/{y}"
    df.at[i, "AUTHORED_DATE"] = planted
    note(i, "us_date_format", "AUTHORED_DATE", original, planted, "R-RF06", "Fix")

    # 8. Duplicate row (appended at the end, so the first copy is the one kept)
    i = pick(lambda r: True)
    df = pd.concat([df, df.loc[[i]]], ignore_index=True)
    note(i, "duplicate_record", "(whole row)", "", "row appears twice", "R-RF07", "Fix")

    DIRTY_FILE.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(DIRTY_FILE, index=False)
    ANSWER_KEY.parent.mkdir(parents=True, exist_ok=True)
    with ANSWER_KEY.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(answer[0].keys()))
        writer.writeheader()
        writer.writerows(answer)

    print(f"Legacy export with planted defects: {DIRTY_FILE} ({len(df)} rows)")
    for a in answer:
        print(f"  {a['error_type']:<18} {a['record_id'][:24]}  -> {a['expected_rule']} {a['expected_action']}")
    print(f"Answer key: {ANSWER_KEY}")


if __name__ == "__main__":
    main()
