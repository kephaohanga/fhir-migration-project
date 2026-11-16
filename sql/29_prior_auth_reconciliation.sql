/* =============================================================================
   29_prior_auth_reconciliation.sql
   Phase 3, step 2: reconcile prior authorizations -> Claim (request) + ClaimResponse (decision).

   Run AFTER:  python src\plant_prior_auth_errors.py
               python src\phase3_prior_auth.py
   How to run: open in SSMS, press F5. Read the result grids top to bottom.
   Safe to re-run (CREATE OR ALTER).

   Five questions:
     1. Did every legacy row get exactly one disposition, and did every decided
        authorization (approved or denied) get its ClaimResponse?          (row reconciliation)
     2. Does the server hold exactly what we sent?                          (field comparison)
     3. Does each request point to the right patient, payer, policy and referral,
        and each decision to its own request?                               (link integrity)
     4. Does the server tell approved, denied and pending apart correctly?  (decision check)
     5. Which approvals have expired by the extract date?                   (open-work check)
   ========================================================================== */

USE FhirMigration;
GO

IF SCHEMA_ID('rpt') IS NULL EXEC('CREATE SCHEMA rpt');
GO

-- 1. ROW RECONCILIATION
CREATE OR ALTER VIEW rpt.v_prior_auth_row_reconciliation AS
WITH d AS (
    SELECT
        (SELECT COUNT(*) FROM stg.prior_auths)                                     AS source_rows,
        SUM(CASE WHEN disposition = 'Migrated'          THEN 1 ELSE 0 END)         AS disposition_migrated,
        SUM(CASE WHEN disposition = 'Rejected'          THEN 1 ELSE 0 END)         AS rejected,
        SUM(CASE WHEN disposition = 'Duplicate removed' THEN 1 ELSE 0 END)         AS duplicates_removed,
        COUNT(DISTINCT source_row)                                                 AS rows_with_disposition,
        (SELECT COUNT(*) FROM etl.prior_auth_crosswalk
          WHERE resource_type = 'Claim' AND fhir_id IS NOT NULL)                   AS claims_on_server,
        (SELECT COUNT(*) FROM etl.prior_auth_sources WHERE decision <> 'PENDING')  AS decided,
        (SELECT COUNT(*) FROM etl.prior_auth_crosswalk
          WHERE resource_type = 'ClaimResponse' AND fhir_id IS NOT NULL)           AS responses_on_server
    FROM etl.prior_auth_disposition
)
SELECT source_rows, disposition_migrated, claims_on_server, decided, responses_on_server,
       rejected, duplicates_removed,
       source_rows - disposition_migrated - rejected - duplicates_removed AS unaccounted,
       CASE WHEN source_rows = disposition_migrated + rejected + duplicates_removed
             AND rows_with_disposition = source_rows
             AND claims_on_server = disposition_migrated
             AND responses_on_server = decided
            THEN 'PASS' ELSE 'FAIL' END AS result
FROM d;
GO

-- 2. FIELD COMPARISON (long format, both resource types)
CREATE OR ALTER VIEW rpt.v_prior_auth_field_compare AS
SELECT e.resource_type, e.legacy_id, x.fhir_id, e.field_name, e.expected_value, t.actual_value,
       CASE WHEN ISNULL(e.expected_value, N'<null>') COLLATE Latin1_General_BIN2
               = ISNULL(t.actual_value,   N'<null>') COLLATE Latin1_General_BIN2
            THEN 1 ELSE 0 END AS is_match
FROM etl.prior_auth_expected AS e
JOIN etl.prior_auth_crosswalk AS x
  ON x.resource_type = e.resource_type AND x.legacy_id = e.legacy_id AND x.fhir_id IS NOT NULL
LEFT JOIN tgt.prior_auth_resources AS t
  ON t.resource_type = e.resource_type AND t.fhir_id = x.fhir_id AND t.field_name = e.field_name;
GO

-- 3. LINK INTEGRITY: every reference is the one migrated from the SAME legacy record
CREATE OR ALTER VIEW rpt.v_prior_auth_link_check AS
WITH claim AS (
    SELECT fhir_id,
           MAX(CASE WHEN field_name = 'patient_ref'  THEN actual_value END) AS patient_ref,
           MAX(CASE WHEN field_name = 'insurer_ref'  THEN actual_value END) AS insurer_ref,
           MAX(CASE WHEN field_name = 'coverage_ref' THEN actual_value END) AS coverage_ref,
           MAX(CASE WHEN field_name = 'referral_ref' THEN actual_value END) AS referral_ref
    FROM tgt.prior_auth_resources WHERE resource_type = 'Claim' GROUP BY fhir_id
),
resp AS (
    SELECT fhir_id,
           MAX(CASE WHEN field_name = 'request_ref' THEN actual_value END) AS request_ref,
           MAX(CASE WHEN field_name = 'patient_ref' THEN actual_value END) AS patient_ref
    FROM tgt.prior_auth_resources WHERE resource_type = 'ClaimResponse' GROUP BY fhir_id
),
policy AS (     -- the patient's migrated primary Coverage, found by patient + payer + member ID
    SELECT DISTINCT s.legacy_patient, s.legacy_payer, pt.MEMBERID, x.fhir_id
    FROM etl.coverage_sources AS s
    JOIN etl.coverage_crosswalk AS x
      ON x.resource_type = 'Coverage' AND x.legacy_id = s.coverage_key AND x.fhir_id IS NOT NULL
    JOIN cur.payer_transitions AS pt ON CONCAT(pt.PATIENT, '_', pt.START_DATE) = s.record_key
    WHERE s.coverage_order = '1'
),
referral AS (   -- each migrated referral: its FHIR id, its patient and its specialty on the server
    SELECT x.legacy_id, x.fhir_id, s.legacy_patient,
           (SELECT MAX(t.actual_value) FROM tgt.referral_resources AS t
             WHERE t.fhir_id = x.fhir_id AND t.field_name = 'performer_type_code') AS specialty_code
    FROM etl.referral_crosswalk AS x
    JOIN etl.referral_sources AS s ON s.referral_key = x.legacy_id
    WHERE x.fhir_id IS NOT NULL
)
SELECT s.auth_key, s.service_type, s.decision, xc.fhir_id AS claim_fhir_id,
       CASE WHEN c.patient_ref  = N'Patient/'      + px.fhir_id THEN 'OK' ELSE 'BROKEN' END AS patient_link,
       CASE WHEN c.insurer_ref  = N'Organization/' + py.fhir_id THEN 'OK' ELSE 'BROKEN' END AS insurer_link,
       CASE WHEN c.coverage_ref = N'Coverage/'     + pol.fhir_id THEN 'OK' ELSE 'BROKEN' END AS coverage_link,
       CASE WHEN s.service_type <> 'REFERRAL'                    THEN 'n/a'
            WHEN c.referral_ref = N'ServiceRequest/' + rf.fhir_id
             AND rf.legacy_patient = s.legacy_patient
             AND rf.specialty_code = s.service_code               THEN 'OK'
            ELSE 'BROKEN' END                                                          AS referral_link,
       CASE WHEN s.decision = 'PENDING' AND xr.fhir_id IS NULL    THEN 'n/a (pending)'
            WHEN r.request_ref = N'Claim/' + xc.fhir_id
             AND r.patient_ref = c.patient_ref                    THEN 'OK'
            ELSE 'BROKEN' END                                                          AS decision_link
FROM etl.prior_auth_sources AS s
JOIN cur.prior_auths AS pa ON pa.source_row = TRY_CAST(s.source_row AS INT)
JOIN etl.prior_auth_crosswalk AS xc
  ON xc.resource_type = 'Claim' AND xc.legacy_id = s.auth_key AND xc.fhir_id IS NOT NULL
LEFT JOIN claim AS c ON c.fhir_id = xc.fhir_id
LEFT JOIN etl.prior_auth_crosswalk AS xr
  ON xr.resource_type = 'ClaimResponse' AND xr.legacy_id = s.auth_key AND xr.fhir_id IS NOT NULL
LEFT JOIN resp AS r ON r.fhir_id = xr.fhir_id
LEFT JOIN etl.fhir_crosswalk AS px ON px.resource_type = 'Patient' AND px.legacy_id = s.legacy_patient
LEFT JOIN etl.coverage_crosswalk AS py ON py.resource_type = 'Organization' AND py.legacy_id = s.legacy_payer
LEFT JOIN policy AS pol
  ON pol.legacy_patient = s.legacy_patient AND pol.legacy_payer = s.legacy_payer AND pol.MEMBERID = pa.MEMBERID
LEFT JOIN referral AS rf ON rf.legacy_id = s.legacy_referral;
GO

-- 4. DECISION CHECK: the legacy decision, as the server now expresses it
CREATE OR ALTER VIEW rpt.v_prior_auth_decision_check AS
WITH resp AS (
    SELECT legacy_id,
           MAX(CASE WHEN field_name = 'disposition'  THEN actual_value END) AS disposition,
           MAX(CASE WHEN field_name = 'pre_auth_ref' THEN actual_value END) AS pre_auth_ref
    FROM tgt.prior_auth_resources WHERE resource_type = 'ClaimResponse' GROUP BY legacy_id
)
SELECT s.auth_key, s.service_type, s.decision AS legacy_decision,
       CASE WHEN r.legacy_id IS NULL THEN '(no response yet)' ELSE r.disposition END AS server_disposition,
       r.pre_auth_ref,
       CASE WHEN s.decision = 'PENDING'  AND r.legacy_id IS NULL                THEN 'OK'
            WHEN s.decision = 'APPROVED' AND r.disposition = 'Approved'         THEN 'OK'
            WHEN s.decision = 'DENIED'   AND r.disposition LIKE 'Denied%'       THEN 'OK'
            ELSE 'WRONG' END AS result
FROM etl.prior_auth_sources AS s
LEFT JOIN resp AS r ON r.legacy_id = s.auth_key;
GO

/* =============================================================================
   THE REPORT
   ========================================================================== */

-- 1. Every legacy row accounted for, every decision has its response  (expect PASS)
SELECT * FROM rpt.v_prior_auth_row_reconciliation;

-- 2. Field match, by resource  (expect 100.0 for both)
SELECT resource_type, COUNT(*) AS values_compared, SUM(is_match) AS values_matched,
       COUNT(*) - SUM(is_match) AS values_mismatched,
       CAST(100.0 * SUM(is_match) / NULLIF(COUNT(*), 0) AS DECIMAL(5,1)) AS match_pct
FROM rpt.v_prior_auth_field_compare
GROUP BY resource_type;

-- 2b. Any mismatches?  (expect NO rows)
SELECT TOP (50) * FROM rpt.v_prior_auth_field_compare WHERE is_match = 0;

-- 3. Link integrity  (expect OK or n/a only)
SELECT service_type, patient_link, insurer_link, coverage_link, referral_link, decision_link,
       COUNT(*) AS authorizations
FROM rpt.v_prior_auth_link_check
GROUP BY service_type, patient_link, insurer_link, coverage_link, referral_link, decision_link
ORDER BY service_type;

-- 4. Decisions as the server holds them  (expect only OK)
SELECT service_type, legacy_decision, server_disposition, result, COUNT(*) AS authorizations
FROM rpt.v_prior_auth_decision_check
GROUP BY service_type, legacy_decision, server_disposition, result
ORDER BY service_type, legacy_decision;

-- 5. Approvals on the server, and which have expired by the extract date
--    (expect the planted expired authorization here until staff enter its extension)
WITH r AS (
    SELECT legacy_id,
           MAX(CASE WHEN field_name = 'pre_auth_ref'   THEN actual_value END) AS pre_auth_ref,
           MAX(CASE WHEN field_name = 'pre_auth_start' THEN actual_value END) AS valid_from,
           MAX(CASE WHEN field_name = 'pre_auth_end'   THEN actual_value END) AS valid_to
    FROM tgt.prior_auth_resources WHERE resource_type = 'ClaimResponse' GROUP BY legacy_id
)
SELECT s.service_type, pa.SERVICE_DESCRIPTION, r.pre_auth_ref, r.valid_from, r.valid_to,
       CASE WHEN r.valid_to < (SELECT LEFT(MAX(START_DATE), 10) FROM cur.payer_transitions)
            THEN 'EXPIRED' ELSE 'valid' END AS on_extract_date,
       CASE WHEN r.pre_auth_ref IS NULL THEN 'MISSING' ELSE 'present' END AS auth_number
FROM r
JOIN etl.prior_auth_sources AS s ON s.auth_key = r.legacy_id AND s.decision = 'APPROVED'
JOIN cur.prior_auths AS pa ON pa.source_row = TRY_CAST(s.source_row AS INT)
ORDER BY on_extract_date, r.valid_to;

-- 5b. Rules triggered
SELECT rule_id, action, COUNT(*) AS rows_affected
FROM etl.prior_auth_issue_log
GROUP BY rule_id, action
ORDER BY rule_id, action;
