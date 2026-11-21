/* =============================================================================
   32_diagnostic_report_reconciliation.sql
   Phase 3, step 5: reconcile the lab reports (FHIR DiagnosticReport) built from the
   lab results migrated in Phase 2.

   Run AFTER:  python src\phase3_diagnostic_report.py
   How to run: open in SSMS, press F5. Read the result grids top to bottom.
   Safe to re-run (CREATE OR ALTER).

   Four questions:
     1. Is every migrated lab result in exactly one report (or flagged), and is every
        report on the server?                                          (coverage)
     2. Does the server hold exactly what we sent?                     (field comparison)
     3. Does each report list exactly its own results - none missing, none extra -
        and sit on the same patient and visit as those results?        (result integrity)
     4. Which reports were built: panels, combined panels, single tests (summary)
   ========================================================================== */

USE FhirMigration;
GO

IF SCHEMA_ID('rpt') IS NULL EXEC('CREATE SCHEMA rpt');
GO

-- 1. COVERAGE: every migrated lab result placed exactly once; every report on the server
CREATE OR ALTER VIEW rpt.v_report_coverage AS
WITH lab AS (    -- the migrated lab results (the Phase 2 Observations the reports are built from)
    SELECT s.observation_key
    FROM etl.observation_sources AS s
    JOIN etl.observation_crosswalk AS x
      ON x.resource_type = 'Observation' AND x.legacy_id = s.observation_key AND x.fhir_id IS NOT NULL
    JOIN cur.observations AS o ON o.source_row = TRY_CAST(s.source_row AS INT)
    WHERE LOWER(LTRIM(RTRIM(o.CATEGORY))) = 'laboratory'
),
d AS (
    SELECT (SELECT COUNT(*) FROM lab)                                                       AS lab_results,
           (SELECT COUNT(*) FROM etl.report_coverage WHERE disposition = 'In a report')     AS in_a_report,
           (SELECT COUNT(*) FROM etl.report_coverage WHERE disposition <> 'In a report')    AS flagged,
           (SELECT COUNT(*) FROM (SELECT observation_key FROM etl.report_members
                                  GROUP BY observation_key HAVING COUNT(*) > 1) AS t)       AS in_two_reports,
           (SELECT COUNT(*) FROM lab
             WHERE observation_key NOT IN (SELECT observation_key FROM etl.report_coverage)) AS not_placed,
           (SELECT COUNT(*) FROM etl.report_sources)                                        AS reports_built,
           (SELECT COUNT(*) FROM etl.report_crosswalk WHERE fhir_id IS NOT NULL)            AS confirmed_on_server,
           (SELECT COUNT(DISTINCT fhir_id) FROM tgt.report_resources)                       AS found_on_server
)
SELECT *,
       CASE WHEN lab_results = in_a_report + flagged AND in_two_reports = 0 AND not_placed = 0
             AND confirmed_on_server = reports_built AND found_on_server = reports_built
            THEN 'PASS' ELSE 'FAIL' END AS result
FROM d;
GO

-- 2. FIELD COMPARISON (long format)
CREATE OR ALTER VIEW rpt.v_report_field_compare AS
SELECT e.legacy_id, x.fhir_id, e.field_name, e.expected_value, t.actual_value,
       CASE WHEN ISNULL(e.expected_value, N'<null>') COLLATE Latin1_General_BIN2
               = ISNULL(t.actual_value,   N'<null>') COLLATE Latin1_General_BIN2
            THEN 1 ELSE 0 END AS is_match
FROM etl.report_expected AS e
JOIN etl.report_crosswalk AS x ON x.legacy_id = e.legacy_id AND x.fhir_id IS NOT NULL
LEFT JOIN tgt.report_resources AS t ON t.fhir_id = x.fhir_id AND t.field_name = e.field_name;
GO

-- 3. RESULT INTEGRITY: each expected member is a result of its report on the server, and
--    the server lists no result that was not expected
CREATE OR ALTER VIEW rpt.v_report_result_check AS
SELECT m.report_key, m.observation_key,
       CASE WHEN r.result_ref IS NOT NULL THEN 'OK' ELSE 'MISSING ON SERVER' END AS result_link
FROM etl.report_members AS m
JOIN etl.report_crosswalk AS x ON x.legacy_id = m.report_key AND x.fhir_id IS NOT NULL
LEFT JOIN tgt.report_results AS r
  ON r.report_fhir_id = x.fhir_id AND r.result_ref = N'Observation/' + m.observation_fhir_id
UNION ALL
SELECT r.report_key, r.result_ref, 'EXTRA ON SERVER'
FROM tgt.report_results AS r
JOIN etl.report_crosswalk AS x ON x.fhir_id = r.report_fhir_id
WHERE NOT EXISTS (SELECT 1 FROM etl.report_members AS m
                  WHERE m.report_key = x.legacy_id AND N'Observation/' + m.observation_fhir_id = r.result_ref);
GO

-- 3b. Each report on the same patient and visit as its results
CREATE OR ALTER VIEW rpt.v_report_link_check AS
WITH rep AS (
    SELECT legacy_id,
           MAX(CASE WHEN field_name = 'subject_ref'   THEN actual_value END) AS subject_ref,
           MAX(CASE WHEN field_name = 'encounter_ref' THEN actual_value END) AS encounter_ref
    FROM tgt.report_resources GROUP BY legacy_id
)
SELECT s.report_key,
       CASE WHEN r.subject_ref   = N'Patient/'   + px.fhir_id THEN 'OK' ELSE 'BROKEN' END AS subject_link,
       CASE WHEN r.encounter_ref = N'Encounter/' + ex.fhir_id THEN 'OK' ELSE 'BROKEN' END AS encounter_link
FROM etl.report_sources AS s
LEFT JOIN rep AS r ON r.legacy_id = s.report_key
LEFT JOIN etl.fhir_crosswalk AS px ON px.resource_type = 'Patient'   AND px.legacy_id = s.legacy_patient
LEFT JOIN etl.fhir_crosswalk AS ex ON ex.resource_type = 'Encounter' AND ex.legacy_id = s.legacy_encounter;
GO

/* =============================================================================
   THE REPORT
   ========================================================================== */

-- 1. Every lab result placed exactly once, every report on the server  (expect PASS)
SELECT * FROM rpt.v_report_coverage;

-- 2. Field match  (expect 100.0)
SELECT COUNT(*) AS values_compared, SUM(is_match) AS values_matched,
       COUNT(*) - SUM(is_match) AS values_mismatched,
       CAST(100.0 * SUM(is_match) / NULLIF(COUNT(*), 0) AS DECIMAL(5,1)) AS match_pct
FROM rpt.v_report_field_compare;

-- 2b. Any mismatches?  (expect NO rows)
SELECT TOP (50) * FROM rpt.v_report_field_compare WHERE is_match = 0;

-- 3. Each report lists exactly its own results  (expect only OK)
SELECT result_link, COUNT(*) AS results FROM rpt.v_report_result_check GROUP BY result_link;

-- 3b. Reports on the same patient and visit as their results  (expect only OK)
SELECT subject_link, encounter_link, COUNT(*) AS reports
FROM rpt.v_report_link_check GROUP BY subject_link, encounter_link;

-- 4. Reports by panel: panels, combined panels, single tests
SELECT s.kind, s.panel_code, n.report_name,
       COUNT(*) AS reports, SUM(CAST(s.members AS INT)) AS results
FROM etl.report_sources AS s
OUTER APPLY (SELECT TOP (1) e.expected_value AS report_name FROM etl.report_expected AS e
             WHERE e.legacy_id = s.report_key AND e.field_name = 'code_display') AS n
GROUP BY s.kind, s.panel_code, n.report_name
ORDER BY s.kind, reports DESC;

-- 4b. Rules triggered
SELECT rule_id, action, COUNT(*) AS rows_affected, MIN(detail) AS example
FROM etl.report_issue_log
GROUP BY rule_id, action
ORDER BY rule_id, action;
