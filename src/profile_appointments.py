r"""
profile_appointments.py
Phase 3, step 3 (before mapping): look at data/legacy/appointments.csv as it really is.

Run:  python src\profile_appointments.py
Read-only: it prints counts and changes no file.

Why: an Appointment is the last piece of open work. It links to almost everything
migrated so far - the patient, the provider, the diagnosis, the prior authorization
(and through it the referral). Before mapping we check the values each column holds,
that every link exists, and that the schedule itself makes sense: clinic days and
hours, no double booking, nobody booked after they died.
"""
from datetime import date

import pandas as pd

LEGACY = "data/legacy/"
CLINIC_HOLIDAYS = {date(2026, 11, 26), date(2026, 12, 25), date(2027, 1, 1)}


def read(name):
    return pd.read_csv(LEGACY + name, dtype=str, keep_default_na=False)


ap = read("appointments.csv")
patients = read("patients.csv")
providers = read("providers.csv")
conditions = read("conditions.csv")
auths = read("prior_auths.csv")
referrals = read("referrals.csv")
transitions = read("payer_transitions.csv")
as_of = date.fromisoformat(max(s for s in transitions["START_DATE"] if s)[:10])

print(f"appointments.csv: {len(ap)} rows, {ap.shape[1]} columns   (extract date {as_of})\n")

print("1. Blank values per column")
for col in ap.columns:
    print(f"   {col:<20} {(ap[col].str.strip() == '').sum()}")

print("\n2. Values of the coded columns")
print("   VISIT_TYPE by STATUS:")
for (t, s), n in ap.groupby(["VISIT_TYPE", "STATUS"]).size().items():
    print(f"      {t:<11} {s:<22} {n}")
start = pd.to_datetime(ap["START"], format="%Y-%m-%dT%H:%M:%SZ", errors="coerce")
end = pd.to_datetime(ap["END"], format="%Y-%m-%dT%H:%M:%SZ", errors="coerce")
minutes = ((end - start).dt.total_seconds() / 60)
print("   Length in minutes by VISIT_TYPE:")
for t, g in minutes.groupby(ap["VISIT_TYPE"]):
    print(f"      {t:<11} {', '.join(f'{int(m)} min x{n}' for m, n in g.value_counts().sort_index().items())}")
with_reason = ap[ap["REASON_CODE"] != ""]
print(f"   with a REASON_CODE: {len(with_reason)} of {len(ap)}")

print("\n3. Links")
known = set(patients["Id"])
death = dict(zip(patients["Id"], patients["DEATHDATE"]))
print(f"   PATIENT not in patients.csv:        {(~ap['PATIENT'].isin(known)).sum()}")
died_before = sum(1 for p, s in zip(ap["PATIENT"], start)
                  if death.get(p) and pd.notna(s) and date.fromisoformat(death[p][:10]) <= s.date())
print(f"   PATIENT died on or before the visit: {died_before}")
print(f"   PROVIDER not in providers.csv:      {(~ap['PROVIDER'].isin(providers['Id'])).sum()}")
pc = set(zip(conditions["PATIENT"], conditions["CODE"]))
print(f"   REASON_CODE not a condition of that patient: "
      f"{sum((p, c) not in pc for p, c in zip(with_reason['PATIENT'], with_reason['REASON_CODE']))}")
linked = ap[ap["AUTH_ID"] != ""]
auth_by_id = auths.set_index("AUTH_ID")
print(f"   AUTH_ID filled: {len(linked)}; not in prior_auths.csv: {(~linked['AUTH_ID'].isin(auths['AUTH_ID'])).sum()}; "
      f"another patient's: {sum(auth_by_id.at[a, 'PATIENT'] != p for a, p in zip(linked['AUTH_ID'], linked['PATIENT']) if a in auth_by_id.index)}")
ref = ap[ap["REFERRAL_ID"] != ""]
print(f"   REFERRAL_ID filled: {len(ref)}; not in referrals.csv: {(~ref['REFERRAL_ID'].isin(referrals['REFERRAL_ID'])).sum()}")
needs_auth = ap["VISIT_TYPE"].isin(["INFUSION", "PROCEDURE"])
print(f"   INFUSION / PROCEDURE visits without an AUTH_ID: {(needs_auth & (ap['AUTH_ID'] == '')).sum()}")

print("\n4. Dates and the clinic schedule")
print(f"   START or END not YYYY-MM-DDTHH:MM:SSZ: {(start.isna() | end.isna()).sum()}")
print(f"   END not after START:                {(end <= start).sum()}")
print(f"   range: {start.min():%Y-%m-%d} to {start.max():%Y-%m-%d}")
print(f"   before the extract date:            {(start.dt.date < as_of).sum()}")
print(f"   on a weekend:                       {(start.dt.dayofweek >= 5).sum()}")
print(f"   on a clinic holiday:                {start.dt.date.isin(CLINIC_HOLIDAYS).sum()}")
t = start.dt.hour * 60 + start.dt.minute
print(f"   starting outside 13:00-20:30 UTC:   {((t < 13 * 60) | (t > 20 * 60 + 30)).sum()}")

print("\n5. Double booking  (CANCELLED visits do not hold the slot)")
active = ap.assign(s=start, e=end)[ap["STATUS"] != "CANCELLED"]
for who in ("PROVIDER", "PATIENT"):
    clashes = 0
    for _, g in active.sort_values("s").groupby(who):
        clashes += int((g["s"].values[1:] < g["e"].values[:-1]).sum())
    print(f"   same {who.lower():<8} booked twice at once: {clashes}")

print("\n6. Duplicates")
print(f"   repeated APPOINTMENT_ID:            {ap['APPOINTMENT_ID'].duplicated().sum()}")
print(f"   identical rows (every column):      {ap.duplicated().sum()}")

print("\n7. Visits that depend on a prior authorization")
for _, a in linked.iterrows():
    pa = auth_by_id.loc[a["AUTH_ID"]] if a["AUTH_ID"] in auth_by_id.index else None
    if pa is None:
        continue
    inside = pa["VALID_FROM"] <= a["START"][:10] <= pa["VALID_TO"] if pa["DECISION"] == "APPROVED" else None
    print(f"   {a['VISIT_TYPE']:<10} {a['START'][:10]} {a['STATUS']:<22} auth {pa['DECISION']:<9} "
          f"valid {pa['VALID_FROM'] or '-'} to {pa['VALID_TO'] or '-'}  "
          f"{'inside' if inside else 'OUTSIDE' if inside is False else ''}")
