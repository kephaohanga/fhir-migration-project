"""
generate_phase3_legacy.py
-------------------------
Phase 3 test data: simulate the legacy SCHEDULING, REFERRAL and PRIOR
AUTHORIZATION systems. Synthea does not produce these, so this script generates
them - reproducibly (fixed seed) and FROM THE EXISTING LEGACY DATA, so every
record links to real patients, providers, insurance, drugs and diagnoses.

This is the "open work" a clinic carries across on go-live day:
  data/legacy/appointments.csv   future visits in the 90 days after the extract date
  data/legacy/referrals.csv      referrals to specialists, most still open
  data/legacy/prior_auths.csv    prior authorization requests and payer decisions

Rules (chosen by the project owner):
  Prior authorization is required for
    - high-cost drugs:      Epogen (205923), cisplatin (1736854), etoposide (226719), the brand
                            fluticasone/salmeterol inhaler (896209) and Humulin 70/30 (106892)
                            - RxNorm; prescriptions active or started in the last 2 years.
                            Clinic-given drugs (Epogen, chemotherapy) also get infusion visits.
    - planned surgeries:    taken from Synthea's own condition -> surgery pairs   - SNOMED CT
    - specialist referrals: one per matching chronic condition (up to 2 specialties per
                            patient); authorization for Medicaid and private payers
                            (not Medicare / dual, as with traditional Medicare)        - NUCC
  Outcomes: 70% approved, 20% pending, 10% denied (all payers).
  Appointments: weekdays, 90 days after the extract date, clinic hours
    (13:00-20:30 UTC, about 8 am - 4:30 pm Eastern), no double booking per provider.
  Self-pay patients need no prior authorization; deceased patients get no future visits.

These files are generated CLEAN. Defects (expired authorization, referral without a
diagnosis, appointment for a deceased patient, ...) are planted later by the
migration's own plant scripts, exactly as in Phases 1 and 2.

Inputs (clean legacy exports in data/legacy/):
  patients.csv, encounters.csv, conditions.csv, procedures.csv, medications.csv,
  payer_transitions.csv, payers.csv, and docs/referral_specialty_map.csv
Run from the project folder:  python src\\generate_phase3_legacy.py
"""

import csv
import random
import re
import uuid
from collections import Counter, defaultdict
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import pandas as pd

LEGACY = Path("data/legacy")
SPECIALTY_MAP = Path("docs/referral_specialty_map.csv")
SEED = 2032
WINDOW_DAYS = 90
OUTCOMES = [("APPROVED", 0.70), ("PENDING", 0.20), ("DENIED", 0.10)]
DENIAL_REASONS = ["Medical necessity not established", "Step therapy required first",
                  "Documentation incomplete", "Service not covered under plan"]
PA_DRUGS = {"205923": "Epogen", "1736854": "cisplatin", "226719": "etoposide",
            "896209": "fluticasone/salmeterol inhaler", "106892": "Humulin 70/30 insulin"}
CLINIC_ADMINISTERED = {"205923", "1736854", "226719"}  # given in the clinic -> infusion visits;
                                                      # inhaler and insulin are taken at home
DRUG_LOOKBACK_DAYS = 730                              # prescriptions active or started in the last 2 years
SURGICAL_WORDS = re.compile(r"ectomy|otomy|ostomy|plasty|replacement|excision|repair|insertion|"
                            r"transplant|resection|amputation|fixation|arthroscop", re.I)
FMT = "%Y-%m-%dT%H:%M:%SZ"

rng = random.Random(SEED)


def new_id():
    return str(uuid.UUID(int=rng.getrandbits(128)))


def dt(text):
    return datetime.strptime(text, FMT).replace(tzinfo=timezone.utc) if text else None


def outcome():
    x, total = rng.random(), 0.0
    for name, share in OUTCOMES:
        total += share
        if x < total:
            return name
    return OUTCOMES[-1][0]


def read(name):
    return pd.read_csv(LEGACY / name, dtype=str, keep_default_na=False)


# ---------------------------------------------------------------------------
# Context from the existing legacy data
# ---------------------------------------------------------------------------
def load_context():
    patients = read("patients.csv").drop_duplicates("Id")
    encounters = read("encounters.csv")
    conditions = read("conditions.csv")
    procedures = read("procedures.csv")
    medications = read("medications.csv")
    transitions = read("payer_transitions.csv")
    payers = read("payers.csv").set_index("Id")

    as_of = max(dt(s) for s in transitions["START_DATE"] if s)          # same extract date as Coverage
    living = set(patients.loc[patients["DEATHDATE"] == "", "Id"])

    # Each patient's most recent provider (the one they would book a follow-up with)
    enc = encounters[encounters["START"] != ""].copy()
    enc["start_dt"] = enc["START"].map(dt)
    enc = enc[enc["start_dt"] <= as_of].sort_values("start_dt")
    last_provider = enc.groupby("PATIENT")["PROVIDER"].last().to_dict()

    # Current coverage (same rule as phase2_coverage.py)
    coverage = {}
    for _, r in transitions.iterrows():
        start, end = dt(r["START_DATE"]), dt(r["END_DATE"])
        if start and start <= as_of and (end is None or end > as_of):
            p = payers.loc[r["PAYER"]] if r["PAYER"] in payers.index else None
            if p is not None and p["NAME"] != "NO_INSURANCE":
                coverage[r["PATIENT"]] = {"payer": r["PAYER"], "member": r["MEMBERID"],
                                          "name": p["NAME"], "ownership": p["OWNERSHIP"]}

    active_conditions = conditions[(conditions["STOP"] == "") & conditions["PATIENT"].isin(living)]
    return dict(as_of=as_of, living=living, last_provider=last_provider, coverage=coverage,
                active_conditions=active_conditions, procedures=procedures, medications=medications)


def read_specialty_map():
    with SPECIALTY_MAP.open(encoding="utf-8-sig", newline="") as f:
        return [(r["keyword"].lower(), r["specialty"], r["nucc_code"]) for r in csv.DictReader(f)]


# ---------------------------------------------------------------------------
# Scheduling helper: weekday slots, no double booking per provider
# ---------------------------------------------------------------------------
CLINIC_HOLIDAYS = {date_ for date_ in (                 # closed: no appointments
    datetime(2026, 11, 26).date(),                     # Thanksgiving
    datetime(2026, 12, 25).date(),                     # Christmas Day
    datetime(2027, 1, 1).date(),                       # New Year's Day
)}


class Scheduler:
    def __init__(self, as_of):
        first = (as_of + timedelta(days=1)).date()
        self.days = [first + timedelta(days=i) for i in range(WINDOW_DAYS)
                     if (first + timedelta(days=i)).weekday() < 5
                     and (first + timedelta(days=i)) not in CLINIC_HOLIDAYS]
        self.slots = [time(13 + m // 60, m % 60) for m in range(0, 480, 30)]   # 13:00 .. 20:30 UTC
        self.taken = set()

    def book(self, provider, minutes, after=None):
        days = [d for d in self.days if after is None or d >= after]
        for _ in range(200):
            day, slot = rng.choice(days), rng.choice(self.slots)
            start = datetime.combine(day, slot, tzinfo=timezone.utc)
            blocks = {(provider, start + timedelta(minutes=30 * k)) for k in range(max(1, minutes // 30))}
            if not blocks & self.taken:
                self.taken |= blocks
                return start, start + timedelta(minutes=minutes)
        raise RuntimeError("No free slot found - widen the window")


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------
def prior_auth(ctx, patient, service_type, system, code, description, request_date, referral_id=""):
    cov = ctx["coverage"][patient]
    decision = outcome()
    decision_date = request_date + timedelta(days=rng.randint(2, 14)) if decision != "PENDING" else None
    return {
        "AUTH_ID": new_id(), "PATIENT": patient, "PAYER": cov["payer"], "MEMBERID": cov["member"],
        "SERVICE_TYPE": service_type, "SERVICE_SYSTEM": system, "SERVICE_CODE": code,
        "SERVICE_DESCRIPTION": description, "REQUEST_DATE": request_date.date().isoformat(),
        "DECISION": decision, "DECISION_DATE": decision_date.date().isoformat() if decision_date else "",
        "AUTH_NUMBER": f"PA{rng.randint(10**7, 10**8 - 1)}" if decision == "APPROVED" else "",
        "VALID_FROM": decision_date.date().isoformat() if decision == "APPROVED" else "",
        "VALID_TO": (decision_date + timedelta(days=180)).date().isoformat() if decision == "APPROVED" else "",
        "DENIAL_REASON": rng.choice(DENIAL_REASONS) if decision == "DENIED" else "",
        "REFERRAL_ID": referral_id,
    }


def appointment(patient, provider, start, end, visit_type, status, reason_code="", reason_text="",
                auth_id="", referral_id=""):
    return {"APPOINTMENT_ID": new_id(), "PATIENT": patient, "PROVIDER": provider,
            "START": start.strftime(FMT), "END": end.strftime(FMT), "VISIT_TYPE": visit_type,
            "STATUS": status, "REASON_CODE": reason_code, "REASON_DESCRIPTION": reason_text,
            "AUTH_ID": auth_id, "REFERRAL_ID": referral_id}


def generate():
    ctx = load_context()
    as_of = ctx["as_of"]
    sched = Scheduler(as_of)
    specialty_map = read_specialty_map()
    appointments, referrals, auths = [], [], []
    living = sorted(ctx["living"])

    # 1. Follow-up visits with each living patient's most recent provider
    for p in living:
        provider = ctx["last_provider"].get(p)
        if not provider:
            continue
        for _ in range(rng.choice([1, 1, 2])):
            start, end = sched.book(provider, 30)
            status = rng.choices(["SCHEDULED", "CONFIRMED", "CANCELLED"], [60, 30, 10])[0]
            appointments.append(appointment(p, provider, start, end, "FOLLOW UP", status))

    # 2. High-cost drugs: one prior authorization per patient + drug, then infusion visits
    meds = ctx["medications"]
    recent = meds[meds["CODE"].isin(PA_DRUGS) & meds["PATIENT"].isin(ctx["living"])]
    recent = recent[(recent["STOP"] == "")
                    | (recent["START"].map(dt) >= as_of - timedelta(days=DRUG_LOOKBACK_DAYS))]
    for (p, code), group in recent.groupby(["PATIENT", "CODE"]):
        if p not in ctx["coverage"]:
            continue                                        # self-pay: no authorization needed
        row = group.iloc[-1]
        request = as_of - timedelta(days=rng.randint(1, 30))
        auth = prior_auth(ctx, p, "MEDICATION", "RxNorm", code, row["DESCRIPTION"], request)
        auths.append(auth)
        provider = ctx["last_provider"].get(p)
        if provider and auth["DECISION"] != "DENIED" and code in CLINIC_ADMINISTERED:
            status = "SCHEDULED" if auth["DECISION"] == "APPROVED" else "PENDING AUTHORIZATION"
            for _ in range(rng.randint(1, 3)):
                start, end = sched.book(provider, 60)
                appointments.append(appointment(p, provider, start, end, "INFUSION", status,
                                                auth_id=auth["AUTH_ID"]))

    # 3. Planned surgeries: Synthea's own condition -> surgery pairs, for patients who still
    #    have the condition but have not had that surgery
    procs = ctx["procedures"]
    surgical = procs[procs["DESCRIPTION"].str.contains(SURGICAL_WORDS) & (procs["REASONCODE"] != "")]
    pairs = surgical.groupby(["REASONCODE", "CODE", "DESCRIPTION"]).size().reset_index()
    done = set(zip(procs["PATIENT"], procs["CODE"]))
    candidates = []
    for _, c in ctx["active_conditions"].iterrows():
        for _, s in pairs[pairs["REASONCODE"] == c["CODE"]].iterrows():
            if (c["PATIENT"], s["CODE"]) not in done and c["PATIENT"] in ctx["coverage"]:
                candidates.append((c["PATIENT"], c["CODE"], c["DESCRIPTION"], s["CODE"], s["DESCRIPTION"]))
    chosen, seen_patients = [], set()
    for cand in rng.sample(candidates, len(candidates)):
        if cand[0] not in seen_patients and len(chosen) < 8:
            chosen.append(cand)
            seen_patients.add(cand[0])
    for p, reason_code, reason_text, code, description in chosen:
        request = as_of - timedelta(days=rng.randint(1, 21))
        auth = prior_auth(ctx, p, "PROCEDURE", "SNOMED-CT", code, description, request)
        auths.append(auth)
        provider = ctx["last_provider"].get(p)
        if provider and auth["DECISION"] != "DENIED":
            status = "SCHEDULED" if auth["DECISION"] == "APPROVED" else "PENDING AUTHORIZATION"
            start, end = sched.book(provider, 120, after=(as_of + timedelta(days=14)).date())
            appointments.append(appointment(p, provider, start, end, "PROCEDURE", status,
                                            reason_code, reason_text, auth_id=auth["AUTH_ID"]))

    # 4. Specialist referrals for active chronic conditions (first keyword match wins)
    by_patient = defaultdict(list)
    for _, c in ctx["active_conditions"].iterrows():
        text = c["DESCRIPTION"].lower()
        match = next(((spec, nucc) for kw, spec, nucc in specialty_map if kw in text), None)
        if match:
            by_patient[c["PATIENT"]].append((c, match))
    for p, items in sorted(by_patient.items()):
        provider = ctx["last_provider"].get(p)
        if not provider:
            continue
        specialties_done = set()
        for c, (specialty, nucc) in rng.sample(items, min(2, len(items))):
            if specialty in specialties_done:
                continue
            specialties_done.add(specialty)
            authored = as_of - timedelta(days=rng.randint(1, 120))
            referral = {"REFERRAL_ID": new_id(), "PATIENT": p, "REQUESTER": provider,
                        "SPECIALTY": specialty, "SPECIALTY_CODE": nucc,
                        "REASON_CODE": c["CODE"], "REASON_DESCRIPTION": c["DESCRIPTION"],
                        "AUTHORED_DATE": authored.date().isoformat(),
                        "STATUS": rng.choices(["OPEN", "SCHEDULED", "COMPLETED"], [55, 30, 15])[0],
                        "PRIORITY": rng.choices(["ROUTINE", "URGENT"], [85, 15])[0]}
            referrals.append(referral)
            cov = ctx["coverage"].get(p)
            if cov and (cov["ownership"] == "PRIVATE" or cov["name"] == "Medicaid") \
                    and referral["STATUS"] != "COMPLETED":
                auths.append(prior_auth(ctx, p, "REFERRAL", "NUCC", nucc, f"Referral to {specialty}",
                                        authored + timedelta(days=1), referral_id=referral["REFERRAL_ID"]))
    return ctx, appointments, referrals, auths


def write(name, rows):
    path = LEGACY / name
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return path


def main():
    ctx, appointments, referrals, auths = generate()
    for name, rows in (("appointments.csv", appointments), ("referrals.csv", referrals),
                       ("prior_auths.csv", auths)):
        if not rows:
            raise SystemExit(f"No rows generated for {name} - check the input files.")
        print(f"{write(name, rows)}  {len(rows)} rows")

    insured_living = len(ctx["living"] & set(ctx["coverage"]))
    print(f"\nExtract date: {ctx['as_of']:%Y-%m-%d}   window: {WINDOW_DAYS} days   "
          f"living patients: {len(ctx['living'])} ({insured_living} insured, "
          f"{len(ctx['living']) - insured_living} self-pay or no current policy)")
    print("\nAppointments by visit type and status:")
    for (vt, st), n in sorted(Counter((a["VISIT_TYPE"], a["STATUS"]) for a in appointments).items()):
        print(f"  {vt:<10} {st:<22} {n}")
    print("\nReferrals by specialty:")
    for spec, n in Counter(r["SPECIALTY"] for r in referrals).most_common():
        print(f"  {spec:<36} {n}")
    print("\nPrior authorizations by service type and decision:")
    for (t, d), n in sorted(Counter((a["SERVICE_TYPE"], a["DECISION"]) for a in auths).items()):
        print(f"  {t:<11} {d:<9} {n}")
    print("\nPrior authorizations by payer:")
    names = {p: c["name"] for p, c in ctx["coverage"].items()}
    for payer, n in Counter(names[a["PATIENT"]] for a in auths).most_common():
        print(f"  {payer:<24} {n}")


if __name__ == "__main__":
    main()
