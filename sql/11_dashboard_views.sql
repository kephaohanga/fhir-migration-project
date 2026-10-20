/* =============================================================================
   11_dashboard_views.sql
   Step 11: two extra views shaped for Power BI visuals.
   Run once in SSMS (F5) after 10_reconciliation.sql. Safe to re-run.
   ========================================================================== */

USE FhirMigration;
GO

/* -----------------------------------------------------------------------------
   rpt.v_row_outcomes - one row per table per outcome ("long" format).
   Power BI charts work best with long data: one column for the category
   (outcome) and one for the number (row_count).
   -------------------------------------------------------------------------- */
CREATE OR ALTER VIEW rpt.v_row_outcomes AS
SELECT r.table_name,
       o.outcome,
       o.outcome_order,
       o.row_count
FROM rpt.v_row_reconciliation AS r
CROSS APPLY (VALUES
    ('Migrated',           1, r.migrated),
    ('Rejected - direct',  2, r.rejected_direct),
    ('Rejected - cascade', 3, r.rejected_cascade),
    ('Duplicate removed',  4, r.duplicates_removed)
) AS o (outcome, outcome_order, row_count);
GO

/* -----------------------------------------------------------------------------
   rpt.v_load_by_patient - how big each patient's bundle was and how long it
   took to load (from the Step 8 load log). Shows the "few patients hold most
   of the records" pattern.
   -------------------------------------------------------------------------- */
CREATE OR ALTER VIEW rpt.v_load_by_patient AS
SELECT l.legacy_patient_id,
       x.fhir_id                    AS patient_fhir_id,
       CONCAT(p.FIRST, ' ', p.LAST) AS patient_name,
       l.entries                    AS resources_in_bundle,
       l.seconds                    AS load_seconds,
       l.outcome
FROM etl.load_log AS l
LEFT JOIN etl.fhir_crosswalk AS x
       ON x.resource_type = 'Patient' AND x.legacy_id = l.legacy_patient_id
OUTER APPLY (SELECT TOP (1) FIRST, LAST
             FROM stg.patients
             WHERE Id = l.legacy_patient_id
             ORDER BY source_row) AS p;
GO

-- Quick check: expect 12 rows (3 tables x 4 outcomes) and 52 rows
SELECT * FROM rpt.v_row_outcomes ORDER BY table_name DESC, outcome_order;
SELECT TOP (5) * FROM rpt.v_load_by_patient ORDER BY resources_in_bundle DESC;


GO

/* -----------------------------------------------------------------------------
   rpt.v_run_info - when the pipeline ran and when the report was refreshed.
   One row. Shown as a footnote on the dashboard so every PASS is tied to a
   specific run. SYSUTCDATETIME() records the moment Power BI refreshes.
   -------------------------------------------------------------------------- */
CREATE OR ALTER VIEW rpt.v_run_info AS
SELECT
    (SELECT MAX(loaded_at)    FROM etl.load_log)   AS fhir_load_utc,
    (SELECT MAX(extracted_at) FROM tgt.patients)   AS fhir_extract_utc,
    SYSUTCDATETIME()                               AS report_refreshed_utc;
GO

-- Quick check: expect 1 row with three timestamps
SELECT * FROM rpt.v_run_info;