/* =============================================================================
   27_observation_reconciliation.sql
   Phase 2, step 8: reconcile the Observation migration (vital signs, labs, surveys...).

   Run AFTER:  python src\plant_observation_errors.py
               python src\phase2_observation.py
   How to run: open in SSMS, press F5. Read the result grids top to bottom.
   This is the largest comparison in the project (about 340,000 values): allow a minute.
   Safe to re-run (CREATE OR ALTER).

   Four questions:
     1. Did every legacy observation row get exactly one disposition?   (row reconciliation)
     2. Does the server hold exactly what we sent?                      (field comparison)
     3. Does each observation point to the right patient and visit?     (link integrity)
     4. What was migrated: categories, numeric vs text, top tests       (summary)
   ========================================================================== */

USE FhirMigration;
GO

IF SCHEMA_ID('rpt') IS NULL EXEC('CREATE SCHEMA rpt');
GO

/* -----------------------------------------------------------------------------
   0. INDEXES - needed for speed at this size (about 375,000 values each side).
   Without them SQL Server may compare every row with every other row, which
   takes hours. phase2_observation.py recreates these tables on every run (which
   drops any index), so the indexes are created here, each time, before comparing.
   SSMS may print "Warning! The maximum key length ... 1700 bytes": harmless here,
   because the actual IDs and field names are far shorter than that.
   -------------------------------------------------------------------------- */
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_obs_expected'
               AND object_id = OBJECT_ID('etl.observation_expected'))
    CREATE INDEX ix_obs_expected ON etl.observation_expected (legacy_id, field_name)
        INCLUDE (expected_value, resource_type);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_obs_actual'
               AND object_id = OBJECT_ID('tgt.observation_resources'))
    CREATE INDEX ix_obs_actual ON tgt.observation_resources (fhir_id, field_name)
        INCLUDE (actual_value, resource_type, legacy_id);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_obs_xwalk'
               AND object_id = OBJECT_ID('etl.observation_crosswalk'))
    CREATE INDEX ix_obs_xwalk ON etl.observation_crosswalk (legacy_id)
        INCLUDE (fhir_id, resource_type);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_obs_sources'
               AND object_id = OBJECT_ID('etl.observation_sources'))
    CREATE INDEX ix_obs_sources ON etl.observation_sources (observation_key)
        INCLUDE (legacy_patient, legacy_encounter);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_fhir_xwalk_lookup'
               AND object_id = OBJECT_ID('etl.fhir_crosswalk'))
    CREATE INDEX ix_fhir_xwalk_lookup ON etl.fhir_crosswalk (resource_type, legacy_id)
        INCLUDE (fhir_id);
GO

-- 1. ROW RECONCILIATION
CREATE OR ALTER VIEW rpt.v_observation_row_reconciliation AS
WITH d AS (
    SELECT
        (SELECT COUNT(*) FROM stg.observations)                                       AS source_rows,
        SUM(CASE WHEN disposition = 'Migrated'                        THEN 1 ELSE 0 END) AS disposition_migrated,
        SUM(CASE WHEN disposition = 'Not migrated - simulator metric' THEN 1 ELSE 0 END) AS simulator_metrics,
        SUM(CASE WHEN disposition = 'Rejected'                        THEN 1 ELSE 0 END) AS rejected,
        SUM(CASE WHEN disposition = 'Rejected (cascade)'              THEN 1 ELSE 0 END) AS rejected_cascade,
        SUM(CASE WHEN disposition = 'Duplicate removed'               THEN 1 ELSE 0 END) AS duplicates_removed,
        COUNT(DISTINCT source_row)                                                    AS rows_with_disposition,
        (SELECT COUNT(*) FROM etl.observation_crosswalk WHERE fhir_id IS NOT NULL)    AS confirmed_on_server
    FROM etl.observation_disposition
)
SELECT source_rows, disposition_migrated, confirmed_on_server, simulator_metrics,
       rejected, rejected_cascade, duplicates_removed,
       source_rows - disposition_migrated - simulator_metrics - rejected - rejected_cascade - duplicates_removed
           AS unaccounted,
       CASE WHEN source_rows = disposition_migrated + simulator_metrics + rejected + rejected_cascade + duplicates_removed
             AND rows_with_disposition = source_rows
             AND confirmed_on_server = disposition_migrated
            THEN 'PASS' ELSE 'FAIL' END AS result
FROM d;
GO

-- 2. FIELD COMPARISON (long format)
CREATE OR ALTER VIEW rpt.v_observation_field_compare AS
SELECT e.legacy_id, x.fhir_id, e.field_name, e.expected_value, t.actual_value,
       CASE WHEN ISNULL(e.expected_value, N'<null>') COLLATE Latin1_General_BIN2
               = ISNULL(t.actual_value,   N'<null>') COLLATE Latin1_General_BIN2
            THEN 1 ELSE 0 END AS is_match
FROM etl.observation_expected AS e
JOIN etl.observation_crosswalk AS x
  ON x.resource_type = 'Observation' AND x.legacy_id = e.legacy_id AND x.fhir_id IS NOT NULL
LEFT JOIN tgt.observation_resources AS t
  ON t.resource_type = 'Observation' AND t.fhir_id = x.fhir_id AND t.field_name = e.field_name;
GO

-- 3. LINK INTEGRITY: subject and visit are the ones migrated from the legacy IDs
CREATE OR ALTER VIEW rpt.v_observation_link_check AS
WITH links AS (
    SELECT fhir_id,
           MAX(CASE WHEN field_name = 'subject_ref'   THEN actual_value END) AS subject_ref,
           MAX(CASE WHEN field_name = 'encounter_ref' THEN actual_value END) AS encounter_ref
    FROM tgt.observation_resources
    WHERE resource_type = 'Observation'
    GROUP BY fhir_id
)
SELECT s.observation_key, x.fhir_id,
       CASE WHEN l.subject_ref   = N'Patient/'   + px.fhir_id THEN 'OK' ELSE 'BROKEN' END AS subject_link,
       CASE WHEN l.encounter_ref = N'Encounter/' + ex.fhir_id THEN 'OK' ELSE 'BROKEN' END AS encounter_link
FROM etl.observation_sources AS s
JOIN etl.observation_crosswalk AS x
  ON x.resource_type = 'Observation' AND x.legacy_id = s.observation_key AND x.fhir_id IS NOT NULL
LEFT JOIN links AS l ON l.fhir_id = x.fhir_id
LEFT JOIN etl.fhir_crosswalk AS px ON px.resource_type = 'Patient'   AND px.legacy_id = s.legacy_patient
LEFT JOIN etl.fhir_crosswalk AS ex ON ex.resource_type = 'Encounter' AND ex.legacy_id = s.legacy_encounter;
GO

/* =============================================================================
   THE REPORT
   ========================================================================== */

-- 1. Every legacy observation row accounted for  (expect PASS, unaccounted 0)
SELECT * FROM rpt.v_observation_row_reconciliation;

-- 2. Field match  (expect 100.0)
SELECT COUNT(*) AS values_compared, SUM(is_match) AS values_matched,
       COUNT(*) - SUM(is_match) AS values_mismatched,
       CAST(100.0 * SUM(is_match) / NULLIF(COUNT(*), 0) AS DECIMAL(5,1)) AS match_pct
FROM rpt.v_observation_field_compare;

-- 2b. Any mismatches?  (expect NO rows)
SELECT TOP (50) * FROM rpt.v_observation_field_compare WHERE is_match = 0;

-- 3. Link integrity  (expect only OK)
SELECT subject_link, encounter_link, COUNT(*) AS observations
FROM rpt.v_observation_link_check
GROUP BY subject_link, encounter_link;

-- 4. Category and value type of what was migrated
WITH o AS (
    SELECT fhir_id,
           MAX(CASE WHEN field_name = 'category'     THEN actual_value END) AS category,
           MAX(CASE WHEN field_name = 'value_number' THEN actual_value END) AS value_number
    FROM tgt.observation_resources
    WHERE resource_type = 'Observation'
    GROUP BY fhir_id
)
SELECT ISNULL(category, '(none)') AS category,
       SUM(CASE WHEN value_number IS NOT NULL THEN 1 ELSE 0 END) AS numeric_values,
       SUM(CASE WHEN value_number IS NULL     THEN 1 ELSE 0 END) AS text_values,
       COUNT(*) AS observations
FROM o
GROUP BY category
ORDER BY observations DESC;

-- 4a. Code systems migrated  (LOINC, plus the confirmed exceptions from docs\observation_code_map.csv)
WITH o AS (
    SELECT fhir_id, MAX(CASE WHEN field_name = 'code_system' THEN actual_value END) AS code_system
    FROM tgt.observation_resources
    WHERE resource_type = 'Observation'
    GROUP BY fhir_id
)
SELECT CASE code_system WHEN 'http://loinc.org'       THEN 'LOINC'
                        WHEN 'http://snomed.info/sct' THEN 'SNOMED CT'
                        WHEN 'urn:fhirmigration:legacy-observation-code' THEN 'Local (to be mapped)'
                        ELSE ISNULL(code_system, '(none)') END AS code_system,
       COUNT(*) AS observations
FROM o
GROUP BY code_system
ORDER BY observations DESC;

-- 4b. Most common tests (LOINC) with their units
WITH o AS (
    SELECT fhir_id,
           MAX(CASE WHEN field_name = 'code'         THEN actual_value END) AS loinc_code,
           MAX(CASE WHEN field_name = 'code_display' THEN actual_value END) AS description,
           MAX(CASE WHEN field_name = 'value_unit'   THEN actual_value END) AS unit
    FROM tgt.observation_resources
    WHERE resource_type = 'Observation'
    GROUP BY fhir_id
)
SELECT TOP (10) loinc_code, description, unit, COUNT(*) AS observations
FROM o
GROUP BY loinc_code, description, unit
ORDER BY observations DESC;

-- 4c. Rules triggered
SELECT rule_id, action, COUNT(*) AS rows_affected
FROM etl.observation_issue_log
GROUP BY rule_id, action
ORDER BY rule_id, action;
