"""
transform.py
------------
Step 7: Validate the staged legacy data and transform it into FHIR R4 resources.

Follows docs/source_to_target_mapping.xlsx rule by rule:
  - every check in the Validation_Rules tab is applied (Reject / Fix / Flag)
  - every problem is written to an issue log (nothing is silently dropped)
  - records that pass become FHIR Patient, Encounter and Condition resources

Input : SQL Server  FhirMigration.cur.patients / cur.encounters / cur.conditions
        (the curated layer = raw staging + approved corrections, built by
        src/apply_corrections.py - run that first)
Output: output/fhir_bundles/patient_<legacy id>.json   one FHIR transaction Bundle per patient
        output/issue_log.csv  and  SQL table etl.issue_log
Run from the project folder:  python src\\transform.py
Safe to re-run: output files and etl.issue_log are rebuilt every run.
"""

import csv
import json
import re
import shutil
import uuid
from datetime import date, datetime
from pathlib import Path

import pyodbc

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
SERVER = "localhost"
DRIVER = "ODBC Driver 17 for SQL Server"
DATABASE = "FhirMigration"
OUTPUT_DIR = Path("output")
BUNDLE_DIR = OUTPUT_DIR / "fhir_bundles"
ANSWER_KEY = Path("docs/planted_errors_answer_key.csv")

# Identifier "systems": a namespace that says where an identifier value came from
SYS_PATIENT = "urn:fhirmigration:legacy-patient-id"
SYS_ENCOUNTER = "urn:fhirmigration:legacy-encounter-id"
SYS_CONDITION = "urn:fhirmigration:legacy-condition-key"
PROJECT_TAG = {"system": "urn:fhirmigration:project", "code": "kepha-migration-2026"}
CONDITION_NAMESPACE = uuid.UUID("6f1c2b4e-1d2a-4c3b-9e8f-0a1b2c3d4e5f")  # for repeatable condition UUIDs

# ---------------------------------------------------------------------------
# Value maps (copied from the Value_Maps tab of the mapping workbook)
# ---------------------------------------------------------------------------
GENDER_MAP = {"M": "male", "F": "female"}

MARITAL_SYSTEM = "http://terminology.hl7.org/CodeSystem/v3-MaritalStatus"
MARITAL_MAP = {"M": "Married", "S": "Never Married", "D": "Divorced", "W": "Widowed"}

ACTCODE_SYSTEM = "http://terminology.hl7.org/CodeSystem/v3-ActCode"
CLASS_MAP = {
    "ambulatory": ("AMB", "ambulatory"),
    "wellness": ("AMB", "ambulatory"),
    "outpatient": ("AMB", "ambulatory"),
    "urgentcare": ("AMB", "ambulatory"),          # Decision D2
    "emergency": ("EMER", "emergency"),
    "inpatient": ("IMP", "inpatient encounter"),
    "snf": ("NONAC", "inpatient non-acute"),      # Decision D2
    "hospice": ("NONAC", "inpatient non-acute"),  # Decision D2
    "home": ("HH", "home health"),
    "virtual": ("VR", "virtual"),
}

CODE_SYSTEM_MAP = {
    "SNOMED-CT": "http://snomed.info/sct",
    "": "http://snomed.info/sct",
    "ICD-10-CM": "http://hl7.org/fhir/sid/icd-10-cm",
}

STATE_MAP = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR", "California": "CA",
    "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE", "District of Columbia": "DC",
    "Florida": "FL", "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID", "Illinois": "IL",
    "Indiana": "IN", "Iowa": "IA", "Kansas": "KS", "Kentucky": "KY", "Louisiana": "LA",
    "Maine": "ME", "Maryland": "MD", "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN",
    "Mississippi": "MS", "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV",
    "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY",
    "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK", "Oregon": "OR",
    "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC", "South Dakota": "SD",
    "Tennessee": "TN", "Texas": "TX", "Utah": "UT", "Vermont": "VT", "Virginia": "VA",
    "Washington": "WA", "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY",
}

# ---------------------------------------------------------------------------
# Issue log: every Reject / Fix / Flag is recorded here
# ---------------------------------------------------------------------------
issues = []


def log_issue(table, record_key, source_row, rule_id, action, detail, blocked_by=None):
    """blocked_by = for a cascade reject, the patient whose rejection blocks this record.
    It lets the remediation worklist show how many records each fix would release."""
    issues.append({
        "table_name": table,
        "record_key": record_key,
        "source_row": source_row,
        "rule_id": rule_id,
        "action": action,
        "detail": detail,
        "blocked_by": blocked_by,
    })


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def v(row, column):
    """Return a cleaned value: None if missing/blank, otherwise trimmed text."""
    value = row.get(column)
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def parse_birthdate(raw):
    """Returns (iso_date_string, was_fixed). Raises ValueError if unusable."""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return date.fromisoformat(raw).isoformat(), False
    if re.fullmatch(r"\d{2}/\d{2}/\d{4}", raw):        # legacy US format MM/DD/YYYY
        return datetime.strptime(raw, "%m/%d/%Y").date().isoformat(), True
    raise ValueError(f"unrecognised date format: {raw}")


def to_datetime(raw):
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def coding(system, code, display=None):
    c = {"system": system, "code": code}
    if display:
        c["display"] = display
    return c


# ---------------------------------------------------------------------------
# Read staging tables from SQL Server
# ---------------------------------------------------------------------------
def connect():
    return pyodbc.connect(
        f"DRIVER={{{DRIVER}}};SERVER={SERVER};DATABASE={DATABASE};"
        "Trusted_Connection=yes;TrustServerCertificate=yes;",
        autocommit=True,
    )


def read_staging():
    """Returns {'patients': [row dicts], 'encounters': [...], 'conditions': [...]}.
    Reads the curated layer (cur.*): raw staging plus approved corrections."""
    data = {}
    with connect() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT OBJECT_ID('cur.patients');")
        if cursor.fetchone()[0] is None:
            raise SystemExit("cur.patients not found - run  python src\\apply_corrections.py  first.")
        for table in ("patients", "encounters", "conditions"):
            cursor.execute(f"SELECT * FROM cur.{table} ORDER BY source_row;")
            columns = [col[0] for col in cursor.description]
            data[table] = [dict(zip(columns, row)) for row in cursor.fetchall()]
    return data


def save_issue_log_to_sql(rows):
    with connect() as conn:
        cursor = conn.cursor()
        cursor.execute("IF SCHEMA_ID('etl') IS NULL EXEC('CREATE SCHEMA etl');")
        cursor.execute("DROP TABLE IF EXISTS etl.issue_log;")
        cursor.execute(
            "CREATE TABLE etl.issue_log ("
            " table_name NVARCHAR(50), record_key NVARCHAR(300), source_row INT,"
            " rule_id NVARCHAR(20), action NVARCHAR(30), detail NVARCHAR(500),"
            " blocked_by NVARCHAR(100), logged_at DATETIME2 DEFAULT SYSUTCDATETIME());"
        )
        if rows:
            cursor.fast_executemany = True
            cursor.executemany(
                "INSERT INTO etl.issue_log (table_name, record_key, source_row, rule_id, action, detail, blocked_by)"
                " VALUES (?, ?, ?, ?, ?, ?, ?);",
                [(r["table_name"], r["record_key"], r["source_row"], r["rule_id"], r["action"], r["detail"],
                  r["blocked_by"]) for r in rows],
            )


# ---------------------------------------------------------------------------
# PATIENTS
# ---------------------------------------------------------------------------
def transform_patients(rows):
    """Returns ({legacy id: Patient resource}, set of rejected legacy ids, duplicates removed)."""
    accepted, rejected, seen = {}, set(), set()
    duplicates_removed = 0
    today = date.today()

    for row in rows:
        pid, src = v(row, "Id"), row["source_row"]

        # R-P05 duplicate Id: keep the first, remove the rest
        if pid in seen:
            duplicates_removed += 1
            log_issue("patients", pid, src, "R-P05", "Fix", "Duplicate patient row removed (first copy kept)")
            continue
        seen.add(pid)

        reasons = []  # reject reasons found for this patient

        # R-P01 / R-P02 / R-P06 birthdate
        birth = None
        raw_birth = v(row, "BIRTHDATE")
        if raw_birth is None:
            reasons.append(("R-P01", "BIRTHDATE is blank"))
        else:
            try:
                birth, fixed = parse_birthdate(raw_birth)
                if fixed:
                    log_issue("patients", pid, src, "R-P02", "Fix", f"BIRTHDATE {raw_birth} converted to {birth}")
                if date.fromisoformat(birth) > today:
                    reasons.append(("R-P06", f"BIRTHDATE {birth} is in the future"))
            except ValueError as e:
                reasons.append(("R-P02", f"BIRTHDATE could not be parsed ({e})"))

        death = v(row, "DEATHDATE")
        if death and birth and death < birth:
            reasons.append(("R-P06", f"DEATHDATE {death} is before BIRTHDATE {birth}"))

        # R-P03 gender
        gender_code = v(row, "GENDER")
        if gender_code not in GENDER_MAP:
            reasons.append(("R-P03", f"GENDER '{gender_code}' is not M or F"))

        # R-P07 name
        first, last = v(row, "FIRST"), v(row, "LAST")
        if not first or not last:
            reasons.append(("R-P07", "FIRST or LAST name is blank"))

        if reasons:
            rejected.add(pid)
            for rule_id, detail in reasons:
                log_issue("patients", pid, src, rule_id, "Reject", detail)
            continue

        # ---- Flags (record still loads) ----
        if v(row, "ADDRESS") is None:
            log_issue("patients", pid, src, "R-P04", "Flag", "ADDRESS is blank - patient loaded, address incomplete")
        marital = v(row, "MARITAL")
        if marital and marital not in MARITAL_MAP:
            log_issue("patients", pid, src, "R-P08", "Flag", f"MARITAL '{marital}' not in value map - omitted")

        # ---- Build the FHIR Patient (Patient_Map P01-P29) ----
        official = {"use": "official", "family": last, "given": [first]}
        if v(row, "MIDDLE"):
            official["given"].append(v(row, "MIDDLE"))
        if v(row, "PREFIX"):
            official["prefix"] = [v(row, "PREFIX")]
        if v(row, "SUFFIX"):
            official["suffix"] = [v(row, "SUFFIX")]
        names = [official]
        if v(row, "MAIDEN"):
            names.append({"use": "maiden", "family": v(row, "MAIDEN"), "given": [first]})

        address = {"use": "home", "country": "US"}
        if v(row, "ADDRESS"):
            address["line"] = [v(row, "ADDRESS")]
        for column, element in (("CITY", "city"), ("COUNTY", "district"), ("ZIP", "postalCode")):
            if v(row, column):
                address[element] = v(row, column)
        if v(row, "STATE"):
            address["state"] = STATE_MAP.get(v(row, "STATE"), v(row, "STATE"))

        patient = {
            "resourceType": "Patient",
            "meta": {"tag": [PROJECT_TAG]},
            "identifier": [{"system": SYS_PATIENT, "value": pid}],
            "name": names,
            "gender": GENDER_MAP[gender_code],
            "birthDate": birth,
            "address": [address],
        }
        if death:
            patient["deceasedDateTime"] = death
        if marital in MARITAL_MAP:
            patient["maritalStatus"] = {
                "coding": [coding(MARITAL_SYSTEM, marital, MARITAL_MAP[marital])],
                "text": MARITAL_MAP[marital],
            }
        accepted[pid] = patient

    return accepted, rejected, duplicates_removed


# ---------------------------------------------------------------------------
# ENCOUNTERS
# ---------------------------------------------------------------------------
def transform_encounters(rows, source_patient_ids, accepted_patient_ids):
    """Returns ({legacy encounter id: (legacy patient id, Encounter resource)}, counts)."""
    accepted = {}
    counts = {"rejected_direct": 0, "rejected_cascade": 0}

    for row in rows:
        eid, pid, src = v(row, "Id"), v(row, "PATIENT"), row["source_row"]
        direct = []

        if pid not in source_patient_ids:                                   # R-E01
            direct.append(("R-E01", f"PATIENT {pid} does not exist in the source patient list (orphan)"))
        if v(row, "START") is None:                                         # R-E05
            direct.append(("R-E05", "START is blank"))
        enc_class = v(row, "ENCOUNTERCLASS")
        if enc_class not in CLASS_MAP:                                      # R-E03
            direct.append(("R-E03", f"ENCOUNTERCLASS '{enc_class}' not in value map"))

        if direct:
            counts["rejected_direct"] += 1
            for rule_id, detail in direct:
                log_issue("encounters", eid, src, rule_id, "Reject", detail)
            continue
        if pid not in accepted_patient_ids:                                 # R-E04 cascade
            counts["rejected_cascade"] += 1
            log_issue("encounters", eid, src, "R-E04", "Reject (cascade)",
                      f"Patient {pid} was rejected, so this visit cannot load", blocked_by=pid)
            continue

        start, stop = v(row, "START"), v(row, "STOP")
        if stop and to_datetime(stop) < to_datetime(start):                 # R-E02
            log_issue("encounters", eid, src, "R-E02", "Flag", f"STOP {stop} is before START {start}")

        class_code, class_display = CLASS_MAP[enc_class]
        encounter = {
            "resourceType": "Encounter",
            "meta": {"tag": [PROJECT_TAG]},
            "identifier": [{"system": SYS_ENCOUNTER, "value": eid}],
            "status": "finished" if stop else "in-progress",
            "class": coding(ACTCODE_SYSTEM, class_code, class_display),
            "subject": {"reference": f"urn:uuid:{pid}"},
            "period": {"start": start},
        }
        if stop:
            encounter["period"]["end"] = stop
        if v(row, "CODE"):
            encounter["type"] = [{
                "coding": [coding("http://snomed.info/sct", v(row, "CODE"), v(row, "DESCRIPTION"))],
                "text": v(row, "DESCRIPTION"),
            }]
        if v(row, "REASONCODE"):
            encounter["reasonCode"] = [{
                "coding": [coding("http://snomed.info/sct", v(row, "REASONCODE"), v(row, "REASONDESCRIPTION"))],
                "text": v(row, "REASONDESCRIPTION"),
            }]
        accepted[eid] = (pid, encounter)

    return accepted, counts


# ---------------------------------------------------------------------------
# CONDITIONS
# ---------------------------------------------------------------------------
def transform_conditions(rows, accepted_patient_ids, accepted_encounter_ids):
    """Returns (list of (legacy patient id, Condition resource), counts)."""
    accepted, seen = [], set()
    counts = {"rejected_direct": 0, "rejected_cascade": 0, "duplicates_removed": 0}

    for row in rows:
        pid, eid, src = v(row, "PATIENT"), v(row, "ENCOUNTER"), row["source_row"]
        code, start = v(row, "CODE"), v(row, "START")
        # Legacy key (Condition_Map C01). "_" separator because "|" is a special
        # character in FHIR search URLs.
        key = "_".join(str(part or "") for part in (pid, eid, code, start))

        if key in seen and code:                                            # R-C05
            counts["duplicates_removed"] += 1
            log_issue("conditions", key, src, "R-C05", "Fix", "Duplicate condition removed (first copy kept)")
            continue
        seen.add(key)

        direct = []
        if code is None:                                                    # R-C01
            direct.append(("R-C01", "CODE is blank - diagnosis unusable"))
        system_raw = v(row, "SYSTEM") or ""
        if system_raw not in CODE_SYSTEM_MAP:                               # R-C06
            direct.append(("R-C06", f"SYSTEM '{system_raw}' not recognised"))

        if direct:
            counts["rejected_direct"] += 1
            for rule_id, detail in direct:
                log_issue("conditions", key, src, rule_id, "Reject", detail)
            continue
        if pid not in accepted_patient_ids:                                 # R-C02 cascade
            counts["rejected_cascade"] += 1
            log_issue("conditions", key, src, "R-C02", "Reject (cascade)",
                      f"Patient {pid} was rejected or does not exist", blocked_by=pid)
            continue

        link_encounter = eid in accepted_encounter_ids
        if not link_encounter:                                              # R-C03
            log_issue("conditions", key, src, "R-C03", "Flag",
                      f"Encounter {eid} was not migrated - condition loaded without encounter link")
        if start is None:                                                   # R-C04
            log_issue("conditions", key, src, "R-C04", "Flag", "START is blank - onset unknown")

        stop = v(row, "STOP")
        condition = {
            "resourceType": "Condition",
            "meta": {"tag": [PROJECT_TAG]},
            "identifier": [{"system": SYS_CONDITION, "value": key}],
            "clinicalStatus": {"coding": [coding(
                "http://terminology.hl7.org/CodeSystem/condition-clinical",
                "resolved" if stop else "active")]},
            "verificationStatus": {"coding": [coding(
                "http://terminology.hl7.org/CodeSystem/condition-ver-status", "confirmed")]},
            "category": [{"coding": [coding(
                "http://terminology.hl7.org/CodeSystem/condition-category",
                "encounter-diagnosis", "Encounter Diagnosis")]}],
            "code": {
                "coding": [coding(CODE_SYSTEM_MAP[system_raw], code, v(row, "DESCRIPTION"))],
                "text": v(row, "DESCRIPTION"),
            },
            "subject": {"reference": f"urn:uuid:{pid}"},
        }
        if link_encounter:
            condition["encounter"] = {"reference": f"urn:uuid:{eid}"}
        if start:
            condition["onsetDateTime"] = start
        if stop:
            condition["abatementDateTime"] = stop
        accepted.append((pid, condition))

    return accepted, counts


# ---------------------------------------------------------------------------
# BUNDLES: one FHIR transaction per patient
# ---------------------------------------------------------------------------
def entry(full_url, resource, resource_type, id_system, id_value):
    """A transaction entry that POSTs the resource only if no resource with the
    same legacy identifier exists yet (conditional create = safe to re-run)."""
    return {
        "fullUrl": full_url,
        "resource": resource,
        "request": {
            "method": "POST",
            "url": resource_type,
            "ifNoneExist": f"identifier={id_system}|{id_value}",
        },
    }


def build_bundles(patients, encounters, conditions):
    enc_by_patient, cond_by_patient = {}, {}
    for eid, (pid, enc) in encounters.items():
        enc_by_patient.setdefault(pid, []).append((eid, enc))
    for pid, cond in conditions:
        cond_by_patient.setdefault(pid, []).append(cond)

    if BUNDLE_DIR.exists():
        shutil.rmtree(BUNDLE_DIR)
    BUNDLE_DIR.mkdir(parents=True)

    for pid, patient in patients.items():
        entries = [entry(f"urn:uuid:{pid}", patient, "Patient", SYS_PATIENT, pid)]
        for eid, enc in enc_by_patient.get(pid, []):
            entries.append(entry(f"urn:uuid:{eid}", enc, "Encounter", SYS_ENCOUNTER, eid))
        for cond in cond_by_patient.get(pid, []):
            key = cond["identifier"][0]["value"]
            cond_uuid = uuid.uuid5(CONDITION_NAMESPACE, key)
            entries.append(entry(f"urn:uuid:{cond_uuid}", cond, "Condition", SYS_CONDITION, key))
        bundle = {"resourceType": "Bundle", "type": "transaction", "entry": entries}
        (BUNDLE_DIR / f"patient_{pid}.json").write_text(json.dumps(bundle, indent=2), encoding="utf-8")
    return len(patients)


# ---------------------------------------------------------------------------
# Planted-error self-check against the answer key
# ---------------------------------------------------------------------------
EXPECTED_RULE = {
    ("patients", "missing_required_value"): "R-P01",
    ("patients", "nonstandard_date_format"): "R-P02",
    ("patients", "invalid_code"): "R-P03",
    ("patients", "missing_optional_value"): "R-P04",
    ("patients", "duplicate_record"): "R-P05",
    ("encounters", "orphan_reference"): "R-E01",
    ("conditions", "missing_required_value"): "R-C01",
}


def read_corrections():
    """Corrections made so far, looked up two ways: by record key and by source row."""
    by_key, by_row = {}, {}
    with connect() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT OBJECT_ID('etl.corrections');")
        if cursor.fetchone()[0] is None:
            return by_key, by_row
        cursor.execute("SELECT table_name, source_row, record_key, disposition FROM etl.corrections;")
        for table, row, key, disposition in cursor.fetchall():
            by_key[(table, key)] = disposition
            by_row[(table, row)] = disposition
    return by_key, by_row


def check_answer_key():
    if not ANSWER_KEY.exists():
        print("\n(No answer key found - skipping planted-error check)")
        return
    with ANSWER_KEY.open(encoding="utf-8") as f:
        planted = list(csv.DictReader(f))
    by_key, by_row = read_corrections()
    print("\nPlanted-error check (answer key vs. issue log and corrections)")
    handled = 0
    for n, p in enumerate(planted, 1):
        rule = EXPECTED_RULE.get((p["table"], p["error_type"]))
        if p["table"] == "conditions":
            row_no = int(re.search(r"row (\d+)", p["record_id"]).group(1))
            hit = any(i["rule_id"] == rule and i["source_row"] == row_no for i in issues)
            corrected = by_row.get(("conditions", row_no))
        else:
            hit = any(i["rule_id"] == rule and i["record_key"] == p["record_id"] for i in issues)
            corrected = by_key.get((p["table"], p["record_id"]))
        if hit and corrected == "Archive":
            status = "CAUGHT, then archived"
        elif hit:
            status = "CAUGHT"
        elif corrected == "Correct":
            status = "CAUGHT earlier, now CORRECTED"
        else:
            status = "MISSED"
        handled += status != "MISSED"
        print(f"  {n}. {p['table']:<10} {p['error_type']:<24} expected {rule:<6} {status}")
    print(f"  Result: {handled} of {len(planted)} planted errors caught or corrected")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main():
    issues.clear()
    print("Reading staging tables from SQL Server ...")
    data = read_staging()
    n_pat, n_enc, n_cond = len(data["patients"]), len(data["encounters"]), len(data["conditions"])
    print(f"  stg.patients={n_pat}  stg.encounters={n_enc}  stg.conditions={n_cond}")

    source_patient_ids = {v(r, "Id") for r in data["patients"]}
    patients, rejected_pids, dup_patients = transform_patients(data["patients"])
    encounters, enc_counts = transform_encounters(data["encounters"], source_patient_ids, set(patients))
    conditions, cond_counts = transform_conditions(data["conditions"], set(patients), set(encounters))

    n_bundles = build_bundles(patients, encounters, conditions)

    OUTPUT_DIR.mkdir(exist_ok=True)
    fields = ["table_name", "record_key", "source_row", "rule_id", "action", "detail", "blocked_by"]
    with (OUTPUT_DIR / "issue_log.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(issues)
    save_issue_log_to_sql(issues)

    # ---- Reconciliation: every source row must be accounted for ----
    print("\nReconciliation: source rows = transformed + rejected (direct) + rejected (cascade) + duplicates removed")
    lines = [
        ("patients", n_pat, len(patients), len(rejected_pids), 0, dup_patients),
        ("encounters", n_enc, len(encounters), enc_counts["rejected_direct"], enc_counts["rejected_cascade"], 0),
        ("conditions", n_cond, len(conditions), cond_counts["rejected_direct"],
         cond_counts["rejected_cascade"], cond_counts["duplicates_removed"]),
    ]
    print(f"  {'Table':<11}{'Source':>7}{'Transformed':>13}{'Rej-direct':>12}{'Rej-cascade':>13}{'Dupes':>7}   Result")
    for name, src, ok, rd, rc, dup in lines:
        result = "PASS" if src == ok + rd + rc + dup else "FAIL"
        print(f"  {name:<11}{src:>7}{ok:>13}{rd:>12}{rc:>13}{dup:>7}   {result}")

    by_action = {}
    for i in issues:
        by_action[i["action"]] = by_action.get(i["action"], 0) + 1
    print(f"\nIssue log: {len(issues)} entries " + str(dict(sorted(by_action.items())))
          + " -> output/issue_log.csv and etl.issue_log")
    print(f"FHIR bundles written: {n_bundles} (one per accepted patient) -> {BUNDLE_DIR}")

    check_answer_key()


if __name__ == "__main__":
    main()
