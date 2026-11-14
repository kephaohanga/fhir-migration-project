"""
phase2_allergy.py
-----------------
Phase 2, step 6: migrate legacy allergies to FHIR AllergyIntolerance.

An allergy lost in a conversion is a patient-safety risk: the new system would
not warn a prescriber. So every code must carry a CONFIRMED code system.

Profiling found SYSTEM = "Unknown" on every legacy row, so the system is never
guessed: docs/allergy_code_map.csv lists each code with its confirmed system
(SNOMED CT descriptions end with a tag such as "(substance)"; RxNorm drug names
do not). A code missing from the map is rejected, not guessed.

Mapping:
  CODE, DESCRIPTION (+ map)  -> code               SNOMED CT or RxNorm, from the map
  TYPE                       -> type               allergy | intolerance
  CATEGORY                   -> category           food | medication | environment
  STOP blank / present       -> clinicalStatus     active / resolved (STOP date kept in a note:
                                                   FHIR R4 has no "resolved on" date field)
  (always)                   -> verificationStatus confirmed
  PATIENT                    -> patient            Patient (must be the visit's patient)
  ENCOUNTER                  -> encounter          Encounter (must be migrated)
  START                      -> onsetDateTime
  REACTIONn, DESCRIPTIONn,   -> reaction[n]        manifestation (code from the map) and severity
  SEVERITYn                                        (mild | moderate | severe)

Rules:
  R-AL01 Reject            PATIENT is not a migrated patient
  R-AL02 Reject            ENCOUNTER is not in the legacy encounter file (orphan)
  R-AL02 Reject (cascade)  the visit exists but was not migrated: stays with its visit in legacy
  R-AL03 Reject            PATIENT is not the visit's patient
  R-AL04 Reject            CODE blank or not in docs/allergy_code_map.csv (system unknown)
  R-AL05 Reject            START blank/invalid, or STOP before START
  R-AL06 Fix               duplicate allergy row removed (first copy kept)
  R-AL07 Flag              TYPE, CATEGORY or SEVERITY value not recognized - that value omitted
  R-AL08 Flag              reaction code not in the map - reaction loaded as text only

Run AFTER Phase 1. From the project folder:
  python src\\phase2_allergy.py --limit 1   <- ONE bundle of 50 (a test)
  python src\\phase2_allergy.py             <- everything
Then open sql\\25_allergy_reconciliation.sql in SSMS and press F5.
"""

import argparse
import csv
import re
import time
from collections import defaultdict
from pathlib import Path

import pandas as pd

import apply_corrections
from phase2_clinic_structure import identifier, parse_reply, put_entry, search_all, send_bundle
from phase2_practitioner import (BUNDLE_SIZE, FHIR_SERVER, PAUSE_BETWEEN_BUNDLES, PROJECT_TAG,
                                 connect, save_table, v, write_csv)
from phase2_procedure import legacy_key, long_rows, parse_dt, read_table

SOURCE_FILE = Path("data/legacy_dirty/allergies.csv")
CODE_MAP = Path("docs/allergy_code_map.csv")
ANSWER_KEY = Path("docs/planted_allergy_errors_answer_key.csv")
OUTPUT_DIR = Path("output/phase2_allergy")

SYS_ALLERGY = "urn:fhirmigration:allergy-id"
SYSTEM_URIS = {"SNOMED-CT": "http://snomed.info/sct",
               "RXNORM": "http://www.nlm.nih.gov/research/umls/rxnorm"}
SYS_CLINICAL = "http://terminology.hl7.org/CodeSystem/allergyintolerance-clinical"
SYS_VERIFICATION = "http://terminology.hl7.org/CodeSystem/allergyintolerance-verification"

TYPES = {"allergy", "intolerance"}
CATEGORIES = {"food", "medication", "environment", "biologic"}
SEVERITIES = {"mild", "moderate", "severe"}

ISSUE_COLS = ["record_key", "source_row", "rule_id", "action", "detail"]
DISPOSITION_COLS = ["record_key", "source_row", "disposition"]
SOURCE_COLS = ["allergy_key", "record_key", "legacy_patient", "legacy_encounter"]
LONG_EXPECTED = ["resource_type", "legacy_id", "field_name", "expected_value"]
LONG_ACTUAL = ["resource_type", "fhir_id", "legacy_id", "field_name", "actual_value"]
XWALK_COLS = ["resource_type", "legacy_id", "fhir_id", "server_status"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def read_code_map():
    """{code: (system URI, display)} - only rows with a recognized code system."""
    if not CODE_MAP.exists():
        raise SystemExit(f"{CODE_MAP} not found - every allergy would be rejected.")
    result = {}
    with CODE_MAP.open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            uri = SYSTEM_URIS.get((r.get("code_system") or "").strip().upper())
            if r.get("code") and uri:
                result[r["code"].strip()] = (uri, (r.get("display") or "").strip())
    return result


def first_coding(cc):
    return ((cc or {}).get("coding") or [{}])[0]


def flatten_allergy(r):
    coding = first_coding(r.get("code"))
    fields = {
        "clinical_status": first_coding(r.get("clinicalStatus")).get("code"),
        "verification_status": first_coding(r.get("verificationStatus")).get("code"),
        "type": r.get("type"),
        "category": (r.get("category") or [None])[0],
        "code_system": coding.get("system"),
        "code": coding.get("code"),
        "code_display": coding.get("display"),
        "patient_ref": (r.get("patient") or {}).get("reference"),
        "encounter_ref": (r.get("encounter") or {}).get("reference"),
        "onset": r.get("onsetDateTime"),
        "note": ((r.get("note") or [{}])[0]).get("text"),
    }
    reactions = r.get("reaction") or []
    for n in (1, 2):
        reaction = reactions[n - 1] if len(reactions) >= n else {}
        manifestation = (reaction.get("manifestation") or [{}])[0]
        fields[f"reaction{n}_code"] = first_coding(manifestation).get("code")
        fields[f"reaction{n}_text"] = manifestation.get("text")
        fields[f"reaction{n}_severity"] = reaction.get("severity")
    return fields


# ---------------------------------------------------------------------------
# 1. STAGE
# ---------------------------------------------------------------------------
def stage():
    if not SOURCE_FILE.exists():
        raise SystemExit(f"{SOURCE_FILE} not found. Run  python src\\plant_allergy_errors.py  first.")
    df = pd.read_csv(SOURCE_FILE, dtype=str, keep_default_na=False)
    columns = list(df.columns)
    data = [[None if x == "" else x for x in values] + [n + 2]
            for n, values in enumerate(df.itertuples(index=False, name=None))]
    conn = connect()
    cur = conn.cursor()
    cur.execute("IF SCHEMA_ID('stg') IS NULL EXEC('CREATE SCHEMA stg');")
    cur.execute("DROP TABLE IF EXISTS stg.allergies;")
    col_defs = ", ".join(f"[{c}] NVARCHAR(500) NULL" for c in columns)
    cur.execute(f"CREATE TABLE stg.allergies ({col_defs}, [source_row] INT NOT NULL, "
                f"[loaded_at] DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME());")
    cur.fast_executemany = True
    cur.executemany(
        f"INSERT INTO stg.allergies ({', '.join(f'[{c}]' for c in columns)}, [source_row]) "
        f"VALUES ({', '.join('?' for _ in range(len(columns) + 1))});", data)
    sql_rows = cur.execute("SELECT COUNT(*) FROM stg.allergies;").fetchone()[0]
    conn.close()
    print("1. STAGE")
    print(f"   CSV rows {len(df)}   stg.allergies rows {sql_rows}   {'PASS' if sql_rows == len(df) else 'FAIL'}")


# ---------------------------------------------------------------------------
# 3. TRANSFORM
# ---------------------------------------------------------------------------
def transform(rows, encounters, enc_xwalk, patient_xwalk, code_map, expected):
    issues, dispositions, allergies, sources = [], [], [], []
    seen, unmapped_reactions = set(), set()

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

        if key in seen:                                                               # R-AL06
            log(key, src, "R-AL06", "Fix", "Duplicate allergy row removed (first copy kept)")
            dispose(key, src, "Duplicate removed")
            continue
        seen.add(key)

        if patient not in patient_xwalk:                                              # R-AL01
            reject(key, src, "R-AL01", f"PATIENT {patient} is not a migrated patient")
            continue
        visit = encounters.get(enc_id)
        if visit is None:                                                             # R-AL02
            reject(key, src, "R-AL02", f"ENCOUNTER {enc_id} is not in the legacy encounter file (orphan)")
            continue
        if enc_id not in enc_xwalk:                                                   # R-AL02 cascade
            reject(key, src, "R-AL02",
                   f"Visit {enc_id} was not migrated - allergy stays with its visit in the legacy archive",
                   action="Reject (cascade)")
            continue
        if patient != visit["PATIENT"]:                                               # R-AL03
            reject(key, src, "R-AL03", f"PATIENT {patient} is not the visit's patient ({visit['PATIENT']})")
            continue
        if code is None or code not in code_map:                                      # R-AL04
            reject(key, src, "R-AL04",
                   f"CODE '{code}' is not in {CODE_MAP.as_posix()} - code system cannot be confirmed")
            continue
        start, stop = parse_dt(v(r, "START")), parse_dt(v(r, "STOP"))
        if start is None or (v(r, "STOP") and (stop is None or stop < start)):        # R-AL05
            reject(key, src, "R-AL05", f"Dates invalid: START {v(r, 'START')} STOP {v(r, 'STOP')}")
            continue

        system_uri, _ = code_map[code]
        allergy_key = f"{enc_id}_{code}_{re.sub(r'[^0-9]', '', v(r, 'START'))}"
        resolved = v(r, "STOP") is not None
        allergy = {
            "resourceType": "AllergyIntolerance",
            "meta": {"tag": [PROJECT_TAG]},
            "identifier": [{"system": SYS_ALLERGY, "value": allergy_key}],
            "clinicalStatus": {"coding": [{"system": SYS_CLINICAL,
                                           "code": "resolved" if resolved else "active"}]},
            "verificationStatus": {"coding": [{"system": SYS_VERIFICATION, "code": "confirmed"}]},
            "code": {"coding": [{"system": system_uri, "code": code, "display": v(r, "DESCRIPTION")}],
                     "text": v(r, "DESCRIPTION")},
            "patient": {"reference": f"Patient/{patient_xwalk[patient]}"},
            "encounter": {"reference": f"Encounter/{enc_xwalk[enc_id]}"},
            "onsetDateTime": v(r, "START"),
        }
        if resolved:
            allergy["note"] = [{"text": f"Resolved {v(r, 'STOP')} (legacy STOP date)"}]

        allergy_type = (v(r, "TYPE") or "").lower()
        if allergy_type in TYPES:
            allergy["type"] = allergy_type
        elif allergy_type:                                                            # R-AL07
            log(key, src, "R-AL07", "Flag", f"TYPE '{v(r, 'TYPE')}' not recognized - omitted")
        category = (v(r, "CATEGORY") or "").lower()
        if category in CATEGORIES:
            allergy["category"] = [category]
        elif category:                                                                # R-AL07
            log(key, src, "R-AL07", "Flag", f"CATEGORY '{v(r, 'CATEGORY')}' not recognized - omitted")

        reactions = []
        for n in (1, 2):
            reaction_code, reaction_text = v(r, f"REACTION{n}"), v(r, f"DESCRIPTION{n}")
            if not reaction_code and not reaction_text:
                continue
            manifestation = {"text": reaction_text}
            if reaction_code in code_map:
                manifestation["coding"] = [{"system": code_map[reaction_code][0], "code": reaction_code,
                                            "display": reaction_text}]
            else:                                                                     # R-AL08
                unmapped_reactions.add(f"{reaction_code} {reaction_text}")
                log(key, src, "R-AL08", "Flag",
                    f"Reaction code '{reaction_code}' not in {CODE_MAP.as_posix()} - loaded as text only")
            reaction = {"manifestation": [manifestation]}
            severity = (v(r, f"SEVERITY{n}") or "").lower()
            if severity in SEVERITIES:
                reaction["severity"] = severity
            elif severity:                                                            # R-AL07
                log(key, src, "R-AL07", "Flag", f"SEVERITY{n} '{v(r, f'SEVERITY{n}')}' not recognized - omitted")
            reactions.append(reaction)
        if reactions:
            allergy["reaction"] = reactions

        allergies.append((allergy_key, allergy))
        sources.append({"allergy_key": allergy_key, "record_key": key,
                        "legacy_patient": patient, "legacy_encounter": enc_id})
        expected += long_rows("AllergyIntolerance", allergy_key, flatten_allergy(allergy), "expected_value")
        dispose(key, src, "Migrated")

    counts = defaultdict(int)
    for d in dispositions:
        counts[d["disposition"]] += 1
    ok = len(rows) == len(dispositions)
    print("\n3. TRANSFORM   AllergyIntolerance")
    print(f"   Legacy rows {len(rows)} = " + " + ".join(f"{n} {d.lower()}" for d, n in sorted(counts.items()))
          + f"   {'PASS' if ok else 'FAIL'}")
    active = sum(1 for _, a in allergies if a["clinicalStatus"]["coding"][0]["code"] == "active")
    with_reaction = sum(1 for _, a in allergies if "reaction" in a)
    print(f"   Allergies built: {len(allergies)} ({active} active, {len(allergies) - active} resolved; "
          f"{with_reaction} with a reaction)")
    for rule in sorted({i["rule_id"] + " " + i["action"] for i in issues}):
        print(f"   {rule}: {sum(1 for i in issues if i['rule_id'] + ' ' + i['action'] == rule)}")
    if unmapped_reactions:
        print(f"   Reaction codes to add to {CODE_MAP}:")
        for item in sorted(unmapped_reactions):
            print(f"     {item}")
    check_answer_key(issues)
    return allergies, issues, dispositions, sources


def check_answer_key(issues):
    if not ANSWER_KEY.exists():
        return
    with ANSWER_KEY.open(encoding="utf-8") as f:
        planted = list(csv.DictReader(f))
    worked = {r["record_key"]: r["disposition"] for r in read_table(
        "SELECT record_key, disposition FROM etl.corrections WHERE table_name = 'allergies';")}
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
def load(allergies, load_log, xwalk, limit=None):
    chunks = [allergies[i:i + BUNDLE_SIZE] for i in range(0, len(allergies), BUNDLE_SIZE)]
    if limit:
        chunks = chunks[:limit]
    print(f"\n4. LOAD   {len(chunks)} bundle(s) of AllergyIntolerance to {FHIR_SERVER}")
    sent = 0
    for n, chunk in enumerate(chunks, 1):
        entries = [put_entry(a, f"AllergyIntolerance?identifier={SYS_ALLERGY}|{key}") for key, a in chunk]
        sent += len(entries)
        reply = send_bundle(entries, n, "allergies", load_log)
        if reply is not None:
            for (key, _), r in zip(chunk, reply):
                fhir_id, status = parse_reply(r, "AllergyIntolerance")
                xwalk.append({"resource_type": "AllergyIntolerance", "legacy_id": key, "fhir_id": fhir_id,
                              "server_status": status})
        time.sleep(PAUSE_BETWEEN_BUNDLES)
    confirmed = sum(1 for x in xwalk if x["fhir_id"])
    created = sum(1 for x in xwalk if x["server_status"].startswith("201"))
    updated = sum(1 for x in xwalk if x["server_status"].startswith("200"))
    print(f"   Sent {sent}, confirmed {confirmed} ({created} created, {updated} updated)   "
          f"{'PASS' if confirmed == sent else 'FAIL'}")


def extract(xwalk):
    print("\n5. EXTRACT")
    found = search_all("AllergyIntolerance")
    actual = []
    for r in found:
        actual += long_rows("AllergyIntolerance", identifier(r, SYS_ALLERGY), flatten_allergy(r),
                            "actual_value", r.get("id"))
    expected_ids = {x["fhir_id"] for x in xwalk if x["fhir_id"]}
    on_server = {r.get("id") for r in found}
    missing = expected_ids - on_server
    print(f"   AllergyIntolerance  expected {len(expected_ids)}, on server {len(on_server)}, "
          f"missing {len(missing)}   {'PASS' if not missing else 'FAIL'}")
    save_table("tgt.allergy_resources", actual, LONG_ACTUAL)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Phase 2: migrate allergies to FHIR AllergyIntolerance")
    parser.add_argument("--limit", type=int, help="only send the first N bundles (try 1 first)")
    args = parser.parse_args()

    stage()
    print("\n2. CURATE   staff corrections from remediation/corrections.csv")
    if not apply_corrections.main(show_next=False):
        raise SystemExit("Stopped: fix remediation/corrections.csv, then run this script again.")
    rows = read_table("SELECT * FROM cur.allergies ORDER BY source_row;")
    encounters = {}
    for e in read_table("SELECT Id, PATIENT, source_row FROM cur.encounters ORDER BY source_row;"):
        encounters.setdefault(e["Id"], e)
    enc_xwalk = {r["legacy_id"]: r["fhir_id"] for r in read_table(
        "SELECT legacy_id, fhir_id FROM etl.fhir_crosswalk WHERE resource_type = 'Encounter' AND fhir_id IS NOT NULL;")}
    patient_xwalk = {r["legacy_id"]: r["fhir_id"] for r in read_table(
        "SELECT legacy_id, fhir_id FROM etl.fhir_crosswalk WHERE resource_type = 'Patient' AND fhir_id IS NOT NULL;")}
    code_map = read_code_map()
    print(f"   cur.allergies {len(rows)} rows; codes in the map {len(code_map)}; "
          f"migrated visits {len(enc_xwalk)}, patients {len(patient_xwalk)}")

    expected = []
    allergies, issues, dispositions, sources = transform(rows, encounters, enc_xwalk, patient_xwalk,
                                                         code_map, expected)
    save_table("etl.allergy_issue_log", issues, ISSUE_COLS)
    save_table("etl.allergy_disposition", dispositions, DISPOSITION_COLS)
    save_table("etl.allergy_sources", sources, SOURCE_COLS)
    save_table("etl.allergy_expected", expected, LONG_EXPECTED)
    write_csv(OUTPUT_DIR / "issue_log.csv", issues, ISSUE_COLS)

    load_log, xwalk = [], []
    load(allergies, load_log, xwalk, args.limit)
    save_table("etl.allergy_crosswalk", xwalk, XWALK_COLS)
    save_table("etl.allergy_load_log", load_log,
               ["bundle_no", "label", "entries", "http_status", "outcome", "attempts", "seconds", "error"])
    write_csv(OUTPUT_DIR / "crosswalk.csv", xwalk, XWALK_COLS)

    extract(xwalk)
    print("\nNext: open sql\\25_allergy_reconciliation.sql in SSMS and press F5.")


if __name__ == "__main__":
    main()
