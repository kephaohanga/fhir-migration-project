"""
export_worklist.py
------------------
Step 13 (remediation): build the remediation worklist and save it for staff.

  1. runs sql/12_remediation.sql (creates/refreshes the rpt.v_remediation_* views)
  2. writes remediation/worklist.csv - one row per root-cause problem, highest
     impact first, with blank columns for staff to fill in:
        corrected_value, disposition (Correct | Archive), reason,
        corrected_by, corrected_date
     Save the filled-in rows as remediation/corrections.csv, then run
     src/apply_corrections.py and the rest of the pipeline.
  3. prints the worklist and the outcome of every correction made so far

Run from the project folder:  python src\\export_worklist.py
(after src\\apply_corrections.py and src\\transform.py have run at least once)
"""

import csv
import re
from pathlib import Path

import pyodbc

SERVER = "localhost"
DRIVER = "ODBC Driver 17 for SQL Server"
DATABASE = "FhirMigration"
SQL_FILE = Path("sql/12_remediation.sql")
WORKLIST_CSV = Path("remediation/worklist.csv")

FILL_IN = ["corrected_value", "disposition", "reason", "corrected_by", "corrected_date"]


def connect():
    return pyodbc.connect(
        f"DRIVER={{{DRIVER}}};SERVER={SERVER};DATABASE={DATABASE};"
        "Trusted_Connection=yes;TrustServerCertificate=yes;",
        autocommit=True,
    )


def run_sql_file(cursor, path):
    """SSMS understands 'GO'; Python does not. Split the file into batches at each GO line."""
    batches = re.split(r"^\s*GO\s*$", path.read_text(encoding="utf-8"), flags=re.MULTILINE | re.IGNORECASE)
    for batch in batches:
        if batch.strip():
            cursor.execute(batch)
            while cursor.nextset():          # skip any result grids the batch returns
                pass


def query(cursor, sql):
    cursor.execute(sql)
    cols = [c[0] for c in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


def main():
    conn = connect()
    cur = conn.cursor()
    run_sql_file(cur, SQL_FILE)

    worklist = query(cur, "SELECT * FROM rpt.v_remediation_worklist ORDER BY priority;")
    log = query(cur, "SELECT * FROM rpt.v_remediation_log ORDER BY correction_id;")
    conn.close()

    # ---- Save the worklist for staff (utf-8-sig so Excel shows accented names correctly) ----
    WORKLIST_CSV.parent.mkdir(parents=True, exist_ok=True)
    columns = ["priority", "status", "table_name", "source_row", "record_key", "rule_id", "problem",
               "field", "current_value", "patient_id", "patient_name", "context", "records_blocked",
               "suggested_owner", "what_is_needed"] + FILL_IN
    with WORKLIST_CSV.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in worklist:
            writer.writerow({**row, **{k: "" for k in FILL_IN}})

    # ---- Print it ----
    total_blocked = sum(r["records_blocked"] for r in worklist if r["status"] == "Open")
    open_items = [r for r in worklist if r["status"] != "Archived - not migrated"]
    print(f"Remediation worklist: {len(worklist)} root-cause problems "
          f"({len(open_items)} need action) -> {WORKLIST_CSV}")
    print(f"  Open problems are blocking {total_blocked} other records (cascade rejects)\n")
    print(f"  {'#':>2}  {'Status':<38}{'Table':<11}{'Row':>5}  {'Rule':<7}{'Field':<15}{'Blocks':>7}  Owner")
    for r in worklist:
        print(f"  {r['priority']:>2}  {r['status']:<38}{r['table_name']:<11}{r['source_row']:>5}  "
              f"{r['rule_id']:<7}{(r['field'] or ''):<15}{r['records_blocked']:>7}  {r['suggested_owner']}")

    if log:
        print("\nCorrections made so far (etl.corrections):")
        for r in log:
            change = (f"{r['field_name']}: {r['original_value']!r} -> {r['corrected_value']!r}"
                      if r["disposition"] == "Correct" else f"archived - {r['reason']}")
            print(f"  {r['table_name']:<10} row {r['source_row']:<5} {change:<55} {r['outcome']}")
    else:
        print("\nNo corrections yet. Fill in the last five columns of the worklist,")
        print(f"save it as remediation/corrections.csv, then run  python src\\apply_corrections.py")


if __name__ == "__main__":
    main()
