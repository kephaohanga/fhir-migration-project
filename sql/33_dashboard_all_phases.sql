/* =============================================================================
   33_dashboard_all_phases.sql
   Views for the Power BI "All phases" page: one place that summarises every
   reconciliation in the project (steps 10-32).

   These views do NOT recalculate anything: they READ the reconciliation views each
   step already created (rpt.v_*_row_reconciliation, rpt.v_*_field_compare, ...), so the
   dashboard shows exactly the numbers the SQL proved.

   Run AFTER all reconciliation files (10, 20-32) have been run at least once.
   How to run: open in SSMS, press F5. Safe to re-run (CREATE OR ALTER).

   Views:
     rpt.v_dash_scorecard    one row per migration step: rows, FHIR records, values, PASS/FAIL
     rpt.v_dash_rules        every rule that fired, in every step (Reject / Fix / Flag)
     rpt.v_dash_corrections  every staff correction, with the team that owned it
   ========================================================================== */

USE FhirMigration;
GO

IF SCHEMA_ID('rpt') IS NULL EXEC('CREATE SCHEMA rpt');
GO

/* -----------------------------------------------------------------------------
   1. SCORECARD - one row per step.
      source_rows      what the step started from (legacy rows; for the two derived steps,
                       the migrated records they were built from)
      fhir_records     FHIR resources this step put on the server (created or updated)
      not_migrated     source rows rejected, removed as duplicates, or kept in legacy
      values_compared / values_matched   from the step's own field comparison
      planted_defects  known defects planted for testing (from the answer keys in docs\;
                       every one was reported CAUGHT by its script). 0 = derived step.
      rows_result      the step's own PASS / FAIL
   -------------------------------------------------------------------------- */
CREATE OR ALTER VIEW rpt.v_dash_scorecard AS
WITH vals AS (    -- values compared / matched per step, from each field comparison
    SELECT 'Patient' AS step, COUNT(*) AS compared, SUM(is_match) AS matched FROM rpt.v_patient_field_compare
    UNION ALL SELECT 'Encounter',              COUNT(*), SUM(is_match) FROM rpt.v_encounter_field_compare
    UNION ALL SELECT 'Condition',              COUNT(*), SUM(is_match) FROM rpt.v_condition_field_compare
    UNION ALL SELECT 'Practitioner',           COUNT(*), SUM(is_match) FROM rpt.v_practitioner_field_compare
    UNION ALL SELECT 'Clinic structure',       COUNT(*), SUM(is_match) FROM rpt.v_clinic_field_compare
    UNION ALL SELECT 'Visit links',            COUNT(*), SUM(is_match) FROM rpt.v_encounter_link_compare
    UNION ALL SELECT 'Coverage',               COUNT(*), SUM(is_match) FROM rpt.v_coverage_field_compare
    UNION ALL SELECT 'Procedure',              COUNT(*), SUM(is_match) FROM rpt.v_procedure_field_compare
    UNION ALL SELECT 'AllergyIntolerance',     COUNT(*), SUM(is_match) FROM rpt.v_allergy_field_compare
    UNION ALL SELECT 'MedicationRequest',      COUNT(*), SUM(is_match) FROM rpt.v_medication_field_compare
    UNION ALL SELECT 'Observation',            COUNT(*), SUM(is_match) FROM rpt.v_observation_field_compare
    UNION ALL SELECT 'ServiceRequest',         COUNT(*), SUM(is_match) FROM rpt.v_referral_field_compare
    UNION ALL SELECT 'Claim + ClaimResponse',  COUNT(*), SUM(is_match) FROM rpt.v_prior_auth_field_compare
    UNION ALL SELECT 'Appointment',            COUNT(*), SUM(is_match) FROM rpt.v_appointment_field_compare
    UNION ALL SELECT 'Medication',             COUNT(*), SUM(is_match) FROM rpt.v_drug_field_compare
    UNION ALL SELECT 'DiagnosticReport',       COUNT(*), SUM(is_match) FROM rpt.v_report_field_compare
),
link_status AS (
    SELECT COUNT(*) AS encounters, SUM(CASE WHEN link_status = 'OK' THEN 1 ELSE 0 END) AS linked
    FROM rpt.v_encounter_link_status
),
steps AS (
    -- Phase 1 ------------------------------------------------------------------
    SELECT 1 AS phase, 1 AS step_order, 'Patient' AS step, 'Patient' AS fhir_resources, 'patients.csv' AS legacy_source,
           source_rows, migrated AS fhir_records,
           rejected_direct + rejected_cascade + duplicates_removed AS not_migrated, result AS rows_result
    FROM rpt.v_row_reconciliation WHERE table_name = 'patients'
    UNION ALL
    SELECT 1, 2, 'Encounter', 'Encounter', 'encounters.csv', source_rows, migrated,
           rejected_direct + rejected_cascade + duplicates_removed, result
    FROM rpt.v_row_reconciliation WHERE table_name = 'encounters'
    UNION ALL
    SELECT 1, 3, 'Condition', 'Condition', 'conditions.csv', source_rows, migrated,
           rejected_direct + rejected_cascade + duplicates_removed, result
    FROM rpt.v_row_reconciliation WHERE table_name = 'conditions'
    -- Phase 2 ------------------------------------------------------------------
    UNION ALL
    SELECT 2, 4, 'Practitioner', 'Practitioner', 'providers.csv', source_rows, migrated,
           rejected + duplicates_removed, result
    FROM rpt.v_practitioner_row_reconciliation
    UNION ALL
    SELECT 2, 5, 'Clinic structure', 'Organization, Location, PractitionerRole', 'providers.csv (one role per provider)',
           distinct_providers, roles_migrated + organizations + locations, roles_rejected, result
    FROM rpt.v_role_row_reconciliation
    UNION ALL
    SELECT 2, 6, 'Visit links', 'Encounter (updated)', 'encounters.csv (PROVIDER)',
           encounters, encounters, encounters - linked,
           CASE WHEN linked = encounters THEN 'PASS' ELSE 'FAIL' END
    FROM link_status
    UNION ALL
    SELECT 2, 7, 'Coverage', 'Coverage, Organization (payer)', 'payer_transitions.csv', source_rows,
           (SELECT COUNT(*) FROM etl.coverage_crosswalk WHERE fhir_id IS NOT NULL),   -- Coverages + payers
           ended_kept_in_legacy + current_self_pay + superseded + rejected, result
    FROM rpt.v_coverage_row_reconciliation
    UNION ALL
    SELECT 2, 8, 'Procedure', 'Procedure', 'procedures.csv', source_rows, disposition_migrated,
           rejected + rejected_cascade + duplicates_removed, result
    FROM rpt.v_procedure_row_reconciliation
    UNION ALL
    SELECT 2, 9, 'AllergyIntolerance', 'AllergyIntolerance', 'allergies.csv', source_rows, disposition_migrated,
           rejected + rejected_cascade + duplicates_removed, result
    FROM rpt.v_allergy_row_reconciliation
    UNION ALL
    SELECT 2, 10, 'MedicationRequest', 'MedicationRequest', 'medications.csv', source_rows, disposition_migrated,
           rejected + rejected_cascade + duplicates_removed, result
    FROM rpt.v_medication_row_reconciliation
    UNION ALL
    SELECT 2, 11, 'Observation', 'Observation', 'observations.csv', source_rows, disposition_migrated,
           simulator_metrics + rejected + rejected_cascade + duplicates_removed, result
    FROM rpt.v_observation_row_reconciliation
    -- Phase 3 ------------------------------------------------------------------
    UNION ALL
    SELECT 3, 12, 'ServiceRequest', 'ServiceRequest', 'referrals.csv', source_rows, disposition_migrated,
           rejected + duplicates_removed, result
    FROM rpt.v_referral_row_reconciliation
    UNION ALL
    SELECT 3, 13, 'Claim + ClaimResponse', 'Claim, ClaimResponse', 'prior_auths.csv', source_rows,
           claims_on_server + responses_on_server, rejected + duplicates_removed, result
    FROM rpt.v_prior_auth_row_reconciliation
    UNION ALL
    SELECT 3, 14, 'Appointment', 'Appointment', 'appointments.csv', source_rows, disposition_migrated,
           rejected + duplicates_removed, result
    FROM rpt.v_appointment_row_reconciliation
    UNION ALL
    SELECT 3, 15, 'Medication', 'Medication (drug catalogue)', 'derived: migrated prescriptions',
           drugs_in_catalogue, confirmed_on_server, 0, result
    FROM rpt.v_drug_catalogue_check
    UNION ALL
    SELECT 3, 16, 'DiagnosticReport', 'DiagnosticReport (lab reports)', 'derived: migrated lab results',
           lab_results, confirmed_on_server, flagged, result
    FROM rpt.v_report_coverage
),
planted AS (      -- from the answer keys in docs\ (Phase 1: plant_errors.py)
    -- appointments: 8 defects (its answer key has 10 lines: a moved visit changes START and END)
    SELECT step, n FROM (VALUES
        ('Patient', 7), ('Encounter', 1), ('Condition', 1),
        ('Practitioner', 7), ('Coverage', 5), ('Procedure', 5), ('AllergyIntolerance', 5),
        ('MedicationRequest', 5), ('Observation', 5),
        ('ServiceRequest', 8), ('Claim + ClaimResponse', 8), ('Appointment', 8)
    ) AS t (step, n)
)
SELECT CONCAT('Phase ', s.phase)                                        AS phase,
       s.step_order, s.step, s.fhir_resources, s.legacy_source,
       s.source_rows, s.fhir_records, s.not_migrated,
       ISNULL(v.compared, 0)                                            AS values_compared,
       ISNULL(v.matched, 0)                                             AS values_matched,
       CAST(100.0 * ISNULL(v.matched, 0) / NULLIF(v.compared, 0) AS DECIMAL(5,1)) AS match_pct,
       ISNULL(p.n, 0)                                                   AS planted_defects,
       s.rows_result,
       CASE WHEN s.rows_result = 'PASS' AND ISNULL(v.matched, 0) = ISNULL(v.compared, 0)
            THEN 'PASS' ELSE 'FAIL' END                                 AS overall_result
FROM steps AS s
LEFT JOIN vals AS v ON v.step = s.step
LEFT JOIN planted AS p ON p.step = s.step;
GO

/* -----------------------------------------------------------------------------
   2. RULES - every rule that fired, in every step
   -------------------------------------------------------------------------- */
CREATE OR ALTER VIEW rpt.v_dash_rules AS
SELECT phase, step, rule_id, action, COUNT(*) AS rows_affected
FROM (
    SELECT 'Phase 1' AS phase,
           CASE table_name WHEN 'patients' THEN 'Patient' WHEN 'encounters' THEN 'Encounter'
                           ELSE 'Condition' END AS step, rule_id, action FROM etl.issue_log
    UNION ALL SELECT 'Phase 2', 'Practitioner',          rule_id, action FROM etl.practitioner_issue_log
    UNION ALL SELECT 'Phase 2', 'Clinic structure',      rule_id, action FROM etl.clinic_issue_log
    UNION ALL SELECT 'Phase 2', 'Visit links',           rule_id, action FROM etl.encounter_link_issue_log
    UNION ALL SELECT 'Phase 2', 'Coverage',              rule_id, action FROM etl.coverage_issue_log
    UNION ALL SELECT 'Phase 2', 'Procedure',             rule_id, action FROM etl.procedure_issue_log
    UNION ALL SELECT 'Phase 2', 'AllergyIntolerance',    rule_id, action FROM etl.allergy_issue_log
    UNION ALL SELECT 'Phase 2', 'MedicationRequest',     rule_id, action FROM etl.medication_issue_log
    UNION ALL SELECT 'Phase 2', 'Observation',           rule_id, action FROM etl.observation_issue_log
    UNION ALL SELECT 'Phase 3', 'ServiceRequest',        rule_id, action FROM etl.referral_issue_log
    UNION ALL SELECT 'Phase 3', 'Claim + ClaimResponse', rule_id, action FROM etl.prior_auth_issue_log
    UNION ALL SELECT 'Phase 3', 'Appointment',           rule_id, action FROM etl.appointment_issue_log
    UNION ALL SELECT 'Phase 3', 'Medication',            rule_id, action FROM etl.drug_issue_log
    UNION ALL SELECT 'Phase 3', 'DiagnosticReport',      rule_id, action FROM etl.report_issue_log
) AS all_rules
GROUP BY phase, step, rule_id, action;
GO

/* -----------------------------------------------------------------------------
   3. CORRECTIONS - every staff decision, with the team that owned it
      (the same owners the shared worklist, 12_remediation.sql, assigns)
   -------------------------------------------------------------------------- */
CREATE OR ALTER VIEW rpt.v_dash_corrections AS
SELECT CASE WHEN c.table_name IN ('patients', 'encounters', 'conditions') THEN 'Phase 1'
            WHEN c.table_name IN ('referrals', 'prior_auths', 'appointments') THEN 'Phase 3'
            ELSE 'Phase 2' END                                            AS phase,
       c.table_name, c.source_row, c.rule_id, c.field_name, c.disposition, c.corrected_by, c.applied_at,
       CASE
           WHEN c.disposition = 'Archive' OR c.rule_id IN ('R-PA01', 'R-RF01', 'R-AP01')  THEN 'HIM'
           WHEN c.rule_id IN ('R-P01', 'R-P02', 'R-P03', 'R-P04', 'R-CV01')            THEN 'Registration'
           WHEN c.rule_id IN ('R-CV02', 'R-CV03', 'R-CV04', 'R-CV05', 'R-PA02')        THEN 'Insurance Verification'
           WHEN c.rule_id LIKE 'R-PR%' OR c.rule_id IN ('R-RF02', 'R-AP03')           THEN 'Credentialing'
           WHEN c.rule_id IN ('R-C01', 'R-C02', 'R-PC04')                              THEN 'Coding'
           WHEN c.rule_id IN ('R-MD04')                                                THEN 'Pharmacy'
           WHEN c.rule_id IN ('R-OB06')                                                THEN 'Laboratory'
           WHEN c.rule_id IN ('R-AL04', 'R-DR01')                                      THEN 'Clinical Informatics'
           WHEN c.rule_id IN ('R-RF03', 'R-RF05', 'R-PA03')                            THEN 'Referral Coordinator'
           WHEN c.rule_id LIKE 'R-PA%' OR c.rule_id = 'R-AP08'                         THEN 'Prior Authorization'
           WHEN c.rule_id IN ('R-AP02', 'R-AP04', 'R-AP05', 'R-AP06', 'R-AP07')        THEN 'Scheduling'
           ELSE 'HIM'
       END                                                                AS owner_team
FROM etl.corrections AS c;
GO

/* =============================================================================
   QUICK CHECKS - the totals must match the README
   ========================================================================== */

-- 1. One row per step  (expect 16 rows, every overall_result PASS)
SELECT * FROM rpt.v_dash_scorecard ORDER BY step_order;

-- 1b. Project totals  (expect 566,647 values compared = matched; 65 planted defects)
SELECT SUM(values_compared) AS values_compared, SUM(values_matched) AS values_matched,
       SUM(planted_defects) AS planted_defects,
       SUM(CASE WHEN overall_result = 'PASS' THEN 1 ELSE 0 END) AS steps_passed, COUNT(*) AS steps
FROM rpt.v_dash_scorecard;

-- 2. Rules by phase and action
SELECT phase, action, SUM(rows_affected) AS rows_affected
FROM rpt.v_dash_rules GROUP BY phase, action ORDER BY phase, action;

-- 3. Corrections by team  (expect 55 corrections in total, 1 of them an Archive)
SELECT owner_team, disposition, COUNT(*) AS corrections
FROM rpt.v_dash_corrections GROUP BY owner_team, disposition ORDER BY corrections DESC;
GO

-- Footer: when the pipeline last pulled data from the FHIR server, and when the report refreshed
CREATE OR ALTER VIEW rpt.v_dash_run_info AS
SELECT DATEADD(MINUTE, DATEDIFF(MINUTE, GETDATE(), GETUTCDATE()), MAX(t.create_date))
                         AS fhir_extract_utc,
       SYSUTCDATETIME()  AS report_refreshed_utc
FROM sys.tables AS t
WHERE SCHEMA_NAME(t.schema_id) = 'tgt';
GO

SELECT * FROM rpt.v_dash_run_info;