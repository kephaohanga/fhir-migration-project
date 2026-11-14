/* =============================================================================
   25_allergy_reconciliation.sql
   Phase 2, step 6: reconcile the AllergyIntolerance migration.

   Run AFTER:  python src\plant_allergy_errors.py
               python src\phase2_allergy.py
   How to run: open in SSMS, press F5. Read the result grids top to bottom.
   Safe to re-run (CREATE OR ALTER).

   Four questions:
     1. Did every legacy allergy row get exactly one disposition?   (row reconciliation)
     2. Does the server hold exactly what we sent?                  (field comparison)
     3. Does each allergy point to the right patient and visit?     (link integrity)
     4. What was migrated: codes, statuses, categories?             (summary)
   ========================================================================== */

USE FhirMigration;
GO

IF SCHEMA_ID('rpt') IS NULL EXEC('CREATE SCHEMA rpt');
GO

-- 1. ROW RECONCILIATION
CREATE OR ALTER VIEW rpt.v_allergy_row_reconciliation AS
WITH d AS (
    SELECT
        (SELECT COUNT(*) FROM stg.allergies)                                   AS source_rows,
        SUM(CASE WHEN disposition = 'Migrated'           THEN 1 ELSE 0 END)    AS disposition_migrated,
        SUM(CASE WHEN disposition = 'Rejected'           THEN 1 ELSE 0 END)    AS rejected,
        SUM(CASE WHEN disposition = 'Rejected (cascade)' THEN 1 ELSE 0 END)    AS rejected_cascade,
        SUM(CASE WHEN disposition = 'Duplicate removed'  THEN 1 ELSE 0 END)    AS duplicates_removed,
        COUNT(DISTINCT source_row)                                             AS rows_with_disposition,
        (SELECT COUNT(*) FROM etl.allergy_crosswalk WHERE fhir_id IS NOT NULL) AS confirmed_on_server
    FROM etl.allergy_disposition
)
SELECT source_rows, disposition_migrated, confirmed_on_server, rejected, rejected_cascade, duplicates_removed,
       source_rows - disposition_migrated - rejected - rejected_cascade - duplicates_removed AS unaccounted,
       CASE WHEN source_rows = disposition_migrated + rejected + rejected_cascade + duplicates_removed
             AND rows_with_disposition = source_rows
             AND confirmed_on_server = disposition_migrated
            THEN 'PASS' ELSE 'FAIL' END AS result
FROM d;
GO

-- 2. FIELD COMPARISON (long format)
CREATE OR ALTER VIEW rpt.v_allergy_field_compare AS
SELECT e.legacy_id, x.fhir_id, e.field_name, e.expected_value, t.actual_value,
       CASE WHEN ISNULL(e.expected_value, N'<null>') COLLATE Latin1_General_BIN2
               = ISNULL(t.actual_value,   N'<null>') COLLATE Latin1_General_BIN2
            THEN 1 ELSE 0 END AS is_match
FROM etl.allergy_expected AS e
JOIN etl.allergy_crosswalk AS x
  ON x.resource_type = 'AllergyIntolerance' AND x.legacy_id = e.legacy_id AND x.fhir_id IS NOT NULL
LEFT JOIN tgt.allergy_resources AS t
  ON t.resource_type = 'AllergyIntolerance' AND t.fhir_id = x.fhir_id AND t.field_name = e.field_name;
GO

-- 3. LINK INTEGRITY: patient and visit are the ones migrated from the legacy IDs
CREATE OR ALTER VIEW rpt.v_allergy_link_check AS
WITH links AS (
    SELECT fhir_id,
           MAX(CASE WHEN field_name = 'patient_ref'   THEN actual_value END) AS patient_ref,
           MAX(CASE WHEN field_name = 'encounter_ref' THEN actual_value END) AS encounter_ref
    FROM tgt.allergy_resources
    WHERE resource_type = 'AllergyIntolerance'
    GROUP BY fhir_id
)
SELECT s.allergy_key, x.fhir_id,
       CASE WHEN l.patient_ref   = N'Patient/'   + px.fhir_id THEN 'OK' ELSE 'BROKEN' END AS patient_link,
       CASE WHEN l.encounter_ref = N'Encounter/' + ex.fhir_id THEN 'OK' ELSE 'BROKEN' END AS encounter_link
FROM etl.allergy_sources AS s
JOIN etl.allergy_crosswalk AS x
  ON x.resource_type = 'AllergyIntolerance' AND x.legacy_id = s.allergy_key AND x.fhir_id IS NOT NULL
LEFT JOIN links AS l ON l.fhir_id = x.fhir_id
LEFT JOIN etl.fhir_crosswalk AS px ON px.resource_type = 'Patient'   AND px.legacy_id = s.legacy_patient
LEFT JOIN etl.fhir_crosswalk AS ex ON ex.resource_type = 'Encounter' AND ex.legacy_id = s.legacy_encounter;
GO

/* =============================================================================
   THE REPORT
   ========================================================================== */

-- 1. Every legacy allergy row accounted for  (expect PASS, unaccounted 0)
SELECT * FROM rpt.v_allergy_row_reconciliation;

-- 2. Field match  (expect 100.0)
SELECT COUNT(*) AS values_compared, SUM(is_match) AS values_matched,
       COUNT(*) - SUM(is_match) AS values_mismatched,
       CAST(100.0 * SUM(is_match) / NULLIF(COUNT(*), 0) AS DECIMAL(5,1)) AS match_pct
FROM rpt.v_allergy_field_compare;

-- 2b. Any mismatches?  (expect NO rows)
SELECT TOP (50) * FROM rpt.v_allergy_field_compare WHERE is_match = 0;

-- 3. Link integrity  (expect only OK)
SELECT patient_link, encounter_link, COUNT(*) AS allergies
FROM rpt.v_allergy_link_check
GROUP BY patient_link, encounter_link;

-- 4. What was migrated: one row per allergy, then summaries
WITH a AS (
    SELECT fhir_id,
           MAX(CASE WHEN field_name = 'code_system'     THEN actual_value END) AS code_system,
           MAX(CASE WHEN field_name = 'clinical_status' THEN actual_value END) AS clinical_status,
           MAX(CASE WHEN field_name = 'category'        THEN actual_value END) AS category,
           MAX(CASE WHEN field_name = 'type'            THEN actual_value END) AS allergy_type,
           MAX(CASE WHEN field_name = 'reaction1_severity' THEN actual_value END) AS severity
    FROM tgt.allergy_resources
    WHERE resource_type = 'AllergyIntolerance'
    GROUP BY fhir_id
)
SELECT CASE code_system WHEN 'http://snomed.info/sct' THEN 'SNOMED CT'
                        WHEN 'http://www.nlm.nih.gov/research/umls/rxnorm' THEN 'RxNorm'
                        ELSE ISNULL(code_system, '(none)') END AS code_system,
       allergy_type, category, clinical_status,
       ISNULL(severity, '(no reaction)') AS first_reaction_severity,
       COUNT(*) AS allergies
FROM a
GROUP BY code_system, allergy_type, category, clinical_status, severity
ORDER BY allergies DESC;

-- 4b. Rules triggered
SELECT rule_id, action, COUNT(*) AS rows_affected
FROM etl.allergy_issue_log
GROUP BY rule_id, action
ORDER BY rule_id, action;
