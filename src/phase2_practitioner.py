"""
phase2_practitioner.py
----------------------
Phase 2, step 1: migrate legacy providers to FHIR Practitioner resources.

Two files are used, as in a real clinic:
  data/legacy_dirty/providers.csv  the LEGACY EHR EXPORT being migrated
                                   (run src\\plant_provider_errors.py first)
  data/legacy/providers.csv        the CREDENTIALING ROSTER: the source of truth
                                   for provider names, used to catch misspellings

It follows the same pattern as Phase 1, in five stages:
  1. STAGE     legacy export -> SQL table stg.providers (raw, all text, never edited)
  2. CURATE    cur.providers = stg.providers + staff corrections from
               remediation/corrections.csv (runs src\\apply_corrections.py)
  3. TRANSFORM validate each cur.providers row (Reject / Fix / Flag) and build FHIR Practitioners
  4. LOAD      transaction bundles of 50 with conditional UPDATE: creates a practitioner
               the first time, updates it on later runs (safe to re-run)
  5. EXTRACT   read every Practitioner back by project tag (following pagination)

Then open sql/20_practitioner_reconciliation.sql in SSMS and press F5.

This script writes:
  stg.providers, cur.providers, etl.corrections, etl.practitioner_issue_log,
  etl.practitioner_expected, etl.practitioner_crosswalk, etl.practitioner_load_log,
  tgt.practitioners and files in output/phase2_practitioner/
  (apply_corrections.py also rebuilds the Phase 1 cur.* tables from the same
   inputs, so their contents do not change.)

Remediation (rejected or flagged providers):
  python src\\export_worklist.py  -> provider rows appear on remediation/worklist.csv
  add the filled-in provider rows to remediation/corrections.csv, then run this script again

Run from the project folder:
  python src\\phase2_practitioner.py --limit 1   <- send ONE bundle first (a test)
  python src\\phase2_practitioner.py             <- send everything
"""

import argparse
import csv
import difflib
import json
import re
import time
import uuid
from pathlib import Path

import pandas as pd
import pyodbc
import requests

import apply_corrections          # same folder (src/): builds the curated layer

# ---------------------------------------------------------------------------
# Settings (same as Phase 1)
# ---------------------------------------------------------------------------
FHIR_SERVER = "https://hapi.fhir.org/baseR4"
SQL_SERVER = "localhost"
DRIVER = "ODBC Driver 17 for SQL Server"
DATABASE = "FhirMigration"

SOURCE_FILE = Path("data/legacy_dirty/providers.csv")   # legacy EHR export
ROSTER_FILE = Path("data/legacy/providers.csv")         # credentialing roster
ANSWER_KEY = Path("docs/planted_provider_errors_answer_key.csv")
OUTPUT_DIR = Path("output/phase2_practitioner")

# How alike a legacy name must be to the roster name to count as a simple
# misspelling (1.0 = identical). Below this, it may be a different person.
NAME_SIMILARITY_THRESHOLD = 0.85

SYS_PRACTITIONER = "urn:fhirmigration:legacy-practitioner-id"
SYS_NPI = "http://hl7.org/fhir/sid/us-npi"     # official FHIR system for the National Provider Identifier
PROJECT_TAG = {"system": "urn:fhirmigration:project", "code": "kepha-migration-2026"}
TAG_SEARCH = "urn:fhirmigration:project|kepha-migration-2026"

BUNDLE_SIZE = 50                       # practitioners per transaction bundle
PAGE_SIZE = 200
HEADERS = {"Content-Type": "application/fhir+json", "Accept": "application/fhir+json"}
TIMEOUT_SECONDS = 300
RETRY_WAITS = [5, 15, 45]
RETRY_STATUS = {429, 500, 502, 503, 504}
PAUSE_BETWEEN_BUNDLES = 1.0

GENDER_MAP = {"M": "male", "F": "female"}

# Columns compared field by field in the reconciliation
COMPARE_FIELDS = ["npi", "prefix", "given_first", "given_other", "family", "name_text", "gender",
                  "address_line", "city", "state", "postal_code", "country"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def connect():
    return pyodbc.connect(
        f"DRIVER={{{DRIVER}}};SERVER={SQL_SERVER};DATABASE={DATABASE};"
        "Trusted_Connection=yes;TrustServerCertificate=yes;",
        autocommit=True,
    )


def v(row, column):
    """None if missing/blank, otherwise trimmed text."""
    value = row.get(column)
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def save_table(name, rows, columns):
    """(Re)create a SQL table with all-text columns and insert the rows."""
    conn = connect()
    cur = conn.cursor()
    schema = name.split(".")[0]
    cur.execute(f"IF SCHEMA_ID('{schema}') IS NULL EXEC('CREATE SCHEMA {schema}');")
    cur.execute(f"DROP TABLE IF EXISTS {name};")
    col_defs = ", ".join(f"[{c}] NVARCHAR(500) NULL" for c in columns)
    cur.execute(f"CREATE TABLE {name} ({col_defs}, [saved_at] DATETIME2 DEFAULT SYSUTCDATETIME());")
    if rows:
        cur.fast_executemany = True
        cur.executemany(
            f"INSERT INTO {name} ({', '.join(f'[{c}]' for c in columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)});",
            [[None if r.get(c) is None else str(r.get(c)) for c in columns] for r in rows],
        )
    conn.close()


def write_csv(path, rows, columns):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows([{c: r.get(c) for c in columns} for r in rows])


# ---------------------------------------------------------------------------
# 1. STAGE
# ---------------------------------------------------------------------------
def stage():
    if not ROSTER_FILE.exists():
        raise SystemExit(f"{ROSTER_FILE} not found. Copy providers.csv from Synthea's output\\csv folder.")
    if not SOURCE_FILE.exists():
        raise SystemExit(f"{SOURCE_FILE} not found. Run  python src\\plant_provider_errors.py  first.")
    df = pd.read_csv(SOURCE_FILE, dtype=str, keep_default_na=False)
    columns = list(df.columns)
    rows = []
    for position, values in enumerate(df.itertuples(index=False, name=None)):
        row = {c: (None if x == "" else x) for c, x in zip(columns, values)}
        row["source_row"] = position + 2          # header is line 1
        rows.append(row)

    conn = connect()
    cur = conn.cursor()
    cur.execute("IF SCHEMA_ID('stg') IS NULL EXEC('CREATE SCHEMA stg');")
    cur.execute("DROP TABLE IF EXISTS stg.providers;")
    col_defs = ", ".join(f"[{c}] NVARCHAR(500) NULL" for c in columns)
    cur.execute(f"CREATE TABLE stg.providers ({col_defs}, [source_row] INT NOT NULL, "
                f"[loaded_at] DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME());")
    cur.fast_executemany = True
    cur.executemany(
        f"INSERT INTO stg.providers ({', '.join(f'[{c}]' for c in columns)}, [source_row]) "
        f"VALUES ({', '.join('?' for _ in range(len(columns) + 1))});",
        [[r[c] for c in columns] + [r["source_row"]] for r in rows],
    )
    sql_rows = cur.execute("SELECT COUNT(*) FROM stg.providers;").fetchone()[0]
    conn.close()

    print("1. STAGE")
    print(f"   CSV rows: {len(df)}   stg.providers rows: {sql_rows}   "
          f"{'PASS' if sql_rows == len(df) else 'FAIL'}")
    print(f"   Columns found: {', '.join(columns)}")


# ---------------------------------------------------------------------------
# 2. CURATE
# ---------------------------------------------------------------------------
def curate():
    """Build cur.providers (stg + approved staff corrections) and return its rows."""
    print("\n2. CURATE   staff corrections from remediation/corrections.csv")
    if not apply_corrections.main(show_next=False):
        raise SystemExit("Stopped: fix remediation/corrections.csv, then run this script again.")
    conn = connect()
    cur = conn.cursor()
    cur.execute("SELECT * FROM cur.providers ORDER BY source_row;")
    cols = [c[0] for c in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    conn.close()
    print(f"   cur.providers rows: {len(rows)}")
    return rows


# ---------------------------------------------------------------------------
# 3. TRANSFORM
# ---------------------------------------------------------------------------
def split_name(full):
    """'Dr. Ross213 Mayer370' -> (['Dr.'], ['Ross213'], 'Mayer370').
    Words ending in '.' are prefixes; the last remaining word is the family name."""
    parts = full.split()
    prefix = [p for p in parts if p.endswith(".")]
    words = [p for p in parts if not p.endswith(".")]
    family = words[-1] if words else None
    given = words[:-1]
    return prefix, given, family


def read_roster():
    """{provider Id: name} from the credentialing roster."""
    roster = pd.read_csv(ROSTER_FILE, dtype=str, keep_default_na=False)
    return {r["Id"].strip(): r["NAME"].strip() for _, r in roster.iterrows() if r["Id"].strip()}


def similarity(a, b):
    """0.0 (nothing alike) to 1.0 (identical), ignoring upper/lower case."""
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def transform(rows, roster):
    issues, accepted, expected = [], [], []
    seen = set()
    duplicates = 0

    def log(key, src, rule, action, detail):
        issues.append({"record_key": key, "source_row": src, "rule_id": rule,
                       "action": action, "detail": detail})

    for row in rows:
        pid, src = v(row, "Id"), row["source_row"]

        if pid is None:                                                    # R-PR01
            log(f"row {src}", src, "R-PR01", "Reject", "Provider Id is blank")
            continue
        if pid in seen:                                                    # R-PR04
            duplicates += 1
            log(pid, src, "R-PR04", "Fix", "Duplicate provider row removed (first copy kept)")
            continue
        seen.add(pid)

        # R-PR05 the provider must be on the credentialing roster
        roster_name = roster.get(pid)
        if roster_name is None:
            log(pid, src, "R-PR05", "Reject", "Provider not found on the credentialing roster")
            continue

        # R-PR02 the name must match the roster (the source of truth for names)
        legacy_name = v(row, "NAME")
        full_name = roster_name
        if legacy_name != roster_name:
            score = similarity(legacy_name or "", roster_name)
            if legacy_name and score >= NAME_SIMILARITY_THRESHOLD:
                log(pid, src, "R-PR02", "Fix",
                    f"NAME '{legacy_name}' looks misspelt - corrected to roster name "
                    f"'{roster_name}' (similarity {score:.2f})")
            else:
                log(pid, src, "R-PR02", "Reject",
                    f"NAME '{legacy_name}' does not match roster name '{roster_name}' "
                    f"(similarity {score:.2f}) - possible wrong person, route to Credentialing")
                continue
        prefix, given, family = split_name(full_name)
        if family is None:                                                 # R-PR02
            log(pid, src, "R-PR02", "Reject", f"NAME '{full_name}' has no family name")
            continue

        gender_code = v(row, "GENDER")
        gender = GENDER_MAP.get(gender_code)
        if gender_code and gender is None:                                 # R-PR03
            log(pid, src, "R-PR03", "Flag", f"GENDER '{gender_code}' not M or F - omitted")

        # R-PR06 NPI must be exactly 10 digits
        npi = v(row, "NPI")
        if npi is not None and not re.fullmatch(r"\d{10}", npi):
            log(pid, src, "R-PR06", "Flag", f"NPI '{npi}' is not 10 digits - omitted, send to Credentialing")
            npi = None
        elif npi is None:
            log(pid, src, "R-PR06", "Flag", "NPI is blank - send to Credentialing")

        name = {"use": "official", "text": full_name, "family": family}
        if given:
            name["given"] = given
        if prefix:
            name["prefix"] = prefix

        address = {"use": "work", "country": "US"}
        if v(row, "ADDRESS"):
            address["line"] = [v(row, "ADDRESS")]
        for column, element in (("CITY", "city"), ("STATE", "state"), ("ZIP", "postalCode")):
            if v(row, column):
                address[element] = v(row, column)

        practitioner = {
            "resourceType": "Practitioner",
            "meta": {"tag": [PROJECT_TAG]},
            "identifier": [{"system": SYS_PRACTITIONER, "value": pid}],
            "active": True,
            "name": [name],
            "address": [address],
        }
        if npi:
            practitioner["identifier"].append({"use": "official", "system": SYS_NPI, "value": npi})
        if gender:
            practitioner["gender"] = gender
        accepted.append((pid, practitioner))

        # What we expect the server to hold, flattened the same way the extract does
        expected.append({
            "legacy_id": pid,
            "npi": npi,
            "prefix": prefix[0] if prefix else None,
            "given_first": given[0] if given else None,
            "given_other": " ".join(given[1:]) or None,
            "family": family,
            "name_text": full_name,
            "gender": gender,
            "address_line": address.get("line", [None])[0],
            "city": address.get("city"),
            "state": address.get("state"),
            "postal_code": address.get("postalCode"),
            "country": "US",
        })

    rejected = sum(1 for i in issues if i["action"] == "Reject")
    ok = len(rows) == len(accepted) + rejected + duplicates
    print("\n3. TRANSFORM")
    print(f"   Source rows {len(rows)} = transformed {len(accepted)} + rejected {rejected} "
          f"+ duplicates {duplicates}   {'PASS' if ok else 'FAIL'}")
    print(f"   Issue log: {len(issues)} entries")

    save_table("etl.practitioner_issue_log", issues,
               ["record_key", "source_row", "rule_id", "action", "detail"])
    save_table("etl.practitioner_expected", expected, ["legacy_id"] + COMPARE_FIELDS)
    write_csv(OUTPUT_DIR / "issue_log.csv", issues, ["record_key", "source_row", "rule_id", "action", "detail"])
    check_answer_key(issues)
    return accepted


def staff_corrections():
    """{provider Id: 'Correct' or 'Archive'} for providers staff have already worked."""
    conn = connect()
    rows = conn.cursor().execute(
        "SELECT record_key, disposition FROM etl.corrections WHERE table_name = 'providers';").fetchall()
    conn.close()
    return {r[0]: r[1] for r in rows}


def check_answer_key(issues):
    """Did the rules catch every planted defect, with the expected action?
    A defect that staff have since corrected no longer appears in the issue log,
    so it is reported as caught on an earlier run and then corrected (or archived)."""
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
        print(f"     {p['error_type']:<22} expected {p['expected_rule']} {p['expected_action']:<7} {result}")
    print(f"   Result: {caught} of {len(planted)} planted defects caught")


# ---------------------------------------------------------------------------
# 4. LOAD
# ---------------------------------------------------------------------------
def post_bundle(bundle):
    attempts, last_error = 0, ""
    for wait in [0] + RETRY_WAITS:
        if wait:
            print(f"      server busy/unreachable - retrying in {wait}s ...")
            time.sleep(wait)
        attempts += 1
        try:
            resp = requests.post(FHIR_SERVER, headers=HEADERS, data=json.dumps(bundle),
                                 timeout=TIMEOUT_SECONDS)
        except (requests.ConnectionError, requests.Timeout) as e:
            last_error = f"{type(e).__name__}: {e}"
            continue
        if resp.status_code in RETRY_STATUS:
            last_error = f"HTTP {resp.status_code}"
            continue
        return resp, attempts, ""
    return None, attempts, last_error


def load(accepted, limit=None):
    chunks = [accepted[i:i + BUNDLE_SIZE] for i in range(0, len(accepted), BUNDLE_SIZE)]
    if limit:
        chunks = chunks[:limit]
    print(f"\n4. LOAD   {len(chunks)} bundle(s) to {FHIR_SERVER}")

    crosswalk, load_log = [], []
    sent = 0
    for n, chunk in enumerate(chunks, 1):
        entries = [{
            "fullUrl": f"urn:uuid:{uuid.uuid4()}",
            "resource": prac,
            # Conditional update: "PUT the practitioner whose legacy id is X".
            # No match -> the server creates it (201). One match -> it is updated (200).
            "request": {"method": "PUT",
                        "url": f"Practitioner?identifier={SYS_PRACTITIONER}|{pid}"},
        } for pid, prac in chunk]
        sent += len(entries)
        bundle = {"resourceType": "Bundle", "type": "transaction", "entry": entries}

        started = time.time()
        resp, attempts, error = post_bundle(bundle)
        log = {"bundle_no": n, "entries": len(entries), "attempts": attempts,
               "seconds": round(time.time() - started, 2),
               "http_status": resp.status_code if resp is not None else None, "error": error}

        reply = resp.json().get("entry", []) if resp is not None and resp.status_code == 200 else []
        if len(reply) != len(entries):
            log["outcome"] = "FAILED"
            if resp is not None and not log["error"]:
                log["error"] = resp.text[:450]
            print(f"   bundle {n}: FAILED ({log['http_status']}) {str(log['error'])[:150]}")
            load_log.append(log)
            continue

        for (pid, _), r in zip(chunk, reply):
            status = r.get("response", {}).get("status", "")
            m = re.search(r"Practitioner/([^/]+)", r.get("response", {}).get("location", ""))
            crosswalk.append({"legacy_id": pid, "fhir_id": m.group(1) if m else None,
                              "server_status": status})
        log["outcome"] = "OK"
        load_log.append(log)
        print(f"   bundle {n}: OK  {len(entries)} practitioners  {log['seconds']}s")
        time.sleep(PAUSE_BETWEEN_BUNDLES)

    confirmed = sum(1 for c in crosswalk if c["fhir_id"])
    created = sum(1 for c in crosswalk if c["server_status"].startswith("201"))
    updated = sum(1 for c in crosswalk if c["server_status"].startswith("200"))
    print(f"   Sent {sent}, confirmed {confirmed} ({created} created, {updated} updated)   "
          f"{'PASS' if confirmed == sent else 'FAIL'}")

    save_table("etl.practitioner_crosswalk", crosswalk, ["legacy_id", "fhir_id", "server_status"])
    save_table("etl.practitioner_load_log", load_log,
               ["bundle_no", "entries", "http_status", "outcome", "attempts", "seconds", "error"])
    write_csv(OUTPUT_DIR / "crosswalk.csv", crosswalk, ["legacy_id", "fhir_id", "server_status"])
    return crosswalk


# ---------------------------------------------------------------------------
# 5. EXTRACT
# ---------------------------------------------------------------------------
def get_json(url, params=None):
    last = ""
    for wait in [0] + RETRY_WAITS:
        if wait:
            print(f"      server busy ({last}) - retrying in {wait}s ...")
            time.sleep(wait)
        try:
            resp = requests.get(url, params=params, headers={"Accept": "application/fhir+json"}, timeout=120)
        except (requests.ConnectionError, requests.Timeout) as e:
            last = type(e).__name__
            continue
        if resp.status_code in RETRY_STATUS:
            last = f"HTTP {resp.status_code}"
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError(f"Server did not respond after retries: {last}")


def flatten(r):
    name = next((n for n in r.get("name", []) if n.get("use") == "official"),
                (r.get("name") or [{}])[0])
    given = name.get("given", [])
    address = (r.get("address") or [{}])[0]
    legacy = next((i.get("value") for i in r.get("identifier", [])
                   if i.get("system") == SYS_PRACTITIONER), None)
    return {
        "fhir_id": r.get("id"),
        "legacy_id": legacy,
        "npi": next((i.get("value") for i in r.get("identifier", []) if i.get("system") == SYS_NPI), None),
        "prefix": (name.get("prefix") or [None])[0],
        "given_first": given[0] if given else None,
        "given_other": " ".join(given[1:]) or None,
        "family": name.get("family"),
        "name_text": name.get("text"),
        "gender": r.get("gender"),
        "address_line": (address.get("line") or [None])[0],
        "city": address.get("city"),
        "state": address.get("state"),
        "postal_code": address.get("postalCode"),
        "country": address.get("country"),
    }


def extract(crosswalk):
    print("\n5. EXTRACT")
    resources, page = [], 1
    bundle = get_json(f"{FHIR_SERVER}/Practitioner", params={"_tag": TAG_SEARCH, "_count": PAGE_SIZE})
    while True:
        batch = [e["resource"] for e in bundle.get("entry", [])
                 if e.get("resource", {}).get("resourceType") == "Practitioner"]
        resources.extend(batch)
        print(f"   page {page}: {len(batch)} (running total {len(resources)})")
        next_url = next((l["url"] for l in bundle.get("link", []) if l.get("relation") == "next"), None)
        if not next_url:
            break
        page += 1
        bundle = get_json(next_url)

    rows = [flatten(r) for r in resources]
    columns = ["fhir_id", "legacy_id"] + COMPARE_FIELDS
    save_table("tgt.practitioners", rows, columns)
    write_csv(OUTPUT_DIR / "target_practitioners.csv", rows, columns)

    expected_ids = {c["fhir_id"] for c in crosswalk if c["fhir_id"]}
    on_server = {r["fhir_id"] for r in rows}
    missing = expected_ids - on_server
    print(f"   Expected {len(expected_ids)}, on server {len(on_server)}, missing {len(missing)}   "
          f"{'PASS' if not missing else 'FAIL'}")
    if len(on_server) > len(expected_ids):
        print("   (More on the server than this run sent: earlier --limit test runs are included. "
              "The SQL reconciliation uses the crosswalk, so this is fine.)")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Phase 2: migrate providers to FHIR Practitioner")
    parser.add_argument("--limit", type=int, help="only send the first N bundles (try 1 first)")
    args = parser.parse_args()

    stage()
    rows = curate()
    accepted = transform(rows, read_roster())
    crosswalk = load(accepted, args.limit)
    extract(crosswalk)
    print("\nNext: open sql\\20_practitioner_reconciliation.sql in SSMS and press F5,")
    print("      then run  python src\\export_worklist.py  to see the remediation status.")


if __name__ == "__main__":
    main()
