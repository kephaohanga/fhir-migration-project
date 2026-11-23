/* =============================================================================
   34_prove_derive_dont_invent.sql
   Proves the README design decision "Derive, don't invent":

     "Medication and DiagnosticReport are built from records already migrated and
      reconciled, and point to them rather than copying them; resources with no source
      data (administrations, documents) are left out and the reason is stated."

   Each row below is one check. "problems" counts the records that would break the
   statement, so every check expects problems = 0 and result = PASS.

   Run AFTER: sql\31 and sql\32 have passed (they create the rpt views used here).
   How to run: open in SSMS, make sure the database is FhirMigration, press F5.
   Read-only: it creates nothing and changes nothing.
   ========================================================================== */

USE FhirMigration;
GO

WITH checks AS (

    /* ---------- PART A. Medication is DERIVED from migrated data ---------- */

    -- A1. Every drug in the catalogue comes from a curated legacy prescription
    SELECT 'A1' AS check_no,
           'Medication built from migrated records' AS claim_part,
           'Catalogue drugs whose code is NOT in the curated prescriptions (cur.medications)' AS what_is_checked,
           (SELECT COUNT(*) FROM etl.drug_catalogue AS c
             WHERE NOT EXISTS (SELECT 1 FROM cur.medications AS m
                               WHERE LTRIM(RTRIM(m.CODE)) = c.code)) AS problems,
           (SELECT COUNT(*) FROM etl.drug_catalogue) AS records_checked

    UNION ALL
    -- A2. Nothing on the server that the catalogue did not build (nothing invented)
    SELECT 'A2', 'Medication built from migrated records',
           'Medications on the server that are NOT in the catalogue',
           (SELECT COUNT(DISTINCT t.legacy_id) FROM tgt.drug_resources AS t
             WHERE NOT EXISTS (SELECT 1 FROM etl.drug_catalogue AS c WHERE c.code = t.legacy_id)),
           (SELECT COUNT(DISTINCT legacy_id) FROM tgt.drug_resources)

    /* ---------- PART B. Prescriptions POINT to the Medication ---------- */

    UNION ALL
    -- B1. Every migrated prescription carries a reference to the Medication of its own drug
    SELECT 'B1', 'Points to records, does not copy them',
           'Prescriptions (MedicationRequest) NOT pointing to the Medication of their own drug',
           (SELECT COUNT(*) FROM rpt.v_prescription_drug_link WHERE drug_link <> 'OK'),
           (SELECT COUNT(*) FROM rpt.v_prescription_drug_link)

    /* ---------- PART C. DiagnosticReport is DERIVED and POINTS ---------- */

    UNION ALL
    -- C1. Every result inside a report is a lab result that was migrated in Phase 2
    SELECT 'C1', 'DiagnosticReport built from migrated records',
           'Report members that are NOT a migrated Observation (crosswalk with a FHIR id)',
           (SELECT COUNT(*) FROM etl.report_members AS m
             WHERE NOT EXISTS (SELECT 1 FROM etl.observation_crosswalk AS x
                               WHERE x.resource_type = 'Observation'
                                 AND x.legacy_id = m.observation_key
                                 AND x.fhir_id   = m.observation_fhir_id)),
           (SELECT COUNT(*) FROM etl.report_members)

    UNION ALL
    -- C2. No report was made up without results behind it
    SELECT 'C2', 'DiagnosticReport built from migrated records',
           'Reports with NO lab results behind them',
           (SELECT COUNT(*) FROM etl.report_sources AS s
             WHERE NOT EXISTS (SELECT 1 FROM etl.report_members AS m WHERE m.report_key = s.report_key)),
           (SELECT COUNT(*) FROM etl.report_sources)

    UNION ALL
    -- C3. On the server, every report result is a REFERENCE to a migrated Observation
    SELECT 'C3', 'Points to records, does not copy them',
           'Report results on the server that are NOT a reference "Observation/<id>" of a migrated result',
           (SELECT COUNT(*) FROM tgt.report_results AS r
             WHERE NOT EXISTS (SELECT 1 FROM etl.observation_crosswalk AS x
                               WHERE x.resource_type = 'Observation' AND x.fhir_id IS NOT NULL
                                 AND r.result_ref = N'Observation/' + x.fhir_id)),
           (SELECT COUNT(*) FROM tgt.report_results)

    UNION ALL
    -- C4. Each report sits on the same patient and visit as its results
    SELECT 'C4', 'Points to records, does not copy them',
           'Reports whose patient or visit link is BROKEN',
           (SELECT COUNT(*) FROM rpt.v_report_link_check
             WHERE subject_link <> 'OK' OR encounter_link <> 'OK'),
           (SELECT COUNT(*) FROM rpt.v_report_link_check)

    /* ---------- PART D. No source data = left out ---------- */

    UNION ALL
    -- D1. Nothing was built for administrations or documents
    SELECT 'D1', 'Resources with no source data are left out',
           'Tables built for MedicationAdministration or DocumentReference',
           (SELECT COUNT(*) FROM sys.tables
             WHERE name LIKE '%administration%' OR name LIKE '%document%'),
           (SELECT COUNT(*) FROM sys.tables)
)
SELECT check_no, claim_part, what_is_checked, records_checked, problems,
       CASE WHEN problems = 0 THEN 'PASS' ELSE 'FAIL' END AS result
FROM checks
ORDER BY check_no;
