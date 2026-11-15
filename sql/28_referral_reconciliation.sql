/* =============================================================================
   28_referral_reconciliation.sql
   Phase 3, step 1: reconcile the referral -> ServiceRequest migration.

   Run AFTER:  python src\plant_referral_errors.py
               python src\phase3_referral.py
   How to run: open in SSMS, press F5. Read the result grids top to bottom.
   Safe to re-run (CREATE OR ALTER).

   Five questions:
     1. Did every legacy referral row get exactly one disposition?      (row reconciliation)
     2. Does the server hold exactly what we sent?                      (field comparison)
     3. Does each referral point to the right patient and provider?     (link integrity)
     4. Was the legacy status kept? OPEN and SCHEDULED both became
        "active", so the extension must still tell them apart          (status check)
     5. What was migrated: specialties, priority, diagnosis coverage   (summary)
   ========================================================================== */

USE FhirMigration;
GO

IF SCHEMA_ID('rpt') IS NULL EXEC('CREATE SCHEMA rpt');
GO

-- 1. ROW RECONCILIATION
CREATE OR ALTER VIEW rpt.v_referral_row_reconciliation AS
WITH d AS (
    SELECT
        (SELECT COUNT(*) FROM stg.referrals)                                    AS source_rows,
        SUM(CASE WHEN disposition = 'Migrated'          THEN 1 ELSE 0 END)      AS disposition_migrated,
        SUM(CASE WHEN disposition = 'Rejected'          THEN 1 ELSE 0 END)      AS rejected,
        SUM(CASE WHEN disposition = 'Duplicate removed' THEN 1 ELSE 0 END)      AS duplicates_removed,
        COUNT(DISTINCT source_row)                                              AS rows_with_disposition,
        (SELECT COUNT(*) FROM etl.referral_crosswalk WHERE fhir_id IS NOT NULL) AS confirmed_on_server
    FROM etl.referral_disposition
)
SELECT source_rows, disposition_migrated, confirmed_on_server, rejected, duplicates_removed,
       source_rows - disposition_migrated - rejected - duplicates_removed AS unaccounted,
       CASE WHEN source_rows = disposition_migrated + rejected + duplicates_removed
             AND rows_with_disposition = source_rows
             AND confirmed_on_server = disposition_migrated
            THEN 'PASS' ELSE 'FAIL' END AS result
FROM d;
GO

-- 2. FIELD COMPARISON (long format)
CREATE OR ALTER VIEW rpt.v_referral_field_compare AS
SELECT e.legacy_id, x.fhir_id, e.field_name, e.expected_value, t.actual_value,
       CASE WHEN ISNULL(e.expected_value, N'<null>') COLLATE Latin1_General_BIN2
               = ISNULL(t.actual_value,   N'<null>') COLLATE Latin1_General_BIN2
            THEN 1 ELSE 0 END AS is_match
FROM etl.referral_expected AS e
JOIN etl.referral_crosswalk AS x
  ON x.resource_type = 'ServiceRequest' AND x.legacy_id = e.legacy_id AND x.fhir_id IS NOT NULL
LEFT JOIN tgt.referral_resources AS t
  ON t.resource_type = 'ServiceRequest' AND t.fhir_id = x.fhir_id AND t.field_name = e.field_name;
GO

-- 3. LINK INTEGRITY: subject = the migrated patient, requester = the migrated provider
CREATE OR ALTER VIEW rpt.v_referral_link_check AS
WITH links AS (
    SELECT fhir_id,
           MAX(CASE WHEN field_name = 'subject_ref'   THEN actual_value END) AS subject_ref,
           MAX(CASE WHEN field_name = 'requester_ref' THEN actual_value END) AS requester_ref
    FROM tgt.referral_resources
    WHERE resource_type = 'ServiceRequest'
    GROUP BY fhir_id
)
SELECT s.referral_key, x.fhir_id,
       CASE WHEN l.subject_ref = N'Patient/' + px.fhir_id THEN 'OK' ELSE 'BROKEN' END AS subject_link,
       CASE WHEN l.requester_ref IS NULL AND pr.fhir_id IS NULL      THEN 'No requester (flagged)'
            WHEN l.requester_ref = N'Practitioner/' + pr.fhir_id     THEN 'OK'
            ELSE 'BROKEN' END                                                      AS requester_link
FROM etl.referral_sources AS s
JOIN etl.referral_crosswalk AS x
  ON x.resource_type = 'ServiceRequest' AND x.legacy_id = s.referral_key AND x.fhir_id IS NOT NULL
LEFT JOIN links AS l ON l.fhir_id = x.fhir_id
LEFT JOIN etl.fhir_crosswalk AS px ON px.resource_type = 'Patient' AND px.legacy_id = s.legacy_patient
LEFT JOIN etl.practitioner_crosswalk AS pr ON pr.legacy_id = s.legacy_requester AND pr.fhir_id IS NOT NULL;
GO

-- 4. STATUS CHECK: legacy status (tidied) vs FHIR status and the legacy-status extension
CREATE OR ALTER VIEW rpt.v_referral_status_check AS
WITH srv AS (
    SELECT fhir_id,
           MAX(CASE WHEN field_name = 'status'        THEN actual_value END) AS fhir_status,
           MAX(CASE WHEN field_name = 'legacy_status' THEN actual_value END) AS legacy_status_on_server
    FROM tgt.referral_resources
    WHERE resource_type = 'ServiceRequest'
    GROUP BY fhir_id
)
SELECT UPPER(LTRIM(RTRIM(r.STATUS))) AS legacy_status,
       s.fhir_status, s.legacy_status_on_server,
       CASE WHEN s.legacy_status_on_server = UPPER(LTRIM(RTRIM(r.STATUS)))
             AND s.fhir_status = CASE UPPER(LTRIM(RTRIM(r.STATUS)))
                                     WHEN 'COMPLETED' THEN 'completed' ELSE 'active' END
            THEN 'OK' ELSE 'WRONG' END AS result
FROM etl.referral_sources AS src
JOIN cur.referrals AS r ON r.source_row = TRY_CAST(src.source_row AS INT)
JOIN etl.referral_crosswalk AS x
  ON x.resource_type = 'ServiceRequest' AND x.legacy_id = src.referral_key AND x.fhir_id IS NOT NULL
LEFT JOIN srv AS s ON s.fhir_id = x.fhir_id;
GO

/* =============================================================================
   THE REPORT
   ========================================================================== */

-- 1. Every legacy referral row accounted for  (expect PASS, unaccounted 0)
SELECT * FROM rpt.v_referral_row_reconciliation;

-- 2. Field match  (expect 100.0)
SELECT COUNT(*) AS values_compared, SUM(is_match) AS values_matched,
       COUNT(*) - SUM(is_match) AS values_mismatched,
       CAST(100.0 * SUM(is_match) / NULLIF(COUNT(*), 0) AS DECIMAL(5,1)) AS match_pct
FROM rpt.v_referral_field_compare;

-- 2b. Any mismatches?  (expect NO rows)
SELECT TOP (50) * FROM rpt.v_referral_field_compare WHERE is_match = 0;

-- 3. Link integrity  (expect OK; "No requester (flagged)" only for R-RF02 rows)
SELECT subject_link, requester_link, COUNT(*) AS referrals
FROM rpt.v_referral_link_check
GROUP BY subject_link, requester_link;

-- 4. Legacy status kept  (expect only OK; OPEN and SCHEDULED both 'active' but told apart)
SELECT legacy_status, fhir_status, legacy_status_on_server, result, COUNT(*) AS referrals
FROM rpt.v_referral_status_check
GROUP BY legacy_status, fhir_status, legacy_status_on_server, result
ORDER BY legacy_status;

-- 5. Specialties, priority and diagnosis coverage of what was migrated
WITH s AS (
    SELECT fhir_id,
           MAX(CASE WHEN field_name = 'performer_type_code'    THEN actual_value END) AS nucc_code,
           MAX(CASE WHEN field_name = 'performer_type_display' THEN actual_value END) AS specialty,
           MAX(CASE WHEN field_name = 'priority'               THEN actual_value END) AS priority,
           MAX(CASE WHEN field_name = 'reason_code'            THEN actual_value END) AS reason_code
    FROM tgt.referral_resources
    WHERE resource_type = 'ServiceRequest'
    GROUP BY fhir_id
)
SELECT nucc_code, specialty,
       COUNT(*)                                               AS referrals,
       SUM(CASE WHEN priority = 'urgent' THEN 1 ELSE 0 END)   AS urgent,
       SUM(CASE WHEN reason_code IS NULL THEN 1 ELSE 0 END)   AS without_diagnosis
FROM s
GROUP BY nucc_code, specialty
ORDER BY referrals DESC;

-- 5b. Rules triggered
SELECT rule_id, action, COUNT(*) AS rows_affected
FROM etl.referral_issue_log
GROUP BY rule_id, action
ORDER BY rule_id, action;
