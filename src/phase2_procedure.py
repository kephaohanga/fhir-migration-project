"""
phase2_procedure.py
-------------------
Phase 2, step 5: migrate legacy procedures to FHIR Procedure.

Each procedure points to its patient (subject), its visit (encounter) and the
visit's practitioner (performer), so it plugs into everything migrated so far.

Mapping:
  CODE, DESCRIPTION, SYSTEM    -> code          (SNOMED CT: http://snomed.info/sct)
  PATIENT                      -> subject       Patient (must be the visit's patient)
  ENCOUNTER                    -> encounter     Encounter (the visit must be migrated)
  START, STOP                  -> performedPeriod
  REASONCODE, REASONDESCRIPTION-> reasonCode    (SNOMED CT)
  visit's PROVIDER (derived)   -> performer     Practitioner
  BASE_COST                    -> not migrated: cost is billing data, not the clinical
                                  record (open balances stay in legacy - A/R runout)

Stages:
  1. STAGE      data/legacy_dirty/procedures.csv -> stg.procedures (raw text, never edited)
  2. CURATE     cur.* = stg + staff corrections (runs src\\apply_corrections.py)
  3. TRANSFORM  rules below; every legacy row gets one disposition
  4. LOAD       transaction bundles of 50, conditional update (safe to re-run)
  5. EXTRACT    read every Procedure back by project tag

Rules:
  R-PC01 Reject            ENCOUNTER is not in the legacy encounter file (orphan)
  R-PC01 Reject (cascade)  the visit exists but was not migrated (archived or rejected):
                           the procedure stays with its visit in the legacy archive
  R-PC02 Reject            PATIENT is not a migrated patient
  R-PC03 Reject            PATIENT is not the visit's patient
  R-PC04 Reject            CODE blank, or SYSTEM is not SNOMED-CT
  R-PC05 Reject            START blank/invalid, or STOP before START
  R-PC06 Fix               duplicate procedure row removed (first copy kept)
  R-PC07 Flag              STOP blank - loaded as a single performed date-time
  R-PC08 Flag              the visit has no migrated practitioner - performer omitted

Run AFTER Phase 1 and src\\phase2_encounter_links.py. From the project folder:
  python src\\phase2_procedure.py --limit 1   <- ONE bundle of 50 (a test)
  python src\\phase2_procedure.py             <- everything (about 6-8 minutes)
Then open sql\\24_procedure_reconciliation.sql in SSMS and press F5.
"""

import argparse
import contextlib
import csv
import io
import re
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import pandas as pd

import apply_corrections
from phase2_clinic_structure import identifier, parse_reply, put_entry, search_all, send_bundle
from phase2_practitioner import (BUNDLE_SIZE, FHIR_SERVER, PAUSE_BETWEEN_BUNDLES, PROJECT_TAG,
                                 connect, save_table, v, write_csv)

SOURCE_FILE = Path("data/legacy_dirty/procedures.csv")
ANSWER_KEY = Path("docs/planted_procedure_errors_answer_key.csv")
OUTPUT_DIR = Path("output/phase2_procedure")

SYS_PROCEDURE = "urn:fhirmigration:procedure-id"
SYS_SNOMED = "http://snomed.info/sct"

ISSUE_COLS = ["record_key", "source_row", "rule_id", "action", "detail"]
DISPOSITION_COLS = ["record_key", "source_row", "disposition"]
SOURCE_COLS = ["procedure_key", "record_key", "legacy_patient", "legacy_encounter"]
LONG_EXPECTED = ["resource_type", "legacy_id", "field_name", "expected_value"]
LONG_ACTUAL = ["resource_type", "fhir_id", "legacy_id", "field_name", "actual_value"]
XWALK_COLS = ["resource_type", "legacy_id", "fhir_id", "server_status"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def legacy_key(r):
    """Same business key as apply_corrections.py: PATIENT_ENCOUNTER_CODE_START."""
    return "_".join(str(r.get(c) or "") for c in ("PATIENT", "ENCOUNTER", "CODE", "START"))


def parse_dt(text):
    if text is None:
        return None
    try:
        return datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def long_rows(resource_type, legacy_id, fields, value_col, fhir_id=None):
    return [{"resource_type": resource_type, "fhir_id": fhir_id, "legacy_id": legacy_id,
             "field_name": k, value_col: val} for k, val in fields.items()]


def read_table(sql):
    conn = connect()
    cur = conn.cursor()
    cur.execute(sql)
    cols = [c[0] for c in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    conn.close()
    return rows


def flatten_procedure(r):
    coding = next((c for c in (r.get("code") or {}).get("coding", []) if c.get("system") == SYS_SNOMED), {})
    reason = next((c.get("code") for rc in r.get("reasonCode", []) for c in rc.get("coding", [])
                   if c.get("system") == SYS_SNOMED), None)
    period = r.get("performedPeriod") or {}
    return {
        "status": r.get("status"),
        "code": coding.get("code"),
        "code_display": coding.get("display"),
        "subject_ref": (r.get("subject") or {}).get("reference"),
        "encounter_ref": (r.get("encounter") or {}).get("reference"),
        "performed_start": period.get("start") or r.get("performedDateTime"),
        "performed_end": period.get("end"),
        "performer_ref": ((r.get("performer") or [{}])[0].get("actor") or {}).get("reference"),
        "reason_code": reason,
    }


# ---------------------------------------------------------------------------
# 1. STAGE
# ---------------------------------------------------------------------------
def stage():
    if not SOURCE_FILE.exists():
        raise SystemExit(f"{SOURCE_FILE} not found. Run  python src\\plant_procedure_errors.py  first.")
    df = pd.read_csv(SOURCE_FILE, dtype=str, keep_default_na=False)
    columns = list(df.columns)
    data = [[None if x == "" else x for x in values] + [n + 2]
            for n, values in enumerate(df.itertuples(index=False, name=None))]
    conn = connect()
    cur = conn.cursor()
    cur.execute("IF SCHEMA_ID('stg') IS NULL EXEC('CREATE SCHEMA stg');")
    cur.execute("DROP TABLE IF EXISTS stg.procedures;")
    col_defs = ", ".join(f"[{c}] NVARCHAR(500) NULL" for c in columns)
    cur.execute(f"CREATE TABLE stg.procedures ({col_defs}, [source_row] INT NOT NULL, "
                f"[loaded_at] DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME());")
    cur.fast_executemany = True
    cur.executemany(
        f"INSERT INTO stg.procedures ({', '.join(f'[{c}]' for c in columns)}, [source_row]) "
        f"VALUES ({', '.join('?' for _ in range(len(columns) + 1))});", data)
    sql_rows = cur.execute("SELECT COUNT(*) FROM stg.procedures;").fetchone()[0]
    conn.close()
    print("1. STAGE")
    print(f"   CSV rows {len(df)}   stg.procedures rows {sql_rows}   {'PASS' if sql_rows == len(df) else 'FAIL'}")


# ---------------------------------------------------------------------------
# 3. TRANSFORM
# ---------------------------------------------------------------------------
def transform(rows, encounters, enc_xwalk, patient_xwalk, prac_xwalk, expected):
    issues, dispositions, procedures, sources = [], [], [], []
    seen = set()

    def log(key, src, rule, action, detail):
        issues.append({"record_key": key, "source_row": src, "rule_id": rule, "action": action, "detail": detail})

    def dispose(key, src, disposition):
        dispositions.append({"record_key": key, "source_row": src, "disposition": disposition})

    for r in rows:
        key, src = legacy_key(r), r["source_row"]
        enc_id, patient = v(r, "ENCOUNTER"), v(r, "PATIENT")

        if key in seen:                                                               # R-PC06
            log(key, src, "R-PC06", "Fix", "Duplicate procedure row removed (first copy kept)")
            dispose(key, src, "Duplicate removed")
            continue
        seen.add(key)

        visit = encounters.get(enc_id)
        if visit is None:                                                             # R-PC01
            log(key, src, "R-PC01", "Reject", f"ENCOUNTER {enc_id} is not in the legacy encounter file (orphan)")
            dispose(key, src, "Rejected")
            continue
        if enc_id not in enc_xwalk:                                                   # R-PC01 cascade
            log(key, src, "R-PC01", "Reject (cascade)",
                f"Visit {enc_id} was not migrated - procedure stays with its visit in the legacy archive")
            dispose(key, src, "Rejected (cascade)")
            continue
        if patient not in patient_xwalk:                                              # R-PC02
            log(key, src, "R-PC02", "Reject", f"PATIENT {patient} is not a migrated patient")
            dispose(key, src, "Rejected")
            continue
        if patient != visit["PATIENT"]:                                               # R-PC03
            log(key, src, "R-PC03", "Reject",
                f"PATIENT {patient} is not the visit's patient ({visit['PATIENT']})")
            dispose(key, src, "Rejected")
            continue
        if v(r, "CODE") is None or (v(r, "SYSTEM") or "").upper() != "SNOMED-CT":     # R-PC04
            log(key, src, "R-PC04", "Reject",
                f"Procedure code unusable: SYSTEM '{v(r, 'SYSTEM')}' CODE '{v(r, 'CODE')}'")
            dispose(key, src, "Rejected")
            continue
        start, stop = parse_dt(v(r, "START")), parse_dt(v(r, "STOP"))
        if start is None or (v(r, "STOP") and (stop is None or stop < start)):        # R-PC05
            log(key, src, "R-PC05", "Reject", f"Dates invalid: START {v(r, 'START')} STOP {v(r, 'STOP')}")
            dispose(key, src, "Rejected")
            continue

        procedure_key = f"{enc_id}_{v(r, 'CODE')}_{re.sub(r'[^0-9]', '', v(r, 'START'))}"
        procedure = {
            "resourceType": "Procedure",
            "meta": {"tag": [PROJECT_TAG]},
            "identifier": [{"system": SYS_PROCEDURE, "value": procedure_key}],
            "status": "completed",
            "code": {"coding": [{"system": SYS_SNOMED, "code": v(r, "CODE"), "display": v(r, "DESCRIPTION")}],
                     "text": v(r, "DESCRIPTION")},
            "subject": {"reference": f"Patient/{patient_xwalk[patient]}"},
            "encounter": {"reference": f"Encounter/{enc_xwalk[enc_id]}"},
        }
        if v(r, "STOP"):
            procedure["performedPeriod"] = {"start": v(r, "START"), "end": v(r, "STOP")}
        else:                                                                         # R-PC07
            log(key, src, "R-PC07", "Flag", "STOP is blank - loaded as a single performed date-time")
            procedure["performedDateTime"] = v(r, "START")
        practitioner = prac_xwalk.get(visit["PROVIDER"])
        if practitioner:
            procedure["performer"] = [{"actor": {"reference": f"Practitioner/{practitioner}"}}]
        else:                                                                         # R-PC08
            log(key, src, "R-PC08", "Flag", "The visit has no migrated practitioner - performer omitted")
        if v(r, "REASONCODE"):
            procedure["reasonCode"] = [{"coding": [{"system": SYS_SNOMED, "code": v(r, "REASONCODE"),
                                                    "display": v(r, "REASONDESCRIPTION")}],
                                        "text": v(r, "REASONDESCRIPTION")}]

        procedures.append((procedure_key, procedure))
        sources.append({"procedure_key": procedure_key, "record_key": key,
                        "legacy_patient": patient, "legacy_encounter": enc_id})
        expected += long_rows("Procedure", procedure_key, flatten_procedure(procedure), "expected_value")
        dispose(key, src, "Migrated")

    counts = defaultdict(int)
    for d in dispositions:
        counts[d["disposition"]] += 1
    ok = len(rows) == len(dispositions)
    print("\n3. TRANSFORM   Procedure")
    print(f"   Legacy rows {len(rows)} = " + " + ".join(f"{n} {d.lower()}" for d, n in sorted(counts.items()))
          + f"   {'PASS' if ok else 'FAIL'}")
    with_reason = sum(1 for _, p in procedures if "reasonCode" in p)
    print(f"   Procedures built: {len(procedures)} ({with_reason} with a reason code)")
    for rule in sorted({i["rule_id"] + " " + i["action"] for i in issues}):
        print(f"   {rule}: {sum(1 for i in issues if i['rule_id'] + ' ' + i['action'] == rule)}")
    check_answer_key(issues)
    return procedures, issues, dispositions, sources


def check_answer_key(issues):
    if not ANSWER_KEY.exists():
        return
    with ANSWER_KEY.open(encoding="utf-8") as f:
        planted = list(csv.DictReader(f))
    worked = {r["record_key"]: r["disposition"] for r in read_table(
        "SELECT record_key, disposition FROM etl.corrections WHERE table_name = 'procedures';")}
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
# 4. LOAD
# ---------------------------------------------------------------------------
def load(procedures, load_log, xwalk, limit=None):
    chunks = [procedures[i:i + BUNDLE_SIZE] for i in range(0, len(procedures), BUNDLE_SIZE)]
    if limit:
        chunks = chunks[:limit]
    print(f"\n4. LOAD   {len(chunks)} bundle(s) of Procedure to {FHIR_SERVER}")
    sent = 0
    for n, chunk in enumerate(chunks, 1):
        entries = [put_entry(p, f"Procedure?identifier={SYS_PROCEDURE}|{key}") for key, p in chunk]
        sent += len(entries)
        if n % 20 == 1 or n == len(chunks):
            print(f"   ... bundle {n} of {len(chunks)}")
        reply = _send_quiet(entries, n, load_log)
        if reply is not None:
            for (key, _), r in zip(chunk, reply):
                fhir_id, status = parse_reply(r, "Procedure")
                xwalk.append({"resource_type": "Procedure", "legacy_id": key, "fhir_id": fhir_id,
                              "server_status": status})
        time.sleep(PAUSE_BETWEEN_BUNDLES)
    confirmed = sum(1 for x in xwalk if x["fhir_id"])
    created = sum(1 for x in xwalk if x["server_status"].startswith("201"))
    updated = sum(1 for x in xwalk if x["server_status"].startswith("200"))
    failed = sum(1 for l in load_log if l["outcome"] == "FAILED")
    print(f"   Sent {sent}, confirmed {confirmed} ({created} created, {updated} updated), "
          f"failed bundles {failed}   {'PASS' if confirmed == sent else 'FAIL'}")


def _send_quiet(entries, n, load_log):
    """send_bundle, but print only failures (there are about 170 bundles)."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        reply = send_bundle(entries, n, "procedures", load_log)
    if reply is None:
        print(buffer.getvalue().rstrip())
    return reply


# ---------------------------------------------------------------------------
# 5. EXTRACT
# ---------------------------------------------------------------------------
def extract(xwalk):
    print("\n5. EXTRACT")
    found = search_all("Procedure")
    actual = []
    for r in found:
        actual += long_rows("Procedure", identifier(r, SYS_PROCEDURE), flatten_procedure(r),
                            "actual_value", r.get("id"))
    expected_ids = {x["fhir_id"] for x in xwalk if x["fhir_id"]}
    on_server = {r.get("id") for r in found}
    missing = expected_ids - on_server
    print(f"   Procedure  expected {len(expected_ids)}, on server {len(on_server)}, missing {len(missing)}   "
          f"{'PASS' if not missing else 'FAIL'}")
    save_table("tgt.procedure_resources", actual, LONG_ACTUAL)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Phase 2: migrate procedures to FHIR Procedure")
    parser.add_argument("--limit", type=int, help="only send the first N bundles (try 1 first)")
    args = parser.parse_args()

    stage()
    print("\n2. CURATE   staff corrections from remediation/corrections.csv")
    if not apply_corrections.main(show_next=False):
        raise SystemExit("Stopped: fix remediation/corrections.csv, then run this script again.")
    rows = read_table("SELECT * FROM cur.procedures ORDER BY source_row;")
    encounters = {}
    for e in read_table("SELECT Id, PATIENT, PROVIDER, source_row FROM cur.encounters ORDER BY source_row;"):
        encounters.setdefault(e["Id"], e)
    enc_xwalk = {r["legacy_id"]: r["fhir_id"] for r in read_table(
        "SELECT legacy_id, fhir_id FROM etl.fhir_crosswalk WHERE resource_type = 'Encounter' AND fhir_id IS NOT NULL;")}
    patient_xwalk = {r["legacy_id"]: r["fhir_id"] for r in read_table(
        "SELECT legacy_id, fhir_id FROM etl.fhir_crosswalk WHERE resource_type = 'Patient' AND fhir_id IS NOT NULL;")}
    prac_xwalk = {r["legacy_id"]: r["fhir_id"] for r in read_table(
        "SELECT legacy_id, fhir_id FROM etl.practitioner_crosswalk WHERE fhir_id IS NOT NULL;")}
    print(f"   cur.procedures {len(rows)} rows; migrated visits {len(enc_xwalk)}, "
          f"patients {len(patient_xwalk)}, practitioners {len(prac_xwalk)}")

    expected = []
    procedures, issues, dispositions, sources = transform(rows, encounters, enc_xwalk, patient_xwalk,
                                                          prac_xwalk, expected)
    save_table("etl.procedure_issue_log", issues, ISSUE_COLS)
    save_table("etl.procedure_disposition", dispositions, DISPOSITION_COLS)
    save_table("etl.procedure_sources", sources, SOURCE_COLS)
    save_table("etl.procedure_expected", expected, LONG_EXPECTED)
    write_csv(OUTPUT_DIR / "issue_log.csv", issues, ISSUE_COLS)

    load_log, xwalk = [], []
    load(procedures, load_log, xwalk, args.limit)
    save_table("etl.procedure_crosswalk", xwalk, XWALK_COLS)
    save_table("etl.procedure_load_log", load_log,
               ["bundle_no", "label", "entries", "http_status", "outcome", "attempts", "seconds", "error"])
    write_csv(OUTPUT_DIR / "crosswalk.csv", xwalk, XWALK_COLS)

    extract(xwalk)
    print("\nNext: open sql\\24_procedure_reconciliation.sql in SSMS and press F5.")


if __name__ == "__main__":
    main()
