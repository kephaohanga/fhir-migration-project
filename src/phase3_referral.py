r"""
phase3_referral.py
------------------
Phase 3, step 1: migrate legacy referrals to FHIR ServiceRequest.

A ServiceRequest is FHIR's "please do this for the patient" order. A referral is one
kind: the clinic's provider asks an outside specialist to see the patient.

Decision (legacy status kept): FHIR has no "scheduled" status, so OPEN and SCHEDULED
both become "active". The original legacy value is ALSO stored in an extension, so the
difference is not lost and the reconciliation can check it.

Known limit of the legacy data: SCHEDULED referrals have no appointment in the clinic's
appointment file, because the outside specialist books the visit in their own system.

Mapping:
  REFERRAL_ID              -> identifier                  legacy referral ID
  STATUS                   -> status                      OPEN/SCHEDULED -> active, COMPLETED -> completed
  STATUS                   -> extension legacy-status     the original value (OPEN, SCHEDULED, COMPLETED)
  (always)                 -> intent                      order
  (always)                 -> category                    SNOMED CT 3457005 Patient referral
  SPECIALTY                -> code.text                   "Referral to <specialty>"
  SPECIALTY_CODE, SPECIALTY -> performerType              NUCC provider taxonomy
  PRIORITY                 -> priority                    ROUTINE -> routine, URGENT -> urgent
  PATIENT                  -> subject                     Patient (must be migrated)
  REQUESTER                -> requester                   Practitioner (must be migrated)
  REASON_CODE, ...DESCRIPTION -> reasonCode               SNOMED CT (must be one of the patient's conditions)
  AUTHORED_DATE            -> authoredOn                  date the referral was written

Rules:
  R-RF01 Reject  PATIENT is not a migrated patient
  R-RF02 Flag    REQUESTER is not a migrated practitioner - requester omitted, sent to the worklist
  R-RF03 Flag    referral without a valid diagnosis: REASON_CODE blank, or not one of the patient's
                 conditions - reason omitted (an unproven diagnosis is not stated), sent to the worklist
  R-RF04 Fix     STATUS or PRIORITY written with other capitals or spaces ("Open ") - tidied
  R-RF05 Reject  STATUS or PRIORITY is not a known legacy value
  R-RF06 Fix     AUTHORED_DATE written as m/d/yyyy - converted to YYYY-MM-DD
  R-RF06 Reject  AUTHORED_DATE blank or not a date
  R-RF07 Fix     exact duplicate row (EVERY column identical) removed, first copy kept

Run AFTER Phase 1 and Phase 2, and python src\plant_referral_errors.py. From the project folder:
  python src\phase3_referral.py --limit 1   <- ONE bundle of 50 (a test; here that is everything)
  python src\phase3_referral.py
Then open sql\28_referral_reconciliation.sql in SSMS and press F5.
"""

import argparse
import csv
import re
import time
from collections import defaultdict
from datetime import date
from pathlib import Path

import pandas as pd

import apply_corrections
from phase2_clinic_structure import SYS_NUCC, identifier, parse_reply, put_entry, search_all
from phase2_practitioner import (BUNDLE_SIZE, FHIR_SERVER, PAUSE_BETWEEN_BUNDLES, PROJECT_TAG,
                                 connect, save_table, v, write_csv)
from phase2_procedure import _send_quiet, long_rows, read_table

SOURCE_FILE = Path("data/legacy_dirty/referrals.csv")
ANSWER_KEY = Path("docs/planted_referral_errors_answer_key.csv")
OUTPUT_DIR = Path("output/phase3_referral")

SYS_REFERRAL = "urn:fhirmigration:legacy-referral-id"
EXT_LEGACY_STATUS = "urn:fhirmigration:extension:legacy-referral-status"
SYS_SNOMED = "http://snomed.info/sct"
REFERRAL_CATEGORY = {"system": SYS_SNOMED, "code": "3457005", "display": "Patient referral"}

STATUS_MAP = {"OPEN": "active", "SCHEDULED": "active", "COMPLETED": "completed"}
PRIORITY_MAP = {"ROUTINE": "routine", "URGENT": "urgent"}

ISSUE_COLS = ["record_key", "source_row", "rule_id", "action", "detail"]
DISPOSITION_COLS = ["record_key", "source_row", "disposition"]
SOURCE_COLS = ["referral_key", "record_key", "source_row", "legacy_patient", "legacy_requester"]
AUDIT_COLS = {"source_row", "loaded_at"}          # not part of the legacy data itself
LONG_EXPECTED = ["resource_type", "legacy_id", "field_name", "expected_value"]
LONG_ACTUAL = ["resource_type", "fhir_id", "legacy_id", "field_name", "actual_value"]
XWALK_COLS = ["resource_type", "legacy_id", "fhir_id", "server_status"]


def flatten_referral(r):
    """One FHIR ServiceRequest -> the flat fields we compare (same for sent and received)."""
    category = next((c for cat in r.get("category", []) for c in cat.get("coding", [])
                     if c.get("system") == SYS_SNOMED), {})
    performer = next((c for c in (r.get("performerType") or {}).get("coding", [])
                      if c.get("system") == SYS_NUCC), {})      # one CodeableConcept in FHIR R4
    reason = next((c for rc in r.get("reasonCode", []) for c in rc.get("coding", [])
                   if c.get("system") == SYS_SNOMED), {})
    legacy_status = next((e.get("valueString") for e in r.get("extension", [])
                          if e.get("url") == EXT_LEGACY_STATUS), None)
    return {
        "status": r.get("status"),
        "legacy_status": legacy_status,
        "intent": r.get("intent"),
        "category_code": category.get("code"),
        "code_text": (r.get("code") or {}).get("text"),
        "performer_type_code": performer.get("code"),
        "performer_type_display": performer.get("display"),
        "priority": r.get("priority"),
        "subject_ref": (r.get("subject") or {}).get("reference"),
        "requester_ref": (r.get("requester") or {}).get("reference"),
        "reason_code": reason.get("code"),
        "reason_display": reason.get("display"),
        "authored_on": r.get("authoredOn"),
    }


def parse_date(text):
    """Returns (YYYY-MM-DD or None, True if it had to be converted from m/d/yyyy)."""
    text = (text or "").strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return date.fromisoformat(text).isoformat(), False
        m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)
        if m:
            return date(int(m.group(3)), int(m.group(1)), int(m.group(2))).isoformat(), True
    except ValueError:
        pass
    return None, False


# ---------------------------------------------------------------------------
# 1. STAGE
# ---------------------------------------------------------------------------
def stage():
    if not SOURCE_FILE.exists():
        raise SystemExit(f"{SOURCE_FILE} not found. Run  python src\\plant_referral_errors.py  first.")
    df = pd.read_csv(SOURCE_FILE, dtype=str, keep_default_na=False)
    columns = list(df.columns)
    data = [[None if x == "" else x for x in values] + [n + 2]
            for n, values in enumerate(df.itertuples(index=False, name=None))]
    conn = connect()
    cur = conn.cursor()
    cur.execute("IF SCHEMA_ID('stg') IS NULL EXEC('CREATE SCHEMA stg');")
    cur.execute("DROP TABLE IF EXISTS stg.referrals;")
    col_defs = ", ".join(f"[{c}] NVARCHAR(500) NULL" for c in columns)
    cur.execute(f"CREATE TABLE stg.referrals ({col_defs}, [source_row] INT NOT NULL, "
                f"[loaded_at] DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME());")
    cur.fast_executemany = True
    cur.executemany(
        f"INSERT INTO stg.referrals ({', '.join(f'[{c}]' for c in columns)}, [source_row]) "
        f"VALUES ({', '.join('?' for _ in range(len(columns) + 1))});", data)
    sql_rows = cur.execute("SELECT COUNT(*) FROM stg.referrals;").fetchone()[0]
    conn.close()
    print("1. STAGE")
    print(f"   CSV rows {len(df)}   stg.referrals rows {sql_rows}   {'PASS' if sql_rows == len(df) else 'FAIL'}")


# ---------------------------------------------------------------------------
# 3. TRANSFORM
# ---------------------------------------------------------------------------
def transform(rows, patient_xwalk, prac_xwalk, patient_conditions, expected):
    issues, dispositions, referrals, sources = [], [], [], []
    seen_rows = set()          # full-row signatures: for EXACT duplicates

    def log(key, src, rule, action, detail):
        issues.append({"record_key": key, "source_row": src, "rule_id": rule, "action": action, "detail": detail})

    def dispose(key, src, disposition):
        dispositions.append({"record_key": key, "source_row": src, "disposition": disposition})

    def reject(key, src, rule, detail):
        log(key, src, rule, "Reject", detail)
        dispose(key, src, "Rejected")

    def coded(key, src, column, raw, allowed):
        """Tidy a coded value. Returns the legacy value (e.g. OPEN), or None if unknown."""
        raw = raw or ""
        tidy = raw.strip().upper()
        if tidy not in allowed:
            return None
        if tidy != raw:                                                               # R-RF04
            log(key, src, "R-RF04", "Fix", f"{column} '{raw}' tidied to '{tidy}'")
        return tidy

    for r in rows:
        key, src = v(r, "REFERRAL_ID"), r["source_row"]
        patient, requester = v(r, "PATIENT"), v(r, "REQUESTER")

        signature = tuple((c, r[c]) for c in sorted(r) if c not in AUDIT_COLS)
        if signature in seen_rows:                                                    # R-RF07
            log(key, src, "R-RF07", "Fix", "Exact duplicate row (every column identical) removed - first copy kept")
            dispose(key, src, "Duplicate removed")
            continue
        seen_rows.add(signature)

        if patient not in patient_xwalk:                                              # R-RF01
            reject(key, src, "R-RF01", f"PATIENT {patient} is not a migrated patient")
            continue
        status = coded(key, src, "STATUS", r.get("STATUS"), STATUS_MAP)
        priority = coded(key, src, "PRIORITY", r.get("PRIORITY"), PRIORITY_MAP)
        if status is None or priority is None:                                        # R-RF05
            bad = "STATUS" if status is None else "PRIORITY"
            reject(key, src, "R-RF05", f"{bad} '{r.get(bad)}' is not a known legacy value "
                                       f"({', '.join(STATUS_MAP if bad == 'STATUS' else PRIORITY_MAP)})")
            continue
        authored, converted = parse_date(r.get("AUTHORED_DATE"))
        if authored is None:                                                          # R-RF06 Reject
            reject(key, src, "R-RF06", f"AUTHORED_DATE '{r.get('AUTHORED_DATE')}' is blank or not a date")
            continue
        if converted:                                                                 # R-RF06 Fix
            log(key, src, "R-RF06", "Fix", f"AUTHORED_DATE '{r.get('AUTHORED_DATE')}' converted to {authored}")

        specialty = v(r, "SPECIALTY")
        referral = {
            "resourceType": "ServiceRequest",
            "meta": {"tag": [PROJECT_TAG]},
            "extension": [{"url": EXT_LEGACY_STATUS, "valueString": status}],
            "identifier": [{"system": SYS_REFERRAL, "value": key}],
            "status": STATUS_MAP[status],
            "intent": "order",
            "category": [{"coding": [REFERRAL_CATEGORY], "text": "Patient referral"}],
            "code": {"text": f"Referral to {specialty}"},
            "priority": PRIORITY_MAP[priority],
            "subject": {"reference": f"Patient/{patient_xwalk[patient]}"},
            "authoredOn": authored,
            "performerType": {"coding": [{"system": SYS_NUCC, "code": v(r, "SPECIALTY_CODE"),
                                          "display": specialty}],
                              "text": specialty},
        }
        if requester in prac_xwalk:
            referral["requester"] = {"reference": f"Practitioner/{prac_xwalk[requester]}"}
        else:                                                                         # R-RF02
            log(key, src, "R-RF02", "Flag",
                f"REQUESTER {requester} is not a migrated practitioner - requester omitted")
        reason = v(r, "REASON_CODE")
        if reason and (patient, reason) in patient_conditions:
            referral["reasonCode"] = [{"coding": [{"system": SYS_SNOMED, "code": reason,
                                                   "display": v(r, "REASON_DESCRIPTION")}],
                                       "text": v(r, "REASON_DESCRIPTION")}]
        else:                                                                         # R-RF03
            log(key, src, "R-RF03", "Flag",
                "Referral without a diagnosis - REASON_CODE is blank" if not reason else
                f"Referral without a valid diagnosis - REASON_CODE {reason} "
                f"({v(r, 'REASON_DESCRIPTION')}) is not one of the patient's conditions - reason omitted")

        referrals.append((key, referral))
        sources.append({"referral_key": key, "record_key": key, "source_row": src,
                        "legacy_patient": patient, "legacy_requester": requester})
        expected += long_rows("ServiceRequest", key, flatten_referral(referral), "expected_value")
        dispose(key, src, "Migrated")

    counts = defaultdict(int)
    for d in dispositions:
        counts[d["disposition"]] += 1
    ok = len(rows) == len(dispositions)
    print("\n3. TRANSFORM   ServiceRequest (referrals)")
    print(f"   Legacy rows {len(rows)} = " + " + ".join(f"{n} {d.lower()}" for d, n in sorted(counts.items()))
          + f"   {'PASS' if ok else 'FAIL'}")
    by_status = defaultdict(int)
    for _, s in referrals:
        by_status[s["extension"][0]["valueString"]] += 1
    print("   Referrals built: " + ", ".join(f"{n} {s}" for s, n in sorted(by_status.items()))
          + f"; {sum(1 for _, s in referrals if 'reasonCode' in s)} with a diagnosis")
    for rule in sorted({i["rule_id"] + " " + i["action"] for i in issues}):
        print(f"   {rule}: {sum(1 for i in issues if i['rule_id'] + ' ' + i['action'] == rule)}")
    check_answer_key(issues)
    return referrals, issues, dispositions, sources


def check_answer_key(issues):
    if not ANSWER_KEY.exists():
        return
    with ANSWER_KEY.open(encoding="utf-8") as f:
        planted = list(csv.DictReader(f))
    worked = {r["record_key"]: r["disposition"] for r in read_table(
        "SELECT record_key, disposition FROM etl.corrections WHERE table_name = 'referrals';")}
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
def load(referrals, load_log, xwalk, limit=None):
    chunks = [referrals[i:i + BUNDLE_SIZE] for i in range(0, len(referrals), BUNDLE_SIZE)]
    if limit:
        chunks = chunks[:limit]
    print(f"\n4. LOAD   {len(chunks)} bundle(s) of ServiceRequest to {FHIR_SERVER}")
    sent = 0
    for n, chunk in enumerate(chunks, 1):
        entries = [put_entry(s, f"ServiceRequest?identifier={SYS_REFERRAL}|{key}") for key, s in chunk]
        sent += len(entries)
        reply = _send_quiet(entries, n, load_log)
        if reply is not None:
            for (key, _), r in zip(chunk, reply):
                fhir_id, status = parse_reply(r, "ServiceRequest")
                xwalk.append({"resource_type": "ServiceRequest", "legacy_id": key, "fhir_id": fhir_id,
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
    found = search_all("ServiceRequest")
    actual = []
    for r in found:
        actual += long_rows("ServiceRequest", identifier(r, SYS_REFERRAL), flatten_referral(r),
                            "actual_value", r.get("id"))
    expected_ids = {x["fhir_id"] for x in xwalk if x["fhir_id"]}
    on_server = {r.get("id") for r in found}
    missing = expected_ids - on_server
    print(f"   ServiceRequest  expected {len(expected_ids)}, on server {len(on_server)}, "
          f"missing {len(missing)}   {'PASS' if not missing else 'FAIL'}")
    save_table("tgt.referral_resources", actual, LONG_ACTUAL)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Phase 3: migrate referrals to FHIR ServiceRequest")
    parser.add_argument("--limit", type=int, help="only send the first N bundles (try 1 first)")
    args = parser.parse_args()

    stage()
    print("\n2. CURATE   staff corrections from remediation/corrections.csv")
    if not apply_corrections.main(show_next=False):
        raise SystemExit("Stopped: fix remediation/corrections.csv, then run this script again.")
    rows = read_table("SELECT * FROM cur.referrals ORDER BY source_row;")
    patient_xwalk = {r["legacy_id"]: r["fhir_id"] for r in read_table(
        "SELECT legacy_id, fhir_id FROM etl.fhir_crosswalk WHERE resource_type = 'Patient' AND fhir_id IS NOT NULL;")}
    prac_xwalk = {r["legacy_id"]: r["fhir_id"] for r in read_table(
        "SELECT legacy_id, fhir_id FROM etl.practitioner_crosswalk WHERE fhir_id IS NOT NULL;")}
    patient_conditions = {(r["PATIENT"], r["CODE"]) for r in read_table(
        "SELECT DISTINCT PATIENT, CODE FROM cur.conditions;")}
    print(f"   cur.referrals {len(rows)} rows; migrated patients {len(patient_xwalk)}, "
          f"practitioners {len(prac_xwalk)}")

    expected = []
    referrals, issues, dispositions, sources = transform(rows, patient_xwalk, prac_xwalk,
                                                         patient_conditions, expected)
    save_table("etl.referral_issue_log", issues, ISSUE_COLS)
    save_table("etl.referral_disposition", dispositions, DISPOSITION_COLS)
    save_table("etl.referral_sources", sources, SOURCE_COLS)
    save_table("etl.referral_expected", expected, LONG_EXPECTED)
    write_csv(OUTPUT_DIR / "issue_log.csv", issues, ISSUE_COLS)

    load_log, xwalk = [], []
    load(referrals, load_log, xwalk, args.limit)
    save_table("etl.referral_crosswalk", xwalk, XWALK_COLS)
    save_table("etl.referral_load_log", load_log,
               ["bundle_no", "label", "entries", "http_status", "outcome", "attempts", "seconds", "error"])
    write_csv(OUTPUT_DIR / "crosswalk.csv", xwalk, XWALK_COLS)

    extract(xwalk)
    print("\nNext: open sql\\28_referral_reconciliation.sql in SSMS and press F5.")


if __name__ == "__main__":
    main()
