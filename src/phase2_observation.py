"""
phase2_observation.py
---------------------
Phase 2, step 8: migrate legacy observations (vital signs, lab results, surveys...)
to FHIR Observation.

Decisions:
  - All clinical observations move (full history): trends in vitals and labs guide care.
  - QOLS, QALY and DALY are NOT migrated: they are statistics the simulator calculates
    (quality-of-life scores, quality- and disability-adjusted life years), not measurements
    taken from a patient. They have no standard code and no category. Each row gets the
    disposition "Not migrated - simulator metric", so it is counted, not silently dropped.

Code systems: Synthea's data dictionary states CODE is LOINC; units follow UCUM.
Profiling showed the dictionary is not the whole truth: 112 rows are SNOMED CT
(LogMAR visual acuity) and 16 use Synthea's own LOCAL codes (X9999-1, X9999-2).
Codes that match the LOINC pattern are LOINC; the exceptions are listed, with their
confirmed system, in docs/observation_code_map.csv. Local codes are kept, labelled as
local, and flagged so they can be mapped to a standard code later.

Mapping:
  CODE, DESCRIPTION   -> code               LOINC (http://loinc.org)
  CATEGORY            -> category           vital-signs, laboratory, survey, ...
  (always)            -> status             final
  PATIENT             -> subject            Patient (must be the visit's patient)
  ENCOUNTER           -> encounter          Encounter (must be migrated)
  DATE                -> effectiveDateTime
  TYPE numeric        -> valueQuantity      VALUE as a number + UNITS (UCUM)
  TYPE text           -> valueString        VALUE as written

Numbers are checked so a value cannot change on the way to the server: if a VALUE
has more digits than can be sent exactly, it is flagged.

Rules:
  R-OB01 Reject            PATIENT is not a migrated patient
  R-OB02 Reject            ENCOUNTER is not in the legacy encounter file (orphan)
  R-OB02 Reject (cascade)  the visit exists but was not migrated: stays with its visit in legacy
  R-OB03 Reject            PATIENT is not the visit's patient
  R-OB04 Reject            CODE is neither LOINC (digits, hyphen, check digit) nor listed in
                           docs/observation_code_map.csv - its code system cannot be confirmed
  R-OB05 Reject            DATE blank or not a real date
  R-OB06 Reject            VALUE blank, or TYPE numeric but VALUE is not a number
  R-OB07 Fix               exact duplicate row (EVERY column identical) removed, first copy kept
  R-OB08 Flag              same patient, visit, code and date as an earlier row but a different
                           value: kept as a separate observation (identifier gets the row number)
  R-OB09 Flag              CATEGORY not recognized, UNITS blank on a number, a number that
                           cannot be sent exactly, or a LOCAL code (to be mapped later) - the
                           observation is still loaded

Run AFTER Phase 1. From the project folder:
  python src\\phase2_observation.py --limit 1   <- ONE bundle of 100 (a test)
  python src\\phase2_observation.py             <- everything (about 340 bundles: 20-30 minutes)
Then open sql\\27_observation_reconciliation.sql in SSMS and press F5.
"""

import argparse
import csv
import re
import time
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pandas as pd

import apply_corrections
import contextlib
import io

from phase2_clinic_structure import identifier, parse_reply, put_entry, search_all, send_bundle
from phase2_practitioner import (FHIR_SERVER, PAUSE_BETWEEN_BUNDLES, PROJECT_TAG,
                                 connect, save_table, v, write_csv)
from phase2_procedure import long_rows, parse_dt, read_table

SOURCE_FILE = Path("data/legacy_dirty/observations.csv")
CODE_MAP = Path("docs/observation_code_map.csv")
ANSWER_KEY = Path("docs/planted_observation_errors_answer_key.csv")
OUTPUT_DIR = Path("output/phase2_observation")
BUNDLE_SIZE = 100                       # larger bundles: about half the load time of 50

SYS_OBSERVATION = "urn:fhirmigration:observation-id"
SYS_LOINC = "http://loinc.org"
SYSTEM_URIS = {"LOINC": SYS_LOINC,
               "SNOMED-CT": "http://snomed.info/sct",
               "LOCAL": "urn:fhirmigration:legacy-observation-code"}   # the legacy system's own codes
SYS_UCUM = "http://unitsofmeasure.org"
SYS_CATEGORY = "http://terminology.hl7.org/CodeSystem/observation-category"
CATEGORIES = {"social-history", "vital-signs", "imaging", "laboratory", "procedure",
              "survey", "exam", "therapy", "activity"}
SIMULATOR_METRICS = {"QOLS", "QALY", "DALY"}
LOINC = re.compile(r"^\d{1,7}-\d$")

ISSUE_COLS = ["record_key", "source_row", "rule_id", "action", "detail"]
DISPOSITION_COLS = ["record_key", "source_row", "disposition"]
SOURCE_COLS = ["observation_key", "record_key", "source_row", "legacy_patient", "legacy_encounter"]
LONG_EXPECTED = ["resource_type", "legacy_id", "field_name", "expected_value"]
LONG_ACTUAL = ["resource_type", "fhir_id", "legacy_id", "field_name", "actual_value"]
XWALK_COLS = ["resource_type", "legacy_id", "fhir_id", "server_status"]
AUDIT_COLS = {"source_row", "loaded_at"}


def legacy_key(r):
    """Same business key as apply_corrections.py: PATIENT_ENCOUNTER_CODE_DATE."""
    return "_".join(str(r.get(c) or "") for c in ("PATIENT", "ENCOUNTER", "CODE", "DATE"))


def number_text(x):
    """A number as plain text with no trailing zeros: 179.0 -> '179', 0.50 -> '0.5'.
    Used for both expected and actual, so the comparison is about the value."""
    if x is None:
        return None
    return format(Decimal(repr(x) if isinstance(x, float) else str(x)).normalize(), "f")


def read_code_map():
    """{code: system name} for the confirmed non-LOINC codes."""
    if not CODE_MAP.exists():
        print(f"   WARNING: {CODE_MAP} not found - only LOINC codes will be accepted")
        return {}
    with CODE_MAP.open(encoding="utf-8-sig", newline="") as f:
        return {r["code"].strip(): r["code_system"].strip().upper()
                for r in csv.DictReader(f)
                if r.get("code") and (r.get("code_system") or "").strip().upper() in SYSTEM_URIS}


def flatten_observation(r):
    coding = ((r.get("code") or {}).get("coding") or [{}])[0]
    category = next((c.get("code") for cat in r.get("category", []) for c in cat.get("coding", [])
                     if c.get("system") == SYS_CATEGORY), None)
    quantity = r.get("valueQuantity") or {}
    return {
        "status": r.get("status"),
        "category": category,
        "code_system": coding.get("system"),
        "code": coding.get("code"),
        "code_display": coding.get("display"),
        "subject_ref": (r.get("subject") or {}).get("reference"),
        "encounter_ref": (r.get("encounter") or {}).get("reference"),
        "effective": r.get("effectiveDateTime"),
        "value_number": number_text(quantity.get("value")),
        "value_unit": quantity.get("code"),
        "value_string": r.get("valueString"),
    }


# ---------------------------------------------------------------------------
# 1. STAGE
# ---------------------------------------------------------------------------
def stage():
    if not SOURCE_FILE.exists():
        raise SystemExit(f"{SOURCE_FILE} not found. Run  python src\\plant_observation_errors.py  first.")
    df = pd.read_csv(SOURCE_FILE, dtype=str, keep_default_na=False)
    columns = list(df.columns)
    data = [[None if x == "" else x for x in values] + [n + 2]
            for n, values in enumerate(df.itertuples(index=False, name=None))]
    conn = connect()
    cur = conn.cursor()
    cur.execute("IF SCHEMA_ID('stg') IS NULL EXEC('CREATE SCHEMA stg');")
    cur.execute("DROP TABLE IF EXISTS stg.observations;")
    col_defs = ", ".join(f"[{c}] NVARCHAR(500) NULL" for c in columns)
    cur.execute(f"CREATE TABLE stg.observations ({col_defs}, [source_row] INT NOT NULL, "
                f"[loaded_at] DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME());")
    cur.fast_executemany = True
    cur.executemany(
        f"INSERT INTO stg.observations ({', '.join(f'[{c}]' for c in columns)}, [source_row]) "
        f"VALUES ({', '.join('?' for _ in range(len(columns) + 1))});", data)
    sql_rows = cur.execute("SELECT COUNT(*) FROM stg.observations;").fetchone()[0]
    conn.close()
    print("1. STAGE")
    print(f"   CSV rows {len(df)}   stg.observations rows {sql_rows}   {'PASS' if sql_rows == len(df) else 'FAIL'}")


# ---------------------------------------------------------------------------
# 3. TRANSFORM
# ---------------------------------------------------------------------------
def transform(rows, encounters, enc_xwalk, patient_xwalk, code_map, expected):
    issues, dispositions, observations, sources = [], [], [], []
    seen_rows, used_ids = set(), {}

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
        if signature in seen_rows:                                                    # R-OB07
            log(key, src, "R-OB07", "Fix", "Exact duplicate row (every column identical) removed - first copy kept")
            dispose(key, src, "Duplicate removed")
            continue
        seen_rows.add(signature)

        if (code or "").upper() in SIMULATOR_METRICS:                                 # decision, not a defect
            dispose(key, src, "Not migrated - simulator metric")
            continue
        if patient not in patient_xwalk:                                              # R-OB01
            reject(key, src, "R-OB01", f"PATIENT {patient} is not a migrated patient")
            continue
        visit = encounters.get(enc_id)
        if visit is None:                                                             # R-OB02
            reject(key, src, "R-OB02", f"ENCOUNTER {enc_id} is not in the legacy encounter file (orphan)")
            continue
        if enc_id not in enc_xwalk:                                                   # R-OB02 cascade
            reject(key, src, "R-OB02",
                   f"Visit {enc_id} was not migrated - observation stays with its visit in the legacy archive",
                   action="Reject (cascade)")
            continue
        if patient != visit["PATIENT"]:                                               # R-OB03
            reject(key, src, "R-OB03", f"PATIENT {patient} is not the visit's patient ({visit['PATIENT']})")
            continue
        if code and LOINC.match(code):
            code_system = "LOINC"
        elif code in code_map:
            code_system = code_map[code]
        else:                                                                         # R-OB04
            reject(key, src, "R-OB04",
                   f"CODE '{code}' is neither LOINC nor in {CODE_MAP.as_posix()} - code system cannot be confirmed")
            continue
        if parse_dt(v(r, "DATE")) is None:                                            # R-OB05
            reject(key, src, "R-OB05", f"DATE '{v(r, 'DATE')}' is blank or not a real date")
            continue
        value, value_type = v(r, "VALUE"), (v(r, "TYPE") or "").lower()
        number = None
        if value is None:                                                             # R-OB06
            reject(key, src, "R-OB06", "VALUE is blank")
            continue
        if value_type == "numeric":
            try:
                exact = Decimal(value)
                number = float(value)
                if not exact.is_finite():
                    raise InvalidOperation
            except (InvalidOperation, ValueError):                                    # R-OB06
                reject(key, src, "R-OB06", f"TYPE is numeric but VALUE '{value}' is not a number")
                continue
            if Decimal(repr(number)) != exact:                                        # R-OB09
                log(key, src, "R-OB09", "Flag", f"VALUE '{value}' has more digits than can be sent exactly")

        observation_key = f"{enc_id}_{code}_{re.sub(r'[^0-9]', '', v(r, 'DATE'))}"
        if observation_key in used_ids:                                               # R-OB08
            log(key, src, "R-OB08", "Flag",
                f"Same patient, visit, code and date as row {used_ids[observation_key]} but a different value "
                f"('{value}') - kept as a separate observation")
            observation_key += f"_r{src}"
        used_ids[observation_key] = src

        observation = {
            "resourceType": "Observation",
            "meta": {"tag": [PROJECT_TAG]},
            "identifier": [{"system": SYS_OBSERVATION, "value": observation_key}],
            "status": "final",
            "code": {"coding": [{"system": SYSTEM_URIS[code_system], "code": code,
                                 "display": v(r, "DESCRIPTION")}],
                     "text": v(r, "DESCRIPTION")},
            "subject": {"reference": f"Patient/{patient_xwalk[patient]}"},
            "encounter": {"reference": f"Encounter/{enc_xwalk[enc_id]}"},
            "effectiveDateTime": v(r, "DATE"),
        }
        if code_system == "LOCAL":                                                    # R-OB09
            log(key, src, "R-OB09", "Flag",
                f"CODE '{code}' is a local legacy code - kept as local, to be mapped to a standard code later")
        category = (v(r, "CATEGORY") or "").lower()
        if category in CATEGORIES:
            observation["category"] = [{"coding": [{"system": SYS_CATEGORY, "code": category}]}]
        else:                                                                         # R-OB09
            log(key, src, "R-OB09", "Flag", f"CATEGORY '{v(r, 'CATEGORY')}' not recognized - omitted")
        if number is not None:
            quantity = {"value": number}
            if v(r, "UNITS"):
                quantity.update({"unit": v(r, "UNITS"), "system": SYS_UCUM, "code": v(r, "UNITS")})
            else:                                                                     # R-OB09
                log(key, src, "R-OB09", "Flag", "Numeric VALUE has no UNITS - loaded without a unit")
            observation["valueQuantity"] = quantity
        else:
            observation["valueString"] = value

        observations.append((observation_key, observation))
        sources.append({"observation_key": observation_key, "record_key": key, "source_row": src,
                        "legacy_patient": patient, "legacy_encounter": enc_id})
        expected += long_rows("Observation", observation_key, flatten_observation(observation), "expected_value")
        dispose(key, src, "Migrated")

    counts = defaultdict(int)
    for d in dispositions:
        counts[d["disposition"]] += 1
    ok = len(rows) == len(dispositions)
    print("\n3. TRANSFORM   Observation")
    print(f"   Legacy rows {len(rows)} = " + " + ".join(f"{n} {d.lower()}" for d, n in sorted(counts.items()))
          + f"   {'PASS' if ok else 'FAIL'}")
    numeric = sum(1 for _, o in observations if "valueQuantity" in o)
    print(f"   Observations built: {len(observations)} ({numeric} numeric, {len(observations) - numeric} text)")
    systems = defaultdict(int)
    for _, o in observations:
        systems[o["code"]["coding"][0]["system"]] += 1
    names = {uri: name for name, uri in SYSTEM_URIS.items()}
    print("   Code systems: " + ", ".join(f"{names[s]} {n}" for s, n in sorted(systems.items(), key=lambda x: -x[1])))
    for rule in sorted({i["rule_id"] + " " + i["action"] for i in issues}):
        print(f"   {rule}: {sum(1 for i in issues if i['rule_id'] + ' ' + i['action'] == rule)}")
    check_answer_key(issues)
    return observations, issues, dispositions, sources


def check_answer_key(issues):
    if not ANSWER_KEY.exists():
        return
    with ANSWER_KEY.open(encoding="utf-8") as f:
        planted = list(csv.DictReader(f))
    worked = {r["record_key"]: r["disposition"] for r in read_table(
        "SELECT record_key, disposition FROM etl.corrections WHERE table_name = 'observations';")}
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
def _send_quiet(entries, n, load_log):
    """send_bundle, but print only failures (there are about 340 bundles)."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        reply = send_bundle(entries, n, "observations", load_log)
    if reply is None:
        print(buffer.getvalue().rstrip())
    return reply


def load(observations, load_log, xwalk, limit=None):
    chunks = [observations[i:i + BUNDLE_SIZE] for i in range(0, len(observations), BUNDLE_SIZE)]
    if limit:
        chunks = chunks[:limit]
    print(f"\n4. LOAD   {len(chunks)} bundle(s) of {BUNDLE_SIZE} Observations to {FHIR_SERVER}")
    started, sent = time.time(), 0
    for n, chunk in enumerate(chunks, 1):
        entries = [put_entry(o, f"Observation?identifier={SYS_OBSERVATION}|{key}") for key, o in chunk]
        sent += len(entries)
        if n % 25 == 1 or n == len(chunks):
            elapsed = time.time() - started
            remaining = elapsed / max(n - 1, 1) * (len(chunks) - n + 1) if n > 1 else 0
            print(f"   ... bundle {n} of {len(chunks)}   (about {remaining / 60:.0f} min left)")
        reply = _send_quiet(entries, n, load_log)
        if reply is not None:
            for (key, _), r in zip(chunk, reply):
                fhir_id, status = parse_reply(r, "Observation")
                xwalk.append({"resource_type": "Observation", "legacy_id": key, "fhir_id": fhir_id,
                              "server_status": status})
        time.sleep(PAUSE_BETWEEN_BUNDLES)
    confirmed = sum(1 for x in xwalk if x["fhir_id"])
    created = sum(1 for x in xwalk if x["server_status"].startswith("201"))
    updated = sum(1 for x in xwalk if x["server_status"].startswith("200"))
    failed = sum(1 for l in load_log if l["outcome"] == "FAILED")
    print(f"   Sent {sent}, confirmed {confirmed} ({created} created, {updated} updated), "
          f"failed bundles {failed}   {'PASS' if confirmed == sent else 'FAIL'}   "
          f"[{(time.time() - started) / 60:.1f} min]")


def extract(xwalk):
    print("\n5. EXTRACT   (reading every Observation back - several minutes)")
    found = search_all("Observation")
    actual = []
    for r in found:
        actual += long_rows("Observation", identifier(r, SYS_OBSERVATION), flatten_observation(r),
                            "actual_value", r.get("id"))
    expected_ids = {x["fhir_id"] for x in xwalk if x["fhir_id"]}
    on_server = {r.get("id") for r in found}
    missing = expected_ids - on_server
    print(f"   Observation  expected {len(expected_ids)}, on server {len(on_server)}, "
          f"missing {len(missing)}   {'PASS' if not missing else 'FAIL'}")
    save_table("tgt.observation_resources", actual, LONG_ACTUAL)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Phase 2: migrate observations to FHIR Observation")
    parser.add_argument("--limit", type=int, help="only send the first N bundles (try 1 first)")
    args = parser.parse_args()

    stage()
    print("\n2. CURATE   staff corrections from remediation/corrections.csv")
    if not apply_corrections.main(show_next=False):
        raise SystemExit("Stopped: fix remediation/corrections.csv, then run this script again.")
    rows = read_table("SELECT * FROM cur.observations ORDER BY source_row;")
    encounters = {}
    for e in read_table("SELECT Id, PATIENT, source_row FROM cur.encounters ORDER BY source_row;"):
        encounters.setdefault(e["Id"], e)
    enc_xwalk = {r["legacy_id"]: r["fhir_id"] for r in read_table(
        "SELECT legacy_id, fhir_id FROM etl.fhir_crosswalk WHERE resource_type = 'Encounter' AND fhir_id IS NOT NULL;")}
    patient_xwalk = {r["legacy_id"]: r["fhir_id"] for r in read_table(
        "SELECT legacy_id, fhir_id FROM etl.fhir_crosswalk WHERE resource_type = 'Patient' AND fhir_id IS NOT NULL;")}
    code_map = read_code_map()
    print(f"   cur.observations {len(rows)} rows; migrated visits {len(enc_xwalk)}, patients {len(patient_xwalk)}; "
          f"non-LOINC codes in the map {len(code_map)}")

    expected = []
    observations, issues, dispositions, sources = transform(rows, encounters, enc_xwalk, patient_xwalk,
                                                            code_map, expected)
    save_table("etl.observation_issue_log", issues, ISSUE_COLS)
    save_table("etl.observation_disposition", dispositions, DISPOSITION_COLS)
    save_table("etl.observation_sources", sources, SOURCE_COLS)
    save_table("etl.observation_expected", expected, LONG_EXPECTED)
    write_csv(OUTPUT_DIR / "issue_log.csv", issues, ISSUE_COLS)

    load_log, xwalk = [], []
    load(observations, load_log, xwalk, args.limit)
    save_table("etl.observation_crosswalk", xwalk, XWALK_COLS)
    save_table("etl.observation_load_log", load_log,
               ["bundle_no", "label", "entries", "http_status", "outcome", "attempts", "seconds", "error"])
    write_csv(OUTPUT_DIR / "crosswalk.csv", xwalk, XWALK_COLS)

    extract(xwalk)
    print("\nNext: open sql\\27_observation_reconciliation.sql in SSMS and press F5.")


if __name__ == "__main__":
    main()
