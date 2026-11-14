"""
plant_allergy_errors.py
-----------------------
Phase 2 test data: plant known, realistic defects in the legacy allergy file.

  data/legacy/allergies.csv        clean Synthea export
  data/legacy_dirty/allergies.csv  the LEGACY EHR EXPORT (what this script writes)

One defect per chosen allergy, each on a different patient:
  1 x orphan visit        ENCOUNTER is a placeholder ID             -> expected R-AL02 Reject
  1 x patient mismatch    PATIENT is not the visit's patient        -> expected R-AL03 Reject
  1 x unknown code        CODE not in docs/allergy_code_map.csv     -> expected R-AL04 Reject
  1 x invalid start date  START is not a real date                  -> expected R-AL05 Reject
  1 x duplicate row       an exact copy of an allergy row           -> expected R-AL06 Fix

Every defect can be corrected back from the clean file (no blank values needed).

Output: data/legacy_dirty/allergies.csv
        docs/planted_allergy_errors_answer_key.csv
Run from the project folder:  python src\\plant_allergy_errors.py
"""

import csv
import random
from pathlib import Path

import pandas as pd

SOURCE_FILE = Path("data/legacy/allergies.csv")
DIRTY_FILE = Path("data/legacy_dirty/allergies.csv")
DIRTY_ENCOUNTERS = Path("data/legacy_dirty/encounters.csv")
ANSWER_KEY = Path("docs/planted_allergy_errors_answer_key.csv")
SEED = 2029
PLACEHOLDER = "00000000-0000-0000-0000-000000000000"
UNKNOWN_CODE = "999999999"          # not in the code map
BAD_DATE = "2001-13-45"             # month 13, day 45: not a real date

rng = random.Random(SEED)


def key(df, i):
    """Same business key as apply_corrections.py: PATIENT_ENCOUNTER_CODE_START."""
    return "_".join(df.at[i, c] for c in ("PATIENT", "ENCOUNTER", "CODE", "START"))


def main():
    df = pd.read_csv(SOURCE_FILE, dtype=str, keep_default_na=False)

    avoid = set()                                   # visits Phase 1 planted as orphans
    if DIRTY_ENCOUNTERS.exists():
        enc = pd.read_csv(DIRTY_ENCOUNTERS, dtype=str, keep_default_na=False)
        avoid = set(enc.loc[(enc["PATIENT"] == PLACEHOLDER) | (enc["PATIENT"] == ""), "Id"])

    first_per_patient = {}
    for i, r in df.iterrows():
        if r["ENCOUNTER"] not in avoid and r["CODE"]:
            first_per_patient.setdefault(r["PATIENT"], i)
    if len(first_per_patient) < 5:
        raise SystemExit(f"Only {len(first_per_patient)} patients have allergies - need at least 5.")
    rows = [first_per_patient[p] for p in rng.sample(sorted(first_per_patient), 5)]
    answer = []

    def note(i, error_type, column, original, planted, rule, action):
        answer.append({"record_id": key(df, i), "error_type": error_type, "column": column,
                       "original": original, "planted": planted,
                       "expected_rule": rule, "expected_action": action})

    # 1. Orphan visit
    i = rows[0]
    original = df.at[i, "ENCOUNTER"]
    df.at[i, "ENCOUNTER"] = PLACEHOLDER
    note(i, "orphan_visit", "ENCOUNTER", original, PLACEHOLDER, "R-AL02", "Reject")

    # 2. Patient mismatch: another patient who has allergies
    i = rows[1]
    original = df.at[i, "PATIENT"]
    other = rng.choice(sorted(set(df["PATIENT"]) - {original}))
    df.at[i, "PATIENT"] = other
    note(i, "patient_mismatch", "PATIENT", original, other, "R-AL03", "Reject")

    # 3. Unknown code (its code system cannot be confirmed)
    i = rows[2]
    original = df.at[i, "CODE"]
    df.at[i, "CODE"] = UNKNOWN_CODE
    note(i, "unknown_code", "CODE", original, UNKNOWN_CODE, "R-AL04", "Reject")

    # 4. Invalid start date
    i = rows[3]
    original = df.at[i, "START"]
    df.at[i, "START"] = BAD_DATE
    note(i, "invalid_start_date", "START", original, BAD_DATE, "R-AL05", "Reject")

    # 5. Duplicate row (appended at the end, so the first copy is the one kept)
    i = rows[4]
    df = pd.concat([df, df.loc[[i]]], ignore_index=True)
    note(i, "duplicate_record", "(whole row)", "", "row appears twice", "R-AL06", "Fix")

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
