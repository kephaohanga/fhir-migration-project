"""
phase2_clinic_structure.py
--------------------------
Phase 2, step 2: the clinic structure.

  Organization      1   the clinic (a business)            - reference data, defined below
  Location          1   the clinic's single site            - reference data, defined below
  PractitionerRole  1 per migrated provider (184)           - from cur.providers

Design decision (one clinic): Synthea gives every provider their own
organization (184 providers, 184 organizations, verified in SQL). These are
simulator artifacts, so every provider is linked to ONE clinic. The legacy
ORGANIZATION values stay in stg/cur for audit and are not migrated.

Why PractitionerRole exists: Practitioner says WHO a provider is (name, NPI).
PractitionerRole says WHERE and AS WHAT they work (this clinic, this site,
this specialty). Keeping the two apart means a provider is never duplicated
when they work in more than one place.

Stages:
  1. READ       migrated practitioners (etl.practitioner_crosswalk) + cur.providers
  2. CLINIC     build and load Organization + Location in one transaction (conditional update)
  3. TRANSFORM  one PractitionerRole per provider, specialty mapped to the NUCC taxonomy
                using docs/specialty_map.csv
  4. LOAD       roles in bundles of 50 (conditional update: safe to re-run)
  5. EXTRACT    read Organization, Location and PractitionerRole back by project tag

Rules:
  R-RL01 Reject  provider was not migrated as a Practitioner (fix the provider first)
  R-RL02 Flag    SPECIALITY blank - role loaded without a specialty
  R-RL03 Flag    SPECIALITY not in docs/specialty_map.csv - loaded as text only (add it to the map)

Run AFTER src\\phase2_practitioner.py. From the project folder:
  python src\\phase2_clinic_structure.py --limit 1   <- clinic + ONE bundle of roles (a test)
  python src\\phase2_clinic_structure.py             <- everything
Then open sql\\21_clinic_structure_reconciliation.sql in SSMS and press F5.
"""

import argparse
import csv
import re
import time
import uuid
from pathlib import Path

# Shared settings and helpers from the Practitioner step (same src/ folder),
# so both scripts talk to the same server and database in the same way.
from phase2_practitioner import (BUNDLE_SIZE, FHIR_SERVER, PAGE_SIZE, PAUSE_BETWEEN_BUNDLES,
                                 PROJECT_TAG, TAG_SEARCH, connect, get_json, post_bundle,
                                 save_table, v, write_csv)

SPECIALTY_MAP = Path("docs/specialty_map.csv")
OUTPUT_DIR = Path("output/phase2_clinic_structure")

SYS_ORG = "urn:fhirmigration:clinic-id"
SYS_LOC = "urn:fhirmigration:location-id"
SYS_ROLE = "urn:fhirmigration:practitioner-role-id"
SYS_NUCC = "http://nucc.org/provider-taxonomy"          # US provider specialty codes (NUCC taxonomy)
SYS_ORG_TYPE = "http://terminology.hl7.org/CodeSystem/organization-type"
SYS_PHYSICAL = "http://terminology.hl7.org/CodeSystem/location-physical-type"

# The clinic itself. FICTIONAL values: this is synthetic data, so no real
# clinic name, address or phone number is used. 555-01xx numbers are reserved
# for fiction.
CLINIC = {
    "org_id": "CLINIC-001",
    "loc_id": "LOC-001",
    "name": "Synthetic Community Clinic",
    "location_name": "Synthetic Community Clinic - Main Site",
    "phone": "555-0100",
    "address_line": "100 Demo Street",
    "city": "Raleigh",
    "state": "NC",
    "postal_code": "27601",
}

ROLE_FIELDS = ["practitioner_ref", "organization_ref", "location_ref",
               "specialty_code", "specialty_text", "active"]
ISSUE_COLS = ["record_key", "source_row", "rule_id", "action", "detail"]


# ---------------------------------------------------------------------------
# Flatteners: turn a FHIR resource into simple field -> value pairs.
# The SAME functions are used for what we send (expected) and what the server
# returns (actual), so the reconciliation compares like with like.
# ---------------------------------------------------------------------------
def tf(value):
    return None if value is None else ("true" if value else "false")


def identifier(r, system):
    return next((i.get("value") for i in r.get("identifier", []) if i.get("system") == system), None)


def phone(r):
    return next((t.get("value") for t in r.get("telecom", []) if t.get("system") == "phone"), None)


def address_fields(r):
    a = r.get("address") or {}
    if isinstance(a, list):                      # Organization has a list, Location has one address
        a = a[0] if a else {}
    return {"address_line": (a.get("line") or [None])[0], "city": a.get("city"),
            "state": a.get("state"), "postal_code": a.get("postalCode"), "country": a.get("country")}


def flatten_org(r):
    type_code = next((c.get("code") for t in r.get("type", []) for c in t.get("coding", [])
                      if c.get("system") == SYS_ORG_TYPE), None)
    return {"identifier": identifier(r, SYS_ORG), "active": tf(r.get("active")), "name": r.get("name"),
            "type_code": type_code, "phone": phone(r), **address_fields(r)}


def flatten_location(r):
    physical = next((c.get("code") for c in (r.get("physicalType") or {}).get("coding", [])
                     if c.get("system") == SYS_PHYSICAL), None)
    return {"identifier": identifier(r, SYS_LOC), "status": r.get("status"), "mode": r.get("mode"),
            "name": r.get("name"), "physical_type": physical, "phone": phone(r),
            "managing_organization": (r.get("managingOrganization") or {}).get("reference"),
            **address_fields(r)}


def flatten_role(r):
    spec = (r.get("specialty") or [{}])[0]
    code = next((c.get("code") for c in spec.get("coding", []) if c.get("system") == SYS_NUCC), None)
    return {"practitioner_ref": (r.get("practitioner") or {}).get("reference"),
            "organization_ref": (r.get("organization") or {}).get("reference"),
            "location_ref": ((r.get("location") or [{}])[0]).get("reference"),
            "specialty_code": code, "specialty_text": spec.get("text"),
            "active": tf(r.get("active"))}


def long_rows(resource_type, legacy_id, fields, value_col, fhir_id=None):
    """One row per field: easy to compare in SQL with a single JOIN."""
    return [{"resource_type": resource_type, "fhir_id": fhir_id, "legacy_id": legacy_id,
             "field_name": k, value_col: val} for k, val in fields.items()]


# ---------------------------------------------------------------------------
# Loading helpers
# ---------------------------------------------------------------------------
def put_entry(resource, url):
    """Conditional update: 'PUT the resource whose identifier is X' (create if none)."""
    return {"fullUrl": f"urn:uuid:{uuid.uuid4()}", "resource": resource,
            "request": {"method": "PUT", "url": url}}


def send_bundle(entries, bundle_no, label, load_log):
    started = time.time()
    resp, attempts, error = post_bundle({"resourceType": "Bundle", "type": "transaction", "entry": entries})
    log = {"bundle_no": bundle_no, "label": label, "entries": len(entries), "attempts": attempts,
           "seconds": round(time.time() - started, 2),
           "http_status": resp.status_code if resp is not None else None, "error": error}
    reply = resp.json().get("entry", []) if resp is not None and resp.status_code == 200 else []
    if len(reply) != len(entries):
        log["outcome"] = "FAILED"
        if resp is not None and not log["error"]:
            log["error"] = resp.text[:450]
        print(f"   bundle {bundle_no} ({label}): FAILED ({log['http_status']}) {str(log['error'])[:150]}")
        load_log.append(log)
        return None
    log["outcome"] = "OK"
    load_log.append(log)
    print(f"   bundle {bundle_no} ({label}): OK  {len(entries)} resources  {log['seconds']}s")
    return reply


def parse_reply(r, resource_type):
    response = r.get("response", {})
    m = re.search(rf"{resource_type}/([^/]+)", response.get("location", ""))
    return (m.group(1) if m else None), response.get("status", "")


# ---------------------------------------------------------------------------
# 1. READ
# ---------------------------------------------------------------------------
def read_sources():
    conn = connect()
    cur = conn.cursor()
    cur.execute("SELECT * FROM cur.providers ORDER BY source_row;")
    cols = [c[0] for c in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    crosswalk = {r[0]: r[1] for r in cur.execute(
        "SELECT legacy_id, fhir_id FROM etl.practitioner_crosswalk WHERE fhir_id IS NOT NULL;").fetchall()}
    conn.close()

    # One row per provider Id (the first copy). Duplicates and blank Ids were
    # already handled - and logged - by the Practitioner step.
    providers, seen = [], set()
    for r in rows:
        pid = v(r, "Id")
        if pid and pid not in seen:
            seen.add(pid)
            providers.append(r)

    print("1. READ")
    print(f"   cur.providers rows {len(rows)} -> distinct providers {len(providers)}")
    print(f"   Migrated practitioners (crosswalk): {len(crosswalk)}")
    if "SPECIALITY" not in cols:
        print("   WARNING: no SPECIALITY column in cur.providers - every role will be flagged R-RL02")
    return providers, crosswalk


def read_specialty_map():
    """{LEGACY TEXT (upper case): (NUCC code, display)}"""
    if not SPECIALTY_MAP.exists():
        print(f"   WARNING: {SPECIALTY_MAP} not found - specialties will be loaded as text only")
        return {}
    with SPECIALTY_MAP.open(encoding="utf-8-sig", newline="") as f:
        return {r["legacy_specialty"].strip().upper(): (r["nucc_code"].strip(), r["nucc_display"].strip())
                for r in csv.DictReader(f) if r.get("legacy_specialty") and r.get("nucc_code")}


# ---------------------------------------------------------------------------
# 2. CLINIC: Organization + Location
# ---------------------------------------------------------------------------
def clinic_address(as_list):
    a = {"use": "work", "line": [CLINIC["address_line"]], "city": CLINIC["city"],
         "state": CLINIC["state"], "postalCode": CLINIC["postal_code"], "country": "US"}
    return [a] if as_list else a


def load_clinic(load_log, crosswalk_rows, expected_rows):
    print("\n2. CLINIC   Organization + Location")
    org = {
        "resourceType": "Organization",
        "meta": {"tag": [PROJECT_TAG]},
        "identifier": [{"system": SYS_ORG, "value": CLINIC["org_id"]}],
        "active": True,
        "type": [{"coding": [{"system": SYS_ORG_TYPE, "code": "prov", "display": "Healthcare Provider"}]}],
        "name": CLINIC["name"],
        "telecom": [{"system": "phone", "value": CLINIC["phone"], "use": "work"}],
        "address": clinic_address(as_list=True),
    }
    org_entry = put_entry(org, f"Organization?identifier={SYS_ORG}|{CLINIC['org_id']}")

    location = {
        "resourceType": "Location",
        "meta": {"tag": [PROJECT_TAG]},
        "identifier": [{"system": SYS_LOC, "value": CLINIC["loc_id"]}],
        "status": "active",
        "name": CLINIC["location_name"],
        "mode": "instance",            # a real, specific place (not a kind of place)
        "physicalType": {"coding": [{"system": SYS_PHYSICAL, "code": "bu", "display": "Building"}]},
        "telecom": [{"system": "phone", "value": CLINIC["phone"], "use": "work"}],
        "address": clinic_address(as_list=False),
        # Points to the Organization in the SAME bundle by its temporary fullUrl;
        # the server swaps in the real "Organization/<id>" when it saves.
        "managingOrganization": {"reference": org_entry["fullUrl"]},
    }
    loc_entry = put_entry(location, f"Location?identifier={SYS_LOC}|{CLINIC['loc_id']}")

    reply = send_bundle([org_entry, loc_entry], 1, "clinic", load_log)
    if reply is None:
        raise SystemExit("Organization/Location did not load, so no roles were sent. Fix the error and re-run.")
    org_id, org_status = parse_reply(reply[0], "Organization")
    loc_id, loc_status = parse_reply(reply[1], "Location")
    print(f"   Organization/{org_id} ({org_status})   Location/{loc_id} ({loc_status})")

    crosswalk_rows += [
        {"resource_type": "Organization", "legacy_id": CLINIC["org_id"], "fhir_id": org_id, "server_status": org_status},
        {"resource_type": "Location", "legacy_id": CLINIC["loc_id"], "fhir_id": loc_id, "server_status": loc_status},
    ]
    expected_rows += long_rows("Organization", CLINIC["org_id"], flatten_org(org), "expected_value")
    loc_expected = flatten_location(location)
    loc_expected["managing_organization"] = f"Organization/{org_id}"   # real id, not the temporary one
    expected_rows += long_rows("Location", CLINIC["loc_id"], loc_expected, "expected_value")
    return f"Organization/{org_id}", f"Location/{loc_id}"


# ---------------------------------------------------------------------------
# 3. TRANSFORM: PractitionerRole
# ---------------------------------------------------------------------------
def build_roles(providers, crosswalk, specialty_map, org_ref, loc_ref, expected_rows):
    issues, roles = [], []

    def log(key, src, rule, action, detail):
        issues.append({"record_key": key, "source_row": src, "rule_id": rule, "action": action, "detail": detail})

    for p in providers:
        pid, src = v(p, "Id"), p["source_row"]
        fhir_id = crosswalk.get(pid)
        if fhir_id is None:                                                # R-RL01
            log(pid, src, "R-RL01", "Reject", "Provider was not migrated as a Practitioner - fix the provider first")
            continue

        role = {
            "resourceType": "PractitionerRole",
            "meta": {"tag": [PROJECT_TAG]},
            "identifier": [{"system": SYS_ROLE, "value": pid}],
            "active": True,
            "practitioner": {"reference": f"Practitioner/{fhir_id}"},
            "organization": {"reference": org_ref},
            "location": [{"reference": loc_ref}],
        }
        text = v(p, "SPECIALITY")
        if text is None:                                                   # R-RL02
            log(pid, src, "R-RL02", "Flag", "SPECIALITY is blank - role loaded without a specialty")
        else:
            specialty = {"text": text}                                     # the legacy wording, as entered
            mapped = specialty_map.get(text.upper())
            if mapped:
                specialty["coding"] = [{"system": SYS_NUCC, "code": mapped[0], "display": mapped[1]}]
            else:                                                          # R-RL03
                log(pid, src, "R-RL03", "Flag",
                    f"SPECIALITY '{text}' is not in {SPECIALTY_MAP.as_posix()} - loaded as text only")
            role["specialty"] = [specialty]
        roles.append((pid, role))
        expected_rows += long_rows("PractitionerRole", pid, flatten_role(role), "expected_value")

    rejected = sum(1 for i in issues if i["action"] == "Reject")
    ok = len(providers) == len(roles) + rejected
    print("\n3. TRANSFORM   PractitionerRole")
    print(f"   Distinct providers {len(providers)} = roles built {len(roles)} + rejected {rejected}   "
          f"{'PASS' if ok else 'FAIL'}")
    for rule in ("R-RL02", "R-RL03"):
        n = sum(1 for i in issues if i["rule_id"] == rule)
        if n:
            print(f"   {rule} Flag: {n} roles")
    unmapped = sorted({i["detail"].split("'")[1] for i in issues if i["rule_id"] == "R-RL03"})
    if unmapped:
        print(f"   Specialties to add to {SPECIALTY_MAP}: {', '.join(unmapped)}")
    coded = sum(1 for _, r in roles if r.get("specialty", [{}])[0].get("coding"))
    print(f"   Specialty coded (NUCC): {coded} of {len(roles)} roles")
    return roles, issues


# ---------------------------------------------------------------------------
# 4. LOAD
# ---------------------------------------------------------------------------
def load_roles(roles, load_log, crosswalk_rows, limit=None):
    chunks = [roles[i:i + BUNDLE_SIZE] for i in range(0, len(roles), BUNDLE_SIZE)]
    if limit:
        chunks = chunks[:limit]
    print(f"\n4. LOAD   {len(chunks)} bundle(s) of roles to {FHIR_SERVER}")
    sent = 0
    for n, chunk in enumerate(chunks, 2):                 # bundle 1 was the clinic
        entries = [put_entry(role, f"PractitionerRole?identifier={SYS_ROLE}|{pid}") for pid, role in chunk]
        sent += len(entries)
        reply = send_bundle(entries, n, "roles", load_log)
        if reply is not None:
            for (pid, _), r in zip(chunk, reply):
                fhir_id, status = parse_reply(r, "PractitionerRole")
                crosswalk_rows.append({"resource_type": "PractitionerRole", "legacy_id": pid,
                                       "fhir_id": fhir_id, "server_status": status})
        time.sleep(PAUSE_BETWEEN_BUNDLES)

    role_rows = [c for c in crosswalk_rows if c["resource_type"] == "PractitionerRole"]
    confirmed = sum(1 for c in role_rows if c["fhir_id"])
    created = sum(1 for c in role_rows if c["server_status"].startswith("201"))
    updated = sum(1 for c in role_rows if c["server_status"].startswith("200"))
    print(f"   Sent {sent}, confirmed {confirmed} ({created} created, {updated} updated)   "
          f"{'PASS' if confirmed == sent else 'FAIL'}")


# ---------------------------------------------------------------------------
# 5. EXTRACT
# ---------------------------------------------------------------------------
def search_all(resource_type):
    resources = []
    bundle = get_json(f"{FHIR_SERVER}/{resource_type}", params={"_tag": TAG_SEARCH, "_count": PAGE_SIZE})
    while True:
        resources += [e["resource"] for e in bundle.get("entry", [])
                      if e.get("resource", {}).get("resourceType") == resource_type]
        next_url = next((l["url"] for l in bundle.get("link", []) if l.get("relation") == "next"), None)
        if not next_url:
            return resources
        bundle = get_json(next_url)


def extract(crosswalk_rows):
    print("\n5. EXTRACT")
    actual = []
    for resource_type, flatten, system in (("Organization", flatten_org, SYS_ORG),
                                           ("Location", flatten_location, SYS_LOC),
                                           ("PractitionerRole", flatten_role, SYS_ROLE)):
        found = search_all(resource_type)
        for r in found:
            actual += long_rows(resource_type, identifier(r, system), flatten(r), "actual_value", r.get("id"))
        expected_ids = {c["fhir_id"] for c in crosswalk_rows if c["resource_type"] == resource_type and c["fhir_id"]}
        on_server = {r.get("id") for r in found}
        missing = expected_ids - on_server
        print(f"   {resource_type:<17} expected {len(expected_ids):>4}, on server {len(on_server):>4}, "
              f"missing {len(missing)}   {'PASS' if not missing else 'FAIL'}")
    columns = ["resource_type", "fhir_id", "legacy_id", "field_name", "actual_value"]
    save_table("tgt.clinic_resources", actual, columns)
    write_csv(OUTPUT_DIR / "target_clinic_resources.csv", actual, columns)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Phase 2: Organization, Location, PractitionerRole")
    parser.add_argument("--limit", type=int, help="only send the first N bundles of roles (try 1 first)")
    args = parser.parse_args()

    providers, practitioner_crosswalk = read_sources()
    specialty_map = read_specialty_map()
    load_log, crosswalk_rows, expected_rows = [], [], []

    org_ref, loc_ref = load_clinic(load_log, crosswalk_rows, expected_rows)
    roles, issues = build_roles(providers, practitioner_crosswalk, specialty_map, org_ref, loc_ref, expected_rows)
    save_table("etl.clinic_issue_log", issues, ISSUE_COLS)
    write_csv(OUTPUT_DIR / "issue_log.csv", issues, ISSUE_COLS)

    load_roles(roles, load_log, crosswalk_rows, args.limit)
    save_table("etl.clinic_crosswalk", crosswalk_rows, ["resource_type", "legacy_id", "fhir_id", "server_status"])
    save_table("etl.clinic_expected", expected_rows, ["resource_type", "legacy_id", "field_name", "expected_value"])
    save_table("etl.clinic_load_log", load_log,
               ["bundle_no", "label", "entries", "http_status", "outcome", "attempts", "seconds", "error"])
    write_csv(OUTPUT_DIR / "crosswalk.csv", crosswalk_rows, ["resource_type", "legacy_id", "fhir_id", "server_status"])

    extract(crosswalk_rows)
    print("\nNext: open sql\\21_clinic_structure_reconciliation.sql in SSMS and press F5.")


if __name__ == "__main__":
    main()
