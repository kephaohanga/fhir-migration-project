r"""
profile_prior_auths.py
Phase 3, step 2 (before mapping): look at data/legacy/prior_auths.csv as it really is.

Run:  python src\profile_prior_auths.py
Read-only: it prints counts and changes no file.

Why: a prior authorization becomes TWO FHIR resources - a Claim (the clinic's request,
use = preauthorization) and a ClaimResponse (the payer's decision). Before mapping, we
check which values each column holds, whether every link (patient, payer, policy,
referral) points to something that exists, and whether the dates make sense together.
"""
import pandas as pd

LEGACY = "data/legacy/"


def read(name):
    return pd.read_csv(LEGACY + name, dtype=str, keep_default_na=False)


pa = read("prior_auths.csv")
patients = read("patients.csv")
payers = read("payers.csv")
transitions = read("payer_transitions.csv")
referrals = read("referrals.csv")
appts = read("appointments.csv")

print(f"prior_auths.csv: {len(pa)} rows, {pa.shape[1]} columns\n")

print("1. Blank values per column")
for col in pa.columns:
    print(f"   {col:<20} {(pa[col].str.strip() == '').sum()}")

print("\n2. Values of the coded columns")
for col in ("SERVICE_TYPE", "SERVICE_SYSTEM", "DECISION", "DENIAL_REASON"):
    print(f"   {col}: " + ", ".join(f"{k or '(blank)'} {v}" for k, v in pa[col].value_counts().items()))
print("   DECISION by SERVICE_TYPE:")
for (t, d), n in pa.groupby(["SERVICE_TYPE", "DECISION"]).size().items():
    print(f"      {t:<11} {d:<9} {n}")
print("   Services:")
for (c, s), n in pa.groupby(["SERVICE_CODE", "SERVICE_DESCRIPTION"]).size().items():
    print(f"      {c:<11} {s[:55]:<55} {n}")

print("\n3. Which columns are filled, by decision  (AUTH_NUMBER / VALID dates / DENIAL_REASON / DECISION_DATE)")
for d, g in pa.groupby("DECISION"):
    filled = {c: (g[c] != "").sum() for c in ("AUTH_NUMBER", "VALID_FROM", "VALID_TO", "DENIAL_REASON", "DECISION_DATE")}
    print(f"   {d:<9} rows {len(g):>2}: " + ", ".join(f"{c} {n}" for c, n in filled.items()))

print("\n4. Links")
alive = set(patients.loc[patients["DEATHDATE"] == "", "Id"])
print(f"   PATIENT not in patients.csv:        {(~pa['PATIENT'].isin(patients['Id'])).sum()}")
print(f"   PATIENT deceased:                   {(pa['PATIENT'].isin(patients['Id']) & ~pa['PATIENT'].isin(alive)).sum()}")
print(f"   PAYER not in payers.csv:            {(~pa['PAYER'].isin(payers['Id'])).sum()}")
policies = set(zip(transitions["PATIENT"], transitions["PAYER"], transitions["MEMBERID"]))
print(f"   PATIENT + PAYER + MEMBERID not a policy in payer_transitions: "
      f"{sum(k not in policies for k in zip(pa['PATIENT'], pa['PAYER'], pa['MEMBERID']))}")
ref = pa[pa["REFERRAL_ID"] != ""]
print(f"   REFERRAL_ID filled: {len(ref)}  (SERVICE_TYPE REFERRAL: {(pa['SERVICE_TYPE'] == 'REFERRAL').sum()}); "
      f"not in referrals.csv: {(~ref['REFERRAL_ID'].isin(referrals['REFERRAL_ID'])).sum()}")
if len(ref):
    owner = dict(zip(referrals["REFERRAL_ID"], referrals["PATIENT"]))
    print(f"   referral belongs to another patient: "
          f"{sum(owner.get(r) not in (None, p) for r, p in zip(ref['REFERRAL_ID'], ref['PATIENT']))}")

print("\n5. Dates")
for col in ("REQUEST_DATE", "DECISION_DATE", "VALID_FROM", "VALID_TO"):
    filled = pa.loc[pa[col] != "", col]
    d = pd.to_datetime(filled, format="%Y-%m-%d", errors="coerce")
    print(f"   {col:<14} filled {len(filled):>2}, not YYYY-MM-DD {d.isna().sum()}, "
          f"range {d.min():%Y-%m-%d} to {d.max():%Y-%m-%d}" if len(filled) else f"   {col:<14} filled 0")
dt = lambda c: pd.to_datetime(pa[c], format="%Y-%m-%d", errors="coerce")
print(f"   DECISION_DATE before REQUEST_DATE:  {(dt('DECISION_DATE') < dt('REQUEST_DATE')).sum()}")
print(f"   VALID_TO before VALID_FROM:         {(dt('VALID_TO') < dt('VALID_FROM')).sum()}")
span = (dt("VALID_TO") - dt("VALID_FROM")).dt.days.dropna()
if len(span):
    print(f"   validity length in days:            {int(span.min())} to {int(span.max())}")

print("\n6. Duplicates")
print(f"   repeated AUTH_ID:                   {pa['AUTH_ID'].duplicated().sum()}")
print(f"   identical rows (every column):      {pa.duplicated().sum()}")
filled_no = pa.loc[pa["AUTH_NUMBER"] != "", "AUTH_NUMBER"]
print(f"   repeated AUTH_NUMBER:               {filled_no.duplicated().sum()}")

print("\n7. Appointments that depend on an authorization")
linked = appts[appts["AUTH_ID"] != ""]
print(f"   appointments with an AUTH_ID: {len(linked)}; pointing to a missing auth: "
      f"{(~linked['AUTH_ID'].isin(pa['AUTH_ID'])).sum()}")
if len(linked):
    m = linked.merge(pa[["AUTH_ID", "DECISION", "VALID_FROM", "VALID_TO"]], on="AUTH_ID", how="left")
    start = pd.to_datetime(m["START"].str[:10], format="%Y-%m-%d", errors="coerce")
    inside = (start >= pd.to_datetime(m["VALID_FROM"], errors="coerce")) & \
             (start <= pd.to_datetime(m["VALID_TO"], errors="coerce"))
    for (d, st), n in m.groupby(["DECISION", "STATUS"]).size().items():
        print(f"      auth {d:<9} appointment {st:<22} {n}")
    approved = m["DECISION"] == "APPROVED"
    print(f"   approved-auth appointments outside the valid dates: {(approved & ~inside).sum()}")
