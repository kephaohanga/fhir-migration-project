"""
load_staging.py
---------------
Loads the "dirty" legacy CSV files into SQL Server STAGING tables.

Staging = a raw landing area. Data is copied in as-is (all text, no fixing),
so we always have an exact copy of what the legacy system sent us.
Cleaning and validation happen in later steps, never here.

Source : data/legacy_dirty/patients.csv, encounters.csv, conditions.csv
Target : SQL Server database FhirMigration, schema "stg"
         -> stg.patients, stg.encounters, stg.conditions
Run from the project folder:  python src\\load_staging.py
Safe to re-run: each run drops and reloads the staging tables.
"""

from pathlib import Path

import pandas as pd
import pyodbc

# ---------------------------------------------------------------------------
# Settings - change these if your setup is different
# ---------------------------------------------------------------------------
SERVER = "localhost"                    # default SQL Server instance on this computer
DRIVER = "ODBC Driver 17 for SQL Server"
DATABASE = "FhirMigration"
SOURCE_DIR = Path("data/legacy_dirty")

# Which CSV goes into which staging table
FILES = {
    "patients": "patients.csv",
    "encounters": "encounters.csv",
    "conditions": "conditions.csv",
}

# Data minimization (a HIPAA principle): we never load identifiers
# the new system doesn't need. These columns are dropped before loading.
DROP_COLUMNS = {
    "patients": ["SSN", "DRIVERS", "PASSPORT"],
}


def connect(database):
    """Open a connection using Windows login (Trusted_Connection),
    the same way SSMS connects when you choose 'Windows Authentication'."""
    conn_str = (
        f"DRIVER={{{DRIVER}}};"
        f"SERVER={SERVER};"
        f"DATABASE={database};"
        "Trusted_Connection=yes;"
        "TrustServerCertificate=yes;"
    )
    return pyodbc.connect(conn_str, autocommit=True)


# ---------------------------------------------------------------------------
# 1. Create the database and the "stg" schema (only if they don't exist yet)
# ---------------------------------------------------------------------------
print(f"Connecting to SQL Server on {SERVER} ...")
with connect("master") as conn:
    conn.execute(f"IF DB_ID('{DATABASE}') IS NULL CREATE DATABASE [{DATABASE}];")
print(f"Database [{DATABASE}] is ready.")

conn = connect(DATABASE)
cursor = conn.cursor()
cursor.fast_executemany = True  # send rows in big batches = much faster inserts
cursor.execute("IF SCHEMA_ID('stg') IS NULL EXEC('CREATE SCHEMA stg');")

# ---------------------------------------------------------------------------
# 2. Load each CSV into its staging table
# ---------------------------------------------------------------------------
results = []
for table, filename in FILES.items():
    path = SOURCE_DIR / filename
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    csv_rows = len(df)

    # Drop columns we must not carry over
    dropped = [c for c in DROP_COLUMNS.get(table, []) if c in df.columns]
    df = df.drop(columns=dropped)

    columns = list(df.columns)

    # Build the CREATE TABLE statement.
    # Every source column is NVARCHAR (text) in staging - we don't trust the
    # legacy system's types yet. Two audit columns are added:
    #   source_row = the line number in the CSV file (header is line 1)
    #   loaded_at  = when this row was loaded
    column_defs = ",\n    ".join(f"[{c}] NVARCHAR(500) NULL" for c in columns)
    full_name = f"stg.{table}"
    cursor.execute(f"DROP TABLE IF EXISTS {full_name};")
    cursor.execute(
        f"CREATE TABLE {full_name} (\n"
        f"    {column_defs},\n"
        f"    [source_row] INT NOT NULL,\n"
        f"    [loaded_at] DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()\n"
        f");"
    )

    # Prepare the rows: blank cells become NULL (SQL's "no value")
    rows = []
    for position, values in enumerate(df.itertuples(index=False, name=None)):
        cleaned = [None if v == "" else v for v in values]
        rows.append(cleaned + [position + 2])  # +2: header is line 1, data starts line 2

    col_list = ", ".join(f"[{c}]" for c in columns) + ", [source_row]"
    placeholders = ", ".join("?" for _ in range(len(columns) + 1))
    cursor.executemany(
        f"INSERT INTO {full_name} ({col_list}) VALUES ({placeholders});", rows
    )

    # Reconciliation check: rows in SQL must equal rows in the CSV
    sql_rows = cursor.execute(f"SELECT COUNT(*) FROM {full_name};").fetchone()[0]
    status = "PASS" if sql_rows == csv_rows else "FAIL"
    results.append((full_name, csv_rows, sql_rows, status))
    note = f" (dropped: {', '.join(dropped)})" if dropped else ""
    print(f"Loaded {full_name}: {sql_rows} rows{note}")

conn.close()

# ---------------------------------------------------------------------------
# 3. Print the reconciliation summary
# ---------------------------------------------------------------------------
print("\nRow count reconciliation (CSV file vs. SQL staging table)")
print(f"{'Table':<18}{'CSV rows':>10}{'SQL rows':>10}   Result")
for name, csv_rows, sql_rows, status in results:
    print(f"{name:<18}{csv_rows:>10}{sql_rows:>10}   {status}")

if all(r[3] == "PASS" for r in results):
    print("\nAll staging tables match their source files.")
else:
    print("\nWARNING: at least one table does not match its source file.")