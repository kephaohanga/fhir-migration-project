"""
plant_observation_errors.py
---------------------------
Phase 2 test data: plant known, realistic defects in the legacy observation file.

  data/legacy/observations.csv        clean Synthea export
  data/legacy_dirty/observations.csv  the LEGACY EHR EXPORT (what this script writes)

Defects go only into numeric, LOINC-coded observations, one per patient:
  1 x orphan visit       ENCOUNTER is a placeholder ID               -> expected R-OB02 Reject
  1 x patient mismatch   PATIENT is not the visit's patient          -> expected R-OB03 Reject
  1 x invalid date       DATE is not a real date (month 13)          -> expected R-OB05 Reject
  1 x non-numeric value  a typing error in a number (letter O for 0,
                         or a stray letter)                          -> expected R-OB06 Reject
  1 x duplicate row      an exact copy of an observation row         -> expected R-OB07 Fix

Every defect can be corrected back from the clean file.

Output: data/legacy_dirty/observations.csv
        docs/planted_observation_errors_answer_key.csv
Run from the project folder:  python src\\plant_observation_errors.py
"""

import csv
import random
import re
from pathlib import Path

import pandas as pd

SOURCE_FILE = Path("data/legacy/observations.csv")
DIRTY_FILE = Path("data/legacy_dirty/observations.csv")
DIRTY_ENCOUNTERS = Path("data/legacy_dirty/encounters.csv")
ANSWER_KEY = Path("docs/planted_observation_errors_answer_key.csv")
SEED = 2031
PLACEHOLDER = "00000000-0000-0000-0000-000000000000"
LOINC = re.compile(r"^\d{1,7}-\d$")

rng = random.Random(SEED)


def key(df, i):
    """Same business key as apply_corrections.py: PATIENT_ENCOUNTER_CODE_DATE."""
    return "_".join(df.at[i, c] for c in ("PATIENT", "ENCOUNTER", "CODE", "DATE"))


def main():
    df = pd.read_csv(SOURCE_FILE, dtype=str, keep_default_na=False)

    avoid = set()                                   # visits Phase 1 planted as orphans
    if DIRTY_ENCOUNTERS.exists():
        enc = pd.read_csv(DIRTY_ENCOUNTERS, dtype=str, keep_default_na=False)
        avoid = set(enc.loc[(enc["PATIENT"] == PLACEHOLDER) | (enc["PATIENT"] == ""), "Id"])

    first_per_patient = {}
    for i, r in enumerate(df.itertuples(index=False)):
        if (r.TYPE == "numeric" and LOINC.match(r.CODE) and r.ENCOUNTER not in avoid
                and r.VALUE and r.DATE.startswith(("19", "20"))):
            first_per_patient.setdefault(r.PATIENT, i)
    if len(first_per_patient) < 5:
        raise SystemExit(f"Only {len(first_per_patient)} patients qualify - need at least 5.")
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
    note(i, "orphan_visit", "ENCOUNTER", original, PLACEHOLDER, "R-OB02", "Reject")

    # 2. Patient mismatch: another patient from the same file
    i = rows[1]
    original = df.at[i, "PATIENT"]
    other = rng.choice(sorted(set(df["PATIENT"]) - {original, PLACEHOLDER}))
    df.at[i, "PATIENT"] = other
    note(i, "patient_mismatch", "PATIENT", original, other, "R-OB03", "Reject")

    # 3. Invalid date: month 13 (keeps the T...Z shape, so Excel leaves it alone)
    i = rows[2]
    original = df.at[i, "DATE"]
    planted = original[:5] + "13" + original[7:]
    df.at[i, "DATE"] = planted
    note(i, "invalid_date", "DATE", original, planted, "R-OB05", "Reject")

    # 4. Non-numeric value: letter O typed for zero, or a stray letter
    i = rows[3]
    original = df.at[i, "VALUE"]
    planted = original.replace("0", "O", 1) if "0" in original else original + "x"
    df.at[i, "VALUE"] = planted
    note(i, "non_numeric_value", "VALUE", original, planted, "R-OB06", "Reject")

    # 5. Duplicate row (appended at the end, so the first copy is the one kept)
    i = rows[4]
    df = pd.concat([df, df.loc[[i]]], ignore_index=True)
    note(i, "duplicate_record", "(whole row)", "", "row appears twice", "R-OB07", "Fix")

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
