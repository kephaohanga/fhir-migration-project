"""
phase2_coverage.py
------------------
Phase 2, step 4: migrate patients' CURRENT insurance to FHIR Coverage.

  Organization (Payer)  one per insurance company in payers.csv (NO_INSURANCE excluded)
  Coverage              the policy in force on the extract date, per migrated patient:
                        order 1 = primary payer, order 2 = secondary payer (if any)

Design decision (current coverage only): the legacy file holds every policy a
patient ever had. Only the policy in force on the extract date is migrated.
Ended policies stay in the legacy system and are COUNTED, not lost.
Extract date = the latest START_DATE in the file. Synthea creates each yearly
policy in advance, so END_DATE is often in the future and cannot be used; the
most recent policy start is the last activity recorded before the extract.
The policy's END_DATE (its renewal date) is migrated as Coverage.period.end.

Sources:
  data/legacy_dirty/payer_transitions.csv  the legacy export (run src\\plant_coverage_errors.py first)
  data/legacy/payers.csv                   the payer list (reference data)
  docs/payer_type_map.csv                  payer name -> Source of Payment Typology code

Stages:
  1. STAGE      both CSVs -> stg.payer_transitions, stg.payers (raw text, never edited)
  2. CURATE     cur.* = stg + staff corrections (runs src\\apply_corrections.py)
  3. PAYERS     load payer Organizations (conditional update)
  4. TRANSFORM  give every legacy row exactly one disposition; build Coverage
  5. LOAD       Coverage in bundles of 50 (conditional update)
  6. EXTRACT    read payers and Coverage back by project tag

Dispositions (every legacy row gets exactly one):
  Ended - kept in legacy          policy ended before the extract date
  Current - self-pay              current payer is NO_INSURANCE: no Coverage is created
  Current - migrated              current insured policy -> Coverage
  Superseded                      older of two overlapping current rows (R-CV04)
  Rejected                        failed a rule below

Rules (applied to the rows being migrated):
  R-CV01 Reject  PATIENT is not a migrated patient
  R-CV02 Reject  PAYER is not in payers.csv
  R-CV03 Reject  START_DATE blank/invalid, or END_DATE before START_DATE
  R-CV04 Fix     more than one current row for a patient: latest start kept, others superseded
  R-CV05 Flag    MEMBERID blank - subscriberId omitted (a claim would need it)
  R-CV06 Flag    PLAN_OWNERSHIP blank or not recognized - relationship omitted
  R-CV07 Flag    SECONDARY_PAYER not in payers.csv - secondary coverage omitted
  R-CV08 Flag    payer name not in docs/payer_type_map.csv - Coverage.type omitted

Not migrated, by design: OWNER_NAME (another person's name; no RelatedPerson yet -
data minimization), and payer statistics (AMOUNT_COVERED, REVENUE, MEMBER_MONTHS ...).

Run from the project folder:
  python src\\phase2_coverage.py --limit 1   <- payers + ONE bundle of Coverage (a test)
  python src\\phase2_coverage.py             <- everything
Then open sql\\23_coverage_reconciliation.sql in SSMS and press F5.
"""

import argparse
import csv
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import pandas as pd

import apply_corrections
from phase2_clinic_structure import identifier, parse_reply, put_entry, search_all, send_bundle, tf
from phase2_practitioner import (BUNDLE_SIZE, FHIR_SERVER, PAUSE_BETWEEN_BUNDLES, PROJECT_TAG,
                                 connect, save_table, v, write_csv)
import time

TRANSITIONS_FILE = Path("data/legacy_dirty/payer_transitions.csv")
PAYERS_FILE = Path("data/legacy/payers.csv")
TYPE_MAP = Path("docs/payer_type_map.csv")
ANSWER_KEY = Path("docs/planted_coverage_errors_answer_key.csv")
OUTPUT_DIR = Path("output/phase2_coverage")

SYS_PAYER = "urn:fhirmigration:payer-id"
SYS_COVERAGE = "urn:fhirmigration:coverage-id"
SYS_ORG_TYPE = "http://terminology.hl7.org/CodeSystem/organization-type"
SYS_SOPT = "https://nahdo.org/sopt"           # Source of Payment Typology (US Core payer type)
SYS_RELATIONSHIP = "http://terminology.hl7.org/CodeSystem/subscriber-relationship"

# Legacy PLAN_OWNERSHIP -> the patient's relationship to the policy holder.
# GUARDIAN: Synthea gives a guardian's plan to minors, so the patient is the holder's child.
RELATIONSHIP_MAP = {"SELF": ("self", "Self"), "SPOUSE": ("spouse", "Spouse"),
                    "GUARDIAN": ("child", "Child")}

ISSUE_COLS = ["record_key", "source_row", "rule_id", "action", "detail"]
DISPOSITION_COLS = ["record_key", "source_row", "patient_id", "disposition"]
SOURCE_COLS = ["coverage_key", "record_key", "legacy_patient", "legacy_payer", "coverage_order"]
LONG_EXPECTED = ["resource_type", "legacy_id", "field_name", "expected_value"]
LONG_ACTUAL = ["resource_type", "fhir_id", "legacy_id", "field_name", "actual_value"]
XWALK_COLS = ["resource_type", "legacy_id", "fhir_id", "server_status"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def parse_dt(text):
    """(datetime or None, ok). Blank -> (None, True); unreadable -> (None, False)."""
    if text is None:
        return None, True
    try:
        return datetime.fromisoformat(str(text).strip().replace("Z", "+00:00")), True
    except ValueError:
        return None, False


def long_rows(resource_type, legacy_id, fields, value_col, fhir_id=None):
    return [{"resource_type": resource_type, "fhir_id": fhir_id, "legacy_id": legacy_id,
             "field_name": k, value_col: val} for k, val in fields.items()]


def flatten_payer(r):
    type_code = next((c.get("code") for t in r.get("type", []) for c in t.get("coding", [])
                      if c.get("system") == SYS_ORG_TYPE), None)
    return {"identifier": identifier(r, SYS_PAYER), "active": tf(r.get("active")),
            "name": r.get("name"), "type_code": type_code}


def flatten_coverage(r):
    code_of = lambda cc, system: next((c.get("code") for c in (cc or {}).get("coding", [])
                                       if c.get("system") == system), None)
    period = r.get("period") or {}
    return {"status": r.get("status"),
            "type_code": code_of(r.get("type"), SYS_SOPT),
            "subscriber_id": r.get("subscriberId"),
            "beneficiary_ref": (r.get("beneficiary") or {}).get("reference"),
            "relationship": code_of(r.get("relationship"), SYS_RELATIONSHIP),
            "period_start": period.get("start"),
            "period_end": period.get("end"),
            "payor_ref": ((r.get("payor") or [{}])[0]).get("reference"),
            "coverage_order": None if r.get("order") is None else str(r.get("order"))}


# ---------------------------------------------------------------------------
# 1. STAGE
# ---------------------------------------------------------------------------
def stage_csv(path, table):
    if not path.exists():
        hint = "Run  python src\\plant_coverage_errors.py  first." if "dirty" in path.as_posix() \
            else "Copy it from Synthea's output\\csv folder."
        raise SystemExit(f"{path} not found. {hint}")
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    columns = list(df.columns)
    data = [[None if x == "" else x for x in values] + [n + 2]
            for n, values in enumerate(df.itertuples(index=False, name=None))]
    conn = connect()
    cur = conn.cursor()
    cur.execute("IF SCHEMA_ID('stg') IS NULL EXEC('CREATE SCHEMA stg');")
    cur.execute(f"DROP TABLE IF EXISTS stg.{table};")
    col_defs = ", ".join(f"[{c}] NVARCHAR(500) NULL" for c in columns)
    cur.execute(f"CREATE TABLE stg.{table} ({col_defs}, [source_row] INT NOT NULL, "
                f"[loaded_at] DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME());")
    if data:
        cur.fast_executemany = True
        cur.executemany(
            f"INSERT INTO stg.{table} ({', '.join(f'[{c}]' for c in columns)}, [source_row]) "
            f"VALUES ({', '.join('?' for _ in range(len(columns) + 1))});", data)
    sql_rows = cur.execute(f"SELECT COUNT(*) FROM stg.{table};").fetchone()[0]
    conn.close()
    print(f"   {path.name:<24} CSV rows {len(df):>5}   stg.{table} rows {sql_rows:>5}   "
          f"{'PASS' if sql_rows == len(df) else 'FAIL'}")


def read_table(sql):
    conn = connect()
    cur = conn.cursor()
    cur.execute(sql)
    cols = [c[0] for c in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    conn.close()
    return rows


# ---------------------------------------------------------------------------
# 3. PAYERS
# ---------------------------------------------------------------------------
def is_self_pay(p):
    return v(p, "OWNERSHIP") == "NO_INSURANCE" or v(p, "NAME") == "NO_INSURANCE"


def load_payers(payers, load_log, xwalk, expected):
    print("\n3. PAYERS   Organization (type: Payer)")
    real = [p for p in payers if not is_self_pay(p) and v(p, "Id")]
    entries = []
    for p in real:
        org = {"resourceType": "Organization", "meta": {"tag": [PROJECT_TAG]},
               "identifier": [{"system": SYS_PAYER, "value": p["Id"]}], "active": True,
               "type": [{"coding": [{"system": SYS_ORG_TYPE, "code": "pay", "display": "Payer"}]}],
               "name": v(p, "NAME")}
        entries.append(put_entry(org, f"Organization?identifier={SYS_PAYER}|{p['Id']}"))
        expected += long_rows("Organization", p["Id"], flatten_payer(org), "expected_value")
    reply = send_bundle(entries, 1, "payers", load_log)
    if reply is None:
        raise SystemExit("Payer Organizations did not load, so no Coverage was sent.")
    refs = {}
    for p, r in zip(real, reply):
        fhir_id, status = parse_reply(r, "Organization")
        refs[p["Id"]] = f"Organization/{fhir_id}"
        xwalk.append({"resource_type": "Organization", "legacy_id": p["Id"], "fhir_id": fhir_id,
                      "server_status": status})
    print(f"   {len(real)} payers loaded ({len(payers) - len(real)} NO_INSURANCE entry not migrated: not a payer)")
    return refs


# ---------------------------------------------------------------------------
# 4. TRANSFORM
# ---------------------------------------------------------------------------
def read_type_map():
    if not TYPE_MAP.exists():
        print(f"   WARNING: {TYPE_MAP} not found - Coverage.type will be omitted")
        return {}
    with TYPE_MAP.open(encoding="utf-8-sig", newline="") as f:
        return {r["payer_name"].strip().upper(): (r["sopt_code"].strip(), r["sopt_display"].strip())
                for r in csv.DictReader(f) if r.get("payer_name")}


def transform(rows, payers, patient_xwalk, payer_refs, type_map, expected):
    issues, dispositions, coverages, sources = [], [], [], []
    payer_by_id = {p["Id"]: p for p in payers if v(p, "Id")}

    def log(key, src, rule, action, detail):
        issues.append({"record_key": key, "source_row": src, "rule_id": rule, "action": action, "detail": detail})

    def dispose(key, src, patient, disposition):
        dispositions.append({"record_key": key, "source_row": src, "patient_id": patient,
                             "disposition": disposition})

    # The extract date = the latest readable START_DATE in the file
    starts = [d for d, ok in (parse_dt(v(r, "START_DATE")) for r in rows) if ok and d]
    as_of = max(starts)
    print("\n4. TRANSFORM   Coverage")
    print(f"   Extract date (latest START_DATE): {as_of}")

    current = defaultdict(list)                 # patient -> current insured rows
    for r in rows:
        key, src = f"{v(r, 'PATIENT')}_{v(r, 'START_DATE')}", r["source_row"]
        patient, payer = v(r, "PATIENT"), v(r, "PAYER")
        start, start_ok = parse_dt(v(r, "START_DATE"))
        end, end_ok = parse_dt(v(r, "END_DATE"))

        if start is None or not start_ok or not end_ok or (end and end < start):        # R-CV03
            log(key, src, "R-CV03", "Reject",
                f"Dates invalid: START_DATE {v(r, 'START_DATE')} END_DATE {v(r, 'END_DATE')}")
            dispose(key, src, patient, "Rejected")
            continue
        # Current = started at or before the extract moment and ends AFTER it. A renewal
        # ends at the exact moment the next policy starts, so only the new one counts.
        if not (start <= as_of and (end is None or end > as_of)):
            dispose(key, src, patient, "Ended - kept in legacy")
            continue
        if patient not in patient_xwalk:                                                  # R-CV01
            log(key, src, "R-CV01", "Reject", f"PATIENT {patient} is not a migrated patient")
            dispose(key, src, patient, "Rejected")
            continue
        if payer not in payer_by_id:                                                      # R-CV02
            log(key, src, "R-CV02", "Reject", f"PAYER {payer} is not in payers.csv")
            dispose(key, src, patient, "Rejected")
            continue
        if is_self_pay(payer_by_id[payer]):
            dispose(key, src, patient, "Current - self-pay")
            continue
        current[patient].append((start, key, src, r))

    for patient, candidates in current.items():
        candidates.sort(key=lambda c: c[0], reverse=True)                                 # latest start first
        start, key, src, r = candidates[0]
        end, _ = parse_dt(v(r, "END_DATE"))
        for _, old_key, old_src, _ in candidates[1:]:                                     # R-CV04
            log(old_key, old_src, "R-CV04", "Fix",
                f"Overlaps the current policy starting {v(r, 'START_DATE')} - superseded")
            dispose(old_key, old_src, patient, "Superseded")
        dispose(key, src, patient, "Current - migrated")

        member_id = v(r, "MEMBERID")
        if member_id is None:                                                             # R-CV05
            log(key, src, "R-CV05", "Flag", "MEMBERID is blank - subscriberId omitted (a claim would need it)")
        ownership = (v(r, "PLAN_OWNERSHIP") or "").upper()
        relationship = RELATIONSHIP_MAP.get(ownership)
        if relationship is None:                                                          # R-CV06
            log(key, src, "R-CV06", "Flag",
                f"PLAN_OWNERSHIP '{v(r, 'PLAN_OWNERSHIP')}' not recognized - relationship omitted")

        orders = [(1, v(r, "PAYER"))]
        secondary = v(r, "SECONDARY_PAYER")
        if secondary:
            if secondary not in payer_by_id:                                              # R-CV07
                log(key, src, "R-CV07", "Flag", f"SECONDARY_PAYER {secondary} is not in payers.csv - omitted")
            elif not is_self_pay(payer_by_id[secondary]):
                orders.append((2, secondary))

        for order, payer_id in orders:
            coverage_key = f"{patient}_{start.strftime('%Y%m%dT%H%M%S')}_{order}"
            coverage = {
                "resourceType": "Coverage",
                "meta": {"tag": [PROJECT_TAG]},
                "identifier": [{"system": SYS_COVERAGE, "value": coverage_key}],
                "status": "active",
                "beneficiary": {"reference": f"Patient/{patient_xwalk[patient]}"},
                "period": {"start": start.date().isoformat(),
                           **({"end": end.date().isoformat()} if end else {})},
                "payor": [{"reference": payer_refs[payer_id]}],
                "order": order,
            }
            mapped = type_map.get((v(payer_by_id[payer_id], "NAME") or "").upper())
            if mapped:
                coverage["type"] = {"coding": [{"system": SYS_SOPT, "code": mapped[0], "display": mapped[1]}]}
            else:                                                                         # R-CV08
                log(key, src, "R-CV08", "Flag",
                    f"Payer '{v(payer_by_id[payer_id], 'NAME')}' not in {TYPE_MAP.as_posix()} - type omitted")
            if order == 1 and member_id:
                coverage["subscriberId"] = member_id
            if relationship:
                coverage["relationship"] = {"coding": [{"system": SYS_RELATIONSHIP,
                                                        "code": relationship[0], "display": relationship[1]}]}
            coverages.append((coverage_key, coverage))
            sources.append({"coverage_key": coverage_key, "record_key": key, "legacy_patient": patient,
                            "legacy_payer": payer_id, "coverage_order": order})
            expected += long_rows("Coverage", coverage_key, flatten_coverage(coverage), "expected_value")

    counts = defaultdict(int)
    for d in dispositions:
        counts[d["disposition"]] += 1
    ok = len(rows) == len(dispositions)
    print(f"   Legacy rows {len(rows)} = " + " + ".join(f"{n} {d.lower()}" for d, n in sorted(counts.items()))
          + f"   {'PASS' if ok else 'FAIL'}")
    secondaries = sum(1 for s in sources if s["coverage_order"] == 2)
    print(f"   Coverage built: {len(coverages)} ({len(coverages) - secondaries} primary, {secondaries} secondary)")
    for rule in sorted({i["rule_id"] for i in issues}):
        hits = [i for i in issues if i["rule_id"] == rule]
        print(f"   {rule} {hits[0]['action']}: {len(hits)}")
    check_answer_key(issues)
    return coverages, issues, dispositions, sources


def staff_corrections():
    """{legacy record key: 'Correct' or 'Archive'} for insurance rows staff have worked."""
    return {r["record_key"]: r["disposition"] for r in read_table(
        "SELECT record_key, disposition FROM etl.corrections WHERE table_name = 'payer_transitions';")}


def check_answer_key(issues):
    """A defect staff have since corrected no longer appears in the issue log, so it is
    reported as caught on an earlier run and then corrected (or archived)."""
    if not ANSWER_KEY.exists():
        return
    with ANSWER_KEY.open(encoding="utf-8") as f:
        planted = list(csv.DictReader(f))
    worked = staff_corrections()
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
        print(f"     {p['error_type']:<20} expected {p['expected_rule']} {p['expected_action']:<7} {result}")
    print(f"   Result: {caught} of {len(planted)} planted defects caught")


# ---------------------------------------------------------------------------
# 5. LOAD
# ---------------------------------------------------------------------------
def load(coverages, load_log, xwalk, limit=None):
    chunks = [coverages[i:i + BUNDLE_SIZE] for i in range(0, len(coverages), BUNDLE_SIZE)]
    if limit:
        chunks = chunks[:limit]
    print(f"\n5. LOAD   {len(chunks)} bundle(s) of Coverage to {FHIR_SERVER}")
    sent = 0
    for n, chunk in enumerate(chunks, 2):                    # bundle 1 was the payers
        entries = [put_entry(c, f"Coverage?identifier={SYS_COVERAGE}|{key}") for key, c in chunk]
        sent += len(entries)
        reply = send_bundle(entries, n, "coverage", load_log)
        if reply is not None:
            for (key, _), r in zip(chunk, reply):
                fhir_id, status = parse_reply(r, "Coverage")
                xwalk.append({"resource_type": "Coverage", "legacy_id": key, "fhir_id": fhir_id,
                              "server_status": status})
        time.sleep(PAUSE_BETWEEN_BUNDLES)
    rows = [x for x in xwalk if x["resource_type"] == "Coverage"]
    confirmed = sum(1 for x in rows if x["fhir_id"])
    created = sum(1 for x in rows if x["server_status"].startswith("201"))
    updated = sum(1 for x in rows if x["server_status"].startswith("200"))
    print(f"   Sent {sent}, confirmed {confirmed} ({created} created, {updated} updated)   "
          f"{'PASS' if confirmed == sent else 'FAIL'}")


# ---------------------------------------------------------------------------
# 6. EXTRACT
# ---------------------------------------------------------------------------
def extract(xwalk):
    print("\n6. EXTRACT")
    actual = []
    payers = [r for r in search_all("Organization") if identifier(r, SYS_PAYER)]   # skip the clinic
    coverages = search_all("Coverage")
    for resource_type, found, flatten, system in (("Organization", payers, flatten_payer, SYS_PAYER),
                                                  ("Coverage", coverages, flatten_coverage, SYS_COVERAGE)):
        for r in found:
            actual += long_rows(resource_type, identifier(r, system), flatten(r), "actual_value", r.get("id"))
        expected_ids = {x["fhir_id"] for x in xwalk if x["resource_type"] == resource_type and x["fhir_id"]}
        on_server = {r.get("id") for r in found}
        missing = expected_ids - on_server
        print(f"   {resource_type:<13} expected {len(expected_ids):>4}, on server {len(on_server):>4}, "
              f"missing {len(missing)}   {'PASS' if not missing else 'FAIL'}")
    save_table("tgt.coverage_resources", actual, LONG_ACTUAL)
    write_csv(OUTPUT_DIR / "target_coverage_resources.csv", actual, LONG_ACTUAL)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Phase 2: migrate current insurance to FHIR Coverage")
    parser.add_argument("--limit", type=int, help="only send the first N bundles of Coverage (try 1 first)")
    args = parser.parse_args()

    print("1. STAGE")
    stage_csv(TRANSITIONS_FILE, "payer_transitions")
    stage_csv(PAYERS_FILE, "payers")

    print("\n2. CURATE   staff corrections from remediation/corrections.csv")
    if not apply_corrections.main(show_next=False):
        raise SystemExit("Stopped: fix remediation/corrections.csv, then run this script again.")
    rows = read_table("SELECT * FROM cur.payer_transitions ORDER BY source_row;")
    payers = read_table("SELECT * FROM cur.payers ORDER BY source_row;")
    patient_xwalk = {r["legacy_id"]: r["fhir_id"] for r in read_table(
        "SELECT legacy_id, fhir_id FROM etl.fhir_crosswalk WHERE resource_type = 'Patient' AND fhir_id IS NOT NULL;")}
    print(f"   cur.payer_transitions {len(rows)} rows, cur.payers {len(payers)} rows, "
          f"migrated patients {len(patient_xwalk)}")

    load_log, xwalk, expected = [], [], []
    payer_refs = load_payers(payers, load_log, xwalk, expected)
    coverages, issues, dispositions, sources = transform(rows, payers, patient_xwalk, payer_refs,
                                                         read_type_map(), expected)
    save_table("etl.coverage_issue_log", issues, ISSUE_COLS)
    save_table("etl.coverage_disposition", dispositions, DISPOSITION_COLS)
    save_table("etl.coverage_sources", sources, SOURCE_COLS)
    write_csv(OUTPUT_DIR / "issue_log.csv", issues, ISSUE_COLS)

    load(coverages, load_log, xwalk, args.limit)
    save_table("etl.coverage_crosswalk", xwalk, XWALK_COLS)
    save_table("etl.coverage_expected", expected, LONG_EXPECTED)
    save_table("etl.coverage_load_log", load_log,
               ["bundle_no", "label", "entries", "http_status", "outcome", "attempts", "seconds", "error"])
    write_csv(OUTPUT_DIR / "crosswalk.csv", xwalk, XWALK_COLS)

    extract(xwalk)
    print("\nNext: open sql\\23_coverage_reconciliation.sql in SSMS and press F5.")


if __name__ == "__main__":
    main()
