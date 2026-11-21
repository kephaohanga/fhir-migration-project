/* =============================================================================
   26_medication_reconciliation.sql
   Phase 2, step 7: reconcile the MedicationRequest migration (full history).

   Run AFTER:  python src\plant_medication_errors.py
               python src\phase2_medication.py
   How to run: open in SSMS, press F5. Read the result grids top to bottom.
   Safe to re-run (CREATE OR ALTER).

   Four questions:
     1. Did every legacy prescription row get exactly one disposition?  (row reconciliation)
     2. Does the server hold exactly what we sent?                      (field comparison)
     3. Does each prescription point to the right patient, visit and
        the visit's practitioner (the prescriber)?                      (link integrity)
     4. Active vs completed, and the most common drugs                  (summary)
   ========================================================================== */

USE FhirMigration;
GO

IF SCHEMA_ID('rpt') IS NULL EXEC('CREATE SCHEMA rpt');
GO

-- 1. ROW RECONCILIATION
CREATE OR ALTER VIEW rpt.v_medication_row_reconciliation AS
WITH d AS (
    SELECT
        (SELECT COUNT(*) FROM stg.medications)                                    AS source_rows,
        SUM(CASE WHEN disposition = 'Migrated'           THEN 1 ELSE 0 END)       AS disposition_migrated,
        SUM(CASE WHEN disposition = 'Rejected'           THEN 1 ELSE 0 END)       AS rejected,
        SUM(CASE WHEN disposition = 'Rejected (cascade)' THEN 1 ELSE 0 END)       AS rejected_cascade,
        SUM(CASE WHEN disposition = 'Duplicate removed'  THEN 1 ELSE 0 END)       AS duplicates_removed,
        COUNT(DISTINCT source_row)                                                AS rows_with_disposition,
        (SELECT COUNT(*) FROM etl.medication_crosswalk WHERE fhir_id IS NOT NULL) AS confirmed_on_server
    FROM etl.medication_disposition
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
CREATE OR ALTER VIEW rpt.v_medication_field_compare AS
SELECT e.legacy_id, x.fhir_id, e.field_name, e.expected_value, t.actual_value,
       CASE WHEN ISNULL(e.expected_value, N'<null>') COLLATE Latin1_General_BIN2
               = ISNULL(t.actual_value,   N'<null>') COLLATE Latin1_General_BIN2
            THEN 1 ELSE 0 END AS is_match
FROM etl.medication_expected AS e
JOIN etl.medication_crosswalk AS x
  ON x.resource_type = 'MedicationRequest' AND x.legacy_id = e.legacy_id AND x.fhir_id IS NOT NULL
LEFT JOIN tgt.medication_resources AS t
  ON t.resource_type = 'MedicationRequest' AND t.fhir_id = x.fhir_id AND t.field_name = e.field_name;
GO

-- 3. LINK INTEGRITY: patient, visit, and requester = the practitioner linked to that visit
CREATE OR ALTER VIEW rpt.v_medication_link_check AS
WITH links AS (
    SELECT fhir_id,
           MAX(CASE WHEN field_name = 'subject_ref'   THEN actual_value END) AS subject_ref,
           MAX(CASE WHEN field_name = 'encounter_ref' THEN actual_value END) AS encounter_ref,
           MAX(CASE WHEN field_name = 'requester_ref' THEN actual_value END) AS requester_ref
    FROM tgt.medication_resources
    WHERE resource_type = 'MedicationRequest'
    GROUP BY fhir_id
)
SELECT s.medication_key, x.fhir_id,
       CASE WHEN l.subject_ref   = N'Patient/'   + px.fhir_id THEN 'OK' ELSE 'BROKEN' END AS subject_link,
       CASE WHEN l.encounter_ref = N'Encounter/' + ex.fhir_id THEN 'OK' ELSE 'BROKEN' END AS encounter_link,
       CASE WHEN l.requester_ref IS NULL                       THEN 'No requester'
            WHEN l.requester_ref = el.practitioner_ref         THEN 'OK'
            ELSE 'BROKEN' END                                                          AS requester_link
FROM etl.medication_sources AS s
JOIN etl.medication_crosswalk AS x
  ON x.resource_type = 'MedicationRequest' AND x.legacy_id = s.medication_key AND x.fhir_id IS NOT NULL
LEFT JOIN links AS l ON l.fhir_id = x.fhir_id
LEFT JOIN etl.fhir_crosswalk AS px ON px.resource_type = 'Patient'   AND px.legacy_id = s.legacy_patient
LEFT JOIN etl.fhir_crosswalk AS ex ON ex.resource_type = 'Encounter' AND ex.legacy_id = s.legacy_encounter
LEFT JOIN tgt.encounter_links AS el ON el.encounter_fhir_id = ex.fhir_id;
GO

/* =============================================================================
   THE REPORT
   ========================================================================== */

-- 1. Every legacy prescription row accounted for  (expect PASS, unaccounted 0)
SELECT * FROM rpt.v_medication_row_reconciliation;

-- 2. Field match  (expect 100.0)
SELECT COUNT(*) AS values_compared, SUM(is_match) AS values_matched,
       COUNT(*) - SUM(is_match) AS values_mismatched,
       CAST(100.0 * SUM(is_match) / NULLIF(COUNT(*), 0) AS DECIMAL(5,1)) AS match_pct
FROM rpt.v_medication_field_compare;

-- 2b. Any mismatches?  (expect NO rows)
SELECT TOP (50) * FROM rpt.v_medication_field_compare WHERE is_match = 0;

-- 3. Link integrity  (expect only OK)
SELECT subject_link, encounter_link, requester_link, COUNT(*) AS prescriptions
FROM rpt.v_medication_link_check
GROUP BY subject_link, encounter_link, requester_link;

-- 4. Active vs completed, and the most common drugs
WITH m AS (
    SELECT fhir_id,
           MAX(CASE WHEN field_name = 'status'       THEN actual_value END) AS status
    FROM tgt.medication_resources
    WHERE resource_type = 'MedicationRequest'
    GROUP BY fhir_id
)
SELECT status, COUNT(*) AS prescriptions FROM m GROUP BY status;

-- Since Phase 3 step 4 each prescription points to a Medication (the drug catalogue):
-- the drug's name is the reference's display; its RxNorm code is on the Medication.
WITH m AS (
    SELECT fhir_id,
           MAX(CASE WHEN field_name = 'medication_ref'     THEN actual_value END) AS medication_ref,
           MAX(CASE WHEN field_name = 'medication_display' THEN actual_value END) AS description
    FROM tgt.medication_resources
    WHERE resource_type = 'MedicationRequest'
    GROUP BY fhir_id
)
SELECT TOP (10) m.medication_ref, m.description, COUNT(*) AS prescriptions
FROM m
GROUP BY m.medication_ref, m.description
ORDER BY prescriptions DESC;

-- 4b. Rules triggered
SELECT rule_id, action, COUNT(*) AS rows_affected
FROM etl.medication_issue_log
GROUP BY rule_id, action
ORDER BY rule_id, action;
