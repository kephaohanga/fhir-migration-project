"""
plant_errors.py
---------------
Makes a "dirty" copy of the clean Synthea legacy data by planting known,
realistic data-quality errors, and writes an answer key listing every one.

Why: real legacy data is messy. Planting errors we KNOW about lets us prove
later that our validation and reconciliation checks catch every one of them.

Input  (never modified): data/legacy/patients.csv, encounters.csv, conditions.csv
Output: data/legacy_dirty/  (same three files, with errors planted)
        docs/planted_errors_answer_key.csv
Run from the project folder:  python src\\plant_errors.py
"""

import random
from pathlib import Path

import pandas as pd

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
SEED = 42  # fixed seed = same errors every run (reproducible)
CLEAN_DIR = Path("data/legacy")
DIRTY_DIR = Path("data/legacy_dirty")
ANSWER_KEY = Path("docs/planted_errors_answer_key.csv")

rng = random.Random(SEED)
answer_key = []  # one entry per planted error


def log(table, record_id, column, error_type, original, planted, expected_handling):
    """Record one planted error in the answer key."""
    answer_key.append({
        "table": table,
        "record_id": record_id,
        "column": column,
        "error_type": error_type,
        "original_value": original,
        "planted_value": planted,
        "expected_handling": expected_handling,
    })


def read_csv(name):
    """Read a CSV keeping every value as text, exactly as written.
    dtype=str             -> don't let pandas guess number/date types
    keep_default_na=False -> blank cells stay "" instead of becoming NaN
    """
    return pd.read_csv(CLEAN_DIR / name, dtype=str, keep_default_na=False)


# ---------------------------------------------------------------------------
# 1. Load the clean data
# ---------------------------------------------------------------------------
patients = read_csv("patients.csv")
encounters = read_csv("encounters.csv")
conditions = read_csv("conditions.csv")
print(f"Loaded clean data: {len(patients)} patients, "
      f"{len(encounters)} encounters, {len(conditions)} conditions")

# Pick 7 DIFFERENT patient rows so no patient gets two errors
p_rows = rng.sample(list(patients.index), 7)

# ---------------------------------------------------------------------------
# 2. Plant patient errors
# ---------------------------------------------------------------------------
# (a) Missing birthdate x2 -> required field, record must be REJECTED
for i in p_rows[0:2]:
    original = patients.loc[i, "BIRTHDATE"]
    patients.loc[i, "BIRTHDATE"] = ""
    log("patients", patients.loc[i, "Id"], "BIRTHDATE", "missing_required_value",
        original, "", "reject")

# (b) Wrong date format x1 -> legacy systems often use MM/DD/YYYY; pipeline should FIX it
i = p_rows[2]
original = patients.loc[i, "BIRTHDATE"]  # e.g. 1980-03-15
yyyy, mm, dd = original.split("-")
planted = f"{mm}/{dd}/{yyyy}"            # e.g. 03/15/1980
patients.loc[i, "BIRTHDATE"] = planted
log("patients", patients.loc[i, "Id"], "BIRTHDATE", "nonstandard_date_format",
    original, planted, "fix (convert to YYYY-MM-DD)")

# (c) Invalid gender code x1 -> not M or F, record must be REJECTED
i = p_rows[3]
original = patients.loc[i, "GENDER"]
patients.loc[i, "GENDER"] = "U"
log("patients", patients.loc[i, "Id"], "GENDER", "invalid_code", original, "U", "reject")

# (d) Missing address x2 -> not required, record loads but is FLAGGED as incomplete
for i in p_rows[4:6]:
    original = patients.loc[i, "ADDRESS"]
    patients.loc[i, "ADDRESS"] = ""
    log("patients", patients.loc[i, "Id"], "ADDRESS", "missing_optional_value",
        original, "", "load + flag as incomplete")

# (e) Duplicate patient x1 -> exact copy of a row; the copy must be REMOVED
i = p_rows[6]
duplicate = patients.loc[[i]]
patients = pd.concat([patients, duplicate], ignore_index=True)
log("patients", patients.loc[i, "Id"], "(whole row)", "duplicate_record",
    "", "row appears twice", "remove duplicate, keep one")

# ---------------------------------------------------------------------------
# 3. Plant encounter error
# ---------------------------------------------------------------------------
# Orphan encounter x1 -> points to a patient who doesn't exist; must be REJECTED
i = rng.choice(list(encounters.index))
original = encounters.loc[i, "PATIENT"]
fake_patient = "00000000-0000-0000-0000-000000000000"
encounters.loc[i, "PATIENT"] = fake_patient
log("encounters", encounters.loc[i, "Id"], "PATIENT", "orphan_reference",
    original, fake_patient, "reject")

# ---------------------------------------------------------------------------
# 4. Plant condition error
# ---------------------------------------------------------------------------
# Missing diagnosis code x1 -> a diagnosis with no code is unusable; must be REJECTED
# (conditions.csv has no Id column, so we identify the row by its row number)
i = rng.choice(list(conditions.index))
original = conditions.loc[i, "CODE"]
conditions.loc[i, "CODE"] = ""
log("conditions", f"row {i + 2} (patient {conditions.loc[i, 'PATIENT']})", "CODE",
    "missing_required_value", original, "", "reject")

# ---------------------------------------------------------------------------
# 5. Save the dirty files and the answer key
# ---------------------------------------------------------------------------
DIRTY_DIR.mkdir(parents=True, exist_ok=True)
ANSWER_KEY.parent.mkdir(parents=True, exist_ok=True)

patients.to_csv(DIRTY_DIR / "patients.csv", index=False)
encounters.to_csv(DIRTY_DIR / "encounters.csv", index=False)
conditions.to_csv(DIRTY_DIR / "conditions.csv", index=False)
pd.DataFrame(answer_key).to_csv(ANSWER_KEY, index=False)

print(f"Saved dirty data:  {len(patients)} patients, "
      f"{len(encounters)} encounters, {len(conditions)} conditions -> {DIRTY_DIR}")
print(f"Planted {len(answer_key)} errors. Answer key -> {ANSWER_KEY}")
for e in answer_key:
    print(f"  - {e['table']:<10} {e['error_type']:<24} -> {e['expected_handling']}")