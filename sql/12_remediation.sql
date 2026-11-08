/* =============================================================================
   12_remediation.sql
   Step 13: turn rejected records into a staff worklist and track each fix.
   Covers Phase 1 (patients, encounters, conditions) and Phase 2 (providers).

   Creates:
     rpt.v_remediation_worklist  one row per ROOT-CAUSE problem (not per cascade),
                                 with the context staff need, who should fix it,
                                 how many other records it blocks, and its status
     rpt.v_remediation_log       every correction ever entered and its outcome
     rpt.v_remediation_summary   counts by status (for the dashboard)

   Run by src/export_worklist.py (which also writes remediation/worklist.csv),
   or open in SSMS and press F5. Requires src/apply_corrections.py and
   src/transform.py to have run at least once. Safe to re-run.
   Phase 2 rows appear once src/phase2_practitioner.py has run.
   ========================================================================== */

USE FhirMigration;
GO

IF SCHEMA_ID('rpt') IS NULL EXEC('CREATE SCHEMA rpt');
GO

-- etl.corrections is normally created by src/apply_corrections.py; make sure it exists
IF OBJECT_ID('etl.corrections') IS NULL
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
);
GO

-- Phase 2 tables are created by src/phase2_practitioner.py. If Phase 2 has not
-- run yet, create EMPTY placeholders so the views below can still be created.
-- (The script replaces them with the real tables the first time it runs.)
IF OBJECT_ID('etl.practitioner_issue_log') IS NULL
CREATE TABLE etl.practitioner_issue_log (
    record_key NVARCHAR(500) NULL, source_row NVARCHAR(500) NULL, rule_id NVARCHAR(500) NULL,
    action NVARCHAR(500) NULL, detail NVARCHAR(500) NULL,
    saved_at DATETIME2 DEFAULT SYSUTCDATETIME()
);
GO
IF OBJECT_ID('etl.practitioner_crosswalk') IS NULL
CREATE TABLE etl.practitioner_crosswalk (
    legacy_id NVARCHAR(500) NULL, fhir_id NVARCHAR(500) NULL, server_status NVARCHAR(500) NULL,
    saved_at DATETIME2 DEFAULT SYSUTCDATETIME()
);
GO
IF OBJECT_ID('cur.providers') IS NULL
CREATE TABLE cur.providers (
    Id NVARCHAR(500) NULL, NAME NVARCHAR(500) NULL, GENDER NVARCHAR(500) NULL,
    NPI NVARCHAR(500) NULL, source_row INT NULL
);
GO

/* -----------------------------------------------------------------------------
   WORKLIST: direct rejects only. A cascade reject is a symptom; fixing its
   root cause (usually the patient) releases it automatically.
   Providers: rejects, plus the two flags that need Credentialing to supply
   data (R-PR03 gender, R-PR06 NPI). For a rejected provider, "blocked" counts
   the migrated visits that are waiting for that provider.
   -------------------------------------------------------------------------- */
CREATE OR ALTER VIEW rpt.v_remediation_worklist AS
WITH roots AS (
    SELECT i.table_name, i.record_key, i.source_row, i.rule_id, i.detail, i.logged_at,
           -- which source column the rule is about
           CASE i.rule_id
               WHEN 'R-P01' THEN 'BIRTHDATE'      WHEN 'R-P02' THEN 'BIRTHDATE'
               WHEN 'R-P06' THEN 'BIRTHDATE'      WHEN 'R-P03' THEN 'GENDER'
               WHEN 'R-P07' THEN 'FIRST'          WHEN 'R-E01' THEN 'PATIENT'
               WHEN 'R-E03' THEN 'ENCOUNTERCLASS' WHEN 'R-E05' THEN 'START'
               WHEN 'R-C01' THEN 'CODE'           WHEN 'R-C06' THEN 'SYSTEM'
           END AS field
    FROM etl.issue_log AS i
    WHERE i.action = 'Reject'
),
blocked AS (
    SELECT blocked_by,
           SUM(CASE WHEN table_name = 'encounters' THEN 1 ELSE 0 END) AS blocked_encounters,
           SUM(CASE WHEN table_name = 'conditions' THEN 1 ELSE 0 END) AS blocked_conditions
    FROM etl.issue_log
    WHERE action = 'Reject (cascade)'
    GROUP BY blocked_by
),
detail AS (
    SELECT r.*,
           COALESCE(p.Id, e.PATIENT, c.PATIENT)                         AS patient_id,
           COALESCE(NULLIF(LTRIM(CONCAT(p.FIRST,  ' ', p.LAST)),  ''),
                    NULLIF(LTRIM(CONCAT(pe.FIRST, ' ', pe.LAST)), ''),
                    NULLIF(LTRIM(CONCAT(pc.FIRST, ' ', pc.LAST)), ''))  AS patient_name,
           CASE r.table_name
               WHEN 'patients'   THEN CONCAT('DOB on file: ', ISNULL(p.BIRTHDATE, '(blank)'),
                                             ' | gender on file: ', ISNULL(p.GENDER, '(blank)'))
               WHEN 'encounters' THEN CONCAT('Visit ', LEFT(e.START, 10), ' - ', e.DESCRIPTION)
               WHEN 'conditions' THEN CONCAT('Diagnosis: ', ISNULL(c.DESCRIPTION, '(no description)'),
                                             ' | onset ', LEFT(c.START, 10))
           END                                                          AS context,
           CASE r.field
               WHEN 'BIRTHDATE'      THEN p.BIRTHDATE
               WHEN 'GENDER'         THEN p.GENDER
               WHEN 'FIRST'          THEN p.FIRST
               WHEN 'PATIENT'        THEN COALESCE(e.PATIENT, c.PATIENT)
               WHEN 'ENCOUNTERCLASS' THEN e.ENCOUNTERCLASS
               WHEN 'START'          THEN COALESCE(e.START, c.START)
               WHEN 'CODE'           THEN c.CODE
               WHEN 'SYSTEM'         THEN c.SYSTEM
           END                                                          AS current_value,
           ISNULL(b.blocked_encounters, 0)                              AS blocked_encounters,
           ISNULL(b.blocked_conditions, 0)                              AS blocked_conditions,
           CASE r.table_name
               WHEN 'patients'   THEN 'Registration / Patient Access'
               WHEN 'encounters' THEN 'Health Information Management (HIM)'
               WHEN 'conditions' THEN 'Coding'
           END                                                          AS suggested_owner,
           CASE r.rule_id
               WHEN 'R-P01' THEN 'Enter the date of birth from the registration record'
               WHEN 'R-P02' THEN 'Enter the date of birth as YYYY-MM-DD'
               WHEN 'R-P06' THEN 'Check birth and death dates against the registration record'
               WHEN 'R-P03' THEN 'Enter administrative gender (M or F) from the registration record'
               WHEN 'R-P07' THEN 'Enter the patient''s first and last name'
               WHEN 'R-E01' THEN 'Identify the correct patient, or archive if it cannot be identified'
               WHEN 'R-E03' THEN 'Enter a valid encounter class'
               WHEN 'R-E05' THEN 'Enter the visit start date and time'
               WHEN 'R-C01' THEN 'Enter the diagnosis code from the clinical record'
               WHEN 'R-C06' THEN 'Enter the code system (SNOMED-CT or ICD-10-CM)'
           END                                                          AS what_is_needed
    FROM roots AS r
    LEFT JOIN cur.patients   AS p  ON r.table_name = 'patients'   AND p.source_row = r.source_row
    LEFT JOIN cur.encounters AS e  ON r.table_name = 'encounters' AND e.source_row = r.source_row
    LEFT JOIN cur.conditions AS c  ON r.table_name = 'conditions' AND c.source_row = r.source_row
    LEFT JOIN cur.patients   AS pe ON pe.Id = e.PATIENT AND pe.source_row = (SELECT MIN(source_row) FROM cur.patients WHERE Id = e.PATIENT)
    LEFT JOIN cur.patients   AS pc ON pc.Id = c.PATIENT AND pc.source_row = (SELECT MIN(source_row) FROM cur.patients WHERE Id = c.PATIENT)
    LEFT JOIN blocked        AS b  ON r.table_name = 'patients'   AND b.blocked_by = r.record_key
),
-- ---------- Phase 2: providers ----------
provider_visits AS (            -- migrated visits per legacy provider
    SELECT e.PROVIDER AS provider_id, COUNT(*) AS visits
    FROM cur.encounters AS e
    JOIN etl.fhir_crosswalk AS xe
      ON xe.resource_type = 'Encounter' AND xe.legacy_id = e.Id AND xe.fhir_id IS NOT NULL
    GROUP BY e.PROVIDER
),
provider_detail AS (
    SELECT CAST(N'providers' AS NVARCHAR(50))                           AS table_name,
           i.record_key,
           TRY_CAST(i.source_row AS INT)                                AS source_row,
           i.rule_id, i.detail,
           i.saved_at                                                   AS logged_at,
           CASE i.rule_id WHEN 'R-PR02' THEN 'NAME'   WHEN 'R-PR03' THEN 'GENDER'
                          WHEN 'R-PR06' THEN 'NPI'    ELSE 'Id' END     AS field,
           CAST(NULL AS NVARCHAR(500))                                  AS patient_id,
           CAST(NULL AS NVARCHAR(500))                                  AS patient_name,
           CONCAT(N'Provider on file: ', ISNULL(pr.NAME, N'(blank)'),
                  N' | NPI on file: ',   ISNULL(pr.NPI, N'(blank)'),
                  N' | gender on file: ', ISNULL(pr.GENDER, N'(blank)')) AS context,
           CASE i.rule_id WHEN 'R-PR02' THEN pr.NAME  WHEN 'R-PR03' THEN pr.GENDER
                          WHEN 'R-PR06' THEN pr.NPI   ELSE pr.Id END    AS current_value,
           CASE WHEN i.action = 'Reject' THEN ISNULL(pv.visits, 0) ELSE 0 END AS blocked_encounters,
           0                                                            AS blocked_conditions,
           N'Credentialing / Provider Enrollment'                       AS suggested_owner,
           CASE i.rule_id
               WHEN 'R-PR01' THEN 'Identify the provider, or archive if it cannot be identified'
               WHEN 'R-PR02' THEN 'Confirm the provider''s legal name in the credentialing file, or archive if this is not a real provider'
               WHEN 'R-PR03' THEN 'Enter gender (M or F) from the credentialing file'
               WHEN 'R-PR05' THEN 'Add the provider to the credentialing roster, or archive'
               WHEN 'R-PR06' THEN 'Enter the 10-digit NPI from the credentialing file (NPPES)'
           END                                                          AS what_is_needed
    FROM etl.practitioner_issue_log AS i
    LEFT JOIN cur.providers    AS pr ON pr.source_row = TRY_CAST(i.source_row AS INT)
    LEFT JOIN provider_visits  AS pv ON pv.provider_id = i.record_key
    WHERE i.action = 'Reject' OR i.rule_id IN ('R-PR03', 'R-PR06')
),
all_detail AS (
    SELECT table_name, record_key, source_row, rule_id, detail, logged_at, field,
           patient_id, patient_name, context, current_value,
           blocked_encounters, blocked_conditions, suggested_owner, what_is_needed
    FROM detail
    UNION ALL
    SELECT table_name, record_key, source_row, rule_id, detail, logged_at, field,
           patient_id, patient_name, context, current_value,
           blocked_encounters, blocked_conditions, suggested_owner, what_is_needed
    FROM provider_detail
)
SELECT ROW_NUMBER() OVER (ORDER BY d.blocked_encounters + d.blocked_conditions DESC,
                                   CASE d.table_name WHEN 'patients' THEN 1 WHEN 'encounters' THEN 2
                                                     WHEN 'conditions' THEN 3 ELSE 4 END,
                                   d.source_row)                        AS priority,
       CASE
           WHEN k.disposition = 'Archive'                         THEN 'Archived - not migrated'
           WHEN k.disposition = 'Correct' AND k.applied_at > d.logged_at THEN 'Correction entered - rerun transform'
           WHEN k.disposition = 'Correct'                         THEN 'Corrected but still failing - review'
           ELSE 'Open'
       END                                                              AS status,
       d.table_name, d.source_row, d.record_key, d.rule_id,
       d.detail                                                         AS problem,
       d.field, d.current_value, d.patient_id, d.patient_name, d.context,
       d.blocked_encounters, d.blocked_conditions,
       d.blocked_encounters + d.blocked_conditions                      AS records_blocked,
       d.suggested_owner, d.what_is_needed
FROM all_detail AS d
OUTER APPLY (SELECT TOP (1) disposition, applied_at
             FROM etl.corrections AS k
             WHERE k.table_name = d.table_name AND k.source_row = d.source_row
             ORDER BY k.correction_id DESC) AS k;
GO

/* -----------------------------------------------------------------------------
   REMEDIATION LOG: every correction and what happened to the record after it
   -------------------------------------------------------------------------- */
CREATE OR ALTER VIEW rpt.v_remediation_log AS
WITH keyed AS (
    SELECT k.*,
           CASE k.table_name
               WHEN 'patients'   THEN p.Id
               WHEN 'encounters' THEN e.Id
               WHEN 'conditions' THEN CONCAT(c.PATIENT, '_', c.ENCOUNTER, '_', c.CODE, '_', c.START)
               WHEN 'providers'  THEN pr.Id
           END AS current_key,
           CASE k.table_name WHEN 'patients'   THEN 'Patient'
                             WHEN 'encounters' THEN 'Encounter'
                             WHEN 'providers'  THEN 'Practitioner'
                             ELSE 'Condition' END AS resource_type
    FROM etl.corrections AS k
    LEFT JOIN cur.patients   AS p  ON k.table_name = 'patients'   AND p.source_row  = k.source_row
    LEFT JOIN cur.encounters AS e  ON k.table_name = 'encounters' AND e.source_row  = k.source_row
    LEFT JOIN cur.conditions AS c  ON k.table_name = 'conditions' AND c.source_row  = k.source_row
    LEFT JOIN cur.providers  AS pr ON k.table_name = 'providers'  AND pr.source_row = k.source_row
)
SELECT k.correction_id, k.table_name, k.source_row, k.rule_id, k.field_name,
       k.original_value, k.corrected_value, k.disposition, k.reason,
       k.corrected_by, k.corrected_date, k.applied_at,
       COALESCE(x.fhir_id, xp.fhir_id) AS fhir_id,
       CASE
           WHEN k.disposition = 'Archive' THEN 'Archived - not migrated'
           WHEN EXISTS (SELECT 1 FROM etl.issue_log AS i
                        WHERE i.table_name = k.table_name AND i.source_row = k.source_row
                          AND i.action = 'Reject') THEN 'Still failing - review'
           WHEN EXISTS (SELECT 1 FROM etl.practitioner_issue_log AS i
                        WHERE k.table_name = 'providers'
                          AND TRY_CAST(i.source_row AS INT) = k.source_row
                          AND i.rule_id = k.rule_id
                          AND i.action IN ('Reject', 'Flag')) THEN 'Still failing - review'
           WHEN COALESCE(x.fhir_id, xp.fhir_id) IS NOT NULL THEN 'Resolved - migrated'
           ELSE 'Resolved - awaiting load'
       END AS outcome
FROM keyed AS k
LEFT JOIN etl.fhir_crosswalk AS x
       ON x.resource_type = k.resource_type AND x.legacy_id = k.current_key
LEFT JOIN etl.practitioner_crosswalk AS xp
       ON k.resource_type = 'Practitioner' AND xp.legacy_id = k.current_key AND xp.fhir_id IS NOT NULL;
GO

/* -----------------------------------------------------------------------------
   SUMMARY: open vs. closed work (dashboard card / chart)
   -------------------------------------------------------------------------- */
CREATE OR ALTER VIEW rpt.v_remediation_summary AS
SELECT status, COUNT(*) AS problems, SUM(records_blocked) AS records_blocked
FROM rpt.v_remediation_worklist
GROUP BY status
UNION ALL
SELECT outcome, COUNT(*), 0
FROM rpt.v_remediation_log
WHERE outcome LIKE 'Resolved%'
GROUP BY outcome;
GO

-- Quick check
SELECT * FROM rpt.v_remediation_worklist ORDER BY priority;
SELECT * FROM rpt.v_remediation_log ORDER BY correction_id;
SELECT * FROM rpt.v_remediation_summary;
