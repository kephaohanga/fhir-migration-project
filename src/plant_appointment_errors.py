r"""
plant_appointment_errors.py
---------------------------
Phase 3 test data: plant known, realistic defects in the legacy appointment file.

  data/legacy/appointments.csv        clean file from generate_phase3_legacy.py
  data/legacy_dirty/appointments.csv  the LEGACY EHR EXPORT (what this script writes)

One defect per chosen appointment, each a different appointment:
  1 x deceased patient     booked on the record of a patient who died before the visit
                           (the wrong patient picked during booking)              -> R-AP02 Reject
  2 x unknown provider     PROVIDER is a placeholder ID                           -> R-AP03 Flag
  3 x messy status         CONFIRMED written as "Confirmed "                      -> R-AP04 Fix
  4 x end before start     END 30 minutes BEFORE START                            -> R-AP05 Reject
  5 x clinic holiday       moved to Thanksgiving Day (2026-11-26), same time      -> R-AP06 Flag
  6 x double booking       given the same provider, day and time as another
                           active visit; the LATER row in the file is flagged     -> R-AP07 Flag
  7 x infusion, no auth    an infusion with AUTH_ID blank                         -> R-AP08 Flag
  8 x duplicate row        an exact copy of an appointment row                    -> R-AP09 Fix

Every defect can be corrected back from the clean file.

Output: data/legacy_dirty/appointments.csv
        docs/planted_appointment_errors_answer_key.csv
Run from the project folder:  python src\plant_appointment_errors.py
"""

import csv
import random
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

SOURCE_FILE = Path("data/legacy/appointments.csv")
PATIENTS = Path("data/legacy/patients.csv")
DIRTY_FILE = Path("data/legacy_dirty/appointments.csv")
ANSWER_KEY = Path("docs/planted_appointment_errors_answer_key.csv")
SEED = 2035
PLACEHOLDER = "00000000-0000-0000-0000-000000000000"
FMT = "%Y-%m-%dT%H:%M:%SZ"
THANKSGIVING = "2026-11-26"

rng = random.Random(SEED)


def main():
    df = pd.read_csv(SOURCE_FILE, dtype=str, keep_default_na=False)
    patients = pd.read_csv(PATIENTS, dtype=str, keep_default_na=False)
    deceased = patients[patients["DEATHDATE"] != ""].sort_values("Id")

    order = list(df.index)
    rng.shuffle(order)
    used = set()

    def pick(test, what):
        for i in order:
            if i not in used and test(df.loc[i]):
                used.add(i)
                return i
        raise SystemExit(f"No appointment left for: {what}")

    answer = []

    def note(i, error_type, column, original, planted, rule, action):
        answer.append({"record_id": df.at[i, "APPOINTMENT_ID"], "error_type": error_type, "column": column,
                       "original": original, "planted": planted,
                       "expected_rule": rule, "expected_action": action})

    active_follow_up = lambda r: r["VISIT_TYPE"] == "FOLLOW UP" and r["STATUS"] != "CANCELLED"

    # 1. Deceased patient (Reject): a patient who died before the visit date
    i = pick(active_follow_up, "deceased patient")
    visit_day = df.at[i, "START"][:10]
    dead = deceased[deceased["DEATHDATE"].str[:10] < visit_day]
    if dead.empty:
        raise SystemExit("patients.csv has no patient who died before the visits.")
    original, planted = df.at[i, "PATIENT"], dead.iloc[rng.randrange(len(dead))]["Id"]
    df.at[i, "PATIENT"] = planted
    note(i, "deceased_patient", "PATIENT", original, planted, "R-AP02", "Reject")

    # 6. Double booking (Flag) - chosen early so a pair with the same provider is still free
    pairs = [(a, b) for a in order for b in order
             if a < b and a not in used and b not in used
             and active_follow_up(df.loc[a]) and active_follow_up(df.loc[b])
             and df.at[a, "PROVIDER"] == df.at[b, "PROVIDER"]
             and df.at[a, "PATIENT"] != df.at[b, "PATIENT"]]       # two DIFFERENT patients
    if not pairs:
        raise SystemExit("No provider has two active follow-ups to double-book.")
    first, later = pairs[0]                       # a < b: 'later' is further down the file
    used.update((first, later))
    for col in ("START", "END"):
        original, planted = df.at[later, col], df.at[first, col]
        df.at[later, col] = planted
        note(later, "double_booking", col, original, planted, "R-AP07", "Flag")

    # 2. Unknown provider (Flag)
    i = pick(active_follow_up, "unknown provider")
    original = df.at[i, "PROVIDER"]
    df.at[i, "PROVIDER"] = PLACEHOLDER
    note(i, "unknown_provider", "PROVIDER", original, PLACEHOLDER, "R-AP03", "Flag")

    # 3. Messy status (Fix)
    i = pick(lambda r: r["STATUS"] == "CONFIRMED", "messy status")
    df.at[i, "STATUS"] = "Confirmed "
    note(i, "messy_status", "STATUS", "CONFIRMED", "Confirmed ", "R-AP04", "Fix")

    # 4. End before start (Reject)
    i = pick(active_follow_up, "end before start")
    original = df.at[i, "END"]
    planted = (datetime.strptime(df.at[i, "START"], FMT) - timedelta(minutes=30)).strftime(FMT)
    df.at[i, "END"] = planted
    note(i, "end_before_start", "END", original, planted, "R-AP05", "Reject")

    # 5. Clinic holiday (Flag): same time of day, on Thanksgiving
    i = pick(active_follow_up, "clinic holiday")
    for col in ("START", "END"):
        original = df.at[i, col]
        planted = THANKSGIVING + original[10:]
        df.at[i, col] = planted
        note(i, "clinic_holiday", col, original, planted, "R-AP06", "Flag")

    # 7. Infusion without authorization (Flag)
    i = pick(lambda r: r["VISIT_TYPE"] in ("INFUSION", "PROCEDURE") and r["AUTH_ID"] != "",
             "infusion without authorization")
    original = df.at[i, "AUTH_ID"]
    df.at[i, "AUTH_ID"] = ""
    note(i, "missing_authorization", "AUTH_ID", original, "", "R-AP08", "Flag")

    # 8. Duplicate row (appended at the end, so the first copy is the one kept)
    i = pick(lambda r: True, "duplicate")
    df = pd.concat([df, df.loc[[i]]], ignore_index=True)
    note(i, "duplicate_record", "(whole row)", "", "row appears twice", "R-AP09", "Fix")

    DIRTY_FILE.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(DIRTY_FILE, index=False)
    ANSWER_KEY.parent.mkdir(parents=True, exist_ok=True)
    with ANSWER_KEY.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(answer[0].keys()))
        writer.writeheader()
        writer.writerows(answer)

    print(f"Legacy export with planted defects: {DIRTY_FILE} ({len(df)} rows)")
    for a in answer:
        print(f"  {a['error_type']:<22} {a['column']:<9} {a['record_id'][:24]}  -> "
              f"{a['expected_rule']} {a['expected_action']}")
    print(f"Answer key: {ANSWER_KEY}  ({len(answer)} lines for 8 defects: a moved visit changes START and END)")


if __name__ == "__main__":
    main()
