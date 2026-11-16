r"""
plant_prior_auth_errors.py
--------------------------
Phase 3 test data: plant known, realistic defects in the legacy prior-authorization file.

  data/legacy/prior_auths.csv        clean file from generate_phase3_legacy.py
  data/legacy_dirty/prior_auths.csv  the LEGACY EHR EXPORT (what this script writes)

One defect per chosen authorization, each a different authorization:
  1 x expired authorization  APPROVED, but VALID_TO is before the extract date
                             (planted on an authorization that visits depend on)   -> R-PA07 Flag
  2 x missing auth number    APPROVED, AUTH_NUMBER blank                         -> R-PA05 Flag
  3 x wrong referral         REFERRAL_ID is ANOTHER patient's referral           -> R-PA03 Reject
  4 x decision before request DECISION_DATE 3 days before REQUEST_DATE           -> R-PA06 Reject
  5 x messy decision         e.g. DENIED written as "denied "                    -> R-PA04 Fix
  6 x orphan patient         PATIENT is a placeholder ID                         -> R-PA01 Reject
  7 x wrong member ID        MEMBERID is another patient's member ID             -> R-PA02 Reject
  8 x duplicate row          an exact copy of an authorization row               -> R-PA08 Fix

The Reject defects are placed on authorizations no appointment depends on, so this step's
rejects do not spill into the next step (appointments). Every defect can be corrected back
from the clean file.

Output: data/legacy_dirty/prior_auths.csv
        docs/planted_prior_auth_errors_answer_key.csv
Run from the project folder:  python src\plant_prior_auth_errors.py
"""

import csv
import random
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

SOURCE_FILE = Path("data/legacy/prior_auths.csv")
APPOINTMENTS = Path("data/legacy/appointments.csv")
REFERRALS = Path("data/legacy/referrals.csv")
TRANSITIONS = Path("data/legacy/payer_transitions.csv")
DIRTY_FILE = Path("data/legacy_dirty/prior_auths.csv")
ANSWER_KEY = Path("docs/planted_prior_auth_errors_answer_key.csv")
SEED = 2034
PLACEHOLDER = "00000000-0000-0000-0000-000000000000"

rng = random.Random(SEED)


def main():
    df = pd.read_csv(SOURCE_FILE, dtype=str, keep_default_na=False)
    appts = pd.read_csv(APPOINTMENTS, dtype=str, keep_default_na=False)
    referrals = pd.read_csv(REFERRALS, dtype=str, keep_default_na=False)
    transitions = pd.read_csv(TRANSITIONS, dtype=str, keep_default_na=False)
    as_of = date.fromisoformat(max(s for s in transitions["START_DATE"] if s)[:10])   # extract date
    booked = set(appts.loc[appts["AUTH_ID"] != "", "AUTH_ID"])

    order = list(df.index)
    rng.shuffle(order)
    used = set()

    def pick(test, what):
        for i in order:
            if i not in used and test(df.loc[i]):
                used.add(i)
                return i
        raise SystemExit(f"No authorization left for: {what}")

    answer = []

    def note(i, error_type, column, original, planted, rule, action):
        answer.append({"record_id": df.at[i, "AUTH_ID"], "error_type": error_type, "column": column,
                       "original": original, "planted": planted,
                       "expected_rule": rule, "expected_action": action})

    free = lambda r: r["AUTH_ID"] not in booked
    approved = lambda r: r["DECISION"] == "APPROVED"
    can_expire = lambda r: approved(r) and date.fromisoformat(r["VALID_FROM"]) < as_of

    # 1. Expired authorization (Flag): prefer one that visits depend on
    try:
        i = pick(lambda r: can_expire(r) and r["AUTH_ID"] in booked, "expired, with visits")
    except SystemExit:
        i = pick(can_expire, "expired authorization")
    original = df.at[i, "VALID_TO"]
    planted = min(date.fromisoformat(df.at[i, "VALID_FROM"]) + timedelta(days=30),
                  as_of - timedelta(days=1)).isoformat()
    df.at[i, "VALID_TO"] = planted
    note(i, "expired_authorization", "VALID_TO", original, planted, "R-PA07", "Flag")

    # 2. Approved, no authorization number (Flag)
    i = pick(approved, "missing auth number")
    original = df.at[i, "AUTH_NUMBER"]
    df.at[i, "AUTH_NUMBER"] = ""
    note(i, "missing_auth_number", "AUTH_NUMBER", original, "", "R-PA05", "Flag")

    # 3. Wrong referral (Reject): another patient's referral
    i = pick(lambda r: free(r) and r["SERVICE_TYPE"] == "REFERRAL", "wrong referral")
    original = df.at[i, "REFERRAL_ID"]
    others = sorted(referrals.loc[referrals["PATIENT"] != df.at[i, "PATIENT"], "REFERRAL_ID"])
    planted = rng.choice(others)
    df.at[i, "REFERRAL_ID"] = planted
    note(i, "wrong_referral", "REFERRAL_ID", original, planted, "R-PA03", "Reject")

    # 4. Decision before request (Reject)
    try:
        i = pick(lambda r: free(r) and r["DECISION"] == "DENIED", "decision before request")
    except SystemExit:
        i = pick(lambda r: free(r) and r["DECISION"] == "APPROVED", "decision before request")
    original = df.at[i, "DECISION_DATE"]
    planted = (date.fromisoformat(df.at[i, "REQUEST_DATE"]) - timedelta(days=3)).isoformat()
    df.at[i, "DECISION_DATE"] = planted
    note(i, "decision_before_request", "DECISION_DATE", original, planted, "R-PA06", "Reject")

    # 5. Messy decision (Fix)
    i = pick(lambda r: True, "messy decision")
    original = df.at[i, "DECISION"]
    df.at[i, "DECISION"] = original.lower() + " "
    note(i, "messy_decision", "DECISION", original, original.lower() + " ", "R-PA04", "Fix")

    # 6. Orphan patient (Reject)
    i = pick(free, "orphan patient")
    original = df.at[i, "PATIENT"]
    df.at[i, "PATIENT"] = PLACEHOLDER
    note(i, "orphan_patient", "PATIENT", original, PLACEHOLDER, "R-PA01", "Reject")

    # 7. Wrong member ID (Reject): another patient's member ID
    i = pick(free, "wrong member ID")
    original = df.at[i, "MEMBERID"]
    others = sorted(set(transitions.loc[(transitions["PATIENT"] != df.at[i, "PATIENT"])
                                        & (transitions["MEMBERID"] != ""), "MEMBERID"]))
    planted = rng.choice(others)
    df.at[i, "MEMBERID"] = planted
    note(i, "wrong_member_id", "MEMBERID", original, planted, "R-PA02", "Reject")

    # 8. Duplicate row (appended at the end, so the first copy is the one kept)
    i = pick(lambda r: True, "duplicate")
    df = pd.concat([df, df.loc[[i]]], ignore_index=True)
    note(i, "duplicate_record", "(whole row)", "", "row appears twice", "R-PA08", "Fix")

    DIRTY_FILE.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(DIRTY_FILE, index=False)
    ANSWER_KEY.parent.mkdir(parents=True, exist_ok=True)
    with ANSWER_KEY.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(answer[0].keys()))
        writer.writeheader()
        writer.writerows(answer)

    print(f"Extract date {as_of}. Legacy export with planted defects: {DIRTY_FILE} ({len(df)} rows)")
    for a in answer:
        print(f"  {a['error_type']:<24} {a['record_id'][:24]}  -> {a['expected_rule']} {a['expected_action']}")
    print(f"Answer key: {ANSWER_KEY}")


if __name__ == "__main__":
    main()
