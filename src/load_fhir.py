"""
load_fhir.py
------------
Step 8: Send the FHIR transaction bundles to the FHIR server (REST API POST).

For each bundle in output/fhir_bundles/ :
  - POST it to the server's base URL (a FHIR "transaction")
  - read the server's reply to learn the NEW FHIR id of every resource
  - record legacy id <-> FHIR id in a crosswalk table
Retries automatically if the server is busy, and logs every bundle's outcome.

Run from the project folder:
  python src\\load_fhir.py --limit 1     <- send ONE patient first (recommended)
  python src\\load_fhir.py               <- send all bundles
Safe to re-run: bundles use conditional create (ifNoneExist), so records that
already exist on the server are matched, not duplicated.

Outputs: output/load_log.csv, output/fhir_crosswalk.csv
         SQL tables etl.load_log and etl.fhir_crosswalk
"""

import argparse
import csv
import json
import re
import time
from pathlib import Path

import pyodbc
import requests

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
DEFAULT_SERVER = "https://hapi.fhir.org/baseR4"
BUNDLE_DIR = Path("output/fhir_bundles")
OUTPUT_DIR = Path("output")
SQL_SERVER = "localhost"
DRIVER = "ODBC Driver 17 for SQL Server"
DATABASE = "FhirMigration"

HEADERS = {
    "Content-Type": "application/fhir+json",  # "I am sending FHIR JSON"
    "Accept": "application/fhir+json",        # "please answer in FHIR JSON"
    "Prefer": "return=minimal",               # "just send ids/status back, not whole resources"
}
TIMEOUT_SECONDS = 300               # big bundles can take a while on a shared public server
RETRY_WAITS = [5, 15, 45]           # seconds to wait before retry 1, 2, 3
RETRY_STATUS = {429, 500, 502, 503, 504}   # "busy" / temporary server errors worth retrying
PAUSE_BETWEEN_BUNDLES = 1.0         # be polite to the free public server

RESOURCE_TYPES = ("Patient", "Encounter", "Condition")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def post_bundle(server, bundle):
    """POST one transaction bundle. Returns (response or None, attempts, error text)."""
    attempts, last_error = 0, ""
    for wait in [0] + RETRY_WAITS:
        if wait:
            print(f"      server busy/unreachable - retrying in {wait}s ...")
            time.sleep(wait)
        attempts += 1
        try:
            resp = requests.post(server, headers=HEADERS, data=json.dumps(bundle),
                                 timeout=TIMEOUT_SECONDS)
        except (requests.ConnectionError, requests.Timeout) as e:
            last_error = f"{type(e).__name__}: {e}"
            continue
        if resp.status_code in RETRY_STATUS:
            last_error = f"HTTP {resp.status_code}"
            continue
        return resp, attempts, ""
    return None, attempts, last_error


def outcome_message(resp):
    """Pull a readable error message out of a FHIR OperationOutcome, if there is one."""
    try:
        body = resp.json()
        issues = body.get("issue", [])
        if issues:
            return "; ".join(i.get("diagnostics", i.get("code", "")) for i in issues)[:450]
    except ValueError:
        pass
    return resp.text[:450]


def fhir_id_from_location(location):
    """'Patient/123/_history/1' (or a full URL ending like that) -> ('Patient', '123')."""
    m = re.search(r"(Patient|Encounter|Condition)/([^/]+)", location or "")
    return (m.group(1), m.group(2)) if m else (None, None)


def save_to_sql(load_log, crosswalk):
    conn = pyodbc.connect(
        f"DRIVER={{{DRIVER}}};SERVER={SQL_SERVER};DATABASE={DATABASE};"
        "Trusted_Connection=yes;TrustServerCertificate=yes;", autocommit=True)
    cur = conn.cursor()
    cur.execute("IF SCHEMA_ID('etl') IS NULL EXEC('CREATE SCHEMA etl');")
    cur.execute("DROP TABLE IF EXISTS etl.load_log;")
    cur.execute(
        "CREATE TABLE etl.load_log (bundle_file NVARCHAR(200), legacy_patient_id NVARCHAR(100),"
        " entries INT, http_status INT NULL, outcome NVARCHAR(20), attempts INT, seconds DECIMAL(9,2),"
        " error NVARCHAR(500), loaded_at DATETIME2 DEFAULT SYSUTCDATETIME());")
    cur.execute("DROP TABLE IF EXISTS etl.fhir_crosswalk;")
    cur.execute(
        "CREATE TABLE etl.fhir_crosswalk (resource_type NVARCHAR(30), legacy_id NVARCHAR(300),"
        " fhir_id NVARCHAR(100), server_status NVARCHAR(40), bundle_file NVARCHAR(200),"
        " loaded_at DATETIME2 DEFAULT SYSUTCDATETIME());")
    cur.fast_executemany = True
    if load_log:
        cur.executemany(
            "INSERT INTO etl.load_log (bundle_file, legacy_patient_id, entries, http_status, outcome,"
            " attempts, seconds, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?);",
            [(r["bundle_file"], r["legacy_patient_id"], r["entries"], r["http_status"], r["outcome"],
              r["attempts"], r["seconds"], r["error"]) for r in load_log])
    if crosswalk:
        cur.executemany(
            "INSERT INTO etl.fhir_crosswalk (resource_type, legacy_id, fhir_id, server_status, bundle_file)"
            " VALUES (?, ?, ?, ?, ?);",
            [(r["resource_type"], r["legacy_id"], r["fhir_id"], r["server_status"], r["bundle_file"])
             for r in crosswalk])
    conn.close()


def write_csv(path, rows, fields):
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="POST FHIR transaction bundles to a FHIR server")
    parser.add_argument("--limit", type=int, help="only send the first N bundles (try 1 first)")
    parser.add_argument("--server", default=DEFAULT_SERVER, help="FHIR base URL")
    args = parser.parse_args()

    files = sorted(BUNDLE_DIR.glob("patient_*.json"))
    if not files:
        print(f"No bundles found in {BUNDLE_DIR}. Run src\\transform.py first.")
        return
    if args.limit:
        files = files[: args.limit]
    print(f"Sending {len(files)} bundle(s) to {args.server}\n")

    load_log, crosswalk = [], []
    expected = {t: 0 for t in RESOURCE_TYPES}

    for n, path in enumerate(files, 1):
        bundle = json.loads(path.read_text(encoding="utf-8"))
        entries = bundle["entry"]
        legacy_pid = entries[0]["resource"]["identifier"][0]["value"]
        for e in entries:
            expected[e["resource"]["resourceType"]] += 1

        started = time.time()
        resp, attempts, error = post_bundle(args.server, bundle)
        seconds = round(time.time() - started, 2)
        log = {"bundle_file": path.name, "legacy_patient_id": legacy_pid, "entries": len(entries),
               "http_status": resp.status_code if resp is not None else None,
               "attempts": attempts, "seconds": seconds, "error": error}

        if resp is None or resp.status_code != 200:
            log["outcome"] = "FAILED"
            if resp is not None:
                log["error"] = outcome_message(resp)
            print(f"  [{n}/{len(files)}] {path.name}: FAILED ({log['http_status']}) {log['error'][:150]}")
            load_log.append(log)
            continue

        # The transaction-response has one entry per request entry, IN THE SAME ORDER
        reply_entries = resp.json().get("entry", [])
        if len(reply_entries) != len(entries):
            log["outcome"] = "FAILED"
            log["error"] = f"server replied with {len(reply_entries)} entries, expected {len(entries)}"
            print(f"  [{n}/{len(files)}] {path.name}: FAILED - {log['error']}")
            load_log.append(log)
            continue

        created = existed = 0
        for sent, reply in zip(entries, reply_entries):
            r = reply.get("response", {})
            status = r.get("status", "")
            rtype, fhir_id = fhir_id_from_location(r.get("location"))
            crosswalk.append({
                "resource_type": sent["resource"]["resourceType"],
                "legacy_id": sent["resource"]["identifier"][0]["value"],
                "fhir_id": fhir_id,
                "server_status": status,
                "bundle_file": path.name,
            })
            if status.startswith("201"):
                created += 1
            elif status.startswith("200"):
                existed += 1

        log["outcome"] = "OK"
        load_log.append(log)
        patient_fhir_id = crosswalk[-len(entries)]["fhir_id"]
        print(f"  [{n}/{len(files)}] {path.name}: OK  {len(entries)} resources "
              f"({created} created, {existed} already existed)  {seconds}s  -> Patient/{patient_fhir_id}")
        time.sleep(PAUSE_BETWEEN_BUNDLES)

    # ---- Save logs ----
    OUTPUT_DIR.mkdir(exist_ok=True)
    write_csv(OUTPUT_DIR / "load_log.csv", load_log,
              ["bundle_file", "legacy_patient_id", "entries", "http_status", "outcome",
               "attempts", "seconds", "error"])
    write_csv(OUTPUT_DIR / "fhir_crosswalk.csv", crosswalk,
              ["resource_type", "legacy_id", "fhir_id", "server_status", "bundle_file"])
    save_to_sql(load_log, crosswalk)

    # ---- Summary / reconciliation ----
    ok = sum(1 for r in load_log if r["outcome"] == "OK")
    print(f"\nBundles: {ok} OK, {len(load_log) - ok} failed  "
          f"-> output/load_log.csv and etl.load_log")
    print("\nReconciliation: resources sent vs. confirmed by the server (have a FHIR id)")
    print(f"  {'Resource':<11}{'Sent':>7}{'Confirmed':>11}{'Created':>9}{'Existed':>9}   Result")
    for t in RESOURCE_TYPES:
        rows = [c for c in crosswalk if c["resource_type"] == t and c["fhir_id"]]
        made = sum(1 for c in rows if c["server_status"].startswith("201"))
        had = sum(1 for c in rows if c["server_status"].startswith("200"))
        result = "PASS" if len(rows) == expected[t] else "FAIL"
        print(f"  {t:<11}{expected[t]:>7}{len(rows):>11}{made:>9}{had:>9}   {result}")
    print("Crosswalk (legacy id <-> FHIR id) -> output/fhir_crosswalk.csv and etl.fhir_crosswalk")

    first_ok = next((c for c in crosswalk if c["resource_type"] == "Patient" and c["fhir_id"]), None)
    if first_ok:
        print(f"\nSee your first migrated patient in a browser:\n  "
              f"{args.server}/Patient/{first_ok['fhir_id']}?_format=json")


if __name__ == "__main__":
    main()
