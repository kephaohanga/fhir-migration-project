r"""
phase3_appointment.py
---------------------
Phase 3, step 3: migrate legacy appointments to FHIR Appointment.

An Appointment is the last piece of open work carried across on go-live day. It links to
almost everything migrated before it: the Patient, the Practitioner and the clinic
Location (Phase 1 and 2), the referral (ServiceRequest, step 1) and the prior
authorization (Claim, step 2).

Status: FHIR can express every legacy value without an extension. The appointment has
its own status, and each PARTICIPANT has one too - so "the patient confirmed" lives in
the patient participant's status:
  SCHEDULED              -> booked     patient needs-action
  CONFIRMED              -> booked     patient accepted
  CANCELLED              -> cancelled  patient declined
  PENDING AUTHORIZATION  -> pending    patient needs-action

Mapping:
  APPOINTMENT_ID           -> identifier
  STATUS                   -> status, participant[patient].status   (table above)
  VISIT_TYPE               -> appointmentType      docs/visit_type_map.csv (HL7 v2-0276) + legacy text
  START, END               -> start, end, minutesDuration
  PATIENT                  -> participant  Patient       (must be migrated, alive on the visit date)
  PROVIDER                 -> participant  Practitioner  (must be migrated)
  (the clinic)             -> participant  Location      (Phase 2)
  AUTH_ID                  -> supportingInformation      Claim (prior authorization, step 2)
  REFERRAL_ID              -> basedOn                    ServiceRequest (step 1)
  REASON_CODE, ...DESCRIPTION -> reasonCode              SNOMED CT, if present

Rules:
  R-AP01 Reject  PATIENT is not a migrated patient
  R-AP02 Reject  appointment for a deceased patient: the patient died on or before the visit
                 date (a CANCELLED visit is allowed - it may have been cancelled for that reason)
  R-AP03 Flag    PROVIDER is not a migrated practitioner - practitioner omitted, sent to the worklist
  R-AP04 Fix     STATUS written with other capitals or spaces ("Confirmed ") - tidied
  R-AP04 Reject  STATUS not a known legacy value, or VISIT_TYPE not in docs/visit_type_map.csv
  R-AP05 Reject  START / END not a date-time, or END not after START
  R-AP06 Flag    booked when the clinic is closed: weekend, clinic holiday, or a start outside
                 13:00-20:30 UTC - migrated, sent to Scheduling to rebook
  R-AP07 Flag    double booking: the provider already has an active visit at that time (the LATER
                 row in the file is flagged) - migrated, sent to Scheduling
  R-AP08 Flag    INFUSION / PROCEDURE without a valid authorization: AUTH_ID blank, not migrated,
                 another patient's, DENIED, or the visit falls outside the approved dates
                 (a PENDING authorization is fine for a visit that is itself pending) -
                 migrated, sent to Prior Authorization before the service is given
  R-AP09 Fix     exact duplicate row (EVERY column identical) removed, first copy kept

Run AFTER phase3_prior_auth.py and python src\plant_appointment_errors.py. From the project folder:
  python src\phase3_appointment.py --limit 1   <- ONE bundle of 50 (a test)
  python src\phase3_appointment.py
Then open sql\30_appointment_reconciliation.sql in SSMS and press F5.
"""

import argparse
import csv
import time
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

import pandas as pd

import apply_corrections
from phase2_clinic_structure import identifier, parse_reply, put_entry, search_all, send_bundle
from phase2_practitioner import (BUNDLE_SIZE, FHIR_SERVER, PAUSE_BETWEEN_BUNDLES, PROJECT_TAG,
                                 connect, save_table, v, write_csv)
from phase2_procedure import long_rows, read_table

SOURCE_FILE = Path("data/legacy_dirty/appointments.csv")
ANSWER_KEY = Path("docs/planted_appointment_errors_answer_key.csv")
VISIT_TYPE_MAP = Path("docs/visit_type_map.csv")
OUTPUT_DIR = Path("output/phase3_appointment")

SYS_APPOINTMENT = "urn:fhirmigration:legacy-appointment-id"
SYS_SNOMED = "http://snomed.info/sct"
FMT = "%Y-%m-%dT%H:%M:%SZ"

STATUS_MAP = {                    # legacy -> (Appointment.status, patient participant status)
    "SCHEDULED": ("booked", "needs-action"),
    "CONFIRMED": ("booked", "accepted"),
    "CANCELLED": ("cancelled", "declined"),
    "PENDING AUTHORIZATION": ("pending", "needs-action"),
}
NEEDS_AUTH = {"INFUSION", "PROCEDURE"}
CLINIC_HOLIDAYS = {date(2026, 11, 26), date(2026, 12, 25), date(2027, 1, 1)}
OPEN_FROM, OPEN_UNTIL = 13 * 60, 20 * 60 + 30            # first and last start time, minutes UTC

ISSUE_COLS = ["record_key", "source_row", "rule_id", "action", "detail"]
DISPOSITION_COLS = ["record_key", "source_row", "disposition"]
SOURCE_COLS = ["appointment_key", "record_key", "source_row", "legacy_status", "visit_type",
               "legacy_patient", "legacy_provider", "legacy_auth", "legacy_referral"]
AUDIT_COLS = {"source_row", "loaded_at"}
LONG_EXPECTED = ["resource_type", "legacy_id", "field_name", "expected_value"]
LONG_ACTUAL = ["resource_type", "fhir_id", "legacy_id", "field_name", "actual_value"]
XWALK_COLS = ["resource_type", "legacy_id", "fhir_id", "server_status"]


def flatten_appointment(r):
    """One FHIR Appointment -> the flat fields we compare (same for sent and received)."""
    def actor(prefix):
        return next((p for p in r.get("participant", [])
                     if ((p.get("actor") or {}).get("reference") or "").startswith(prefix)), {})
    patient, practitioner, location = actor("Patient/"), actor("Practitioner/"), actor("Location/")
    kind = r.get("appointmentType") or {}
    reason = next((c for rc in r.get("reasonCode", []) for c in rc.get("coding", [])), {})
    return {
        "status": r.get("status"),
        "appointment_type_code": next(iter(kind.get("coding", [])), {}).get("code"),
        "appointment_type_text": kind.get("text"),
        "start": r.get("start"),
        "end": r.get("end"),
        "minutes_duration": None if r.get("minutesDuration") is None else str(r.get("minutesDuration")),
        "patient_ref": (patient.get("actor") or {}).get("reference"),
        "patient_status": patient.get("status"),
        "practitioner_ref": (practitioner.get("actor") or {}).get("reference"),
        "location_ref": (location.get("actor") or {}).get("reference"),
        "auth_ref": next(iter(r.get("supportingInformation", [])), {}).get("reference"),
        "referral_ref": next(iter(r.get("basedOn", [])), {}).get("reference"),
        "reason_code": reason.get("code"),
    }


def as_instant(text):
    """'2026-11-09T14:00:00Z' -> datetime, or None."""
    try:
        return datetime.strptime((text or "").strip(), FMT)
    except ValueError:
        return None


def read_visit_types():
    with VISIT_TYPE_MAP.open(encoding="utf-8-sig", newline="") as f:
        return {r["visit_type"].strip().upper(): r for r in csv.DictReader(f)}


# ---------------------------------------------------------------------------
# 1. STAGE
# ---------------------------------------------------------------------------
def stage():
    if not SOURCE_FILE.exists():
        raise SystemExit(f"{SOURCE_FILE} not found. Run  python src\\plant_appointment_errors.py  first.")
    df = pd.read_csv(SOURCE_FILE, dtype=str, keep_default_na=False)
    columns = list(df.columns)
    data = [[None if x == "" else x for x in values] + [n + 2]
            for n, values in enumerate(df.itertuples(index=False, name=None))]
    conn = connect()
    cur = conn.cursor()
    cur.execute("IF SCHEMA_ID('stg') IS NULL EXEC('CREATE SCHEMA stg');")
    cur.execute("DROP TABLE IF EXISTS stg.appointments;")
    col_defs = ", ".join(f"[{c}] NVARCHAR(500) NULL" for c in columns)
    cur.execute(f"CREATE TABLE stg.appointments ({col_defs}, [source_row] INT NOT NULL, "
                f"[loaded_at] DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME());")
    cur.fast_executemany = True
    cur.executemany(
        f"INSERT INTO stg.appointments ({', '.join(f'[{c}]' for c in columns)}, [source_row]) "
        f"VALUES ({', '.join('?' for _ in range(len(columns) + 1))});", data)
    sql_rows = cur.execute("SELECT COUNT(*) FROM stg.appointments;").fetchone()[0]
    conn.close()
    print("1. STAGE")
    print(f"   CSV rows {len(df)}   stg.appointments rows {sql_rows}   {'PASS' if sql_rows == len(df) else 'FAIL'}")


# ---------------------------------------------------------------------------
# 3. TRANSFORM
# ---------------------------------------------------------------------------
def transform(rows, ctx, expected):
    """ctx: patients {legacy: fhir}, deaths {legacy: date}, practitioners, location (fhir id),
    auths {legacy: {fhir_id, patient, decision, valid_from, valid_to}}, referrals {legacy: fhir},
    visit_types {VISIT TYPE: map row}."""
    issues, dispositions, appointments, sources = [], [], [], []
    seen_rows = set()
    booked = defaultdict(list)          # provider -> [(start, end, appointment key)] of active visits

    def log(key, src, rule, action, detail):
        issues.append({"record_key": key, "source_row": src, "rule_id": rule, "action": action, "detail": detail})

    def dispose(key, src, disposition):
        dispositions.append({"record_key": key, "source_row": src, "disposition": disposition})

    def reject(key, src, rule, detail):
        log(key, src, rule, "Reject", detail)
        dispose(key, src, "Rejected")

    for r in rows:
        key, src = v(r, "APPOINTMENT_ID"), r["source_row"]
        patient, provider = v(r, "PATIENT"), v(r, "PROVIDER")
        auth_id, referral_id = v(r, "AUTH_ID"), v(r, "REFERRAL_ID")

        signature = tuple((c, r[c]) for c in sorted(r) if c not in AUDIT_COLS)
        if signature in seen_rows:                                                    # R-AP09
            log(key, src, "R-AP09", "Fix", "Exact duplicate row (every column identical) removed - first copy kept")
            dispose(key, src, "Duplicate removed")
            continue
        seen_rows.add(signature)

        if patient not in ctx["patients"]:                                            # R-AP01
            reject(key, src, "R-AP01", f"PATIENT {patient} is not a migrated patient")
            continue
        raw = r.get("STATUS") or ""
        status = raw.strip().upper()
        visit_type = (v(r, "VISIT_TYPE") or "").upper()
        if status not in STATUS_MAP:                                                  # R-AP04 Reject
            reject(key, src, "R-AP04", f"STATUS '{raw}' is not a known legacy value ({', '.join(STATUS_MAP)})")
            continue
        if visit_type not in ctx["visit_types"]:
            reject(key, src, "R-AP04", f"VISIT_TYPE '{v(r, 'VISIT_TYPE')}' is not in {VISIT_TYPE_MAP}")
            continue
        if status != raw:                                                             # R-AP04 Fix
            log(key, src, "R-AP04", "Fix", f"STATUS '{raw}' tidied to '{status}'")
        start, end = as_instant(r.get("START")), as_instant(r.get("END"))
        if start is None or end is None or end <= start:                              # R-AP05
            reject(key, src, "R-AP05", f"START {r.get('START')} / END {r.get('END')}: "
                                       f"not a date-time, or END is not after START")
            continue
        died = ctx["deaths"].get(patient)
        if died and died <= start.date() and status != "CANCELLED":                  # R-AP02
            reject(key, src, "R-AP02", f"Appointment on {start.date()} for a patient who died on {died} "
                                       f"- confirm the patient (wrong record?) or archive the visit")
            continue

        kind = ctx["visit_types"][visit_type]
        appointment_status, patient_status = STATUS_MAP[status]
        participants = [{"actor": {"reference": f"Patient/{ctx['patients'][patient]}"},
                         "required": "required", "status": patient_status}]
        if provider in ctx["practitioners"]:
            participants.append({"actor": {"reference": f"Practitioner/{ctx['practitioners'][provider]}"},
                                 "required": "required", "status": "accepted"})
        else:                                                                         # R-AP03
            log(key, src, "R-AP03", "Flag", f"PROVIDER {provider} is not a migrated practitioner - practitioner omitted")
        participants.append({"actor": {"reference": f"Location/{ctx['location']}"},
                             "required": "required", "status": "accepted"})
        appointment = {
            "resourceType": "Appointment",
            "meta": {"tag": [PROJECT_TAG]},
            "identifier": [{"system": SYS_APPOINTMENT, "value": key}],
            "status": appointment_status,
            "appointmentType": {"coding": [{"system": kind["system"], "code": kind["code"],
                                            "display": kind["display"]}],
                                "text": kind["text"]},
            "start": start.strftime(FMT),
            "end": end.strftime(FMT),
            "minutesDuration": int((end - start).total_seconds() // 60),
            "participant": participants,
        }
        if v(r, "REASON_CODE"):
            appointment["reasonCode"] = [{"coding": [{"system": SYS_SNOMED, "code": v(r, "REASON_CODE"),
                                                      "display": v(r, "REASON_DESCRIPTION")}],
                                          "text": v(r, "REASON_DESCRIPTION")}]
        if referral_id and referral_id in ctx["referrals"]:
            appointment["basedOn"] = [{"reference": f"ServiceRequest/{ctx['referrals'][referral_id]}"}]

        # R-AP06: the clinic is closed
        minute = start.hour * 60 + start.minute
        closed = ("a weekend" if start.weekday() >= 5 else
                  "a clinic holiday" if start.date() in CLINIC_HOLIDAYS else
                  "outside clinic hours (13:00-20:30 UTC)" if not OPEN_FROM <= minute <= OPEN_UNTIL else None)
        if closed and status != "CANCELLED":
            log(key, src, "R-AP06", "Flag", f"Booked on {start.date()} {start:%H:%M} UTC, {closed} - rebook")

        # R-AP07: the provider is already booked at that time (active visits only)
        if status != "CANCELLED" and provider:
            clash = next((k for s, e, k in booked[provider] if start < e and s < end), None)
            if clash:
                log(key, src, "R-AP07", "Flag",
                    f"Double booking: provider {provider} already has appointment {clash} at {start:%Y-%m-%d %H:%M} UTC")
            booked[provider].append((start, end, key))

        # R-AP08: services that need an authorization
        auth = ctx["auths"].get(auth_id) if auth_id else None
        if auth and auth["patient"] == patient:
            appointment["supportingInformation"] = [{"reference": f"Claim/{auth['fhir_id']}"}]
        if visit_type in NEEDS_AUTH and status != "CANCELLED":
            day = start.date().isoformat()
            problem = ("no AUTH_ID" if not auth_id else
                       f"AUTH_ID {auth_id} is not a migrated authorization" if auth is None else
                       f"AUTH_ID {auth_id} belongs to another patient" if auth["patient"] != patient else
                       "the authorization was DENIED" if auth["decision"] == "DENIED" else
                       None if auth["decision"] == "PENDING" and status == "PENDING AUTHORIZATION" else
                       "the authorization is still PENDING but the visit is booked" if auth["decision"] == "PENDING" else
                       f"the visit ({day}) is outside the approved dates {auth['valid_from']} to {auth['valid_to']}"
                       if not (auth["valid_from"] or "") <= day <= (auth["valid_to"] or "") else None)
            if problem:
                log(key, src, "R-AP08", "Flag", f"{visit_type.title()} without a valid authorization: {problem}")

        appointments.append((key, appointment))
        sources.append({"appointment_key": key, "record_key": key, "source_row": src, "legacy_status": status,
                        "visit_type": visit_type, "legacy_patient": patient, "legacy_provider": provider,
                        "legacy_auth": auth_id, "legacy_referral": referral_id})
        expected += long_rows("Appointment", key, flatten_appointment(appointment), "expected_value")
        dispose(key, src, "Migrated")

    counts = defaultdict(int)
    for d in dispositions:
        counts[d["disposition"]] += 1
    ok = len(rows) == len(dispositions)
    print("\n3. TRANSFORM   Appointment")
    print(f"   Legacy rows {len(rows)} = " + " + ".join(f"{n} {d.lower()}" for d, n in sorted(counts.items()))
          + f"   {'PASS' if ok else 'FAIL'}")
    by_status = defaultdict(int)
    for s in sources:
        by_status[s["legacy_status"]] += 1
    print("   Appointments built: " + ", ".join(f"{n} {s}" for s, n in sorted(by_status.items())))
    for rule in sorted({i["rule_id"] + " " + i["action"] for i in issues}):
        print(f"   {rule}: {sum(1 for i in issues if i['rule_id'] + ' ' + i['action'] == rule)}")
    check_answer_key(issues)
    return appointments, issues, dispositions, sources


def check_answer_key(issues):
    if not ANSWER_KEY.exists():
        return
    with ANSWER_KEY.open(encoding="utf-8") as f:
        planted = list({(r["record_id"], r["error_type"]): r for r in csv.DictReader(f)}.values())  # one per defect
    worked = {r["record_key"]: r["disposition"] for r in read_table(
        "SELECT record_key, disposition FROM etl.corrections WHERE table_name = 'appointments';")}
    done = {"Correct": "corrected", "Archive": "archived"}
    caught = 0
    print("   Planted-defect check:")
    for p in planted:
        hit = any(i["record_key"] == p["record_id"] and i["rule_id"] == p["expected_rule"]
                  and i["action"] == p["expected_action"] for i in issues)
        if hit:
            result = "CAUGHT"
        elif p["record_id"] in worked:
            result = f"CAUGHT earlier, {done.get(worked[p['record_id']], 'worked')} by staff"
        else:
            result = "MISSED"
        caught += result != "MISSED"
        print(f"     {p['error_type']:<22} expected {p['expected_rule']} {p['expected_action']:<7} {result}")
    print(f"   Result: {caught} of {len(planted)} planted defects caught")


# ---------------------------------------------------------------------------
# 4. LOAD and 5. EXTRACT
# ---------------------------------------------------------------------------
def load(appointments, load_log, xwalk, limit=None):
    chunks = [appointments[i:i + BUNDLE_SIZE] for i in range(0, len(appointments), BUNDLE_SIZE)]
    if limit:
        chunks = chunks[:limit]
    print(f"\n4. LOAD   {len(chunks)} bundle(s) of Appointment to {FHIR_SERVER}")
    sent = 0
    for n, chunk in enumerate(chunks, 1):
        entries = [put_entry(a, f"Appointment?identifier={SYS_APPOINTMENT}|{key}") for key, a in chunk]
        sent += len(entries)
        reply = send_bundle(entries, n, "Appointment", load_log)
        if reply is not None:
            for (key, _), r in zip(chunk, reply):
                fhir_id, status = parse_reply(r, "Appointment")
                xwalk.append({"resource_type": "Appointment", "legacy_id": key, "fhir_id": fhir_id,
                              "server_status": status})
        time.sleep(PAUSE_BETWEEN_BUNDLES)
    confirmed = sum(1 for x in xwalk if x["fhir_id"])
    created = sum(1 for x in xwalk if x["server_status"].startswith("201"))
    updated = sum(1 for x in xwalk if x["server_status"].startswith("200"))
    failed = sum(1 for l in load_log if l["outcome"] == "FAILED")
    print(f"   Sent {sent}, confirmed {confirmed} ({created} created, {updated} updated), "
          f"failed bundles {failed}   {'PASS' if confirmed == sent else 'FAIL'}")


def extract(xwalk):
    print("\n5. EXTRACT")
    found = search_all("Appointment")
    actual = []
    for r in found:
        actual += long_rows("Appointment", identifier(r, SYS_APPOINTMENT), flatten_appointment(r),
                            "actual_value", r.get("id"))
    expected_ids = {x["fhir_id"] for x in xwalk if x["fhir_id"]}
    on_server = {r.get("id") for r in found}
    missing = expected_ids - on_server
    print(f"   Appointment  expected {len(expected_ids)}, on server {len(on_server)}, "
          f"missing {len(missing)}   {'PASS' if not missing else 'FAIL'}")
    save_table("tgt.appointment_resources", actual, LONG_ACTUAL)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def read_context():
    location = read_table("SELECT fhir_id FROM etl.clinic_crosswalk "
                          "WHERE resource_type = 'Location' AND fhir_id IS NOT NULL;")
    if not location:
        raise SystemExit("Clinic Location not found - run src\\phase2_clinic_structure.py first.")
    deaths = {}
    for r in read_table("SELECT Id, DEATHDATE FROM cur.patients WHERE DEATHDATE IS NOT NULL;"):
        try:
            deaths[r["Id"]] = date.fromisoformat(r["DEATHDATE"][:10])
        except (TypeError, ValueError):
            pass
    auths = {r["legacy_id"]: {"fhir_id": r["fhir_id"], "patient": r["PATIENT"], "decision": r["decision"],
                              "valid_from": r["VALID_FROM"], "valid_to": r["VALID_TO"]}
             for r in read_table("""
        SELECT x.legacy_id, x.fhir_id, pa.PATIENT, s.decision, pa.VALID_FROM, pa.VALID_TO
        FROM etl.prior_auth_crosswalk AS x
        JOIN etl.prior_auth_sources AS s ON s.auth_key = x.legacy_id
        JOIN cur.prior_auths AS pa ON pa.source_row = TRY_CAST(s.source_row AS INT)
        WHERE x.resource_type = 'Claim' AND x.fhir_id IS NOT NULL;""")}
    lookup = lambda sql: {r["legacy_id"]: r["fhir_id"] for r in read_table(sql)}
    return {
        "patients": lookup("SELECT legacy_id, fhir_id FROM etl.fhir_crosswalk "
                           "WHERE resource_type = 'Patient' AND fhir_id IS NOT NULL;"),
        "deaths": deaths,
        "practitioners": lookup("SELECT legacy_id, fhir_id FROM etl.practitioner_crosswalk WHERE fhir_id IS NOT NULL;"),
        "location": location[0]["fhir_id"],
        "auths": auths,
        "referrals": lookup("SELECT legacy_id, fhir_id FROM etl.referral_crosswalk WHERE fhir_id IS NOT NULL;"),
        "visit_types": read_visit_types(),
    }


def main():
    parser = argparse.ArgumentParser(description="Phase 3: migrate appointments to FHIR Appointment")
    parser.add_argument("--limit", type=int, help="only send the first N bundles (try 1 first)")
    args = parser.parse_args()

    stage()
    print("\n2. CURATE   staff corrections from remediation/corrections.csv")
    if not apply_corrections.main(show_next=False):
        raise SystemExit("Stopped: fix remediation/corrections.csv, then run this script again.")
    rows = read_table("SELECT * FROM cur.appointments ORDER BY source_row;")
    ctx = read_context()
    print(f"   cur.appointments {len(rows)} rows; migrated patients {len(ctx['patients'])} "
          f"({len(ctx['deaths'])} with a death date), practitioners {len(ctx['practitioners'])}, "
          f"authorizations {len(ctx['auths'])}, referrals {len(ctx['referrals'])}")

    expected = []
    appointments, issues, dispositions, sources = transform(rows, ctx, expected)
    save_table("etl.appointment_issue_log", issues, ISSUE_COLS)
    save_table("etl.appointment_disposition", dispositions, DISPOSITION_COLS)
    save_table("etl.appointment_sources", sources, SOURCE_COLS)
    save_table("etl.appointment_expected", expected, LONG_EXPECTED)
    write_csv(OUTPUT_DIR / "issue_log.csv", issues, ISSUE_COLS)

    load_log, xwalk = [], []
    load(appointments, load_log, xwalk, args.limit)
    save_table("etl.appointment_crosswalk", xwalk, XWALK_COLS)
    save_table("etl.appointment_load_log", load_log,
               ["bundle_no", "label", "entries", "http_status", "outcome", "attempts", "seconds", "error"])
    write_csv(OUTPUT_DIR / "crosswalk.csv", xwalk, XWALK_COLS)

    extract(xwalk)
    print("\nNext: open sql\\30_appointment_reconciliation.sql in SSMS and press F5.")


if __name__ == "__main__":
    main()
