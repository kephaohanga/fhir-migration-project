/* =============================================================================
   23_coverage_reconciliation.sql
   Phase 2, step 4: reconcile the insurance migration (payer Organization, Coverage).

   Run AFTER:  python src\plant_coverage_errors.py
               python src\phase2_coverage.py
   How to run: open in SSMS, press F5. Read the result grids top to bottom.
   Safe to re-run (CREATE OR ALTER).

   Five questions:
     1. Did every legacy insurance row get exactly one disposition?   (row reconciliation)
     2. Was every current insured policy migrated?                    (coverage counts)
     3. Does the server hold exactly what we sent?                    (field comparison)
     4. Does each Coverage point to the RIGHT patient and payer?      (link integrity)
     5. What is each migrated patient's insurance status?             (patient view)
   ========================================================================== */

USE FhirMigration;
GO

IF SCHEMA_ID('rpt') IS NULL EXEC('CREATE SCHEMA rpt');
GO

/* -----------------------------------------------------------------------------
   1. ROW RECONCILIATION: source rows = sum of dispositions, one per row
   -------------------------------------------------------------------------- */
CREATE OR ALTER VIEW rpt.v_coverage_row_reconciliation AS
WITH d AS (
    SELECT
        (SELECT COUNT(*) FROM stg.payer_transitions)                                     AS source_rows,
        SUM(CASE WHEN disposition = 'Ended - kept in legacy' THEN 1 ELSE 0 END)          AS ended_kept_in_legacy,
        SUM(CASE WHEN disposition = 'Current - migrated'     THEN 1 ELSE 0 END)          AS current_migrated,
        SUM(CASE WHEN disposition = 'Current - self-pay'     THEN 1 ELSE 0 END)          AS current_self_pay,
        SUM(CASE WHEN disposition = 'Superseded'             THEN 1 ELSE 0 END)          AS superseded,
        SUM(CASE WHEN disposition = 'Rejected'               THEN 1 ELSE 0 END)          AS rejected,
        COUNT(DISTINCT source_row)                                                       AS rows_with_disposition
    FROM etl.coverage_disposition
)
SELECT source_rows, ended_kept_in_legacy, current_migrated, current_self_pay, superseded, rejected,
       source_rows - ended_kept_in_legacy - current_migrated - current_self_pay - superseded - rejected AS unaccounted,
       CASE WHEN source_rows = ended_kept_in_legacy + current_migrated + current_self_pay + superseded + rejected
             AND rows_with_disposition = source_rows
            THEN 'PASS' ELSE 'FAIL' END AS result
FROM d;
GO

/* -----------------------------------------------------------------------------
   3. FIELD COMPARISON (payer Organizations and Coverage, long format)
   -------------------------------------------------------------------------- */
CREATE OR ALTER VIEW rpt.v_coverage_field_compare AS
SELECT e.resource_type, e.legacy_id, x.fhir_id, e.field_name, e.expected_value, t.actual_value,
       CASE WHEN ISNULL(e.expected_value, N'<null>') COLLATE Latin1_General_BIN2
               = ISNULL(t.actual_value,   N'<null>') COLLATE Latin1_General_BIN2
            THEN 1 ELSE 0 END AS is_match
FROM etl.coverage_expected AS e
JOIN etl.coverage_crosswalk AS x
  ON x.resource_type = e.resource_type AND x.legacy_id = e.legacy_id AND x.fhir_id IS NOT NULL
LEFT JOIN tgt.coverage_resources AS t
  ON t.resource_type = e.resource_type AND t.fhir_id = x.fhir_id AND t.field_name = e.field_name;
GO

/* -----------------------------------------------------------------------------
   4. LINK INTEGRITY: beneficiary = the patient migrated from the legacy PATIENT,
      payor = the payer Organization migrated from the legacy PAYER
   -------------------------------------------------------------------------- */
CREATE OR ALTER VIEW rpt.v_coverage_link_check AS
SELECT s.coverage_key, s.coverage_order, x.fhir_id AS coverage_fhir_id,
       b.actual_value AS beneficiary_ref, p.actual_value AS payor_ref,
       CASE WHEN b.actual_value = N'Patient/' + px.fhir_id      THEN 'OK' ELSE 'BROKEN' END AS beneficiary_link,
       CASE WHEN p.actual_value = N'Organization/' + ox.fhir_id THEN 'OK' ELSE 'BROKEN' END AS payor_link
FROM etl.coverage_sources AS s
JOIN etl.coverage_crosswalk AS x
  ON x.resource_type = 'Coverage' AND x.legacy_id = s.coverage_key AND x.fhir_id IS NOT NULL
LEFT JOIN tgt.coverage_resources AS b
  ON b.resource_type = 'Coverage' AND b.fhir_id = x.fhir_id AND b.field_name = 'beneficiary_ref'
LEFT JOIN tgt.coverage_resources AS p
  ON p.resource_type = 'Coverage' AND p.fhir_id = x.fhir_id AND p.field_name = 'payor_ref'
LEFT JOIN etl.fhir_crosswalk AS px
  ON px.resource_type = 'Patient' AND px.legacy_id = s.legacy_patient
LEFT JOIN etl.coverage_crosswalk AS ox
  ON ox.resource_type = 'Organization' AND ox.legacy_id = s.legacy_payer;
GO

/* -----------------------------------------------------------------------------
   5. PATIENT VIEW: every migrated patient gets one insurance status
   -------------------------------------------------------------------------- */
CREATE OR ALTER VIEW rpt.v_patient_coverage_status AS
WITH pat AS (
    SELECT x.legacy_id AS patient_id, x.fhir_id,
           (SELECT TOP (1) NULLIF(LTRIM(p.DEATHDATE), '') FROM cur.patients AS p
             WHERE p.Id = x.legacy_id ORDER BY p.source_row) AS deathdate
    FROM etl.fhir_crosswalk AS x
    WHERE x.resource_type = 'Patient' AND x.fhir_id IS NOT NULL
)
SELECT pat.patient_id,
       CASE
           WHEN EXISTS (SELECT 1 FROM etl.coverage_sources AS s
                        JOIN etl.coverage_crosswalk AS cx
                          ON cx.resource_type = 'Coverage' AND cx.legacy_id = s.coverage_key AND cx.fhir_id IS NOT NULL
                        WHERE s.legacy_patient = pat.patient_id AND s.coverage_order = '1')
                THEN 'Active coverage migrated'
           WHEN EXISTS (SELECT 1 FROM etl.coverage_disposition AS d
                        WHERE d.patient_id = pat.patient_id AND d.disposition = 'Current - self-pay')
                THEN 'Self-pay (uninsured)'
           WHEN EXISTS (SELECT 1 FROM etl.coverage_disposition AS d
                        WHERE d.patient_id = pat.patient_id AND d.disposition = 'Rejected')
                THEN 'Current policy rejected - needs review'
           WHEN pat.deathdate IS NOT NULL
                THEN 'Deceased - no current coverage'
           ELSE 'No current policy in legacy data'
       END AS coverage_status
FROM pat;
GO

/* =============================================================================
   THE REPORT
   ========================================================================== */

-- 1. Every legacy insurance row accounted for  (expect PASS, unaccounted 0)
SELECT * FROM rpt.v_coverage_row_reconciliation;

-- 2. Coverage counts: primary = current migrated rows; plus secondaries
SELECT
    (SELECT COUNT(*) FROM etl.coverage_disposition WHERE disposition = 'Current - migrated') AS current_policies,
    (SELECT COUNT(*) FROM etl.coverage_sources AS s
       JOIN etl.coverage_crosswalk AS x ON x.resource_type = 'Coverage' AND x.legacy_id = s.coverage_key AND x.fhir_id IS NOT NULL
      WHERE s.coverage_order = '1') AS primary_coverage_migrated,
    (SELECT COUNT(*) FROM etl.coverage_sources AS s
       JOIN etl.coverage_crosswalk AS x ON x.resource_type = 'Coverage' AND x.legacy_id = s.coverage_key AND x.fhir_id IS NOT NULL
      WHERE s.coverage_order = '2') AS secondary_coverage_migrated,
    (SELECT COUNT(*) FROM etl.coverage_crosswalk WHERE resource_type = 'Organization' AND fhir_id IS NOT NULL) AS payers_migrated;

-- 3. Field match by resource type  (expect 100.0)
SELECT resource_type,
       COUNT(*) AS values_compared, SUM(is_match) AS values_matched,
       COUNT(*) - SUM(is_match) AS values_mismatched,
       CAST(100.0 * SUM(is_match) / NULLIF(COUNT(*), 0) AS DECIMAL(5,1)) AS match_pct
FROM rpt.v_coverage_field_compare
GROUP BY resource_type;

-- 3b. Any mismatches?  (expect NO rows)
SELECT TOP (50) * FROM rpt.v_coverage_field_compare WHERE is_match = 0;

-- 4. Link integrity  (expect only OK)
SELECT beneficiary_link, payor_link, COUNT(*) AS coverages
FROM rpt.v_coverage_link_check
GROUP BY beneficiary_link, payor_link;

-- 5. Insurance status of every migrated patient
SELECT coverage_status, COUNT(*) AS patients
FROM rpt.v_patient_coverage_status
GROUP BY coverage_status
ORDER BY patients DESC;

-- 6. Migrated Coverage by payer type (Source of Payment Typology) and order
SELECT MAX(CASE WHEN field_name = 'type_code'      THEN actual_value END) AS sopt_code,
       MAX(CASE WHEN field_name = 'coverage_order' THEN actual_value END) AS coverage_order,
       fhir_id
INTO #cov
FROM tgt.coverage_resources
WHERE resource_type = 'Coverage'
GROUP BY fhir_id;

SELECT ISNULL(sopt_code, '(none)') AS sopt_code,
       CASE sopt_code WHEN '1' THEN 'Medicare' WHEN '2' THEN 'Medicaid'
                      WHEN '14' THEN 'Dual eligible' WHEN '5' THEN 'Private' ELSE 'Not mapped' END AS payer_type,
       coverage_order, COUNT(*) AS coverages
FROM #cov
GROUP BY sopt_code, coverage_order
ORDER BY coverage_order, coverages DESC;
DROP TABLE #cov;

-- 7. Rules triggered (rejects and flags)
SELECT rule_id, action, COUNT(*) AS policies
FROM etl.coverage_issue_log
GROUP BY rule_id, action
ORDER BY rule_id;
