"""
phase2_medication.py
--------------------
Phase 2, step 7: migrate legacy prescriptions to FHIR MedicationRequest.

Decision (full history): every prescription moves, not only the active ones.
Clinicians need medication history (past chemotherapy, drugs behind an allergy
or side effect), and the volume is small. STOP blank -> status "active";
STOP present -> status "completed" (the legacy data says the prescription
ended, not that it was discontinued early, which "stopped" would claim).

Code system: Synthea's data dictionary states that CODE is RxNorm and
REASONCODE is SNOMED CT - confirmed by the source's documentation, not
guessed. A sanity rule still rejects a code that cannot be RxNorm.

Mapping:
  CODE, DESCRIPTION        -> medicationCodeableConcept   RxNorm
  STOP blank / present     -> status                      active / completed
  (always)                 -> intent                      order
  PATIENT                  -> subject                     Patient (must be the visit's patient)
  ENCOUNTER                -> encounter                   Encounter (must be migrated)
  START                    -> authoredOn                  when it was prescribed
  START, STOP              -> dispenseRequest.validityPeriod
  REASONCODE, ...DESCRIPTION -> reasonCode                SNOMED CT
  visit's PROVIDER (derived) -> requester                 Practitioner
  PAYER, BASE_COST, PAYER_COVERAGE, TOTALCOST, DISPENSES
                           -> not migrated: billing and dispensing history, not the
                              prescription itself (open balances stay in legacy)

Rules:
  R-MD01 Reject            PATIENT is not a migrated patient
  R-MD02 Reject            ENCOUNTER is not in the legacy encounter file (orphan)
  R-MD02 Reject (cascade)  the visit exists but was not migrated: stays with its visit in legacy
  R-MD03 Reject            PATIENT is not the visit's patient
  R-MD04 Reject            CODE blank, not numeric, or DESCRIPTION looks like SNOMED CT
                           (ends with a tag such as "(substance)") - cannot be RxNorm
  R-MD05 Reject            START blank/invalid, or STOP before START
  R-MD06 Fix               exact duplicate row (EVERY column identical) removed, first copy kept
  R-MD07 Flag              the visit has no migrated practitioner - requester omitted
  R-MD08 Flag              same patient, visit, drug and start as an earlier row but a different
                           stop date or number of fills: a separate, real prescription - kept
                           (its identifier gets the stop date, or "open", to stay unique)

Lesson behind R-MD06/R-MD08: a first version treated rows sharing patient + visit +
drug + start as duplicates. Profiling showed 8 such pairs differ in stop date and
fills (Synthea writes an initial supply and an ongoing prescription), so that rule
dropped 8 real prescriptions, one of them active. A duplicate must be identical.

Run AFTER Phase 1 and src\\phase2_encounter_links.py. From the project folder:
  python src\\phase2_medication.py --limit 1   <- ONE bundle of 50 (a test)
  python src\\phase2_medication.py             <- everything (about 57 bundles)
Then open sql\\26_medication_reconciliation.sql in SSMS and press F5.
"""

import argparse
import csv
import re
import time
from collections import defaultdict
from pathlib import Path

import pandas as pd

import apply_corrections
from phase2_clinic_structure import identifier, parse_reply, put_entry, search_all
from phase2_practitioner import (BUNDLE_SIZE, FHIR_SERVER, PAUSE_BETWEEN_BUNDLES, PROJECT_TAG,
                                 connect, save_table, v, write_csv)
from phase2_procedure import _send_quiet, legacy_key, long_rows, parse_dt, read_table

SOURCE_FILE = Path("data/legacy_dirty/medications.csv")
ANSWER_KEY = Path("docs/planted_medication_errors_answer_key.csv")
OUTPUT_DIR = Path("output/phase2_medication")

SYS_MEDICATION = "urn:fhirmigration:medication-request-id"
SYS_RXNORM = "http://www.nlm.nih.gov/research/umls/rxnorm"
SYS_SNOMED = "http://snomed.info/sct"
SNOMED_TAG = re.compile(r"\((substance|finding|disorder|procedure|organism|product|situation)\)\s*$", re.I)

ISSUE_COLS = ["record_key", "source_row", "rule_id", "action", "detail"]
DISPOSITION_COLS = ["record_key", "source_row", "disposition"]
SOURCE_COLS = ["medication_key", "record_key", "source_row", "legacy_patient", "legacy_encounter"]
AUDIT_COLS = {"source_row", "loaded_at"}          # not part of the legacy data itself
LONG_EXPECTED = ["resource_type", "legacy_id", "field_name", "expected_value"]
LONG_ACTUAL = ["resource_type", "fhir_id", "legacy_id", "field_name", "actual_value"]
XWALK_COLS = ["resource_type", "legacy_id", "fhir_id", "server_status"]


def flatten_medication(r):
    coding = next((c for c in (r.get("medicationCodeableConcept") or {}).get("coding", [])
                   if c.get("system") == SYS_RXNORM), {})
    reason = next((c.get("code") for rc in r.get("reasonCode", []) for c in rc.get("coding", [])
                   if c.get("system") == SYS_SNOMED), None)
    validity = (r.get("dispenseRequest") or {}).get("validityPeriod") or {}
    return {
        "status": r.get("status"),
        "intent": r.get("intent"),
        "code": coding.get("code"),
        "code_display": coding.get("display"),
        "subject_ref": (r.get("subject") or {}).get("reference"),
        "encounter_ref": (r.get("encounter") or {}).get("reference"),
        "authored_on": r.get("authoredOn"),
        "validity_start": validity.get("start"),
        "validity_end": validity.get("end"),
        "requester_ref": (r.get("requester") or {}).get("reference"),
        "reason_code": reason,
    }


# ---------------------------------------------------------------------------
# 1. STAGE
# ---------------------------------------------------------------------------
def stage():
    if not SOURCE_FILE.exists():
        raise SystemExit(f"{SOURCE_FILE} not found. Run  python src\\plant_medication_errors.py  first.")
    df = pd.read_csv(SOURCE_FILE, dtype=str, keep_default_na=False)
    columns = list(df.columns)
    data = [[None if x == "" else x for x in values] + [n + 2]
            for n, values in enumerate(df.itertuples(index=False, name=None))]
    conn = connect()
    cur = conn.cursor()
    cur.execute("IF SCHEMA_ID('stg') IS NULL EXEC('CREATE SCHEMA stg');")
    cur.execute("DROP TABLE IF EXISTS stg.medications;")
    col_defs = ", ".join(f"[{c}] NVARCHAR(500) NULL" for c in columns)
    cur.execute(f"CREATE TABLE stg.medications ({col_defs}, [source_row] INT NOT NULL, "
                f"[loaded_at] DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME());")
    cur.fast_executemany = True
    cur.executemany(
        f"INSERT INTO stg.medications ({', '.join(f'[{c}]' for c in columns)}, [source_row]) "
        f"VALUES ({', '.join('?' for _ in range(len(columns) + 1))});", data)
    sql_rows = cur.execute("SELECT COUNT(*) FROM stg.medications;").fetchone()[0]
    conn.close()
    print("1. STAGE")
    print(f"   CSV rows {len(df)}   stg.medications rows {sql_rows}   {'PASS' if sql_rows == len(df) else 'FAIL'}")


# ---------------------------------------------------------------------------
# 3. TRANSFORM
# ---------------------------------------------------------------------------
def transform(rows, encounters, enc_xwalk, patient_xwalk, prac_xwalk, expected):
    issues, dispositions, requests, sources = [], [], [], []
    seen_rows = set()          # full-row signatures: for EXACT duplicates
    used_ids = {}              # FHIR identifier -> source row that took it

    def log(key, src, rule, action, detail):
        issues.append({"record_key": key, "source_row": src, "rule_id": rule, "action": action, "detail": detail})

    def dispose(key, src, disposition):
        dispositions.append({"record_key": key, "source_row": src, "disposition": disposition})

    def reject(key, src, rule, detail, action="Reject"):
        log(key, src, rule, action, detail)
        dispose(key, src, "Rejected (cascade)" if action == "Reject (cascade)" else "Rejected")

    for r in rows:
        key, src = legacy_key(r), r["source_row"]
        enc_id, patient, code = v(r, "ENCOUNTER"), v(r, "PATIENT"), v(r, "CODE")

        signature = tuple((c, r[c]) for c in sorted(r) if c not in AUDIT_COLS)
        if signature in seen_rows:                                                    # R-MD06
            log(key, src, "R-MD06", "Fix", "Exact duplicate row (every column identical) removed - first copy kept")
            dispose(key, src, "Duplicate removed")
            continue
        seen_rows.add(signature)

        if patient not in patient_xwalk:                                              # R-MD01
            reject(key, src, "R-MD01", f"PATIENT {patient} is not a migrated patient")
            continue
        visit = encounters.get(enc_id)
        if visit is None:                                                             # R-MD02
            reject(key, src, "R-MD02", f"ENCOUNTER {enc_id} is not in the legacy encounter file (orphan)")
            continue
        if enc_id not in enc_xwalk:                                                   # R-MD02 cascade
            reject(key, src, "R-MD02",
                   f"Visit {enc_id} was not migrated - prescription stays with its visit in the legacy archive",
                   action="Reject (cascade)")
            continue
        if patient != visit["PATIENT"]:                                               # R-MD03
            reject(key, src, "R-MD03", f"PATIENT {patient} is not the visit's patient ({visit['PATIENT']})")
            continue
        if code is None or not code.isdigit() or SNOMED_TAG.search(v(r, "DESCRIPTION") or ""):  # R-MD04
            reject(key, src, "R-MD04",
                   f"CODE '{code}' cannot be RxNorm (blank, not numeric, or SNOMED-style description)")
            continue
        start, stop = parse_dt(v(r, "START")), parse_dt(v(r, "STOP"))
        if start is None or (v(r, "STOP") and (stop is None or stop < start)):        # R-MD05
            reject(key, src, "R-MD05", f"Dates invalid: START {v(r, 'START')} STOP {v(r, 'STOP')}")
            continue

        medication_key = f"{enc_id}_{code}_{re.sub(r'[^0-9]', '', v(r, 'START'))}"
        if medication_key in used_ids:                                                # R-MD08
            first_row = used_ids[medication_key]
            medication_key += "_" + (re.sub(r"[^0-9]", "", v(r, "STOP")) if v(r, "STOP") else "open")
            if medication_key in used_ids:                   # same stop too: fall back to the row number
                medication_key += f"_r{src}"
            log(key, src, "R-MD08", "Flag",
                f"Same patient, visit, drug and start as row {first_row} but different stop/fills "
                f"(STOP {v(r, 'STOP')}, DISPENSES {v(r, 'DISPENSES')}) - kept as a separate prescription")
        used_ids[medication_key] = src
        validity = {"start": v(r, "START")}
        if v(r, "STOP"):
            validity["end"] = v(r, "STOP")
        request = {
            "resourceType": "MedicationRequest",
            "meta": {"tag": [PROJECT_TAG]},
            "identifier": [{"system": SYS_MEDICATION, "value": medication_key}],
            "status": "completed" if v(r, "STOP") else "active",
            "intent": "order",
            "medicationCodeableConcept": {
                "coding": [{"system": SYS_RXNORM, "code": code, "display": v(r, "DESCRIPTION")}],
                "text": v(r, "DESCRIPTION")},
            "subject": {"reference": f"Patient/{patient_xwalk[patient]}"},
            "encounter": {"reference": f"Encounter/{enc_xwalk[enc_id]}"},
            "authoredOn": v(r, "START"),
            "dispenseRequest": {"validityPeriod": validity},
        }
        practitioner = prac_xwalk.get(visit["PROVIDER"])
        if practitioner:
            request["requester"] = {"reference": f"Practitioner/{practitioner}"}
        else:                                                                         # R-MD07
            log(key, src, "R-MD07", "Flag", "The visit has no migrated practitioner - requester omitted")
        if v(r, "REASONCODE"):
            request["reasonCode"] = [{"coding": [{"system": SYS_SNOMED, "code": v(r, "REASONCODE"),
                                                  "display": v(r, "REASONDESCRIPTION")}],
                                      "text": v(r, "REASONDESCRIPTION")}]

        requests.append((medication_key, request))
        sources.append({"medication_key": medication_key, "record_key": key, "source_row": src,
                        "legacy_patient": patient, "legacy_encounter": enc_id})
        expected += long_rows("MedicationRequest", medication_key, flatten_medication(request), "expected_value")
        dispose(key, src, "Migrated")

    counts = defaultdict(int)
    for d in dispositions:
        counts[d["disposition"]] += 1
    ok = len(rows) == len(dispositions)
    print("\n3. TRANSFORM   MedicationRequest")
    print(f"   Legacy rows {len(rows)} = " + " + ".join(f"{n} {d.lower()}" for d, n in sorted(counts.items()))
          + f"   {'PASS' if ok else 'FAIL'}")
    active = sum(1 for _, m in requests if m["status"] == "active")
    with_reason = sum(1 for _, m in requests if "reasonCode" in m)
    print(f"   Prescriptions built: {len(requests)} ({active} active, {len(requests) - active} completed; "
          f"{with_reason} with a reason)")
    for rule in sorted({i["rule_id"] + " " + i["action"] for i in issues}):
        print(f"   {rule}: {sum(1 for i in issues if i['rule_id'] + ' ' + i['action'] == rule)}")
    check_answer_key(issues)
    return requests, issues, dispositions, sources


def check_answer_key(issues):
    if not ANSWER_KEY.exists():
        return
    with ANSWER_KEY.open(encoding="utf-8") as f:
        planted = list(csv.DictReader(f))
    worked = {r["record_key"]: r["disposition"] for r in read_table(
        "SELECT record_key, disposition FROM etl.corrections WHERE table_name = 'medications';")}
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
        print(f"     {p['error_type']:<18} expected {p['expected_rule']} {p['expected_action']:<7} {result}")
    print(f"   Result: {caught} of {len(planted)} planted defects caught")


# ---------------------------------------------------------------------------
# 4. LOAD and 5. EXTRACT
# ---------------------------------------------------------------------------
def load(requests, load_log, xwalk, limit=None):
    chunks = [requests[i:i + BUNDLE_SIZE] for i in range(0, len(requests), BUNDLE_SIZE)]
    if limit:
        chunks = chunks[:limit]
    print(f"\n4. LOAD   {len(chunks)} bundle(s) of MedicationRequest to {FHIR_SERVER}")
    sent = 0
    for n, chunk in enumerate(chunks, 1):
        entries = [put_entry(m, f"MedicationRequest?identifier={SYS_MEDICATION}|{key}") for key, m in chunk]
        sent += len(entries)
        if n % 10 == 1 or n == len(chunks):
            print(f"   ... bundle {n} of {len(chunks)}")
        reply = _send_quiet(entries, n, load_log)
        if reply is not None:
            for (key, _), r in zip(chunk, reply):
                fhir_id, status = parse_reply(r, "MedicationRequest")
                xwalk.append({"resource_type": "MedicationRequest", "legacy_id": key, "fhir_id": fhir_id,
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
    found = search_all("MedicationRequest")
    actual = []
    for r in found:
        actual += long_rows("MedicationRequest", identifier(r, SYS_MEDICATION), flatten_medication(r),
                            "actual_value", r.get("id"))
    expected_ids = {x["fhir_id"] for x in xwalk if x["fhir_id"]}
    on_server = {r.get("id") for r in found}
    missing = expected_ids - on_server
    print(f"   MedicationRequest  expected {len(expected_ids)}, on server {len(on_server)}, "
          f"missing {len(missing)}   {'PASS' if not missing else 'FAIL'}")
    save_table("tgt.medication_resources", actual, LONG_ACTUAL)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Phase 2: migrate prescriptions to FHIR MedicationRequest")
    parser.add_argument("--limit", type=int, help="only send the first N bundles (try 1 first)")
    args = parser.parse_args()

    stage()
    print("\n2. CURATE   staff corrections from remediation/corrections.csv")
    if not apply_corrections.main(show_next=False):
        raise SystemExit("Stopped: fix remediation/corrections.csv, then run this script again.")
    rows = read_table("SELECT * FROM cur.medications ORDER BY source_row;")
    encounters = {}
    for e in read_table("SELECT Id, PATIENT, PROVIDER, source_row FROM cur.encounters ORDER BY source_row;"):
        encounters.setdefault(e["Id"], e)
    enc_xwalk = {r["legacy_id"]: r["fhir_id"] for r in read_table(
        "SELECT legacy_id, fhir_id FROM etl.fhir_crosswalk WHERE resource_type = 'Encounter' AND fhir_id IS NOT NULL;")}
    patient_xwalk = {r["legacy_id"]: r["fhir_id"] for r in read_table(
        "SELECT legacy_id, fhir_id FROM etl.fhir_crosswalk WHERE resource_type = 'Patient' AND fhir_id IS NOT NULL;")}
    prac_xwalk = {r["legacy_id"]: r["fhir_id"] for r in read_table(
        "SELECT legacy_id, fhir_id FROM etl.practitioner_crosswalk WHERE fhir_id IS NOT NULL;")}
    print(f"   cur.medications {len(rows)} rows; migrated visits {len(enc_xwalk)}, "
          f"patients {len(patient_xwalk)}, practitioners {len(prac_xwalk)}")

    expected = []
    requests, issues, dispositions, sources = transform(rows, encounters, enc_xwalk, patient_xwalk,
                                                        prac_xwalk, expected)
    save_table("etl.medication_issue_log", issues, ISSUE_COLS)
    save_table("etl.medication_disposition", dispositions, DISPOSITION_COLS)
    save_table("etl.medication_sources", sources, SOURCE_COLS)
    save_table("etl.medication_expected", expected, LONG_EXPECTED)
    write_csv(OUTPUT_DIR / "issue_log.csv", issues, ISSUE_COLS)

    load_log, xwalk = [], []
    load(requests, load_log, xwalk, args.limit)
    save_table("etl.medication_crosswalk", xwalk, XWALK_COLS)
    save_table("etl.medication_load_log", load_log,
               ["bundle_no", "label", "entries", "http_status", "outcome", "attempts", "seconds", "error"])
    write_csv(OUTPUT_DIR / "crosswalk.csv", xwalk, XWALK_COLS)

    extract(xwalk)
    print("\nNext: open sql\\26_medication_reconciliation.sql in SSMS and press F5.")


if __name__ == "__main__":
    main()
