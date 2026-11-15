r"""
profile_referrals.py
Phase 3, step 1 (before mapping): look at data/legacy/referrals.csv as it really is.

Run:  python src\profile_referrals.py
Read-only: it prints counts and changes no file.

Why: the mapping rules (Reject / Fix / Flag) must fit the real data. This shows the
values each column holds and whether every link (patient, requester, reason) points
to something that exists.
"""
import pandas as pd

LEGACY = "data/legacy/"


def read(name):
    return pd.read_csv(LEGACY + name, dtype=str, keep_default_na=False)


ref = read("referrals.csv")
patients = read("patients.csv")
providers = read("providers.csv")
conditions = read("conditions.csv")

print(f"referrals.csv: {len(ref)} rows, {ref.shape[1]} columns\n")

print("1. Blank values per column")
for col in ref.columns:
    print(f"   {col:<20} {(ref[col].str.strip() == '').sum()}")

print("\n2. Values of the coded columns")
for col in ("STATUS", "PRIORITY"):
    print(f"   {col}: " + ", ".join(f"{k} {v}" for k, v in ref[col].value_counts().items()))
pairs = ref.groupby(["SPECIALTY", "SPECIALTY_CODE"]).size()
print("   SPECIALTY / SPECIALTY_CODE pairs:")
for (s, c), n in pairs.items():
    print(f"      {c}  {s:<38} {n}")

print("\n3. Links")
alive = set(patients.loc[patients["DEATHDATE"] == "", "Id"])
print(f"   PATIENT not in patients.csv:        {(~ref['PATIENT'].isin(patients['Id'])).sum()}")
print(f"   PATIENT deceased:                   {(~ref['PATIENT'].isin(alive) & ref['PATIENT'].isin(patients['Id'])).sum()}")
print(f"   REQUESTER not in providers.csv:     {(~ref['REQUESTER'].isin(providers['Id'])).sum()}")
pc = set(zip(conditions["PATIENT"], conditions["CODE"]))
print(f"   REASON_CODE not a condition of that patient: "
      f"{sum((p, c) not in pc for p, c in zip(ref['PATIENT'], ref['REASON_CODE']))}")

print("\n4. Dates")
d = pd.to_datetime(ref["AUTHORED_DATE"], format="%Y-%m-%d", errors="coerce")
print(f"   AUTHORED_DATE not YYYY-MM-DD:       {d.isna().sum()}")
print(f"   range: {d.min():%Y-%m-%d} to {d.max():%Y-%m-%d}")

print("\n5. Duplicates")
print(f"   repeated REFERRAL_ID:               {ref['REFERRAL_ID'].duplicated().sum()}")
print(f"   identical rows (every column):      {ref.duplicated().sum()}")
same = ref.duplicated(subset=["PATIENT", "SPECIALTY_CODE", "REASON_CODE"], keep=False)
print(f"   same patient + specialty + reason:  {same.sum()} rows")

print("\n6. Links from the other Phase 3 files")
appts = read("appointments.csv")
auths = read("prior_auths.csv")
ids = set(ref["REFERRAL_ID"])
for name, df in (("appointments", appts), ("prior_auths", auths)):
    used = df.loc[df["REFERRAL_ID"] != "", "REFERRAL_ID"]
    print(f"   {name}: {len(used)} rows point to a referral, {(~used.isin(ids)).sum()} to a missing one")
