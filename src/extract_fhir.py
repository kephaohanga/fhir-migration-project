"""
extract_fhir.py
---------------
Step 9: Read everything back FROM the FHIR server (REST API GET) and land it in
SQL Server "target" tables, so it can be compared with staging field by field.

Why: Step 8 proved the server ACCEPTED every record. This step proves what the
server actually STORED - "trust, but verify".

How:
  - search each resource type by our project tag (only OUR records come back)
  - follow the Bundle's "next" links until every page has been read (pagination)
  - flatten each FHIR resource into one table row
  - run integrity checks against the crosswalk from Step 8

Run from the project folder:  python src\\extract_fhir.py
Outputs: SQL tables tgt.patients, tgt.encounters, tgt.conditions
         output/target/*.csv (same data as CSV)
"""

import argparse
import csv
import re
import time
from pathlib import Path

import pyodbc
import requests

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
DEFAULT_SERVER = "https://hapi.fhir.org/baseR4"
PROJECT_TAG = "urn:fhirmigration:project|kepha-migration-2026"   # system|code
PAGE_SIZE = 200
CROSSWALK_CSV = Path("output/fhir_crosswalk.csv")
TARGET_DIR = Path("output/target")
SQL_SERVER = "localhost"
DRIVER = "ODBC Driver 17 for SQL Server"
DATABASE = "FhirMigration"
HEADERS = {"Accept": "application/fhir+json"}
RETRY_WAITS = [5, 15, 45]

SYS_PATIENT = "urn:fhirmigration:legacy-patient-id"
SYS_ENCOUNTER = "urn:fhirmigration:legacy-encounter-id"
SYS_CONDITION = "urn:fhirmigration:legacy-condition-key"


# ---------------------------------------------------------------------------
# Reading from the server
# ---------------------------------------------------------------------------
def get_json(url, params=None):
    """GET with automatic retries when the server is busy."""
    last = ""
    for wait in [0] + RETRY_WAITS:
        if wait:
            print(f"      server busy ({last}) - retrying in {wait}s ...")
            time.sleep(wait)
        try:
            resp = requests.get(url, params=params, headers=HEADERS, timeout=120)
        except (requests.ConnectionError, requests.Timeout) as e:
            last = type(e).__name__
            continue
        if resp.status_code in (429, 500, 502, 503, 504):
            last = f"HTTP {resp.status_code}"
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError(f"Server did not respond after retries: {last}")


def search_all(server, resource_type):
    """Search one resource type by our tag and follow 'next' links through every page."""
    resources, page = [], 1
    bundle = get_json(f"{server}/{resource_type}",
                      params={"_tag": PROJECT_TAG, "_count": PAGE_SIZE})
    while True:
        batch = [e["resource"] for e in bundle.get("entry", [])
                 if e.get("resource", {}).get("resourceType") == resource_type]
        resources.extend(batch)
        print(f"    page {page}: {len(batch)} {resource_type} resources (running total {len(resources)})")
        next_url = next((l["url"] for l in bundle.get("link", []) if l.get("relation") == "next"), None)
        if not next_url:
            return resources
        page += 1
        bundle = get_json(next_url)


# ---------------------------------------------------------------------------
# Flattening: one FHIR resource -> one flat row
# ---------------------------------------------------------------------------
def ref_id(reference):
    """'Patient/123' or 'https://.../Patient/123' -> '123'."""
    m = re.search(r"(?:Patient|Encounter)/([^/]+)$", reference or "")
    return m.group(1) if m else None


def identifier(resource, system):
    return next((i.get("value") for i in resource.get("identifier", []) if i.get("system") == system), None)


def first_coding(concept):
    codings = (concept or {}).get("coding", [])
    return codings[0] if codings else {}


def flatten_patient(r):
    names = r.get("name", [])
    official = next((n for n in names if n.get("use") == "official"), names[0] if names else {})
    maiden = next((n for n in names if n.get("use") == "maiden"), {})
    given = official.get("given", [])
    address = (r.get("address") or [{}])[0]
    return {
        "fhir_id": r.get("id"),
        "legacy_id": identifier(r, SYS_PATIENT),
        "prefix": (official.get("prefix") or [None])[0],
        "given_first": given[0] if len(given) > 0 else None,
        "given_middle": given[1] if len(given) > 1 else None,
        "family": official.get("family"),
        "suffix": (official.get("suffix") or [None])[0],
        "maiden_family": maiden.get("family"),
        "gender": r.get("gender"),
        "birth_date": r.get("birthDate"),
        "deceased_datetime": r.get("deceasedDateTime"),
        "marital_code": first_coding(r.get("maritalStatus")).get("code"),
        "address_line": (address.get("line") or [None])[0],
        "city": address.get("city"),
        "district": address.get("district"),
        "state": address.get("state"),
        "postal_code": address.get("postalCode"),
        "country": address.get("country"),
    }


def flatten_encounter(r):
    reason = (r.get("reasonCode") or [{}])[0]
    return {
        "fhir_id": r.get("id"),
        "legacy_id": identifier(r, SYS_ENCOUNTER),
        "patient_fhir_id": ref_id((r.get("subject") or {}).get("reference")),
        "status": r.get("status"),
        "class_code": (r.get("class") or {}).get("code"),
        "type_code": first_coding((r.get("type") or [{}])[0]).get("code"),
        "type_display": first_coding((r.get("type") or [{}])[0]).get("display"),
        "period_start": (r.get("period") or {}).get("start"),
        "period_end": (r.get("period") or {}).get("end"),
        "reason_code": first_coding(reason).get("code"),
    }


def flatten_condition(r):
    code = first_coding(r.get("code"))
    return {
        "fhir_id": r.get("id"),
        "legacy_key": identifier(r, SYS_CONDITION),
        "patient_fhir_id": ref_id((r.get("subject") or {}).get("reference")),
        "encounter_fhir_id": ref_id((r.get("encounter") or {}).get("reference")),
        "code_system": code.get("system"),
        "code": code.get("code"),
        "code_display": code.get("display"),
        "clinical_status": first_coding(r.get("clinicalStatus")).get("code"),
        "verification_status": first_coding(r.get("verificationStatus")).get("code"),
        "onset_datetime": r.get("onsetDateTime"),
        "abatement_datetime": r.get("abatementDateTime"),
    }


# ---------------------------------------------------------------------------
# Saving: CSV files + SQL tables in the "tgt" schema
# ---------------------------------------------------------------------------
def save_csv(name, rows):
    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    if not rows:
        return
    with (TARGET_DIR / f"{name}.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def save_to_sql(tables):
    conn = pyodbc.connect(
        f"DRIVER={{{DRIVER}}};SERVER={SQL_SERVER};DATABASE={DATABASE};"
        "Trusted_Connection=yes;TrustServerCertificate=yes;", autocommit=True)
    cur = conn.cursor()
    cur.execute("IF SCHEMA_ID('tgt') IS NULL EXEC('CREATE SCHEMA tgt');")
    cur.fast_executemany = True
    for name, rows in tables.items():
        cur.execute(f"DROP TABLE IF EXISTS tgt.{name};")
        if not rows:
            continue
        cols = list(rows[0].keys())
        col_defs = ", ".join(f"[{c}] NVARCHAR(500) NULL" for c in cols)
        cur.execute(f"CREATE TABLE tgt.{name} ({col_defs}, "
                    f"[extracted_at] DATETIME2 DEFAULT SYSUTCDATETIME());")
        cur.executemany(
            f"INSERT INTO tgt.{name} ({', '.join(f'[{c}]' for c in cols)}) "
            f"VALUES ({', '.join('?' for _ in cols)});",
            [[r[c] for c in cols] for r in rows])
    conn.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Read migrated resources back from the FHIR server")
    parser.add_argument("--server", default=DEFAULT_SERVER)
    args = parser.parse_args()

    if not CROSSWALK_CSV.exists():
        print(f"{CROSSWALK_CSV} not found. Run src\\load_fhir.py first.")
        return
    with CROSSWALK_CSV.open(encoding="utf-8") as f:
        crosswalk = list(csv.DictReader(f))
    expected = {}
    for row in crosswalk:
        if row["fhir_id"]:
            expected.setdefault(row["resource_type"], set()).add(row["fhir_id"])

    print(f"Reading resources tagged {PROJECT_TAG} from {args.server}\n")
    flatten = {"Patient": flatten_patient, "Encounter": flatten_encounter, "Condition": flatten_condition}
    table_names = {"Patient": "patients", "Encounter": "encounters", "Condition": "conditions"}
    tables = {}
    for rtype in ("Patient", "Encounter", "Condition"):
        print(f"  {rtype}:")
        resources = search_all(args.server, rtype)
        tables[table_names[rtype]] = [flatten[rtype](r) for r in resources]

    for name, rows in tables.items():
        save_csv(name, rows)
    save_to_sql(tables)

    # ---- Check 1: does the server hold exactly what the crosswalk says it should? ----
    print("\nCheck 1 - Completeness: crosswalk (Step 8) vs. what the server returns")
    print(f"  {'Resource':<11}{'Expected':>9}{'On server':>11}{'Missing':>9}{'Extra':>7}   Result")
    all_ok = True
    for rtype, name in table_names.items():
        on_server = {r["fhir_id"] for r in tables[name]}
        exp = expected.get(rtype, set())
        missing, extra = exp - on_server, on_server - exp
        ok = not missing and not extra
        all_ok &= ok
        print(f"  {rtype:<11}{len(exp):>9}{len(on_server):>11}{len(missing):>9}{len(extra):>7}   "
              f"{'PASS' if ok else 'FAIL'}")
        if missing:
            print(f"      first missing ids: {sorted(missing)[:5]}")

    # ---- Check 2: do all the links between records point to records we migrated? ----
    pat_ids = {r["fhir_id"] for r in tables["patients"]}
    enc_ids = {r["fhir_id"] for r in tables["encounters"]}
    checks = [
        ("Encounter -> Patient", sum(1 for r in tables["encounters"] if r["patient_fhir_id"] not in pat_ids)),
        ("Condition -> Patient", sum(1 for r in tables["conditions"] if r["patient_fhir_id"] not in pat_ids)),
        ("Condition -> Encounter", sum(1 for r in tables["conditions"]
                                       if r["encounter_fhir_id"] and r["encounter_fhir_id"] not in enc_ids)),
    ]
    no_link = sum(1 for r in tables["conditions"] if not r["encounter_fhir_id"])
    print("\nCheck 2 - Referential integrity: every link points to a migrated record")
    for label, broken in checks:
        all_ok &= broken == 0
        print(f"  {label:<24} broken links: {broken:<5} {'PASS' if broken == 0 else 'FAIL'}")
    print(f"  (Conditions loaded without an encounter link, by design - rule R-C03: {no_link})")

    print(f"\nTarget tables written: tgt.patients ({len(tables['patients'])}), "
          f"tgt.encounters ({len(tables['encounters'])}), tgt.conditions ({len(tables['conditions'])})")
    print("CSV copies -> output/target/")
    print("\nOVERALL: " + ("PASS - the server holds exactly what was sent, and every link is intact."
                           if all_ok else "ATTENTION - see FAIL lines above."))


if __name__ == "__main__":
    main()
