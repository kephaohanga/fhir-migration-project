/* =============================================================================
   10_reconciliation.sql
   Step 10: Field-by-field reconciliation - legacy staging (stg) vs. what the
   FHIR server actually holds (tgt), using the crosswalk (etl) to pair records.

   For every migrated record, each field's EXPECTED value is recalculated from
   the curated layer (cur.* = raw staging + approved corrections, Step 13) by
   applying the mapping rules (e.g. F -> female, 07/01/1978 -> 1978-07-01,
   North Carolina -> NC, wellness -> AMB) and compared with the ACTUAL value on
   the server.

   Creates reporting views in schema "rpt" (Power BI will use these in Step 11):
     rpt.v_patient_field_compare     one row per patient per field
     rpt.v_encounter_field_compare   one row per encounter per field
     rpt.v_condition_field_compare   one row per condition per field
     rpt.v_field_summary             match rate per resource + field
     rpt.v_row_reconciliation        every source row accounted for?
     rpt.v_issue_summary             issue log by rule and action

   How to run: open in SSMS, press F5. The results at the bottom are the report.
   Safe to re-run (CREATE OR ALTER).
   ========================================================================== */

USE FhirMigration;
GO

IF SCHEMA_ID('rpt') IS NULL EXEC('CREATE SCHEMA rpt');
GO

/* -----------------------------------------------------------------------------
   PATIENTS
   ROW_NUMBER() keeps only the first copy of the planted duplicate (rule R-P05),
   exactly like the Python transform did.
   -------------------------------------------------------------------------- */
CREATE OR ALTER VIEW rpt.v_patient_field_compare AS
WITH src AS (
    SELECT *,
           ROW_NUMBER() OVER (PARTITION BY Id ORDER BY source_row) AS copy_no
    FROM cur.patients
),
expected AS (
    SELECT
        s.Id                                                    AS legacy_id,
        x.fhir_id,
        CAST(CASE s.GENDER WHEN 'M' THEN 'male' WHEN 'F' THEN 'female' END AS NVARCHAR(500)) AS gender,
        -- Rule R-P02: legacy MM/DD/YYYY dates are converted to YYYY-MM-DD
        CAST(CASE WHEN s.BIRTHDATE LIKE '[0-9][0-9]/[0-9][0-9]/[0-9][0-9][0-9][0-9]'
                  THEN CONVERT(CHAR(10), TRY_CONVERT(DATE, s.BIRTHDATE, 101), 23)
                  ELSE s.BIRTHDATE END AS NVARCHAR(500))      AS birth_date,
        s.DEATHDATE                                             AS deceased_datetime,
        s.PREFIX                                                AS prefix,
        s.FIRST                                                 AS given_first,
        s.MIDDLE                                                AS given_middle,
        s.LAST                                                  AS family,
        s.SUFFIX                                                AS suffix,
        s.MAIDEN                                                AS maiden_family,
        CAST(CASE WHEN s.MARITAL IN ('M','S','D','W') THEN s.MARITAL END AS NVARCHAR(500)) AS marital_code,
        s.ADDRESS                                               AS address_line,
        s.CITY                                                  AS city,
        s.COUNTY                                                AS district,
        -- Value map: full state name -> USPS code (this dataset is all North Carolina;
        -- the Python transform holds the full 50-state table)
        CAST(CASE s.STATE WHEN 'North Carolina' THEN 'NC' ELSE s.STATE END AS NVARCHAR(500)) AS state,
        s.ZIP                                                   AS postal_code,
        CAST(N'US' AS NVARCHAR(500))                            AS country
    FROM src AS s
    JOIN etl.fhir_crosswalk AS x
      ON x.resource_type = 'Patient' AND x.legacy_id = s.Id
    WHERE s.copy_no = 1
)
SELECT e.legacy_id, e.fhir_id, f.field_name, f.expected_value, f.actual_value,
       CASE WHEN ISNULL(f.expected_value, N'<null>') COLLATE Latin1_General_BIN2
               = ISNULL(f.actual_value,   N'<null>') COLLATE Latin1_General_BIN2
            THEN 1 ELSE 0 END AS is_match
FROM expected AS e
JOIN tgt.patients AS t ON t.fhir_id = e.fhir_id
CROSS APPLY (VALUES
    ('gender',            e.gender,            t.gender),
    ('birth_date',        e.birth_date,        t.birth_date),
    ('deceased_datetime', e.deceased_datetime, t.deceased_datetime),
    ('prefix',            e.prefix,            t.prefix),
    ('given_first',       e.given_first,       t.given_first),
    ('given_middle',      e.given_middle,      t.given_middle),
    ('family',            e.family,            t.family),
    ('suffix',            e.suffix,            t.suffix),
    ('maiden_family',     e.maiden_family,     t.maiden_family),
    ('marital_code',      e.marital_code,      t.marital_code),
    ('address_line',      e.address_line,      t.address_line),
    ('city',              e.city,              t.city),
    ('district',          e.district,          t.district),
    ('state',             e.state,             t.state),
    ('postal_code',       e.postal_code,       t.postal_code),
    ('country',           e.country,           t.country)
) AS f (field_name, expected_value, actual_value);
GO

/* -----------------------------------------------------------------------------
   ENCOUNTERS
   -------------------------------------------------------------------------- */
CREATE OR ALTER VIEW rpt.v_encounter_field_compare AS
WITH expected AS (
    SELECT
        s.Id                                                    AS legacy_id,
        x.fhir_id,
        px.fhir_id                                              AS patient_fhir_id,   -- the link must point to the patient's NEW id
        CAST(CASE WHEN s.STOP IS NULL THEN 'in-progress' ELSE 'finished' END AS NVARCHAR(500)) AS status,
        CAST(CASE s.ENCOUNTERCLASS
                 WHEN 'ambulatory' THEN 'AMB'  WHEN 'wellness'   THEN 'AMB'
                 WHEN 'outpatient' THEN 'AMB'  WHEN 'urgentcare' THEN 'AMB'
                 WHEN 'emergency'  THEN 'EMER' WHEN 'inpatient'  THEN 'IMP'
                 WHEN 'snf'        THEN 'NONAC' WHEN 'hospice'   THEN 'NONAC'
                 WHEN 'home'       THEN 'HH'   WHEN 'virtual'    THEN 'VR'
             END AS NVARCHAR(500))                              AS class_code,
        s.CODE                                                  AS type_code,
        CAST(CASE WHEN s.CODE IS NOT NULL THEN s.DESCRIPTION END AS NVARCHAR(500)) AS type_display,
        s.START                                                 AS period_start,
        s.STOP                                                  AS period_end,
        s.REASONCODE                                            AS reason_code
    FROM cur.encounters AS s
    JOIN etl.fhir_crosswalk AS x
      ON x.resource_type = 'Encounter' AND x.legacy_id = s.Id
    LEFT JOIN etl.fhir_crosswalk AS px
      ON px.resource_type = 'Patient' AND px.legacy_id = s.PATIENT
)
SELECT e.legacy_id, e.fhir_id, f.field_name, f.expected_value, f.actual_value,
       CASE WHEN ISNULL(f.expected_value, N'<null>') COLLATE Latin1_General_BIN2
               = ISNULL(f.actual_value,   N'<null>') COLLATE Latin1_General_BIN2
            THEN 1 ELSE 0 END AS is_match
FROM expected AS e
JOIN tgt.encounters AS t ON t.fhir_id = e.fhir_id
CROSS APPLY (VALUES
    ('patient_link', e.patient_fhir_id, t.patient_fhir_id),
    ('status',       e.status,          t.status),
    ('class_code',   e.class_code,      t.class_code),
    ('type_code',    e.type_code,       t.type_code),
    ('type_display', e.type_display,    t.type_display),
    ('period_start', e.period_start,    t.period_start),
    ('period_end',   e.period_end,      t.period_end),
    ('reason_code',  e.reason_code,     t.reason_code)
) AS f (field_name, expected_value, actual_value);
GO

/* -----------------------------------------------------------------------------
   CONDITIONS
   The legacy key is rebuilt exactly like the Python transform:
   PATIENT_ENCOUNTER_CODE_START (CONCAT treats NULL as an empty string).
   -------------------------------------------------------------------------- */
CREATE OR ALTER VIEW rpt.v_condition_field_compare AS
WITH src AS (
    SELECT *,
           CONCAT(PATIENT, '_', ENCOUNTER, '_', CODE, '_', START) AS legacy_key
    FROM cur.conditions
),
dedup AS (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY legacy_key ORDER BY source_row) AS copy_no
    FROM src
),
expected AS (
    SELECT
        s.legacy_key,
        x.fhir_id,
        px.fhir_id                                              AS patient_fhir_id,
        ex.fhir_id                                              AS encounter_fhir_id,  -- NULL if the visit was not migrated (R-C03)
        CAST(CASE WHEN s.SYSTEM IN ('SNOMED-CT', '') OR s.SYSTEM IS NULL THEN 'http://snomed.info/sct'
                  WHEN s.SYSTEM = 'ICD-10-CM' THEN 'http://hl7.org/fhir/sid/icd-10-cm' END AS NVARCHAR(500)) AS code_system,
        s.CODE                                                  AS code,
        s.DESCRIPTION                                           AS code_display,
        CAST(CASE WHEN s.STOP IS NULL THEN 'active' ELSE 'resolved' END AS NVARCHAR(500)) AS clinical_status,
        CAST(N'confirmed' AS NVARCHAR(500))                     AS verification_status,
        s.START                                                 AS onset_datetime,
        s.STOP                                                  AS abatement_datetime
    FROM dedup AS s
    JOIN etl.fhir_crosswalk AS x
      ON x.resource_type = 'Condition' AND x.legacy_id = s.legacy_key
    LEFT JOIN etl.fhir_crosswalk AS px
      ON px.resource_type = 'Patient' AND px.legacy_id = s.PATIENT
    LEFT JOIN etl.fhir_crosswalk AS ex
      ON ex.resource_type = 'Encounter' AND ex.legacy_id = s.ENCOUNTER
    WHERE s.copy_no = 1
)
SELECT e.legacy_key AS legacy_id, e.fhir_id, f.field_name, f.expected_value, f.actual_value,
       CASE WHEN ISNULL(f.expected_value, N'<null>') COLLATE Latin1_General_BIN2
               = ISNULL(f.actual_value,   N'<null>') COLLATE Latin1_General_BIN2
            THEN 1 ELSE 0 END AS is_match
FROM expected AS e
JOIN tgt.conditions AS t ON t.fhir_id = e.fhir_id
CROSS APPLY (VALUES
    ('patient_link',        e.patient_fhir_id,     t.patient_fhir_id),
    ('encounter_link',      e.encounter_fhir_id,   t.encounter_fhir_id),
    ('code_system',         e.code_system,         t.code_system),
    ('code',                e.code,                t.code),
    ('code_display',        e.code_display,        t.code_display),
    ('clinical_status',     e.clinical_status,     t.clinical_status),
    ('verification_status', e.verification_status, t.verification_status),
    ('onset_datetime',      e.onset_datetime,      t.onset_datetime),
    ('abatement_datetime',  e.abatement_datetime,  t.abatement_datetime)
) AS f (field_name, expected_value, actual_value);
GO

/* -----------------------------------------------------------------------------
   FIELD SUMMARY: match rate for every field of every resource
   -------------------------------------------------------------------------- */
CREATE OR ALTER VIEW rpt.v_field_summary AS
SELECT resource_type, field_name,
       COUNT(*)                         AS values_compared,
       SUM(is_match)                    AS values_matched,
       COUNT(*) - SUM(is_match)         AS values_mismatched,
       CAST(100.0 * SUM(is_match) / COUNT(*) AS DECIMAL(5,1)) AS match_pct
FROM (
    SELECT 'Patient'   AS resource_type, field_name, is_match FROM rpt.v_patient_field_compare
    UNION ALL
    SELECT 'Encounter' AS resource_type, field_name, is_match FROM rpt.v_encounter_field_compare
    UNION ALL
    SELECT 'Condition' AS resource_type, field_name, is_match FROM rpt.v_condition_field_compare
) AS all_fields
GROUP BY resource_type, field_name;
GO

/* -----------------------------------------------------------------------------
   ROW RECONCILIATION: source rows = migrated + rejected (direct) + rejected (cascade) + duplicates removed
   -------------------------------------------------------------------------- */
CREATE OR ALTER VIEW rpt.v_row_reconciliation AS
WITH counts AS (
    SELECT 'patients' AS table_name,
           (SELECT COUNT(*) FROM stg.patients)   AS source_rows,
           (SELECT COUNT(*) FROM tgt.patients)   AS migrated
    UNION ALL
    SELECT 'encounters', (SELECT COUNT(*) FROM stg.encounters), (SELECT COUNT(*) FROM tgt.encounters)
    UNION ALL
    SELECT 'conditions', (SELECT COUNT(*) FROM stg.conditions), (SELECT COUNT(*) FROM tgt.conditions)
),
issues AS (
    SELECT table_name,
           COUNT(DISTINCT CASE WHEN action = 'Reject'           THEN record_key END) AS rejected_direct,
           COUNT(DISTINCT CASE WHEN action = 'Reject (cascade)' THEN record_key END) AS rejected_cascade,
           SUM(CASE WHEN rule_id IN ('R-P05', 'R-C05') THEN 1 ELSE 0 END)           AS duplicates_removed
    FROM etl.issue_log
    GROUP BY table_name
)
SELECT c.table_name, c.source_rows, c.migrated,
       ISNULL(i.rejected_direct, 0)    AS rejected_direct,
       ISNULL(i.rejected_cascade, 0)   AS rejected_cascade,
       ISNULL(i.duplicates_removed, 0) AS duplicates_removed,
       c.source_rows - c.migrated - ISNULL(i.rejected_direct, 0)
           - ISNULL(i.rejected_cascade, 0) - ISNULL(i.duplicates_removed, 0) AS unaccounted,
       CASE WHEN c.source_rows - c.migrated - ISNULL(i.rejected_direct, 0)
                 - ISNULL(i.rejected_cascade, 0) - ISNULL(i.duplicates_removed, 0) = 0
            THEN 'PASS' ELSE 'FAIL' END AS result
FROM counts AS c
LEFT JOIN issues AS i ON i.table_name = c.table_name;
GO

/* -----------------------------------------------------------------------------
   ISSUE SUMMARY: what the pipeline found, by rule (for the dashboard)
   -------------------------------------------------------------------------- */
CREATE OR ALTER VIEW rpt.v_issue_summary AS
SELECT table_name, rule_id, action, COUNT(*) AS issues
FROM etl.issue_log
GROUP BY table_name, rule_id, action;
GO

/* =============================================================================
   THE REPORT - run everything above, then read these result grids top to bottom
   ========================================================================== */

-- 1. Is every source row accounted for?  (expect 3 rows, all PASS, unaccounted = 0)
SELECT * FROM rpt.v_row_reconciliation ORDER BY table_name DESC;

-- 2. Overall field match rate per resource  (expect 100.0 for all three)
SELECT resource_type,
       SUM(values_compared)   AS values_compared,
       SUM(values_mismatched) AS values_mismatched,
       CAST(100.0 * SUM(values_matched) / SUM(values_compared) AS DECIMAL(5,1)) AS match_pct
FROM rpt.v_field_summary
GROUP BY resource_type
ORDER BY resource_type DESC;

-- 3. Match rate for every individual field
SELECT * FROM rpt.v_field_summary ORDER BY resource_type DESC, field_name;

-- 4. Any mismatches? (expect NO rows - if rows appear, these are the ones to investigate)
SELECT TOP (50) 'Patient' AS resource_type, * FROM rpt.v_patient_field_compare   WHERE is_match = 0
UNION ALL
SELECT TOP (50) 'Encounter', *             FROM rpt.v_encounter_field_compare WHERE is_match = 0
UNION ALL
SELECT TOP (50) 'Condition', *             FROM rpt.v_condition_field_compare WHERE is_match = 0;

-- 5. Spot check: the planted bad-date patient - legacy value vs. what the server holds
SELECT p.Id AS legacy_id, p.BIRTHDATE AS legacy_birthdate,
       c.expected_value AS expected_on_server, c.actual_value AS actually_on_server, c.is_match
FROM etl.issue_log AS i
JOIN stg.patients AS p ON p.Id = i.record_key
JOIN rpt.v_patient_field_compare AS c ON c.legacy_id = p.Id AND c.field_name = 'birth_date'
WHERE i.rule_id = 'R-P02';

-- 6. Spot check: the two flagged patients (missing address) - loaded, but without a street line
SELECT c.legacy_id, c.fhir_id, c.expected_value, c.actual_value, c.is_match
FROM etl.issue_log AS i
JOIN rpt.v_patient_field_compare AS c ON c.legacy_id = i.record_key AND c.field_name = 'address_line'
WHERE i.rule_id = 'R-P04';
