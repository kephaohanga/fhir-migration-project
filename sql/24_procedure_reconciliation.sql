/* =============================================================================
   24_procedure_reconciliation.sql
   Phase 2, step 5: reconcile the Procedure migration.

   Run AFTER:  python src\plant_procedure_errors.py
               python src\phase2_procedure.py
   How to run: open in SSMS, press F5. Read the result grids top to bottom.
   Safe to re-run (CREATE OR ALTER).

   Four questions:
     1. Did every legacy procedure row get exactly one disposition?  (row reconciliation)
     2. Does the server hold exactly what we sent?                   (field comparison)
     3. Does each Procedure point to the right patient, the right
        visit, and that visit's practitioner?                        (link integrity)
     4. What was migrated and what was caught?                       (summary)
   ========================================================================== */

USE FhirMigration;
GO

IF SCHEMA_ID('rpt') IS NULL EXEC('CREATE SCHEMA rpt');
GO

/* -----------------------------------------------------------------------------
   1. ROW RECONCILIATION
   source rows = migrated + rejected + rejected (cascade) + duplicates removed
   -------------------------------------------------------------------------- */
CREATE OR ALTER VIEW rpt.v_procedure_row_reconciliation AS
WITH d AS (
    SELECT
        (SELECT COUNT(*) FROM stg.procedures)                                  AS source_rows,
        SUM(CASE WHEN disposition = 'Migrated'           THEN 1 ELSE 0 END)    AS disposition_migrated,
        SUM(CASE WHEN disposition = 'Rejected'           THEN 1 ELSE 0 END)    AS rejected,
        SUM(CASE WHEN disposition = 'Rejected (cascade)' THEN 1 ELSE 0 END)    AS rejected_cascade,
        SUM(CASE WHEN disposition = 'Duplicate removed'  THEN 1 ELSE 0 END)    AS duplicates_removed,
        COUNT(DISTINCT source_row)                                             AS rows_with_disposition,
        (SELECT COUNT(*) FROM etl.procedure_crosswalk WHERE fhir_id IS NOT NULL) AS confirmed_on_server
    FROM etl.procedure_disposition
)
SELECT source_rows, disposition_migrated, confirmed_on_server, rejected, rejected_cascade, duplicates_removed,
       source_rows - disposition_migrated - rejected - rejected_cascade - duplicates_removed AS unaccounted,
       CASE WHEN source_rows = disposition_migrated + rejected + rejected_cascade + duplicates_removed
             AND rows_with_disposition = source_rows
             AND confirmed_on_server = disposition_migrated
            THEN 'PASS' ELSE 'FAIL' END AS result
FROM d;
GO

/* -----------------------------------------------------------------------------
   2. FIELD COMPARISON (long format: one row per field)
   -------------------------------------------------------------------------- */
CREATE OR ALTER VIEW rpt.v_procedure_field_compare AS
SELECT e.legacy_id, x.fhir_id, e.field_name, e.expected_value, t.actual_value,
       CASE WHEN ISNULL(e.expected_value, N'<null>') COLLATE Latin1_General_BIN2
               = ISNULL(t.actual_value,   N'<null>') COLLATE Latin1_General_BIN2
            THEN 1 ELSE 0 END AS is_match
FROM etl.procedure_expected AS e
JOIN etl.procedure_crosswalk AS x
  ON x.resource_type = 'Procedure' AND x.legacy_id = e.legacy_id AND x.fhir_id IS NOT NULL
LEFT JOIN tgt.procedure_resources AS t
  ON t.resource_type = 'Procedure' AND t.fhir_id = x.fhir_id AND t.field_name = e.field_name;
GO

/* -----------------------------------------------------------------------------
   3. LINK INTEGRITY
      subject   = the patient migrated from the legacy PATIENT
      encounter = the visit migrated from the legacy ENCOUNTER
      performer = the practitioner linked to that visit (from the encounter-link step)
   -------------------------------------------------------------------------- */
CREATE OR ALTER VIEW rpt.v_procedure_link_check AS
WITH links AS (
    SELECT fhir_id,
           MAX(CASE WHEN field_name = 'subject_ref'   THEN actual_value END) AS subject_ref,
           MAX(CASE WHEN field_name = 'encounter_ref' THEN actual_value END) AS encounter_ref,
           MAX(CASE WHEN field_name = 'performer_ref' THEN actual_value END) AS performer_ref
    FROM tgt.procedure_resources
    WHERE resource_type = 'Procedure'
    GROUP BY fhir_id
)
SELECT s.procedure_key, x.fhir_id,
       CASE WHEN l.subject_ref   = N'Patient/'   + px.fhir_id THEN 'OK' ELSE 'BROKEN' END AS subject_link,
       CASE WHEN l.encounter_ref = N'Encounter/' + ex.fhir_id THEN 'OK' ELSE 'BROKEN' END AS encounter_link,
       CASE WHEN l.performer_ref IS NULL                       THEN 'No performer'
            WHEN l.performer_ref = el.practitioner_ref         THEN 'OK'
            ELSE 'BROKEN' END                                                          AS performer_link
FROM etl.procedure_sources AS s
JOIN etl.procedure_crosswalk AS x
  ON x.resource_type = 'Procedure' AND x.legacy_id = s.procedure_key AND x.fhir_id IS NOT NULL
LEFT JOIN links AS l ON l.fhir_id = x.fhir_id
LEFT JOIN etl.fhir_crosswalk AS px ON px.resource_type = 'Patient'   AND px.legacy_id = s.legacy_patient
LEFT JOIN etl.fhir_crosswalk AS ex ON ex.resource_type = 'Encounter' AND ex.legacy_id = s.legacy_encounter
LEFT JOIN tgt.encounter_links AS el ON el.encounter_fhir_id = ex.fhir_id;
GO

/* =============================================================================
   THE REPORT
   ========================================================================== */

-- 1. Every legacy procedure row accounted for  (expect PASS, unaccounted 0)
SELECT * FROM rpt.v_procedure_row_reconciliation;

-- 2. Field match  (expect 100.0)
SELECT COUNT(*) AS values_compared, SUM(is_match) AS values_matched,
       COUNT(*) - SUM(is_match) AS values_mismatched,
       CAST(100.0 * SUM(is_match) / NULLIF(COUNT(*), 0) AS DECIMAL(5,1)) AS match_pct
FROM rpt.v_procedure_field_compare;

-- 2b. Match by field
SELECT field_name, COUNT(*) AS compared, SUM(is_match) AS matched
FROM rpt.v_procedure_field_compare
GROUP BY field_name
ORDER BY field_name;

-- 2c. Any mismatches?  (expect NO rows)
SELECT TOP (50) * FROM rpt.v_procedure_field_compare WHERE is_match = 0;

-- 3. Link integrity  (expect only OK)
SELECT subject_link, encounter_link, performer_link, COUNT(*) AS procedures
FROM rpt.v_procedure_link_check
GROUP BY subject_link, encounter_link, performer_link;

-- 4. Rules triggered
SELECT rule_id, action, COUNT(*) AS rows_affected
FROM etl.procedure_issue_log
GROUP BY rule_id, action
ORDER BY rule_id, action;

-- 4b. Most common procedures migrated (SNOMED CT)
WITH p AS (
    SELECT fhir_id,
           MAX(CASE WHEN field_name = 'code'         THEN actual_value END) AS snomed_code,
           MAX(CASE WHEN field_name = 'code_display' THEN actual_value END) AS description
    FROM tgt.procedure_resources
    WHERE resource_type = 'Procedure'
    GROUP BY fhir_id
)
SELECT TOP (10) snomed_code, description, COUNT(*) AS procedures
FROM p
GROUP BY snomed_code, description
ORDER BY procedures DESC;
