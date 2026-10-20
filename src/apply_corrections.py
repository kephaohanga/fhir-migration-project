"""
apply_corrections.py
--------------------
Step 13 (remediation): apply staff corrections to the legacy data WITHOUT
changing the raw staging tables.

How it works
  stg.*   raw legacy rows exactly as received        (never edited)
  cur.*   "curated" copy = stg + approved corrections (rebuilt every run)
  etl.corrections  audit trail: who changed what, when, why, old -> new value

Corrections come from remediation/corrections.csv, one row per fix:
  table_name, source_row, record_key, rule_id, field, corrected_value,
  disposition (Correct | Archive), reason, corrected_by, corrected_date
  - Correct : the field is overwritten in cur.* with corrected_value
  - Archive : nothing is changed; the record stays out of the migration and is
              kept in the read-only legacy archive (the reason is required)

Run from the project folder:  python src\\apply_corrections.py
Then run src\\transform.py - it now reads cur.* instead of stg.*
Safe to re-run: cur.* and etl.corrections are rebuilt from the CSV every time.
"""

import csv
import re
from datetime import datetime
from pathlib import Path

import pyodbc

SERVER = "localhost"
DRIVER = "ODBC Driver 17 for SQL Server"
DATABASE = "FhirMigration"
CORRECTIONS_CSV = Path("remediation/corrections.csv")
TABLES = ("patients", "encounters", "conditions")

CSV_FIELDS = ["table_name", "source_row", "record_key", "rule_id", "field", "corrected_value",
              "disposition", "reason", "corrected_by", "corrected_date"]

# Columns that hold dates. Excel often re-saves 1993-07-18 as 7/18/1993, so
# these are converted back to the YYYY-MM-DD format the pipeline expects.
DATE_FIELDS = {"BIRTHDATE", "DEATHDATE"}
PROTECTED_FIELDS = {"Id", "source_row", "loaded_at"}   # identity / audit columns never change

CORRECTIONS_DDL = """
CREATE TABLE etl.corrections (
    correction_id   INT IDENTITY(1,1) PRIMARY KEY,
    table_name      NVARCHAR(50),
    source_row      INT,
    record_key      NVARCHAR(300),
    rule_id         NVARCHAR(20),
    field_name      NVARCHAR(100),
    original_value  NVARCHAR(500),
    corrected_value NVARCHAR(500),
    disposition     NVARCHAR(20),
    reason          NVARCHAR(500),
    corrected_by    NVARCHAR(100),
    corrected_date  NVARCHAR(30),
    applied_at      DATETIME2 DEFAULT SYSUTCDATETIME()
);"""


def connect():
    return pyodbc.connect(
        f"DRIVER={{{DRIVER}}};SERVER={SERVER};DATABASE={DATABASE};"
        "Trusted_Connection=yes;TrustServerCertificate=yes;",
        autocommit=True,
    )


def read_corrections():
    """Read the CSV (Excel may save it as UTF-8 with a BOM, or as Windows-1252)."""
    if not CORRECTIONS_CSV.exists():
        CORRECTIONS_CSV.parent.mkdir(parents=True, exist_ok=True)
        with CORRECTIONS_CSV.open("w", newline="", encoding="utf-8-sig") as f:
            csv.writer(f).writerow(CSV_FIELDS)
        print(f"Created an empty template: {CORRECTIONS_CSV}")
        return []
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            with CORRECTIONS_CSV.open(encoding=encoding, newline="") as f:
                rows = list(csv.DictReader(f))
            break
        except UnicodeDecodeError:
            continue
    # Keep only rows where a decision was made (blank disposition = still open)
    clean = []
    for r in rows:
        r = {k.strip(): (v or "").strip() for k, v in r.items() if k}
        if r.get("disposition"):
            clean.append(r)
    return clean


def normalize_date(value):
    """'7/18/1993' or '07/18/1993' -> '1993-07-18'. Returns (value, note or None)."""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value, None
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", value)
    if m:
        iso = datetime(int(m.group(3)), int(m.group(1)), int(m.group(2))).date().isoformat()
        return iso, f"date {value} converted to {iso}"
    raise ValueError(f"'{value}' is not a date (use YYYY-MM-DD)")


def condition_key(row):
    return "_".join(str(row.get(c) or "") for c in ("PATIENT", "ENCOUNTER", "CODE", "START"))


def main():
    corrections = read_corrections()
    conn = connect()
    cur = conn.cursor()

    # Column list and rows of each staging table, so every correction can be checked
    columns, staging = {}, {}
    for table in TABLES:
        cur.execute(f"SELECT * FROM stg.{table};")
        cols = [c[0] for c in cur.description]
        columns[table] = set(cols)
        staging[table] = {row[cols.index("source_row")]: dict(zip(cols, row)) for row in cur.fetchall()}

    # ---- Validate every correction before changing anything ----
    accepted, errors = [], []
    seen = {}
    for n, c in enumerate(corrections, start=2):          # line 1 of the CSV is the header
        where = f"line {n}"
        table, disposition = c.get("table_name", "").lower(), c.get("disposition", "").capitalize()
        try:
            src = int(float(c.get("source_row", "")))
        except ValueError:
            errors.append(f"{where}: source_row '{c.get('source_row')}' is not a number")
            continue
        if table not in TABLES:
            errors.append(f"{where}: table_name '{table}' must be patients, encounters or conditions")
            continue
        row = staging[table].get(src)
        if row is None:
            errors.append(f"{where}: {table} has no source_row {src}")
            continue
        actual_key = condition_key(row) if table == "conditions" else row.get("Id")
        if c.get("record_key") and c["record_key"] != actual_key:
            errors.append(f"{where}: record_key does not match {table} source_row {src} "
                          f"(expected {actual_key})")
            continue
        if disposition not in ("Correct", "Archive"):
            errors.append(f"{where}: disposition must be Correct or Archive")
            continue
        if not c.get("corrected_by") or not c.get("corrected_date"):
            errors.append(f"{where}: corrected_by and corrected_date are required (audit trail)")
            continue
        field, value, note = c.get("field", ""), c.get("corrected_value", ""), None
        if disposition == "Correct":
            if field not in columns[table] or field in PROTECTED_FIELDS:
                errors.append(f"{where}: '{field}' is not a correctable column of {table}")
                continue
            if not value:
                errors.append(f"{where}: corrected_value is required for disposition Correct")
                continue
            if field in DATE_FIELDS:
                try:
                    value, note = normalize_date(value)
                except ValueError as e:
                    errors.append(f"{where}: {e}")
                    continue
        elif not c.get("reason"):
            errors.append(f"{where}: a reason is required for disposition Archive")
            continue
        try:
            corrected_date, _ = normalize_date(c["corrected_date"])
        except ValueError:
            corrected_date = c["corrected_date"]
        dup_key = (table, src, field)
        if dup_key in seen:
            print(f"  note: {where} replaces an earlier correction for {table} row {src} {field}")
        seen[dup_key] = len(accepted)
        accepted.append({
            "table_name": table, "source_row": src, "record_key": actual_key,
            "rule_id": c.get("rule_id", ""), "field_name": field if disposition == "Correct" else (field or None),
            "original_value": row.get(field) if field in columns[table] else None,
            "corrected_value": value if disposition == "Correct" else None,
            "disposition": disposition, "reason": c.get("reason", ""),
            "corrected_by": c["corrected_by"], "corrected_date": corrected_date, "note": note,
        })

    if errors:
        print("\nCorrections NOT applied - fix these lines in remediation/corrections.csv first:")
        for e in errors:
            print("  - " + e)
        print("\nNothing was changed. cur.* still holds the previous run.")
        return

    # Keep only the last correction per table + row + field
    accepted = [accepted[i] for i in sorted(seen.values())]

    # ---- 1. Audit trail: etl.corrections ----
    cur.execute("IF SCHEMA_ID('etl') IS NULL EXEC('CREATE SCHEMA etl');")
    cur.execute("DROP TABLE IF EXISTS etl.corrections;")
    cur.execute(CORRECTIONS_DDL)
    if accepted:
        cur.executemany(
            "INSERT INTO etl.corrections (table_name, source_row, record_key, rule_id, field_name,"
            " original_value, corrected_value, disposition, reason, corrected_by, corrected_date)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
            [(a["table_name"], a["source_row"], a["record_key"], a["rule_id"], a["field_name"],
              a["original_value"], a["corrected_value"], a["disposition"], a["reason"],
              a["corrected_by"], a["corrected_date"]) for a in accepted])

    # ---- 2. Curated layer: cur.* = copy of stg.* + approved corrections ----
    cur.execute("IF SCHEMA_ID('cur') IS NULL EXEC('CREATE SCHEMA cur');")
    for table in TABLES:
        cur.execute(f"DROP TABLE IF EXISTS cur.{table};")
        cur.execute(f"SELECT * INTO cur.{table} FROM stg.{table};")
    applied = 0
    for a in accepted:
        if a["disposition"] == "Correct":
            # Column name comes from the validated whitelist above, never straight from the CSV
            cur.execute(f"UPDATE cur.{a['table_name']} SET [{a['field_name']}] = ? WHERE source_row = ?;",
                        a["corrected_value"], a["source_row"])
            applied += 1
    conn.close()

    # ---- Report ----
    archived = sum(1 for a in accepted if a["disposition"] == "Archive")
    print(f"\nCorrections read from {CORRECTIONS_CSV}: {len(accepted)} "
          f"({applied} Correct, {archived} Archive)")
    for a in accepted:
        if a["disposition"] == "Correct":
            print(f"  {a['table_name']:<10} row {a['source_row']:<5} {a['field_name']}: "
                  f"{a['original_value']!r} -> {a['corrected_value']!r}  ({a['corrected_by']})"
                  + (f"  [{a['note']}]" if a["note"] else ""))
        else:
            print(f"  {a['table_name']:<10} row {a['source_row']:<5} ARCHIVE - {a['reason']}")
    print("\ncur.patients, cur.encounters, cur.conditions rebuilt (raw stg.* tables unchanged)")
    print("Audit trail -> etl.corrections")
    print("Next: python src\\transform.py")


if __name__ == "__main__":
    main()
