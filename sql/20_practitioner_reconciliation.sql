/* =============================================================================
   20_practitioner_reconciliation.sql
   Phase 2, step 1: reconcile the Practitioner migration.

   Run AFTER:  python src\plant_provider_errors.py
               python src\phase2_practitioner.py
   How to run: open in SSMS, press F5. Read the result grids top to bottom.
   Safe to re-run (CREATE OR ALTER).

   Three questions:
     1. Is every provider row accounted for?        (row reconciliation)
     2. Does the server hold exactly what we sent?  (field-by-field comparison)
     3. Does every migrated visit's provider exist on the server?  (link coverage)
   ========================================================================== */

USE FhirMigration;
GO

IF SCHEMA_ID('rpt') IS NULL EXEC('CREATE SCHEMA rpt');
GO

/* -----------------------------------------------------------------------------
   1. ROW RECONCILIATION
   source rows = migrated + rejected + duplicates removed  ->  unaccounted must be 0
   -------------------------------------------------------------------------- */
CREATE OR ALTER VIEW rpt.v_practitioner_row_reconciliation AS
WITH counts AS (
    SELECT
        (SELECT COUNT(*) FROM stg.providers) AS source_rows,
        (SELECT COUNT(*) FROM etl.practitioner_crosswalk WHERE fhir_id IS NOT NULL) AS migrated,
        (SELECT COUNT(*) FROM etl.practitioner_issue_log WHERE action = 'Reject') AS rejected,
        (SELECT COUNT(*) FROM etl.practitioner_issue_log WHERE rule_id = 'R-PR04') AS duplicates_removed
)
SELECT 'providers' AS table_name, source_rows, migrated, rejected, duplicates_removed,
       source_rows - migrated - rejected - duplicates_removed AS unaccounted,
       CASE WHEN source_rows - migrated - rejected - duplicates_removed = 0
            THEN 'PASS' ELSE 'FAIL' END AS result
FROM counts;
GO

/* -----------------------------------------------------------------------------
   2. FIELD COMPARISON: what we sent (etl.practitioner_expected) vs. what the
   server returned (tgt.practitioners), paired through the crosswalk.
   CROSS APPLY turns each field into its own row, so every value is checked.
   -------------------------------------------------------------------------- */
CREATE OR ALTER VIEW rpt.v_practitioner_field_compare AS
SELECT e.legacy_id, x.fhir_id, f.field_name, f.expected_value, f.actual_value,
       CASE WHEN ISNULL(f.expected_value, N'<null>') COLLATE Latin1_General_BIN2
               = ISNULL(f.actual_value,   N'<null>') COLLATE Latin1_General_BIN2
            THEN 1 ELSE 0 END AS is_match
FROM etl.practitioner_expected AS e
JOIN etl.practitioner_crosswalk AS x ON x.legacy_id = e.legacy_id AND x.fhir_id IS NOT NULL
JOIN tgt.practitioners          AS t ON t.fhir_id   = x.fhir_id
CROSS APPLY (VALUES
    ('npi',          e.npi,          t.npi),
    ('prefix',       e.prefix,       t.prefix),
    ('given_first',  e.given_first,  t.given_first),
    ('given_other',  e.given_other,  t.given_other),
    ('family',       e.family,       t.family),
    ('name_text',    e.name_text,    t.name_text),
    ('gender',       e.gender,       t.gender),
    ('address_line', e.address_line, t.address_line),
    ('city',         e.city,         t.city),
    ('state',        e.state,        t.state),
    ('postal_code',  e.postal_code,  t.postal_code),
    ('country',      e.country,      t.country)
) AS f (field_name, expected_value, actual_value);
GO

/* -----------------------------------------------------------------------------
   3. LINK COVERAGE: every migrated encounter's PROVIDER should be a migrated
   practitioner. LEFT JOIN + IS NULL finds the ones that are not.
   An archived provider is a documented decision, so it gets its own reason.
   -------------------------------------------------------------------------- */
CREATE OR ALTER VIEW rpt.v_encounter_provider_coverage AS
SELECT e.Id       AS legacy_encounter_id,
       e.PROVIDER AS legacy_provider_id,
       CASE WHEN e.PROVIDER IS NULL THEN 'Encounter has no provider'
            WHEN EXISTS (SELECT 1 FROM etl.corrections AS k
                         WHERE k.table_name = 'providers' AND k.record_key = e.PROVIDER
                           AND k.disposition = 'Archive')
                 THEN 'Provider archived - visit keeps legacy provider only'
            WHEN EXISTS (SELECT 1 FROM etl.practitioner_issue_log AS i
                         WHERE i.record_key = e.PROVIDER AND i.action = 'Reject')
                 THEN 'Provider rejected - awaiting Credentialing review'
            ELSE 'Provider not in the legacy provider file' END AS problem
FROM cur.encounters AS e
JOIN etl.fhir_crosswalk AS xe
  ON xe.resource_type = 'Encounter' AND xe.legacy_id = e.Id AND xe.fhir_id IS NOT NULL
LEFT JOIN etl.practitioner_crosswalk AS xp
  ON xp.legacy_id = e.PROVIDER AND xp.fhir_id IS NOT NULL
WHERE xp.legacy_id IS NULL;
GO

/* =============================================================================
   THE REPORT
   ========================================================================== */

-- 1. Is every provider row accounted for?  (expect PASS, unaccounted = 0)
SELECT * FROM rpt.v_practitioner_row_reconciliation;

-- 2. Field match rate  (expect 100.0)
SELECT COUNT(*)                    AS values_compared,
       SUM(is_match)               AS values_matched,
       COUNT(*) - SUM(is_match)    AS values_mismatched,
       CAST(100.0 * SUM(is_match) / NULLIF(COUNT(*), 0) AS DECIMAL(5,1)) AS match_pct
FROM rpt.v_practitioner_field_compare;

-- 2b. Match rate per field
SELECT field_name, COUNT(*) AS compared, SUM(is_match) AS matched
FROM rpt.v_practitioner_field_compare
GROUP BY field_name
ORDER BY field_name;

-- 2c. Any mismatches?  (expect NO rows)
SELECT TOP (50) * FROM rpt.v_practitioner_field_compare WHERE is_match = 0;

-- 2d. Misspelt names corrected from the credentialing roster: legacy vs. server
SELECT i.record_key AS legacy_id, s.NAME AS legacy_name,
       t.name_text  AS name_on_server, i.detail
FROM etl.practitioner_issue_log AS i
JOIN stg.providers AS s ON s.source_row = i.source_row
LEFT JOIN etl.practitioner_crosswalk AS x ON x.legacy_id = i.record_key
LEFT JOIN tgt.practitioners AS t ON t.fhir_id = x.fhir_id
WHERE i.rule_id = 'R-PR02';

-- 3. Migrated visits whose provider is not on the server, by reason.
--    Before remediation: only the visits of the provider rejected for a wrong name.
--    After remediation:  NO rows (provider corrected), or only
--    'Provider archived' rows (a documented decision). Anything else is a finding.
SELECT problem, COUNT(*) AS visits
FROM rpt.v_encounter_provider_coverage
GROUP BY problem;

-- 3b. Coverage summary
SELECT
    (SELECT COUNT(DISTINCT e.PROVIDER)
       FROM cur.encounters AS e
       JOIN etl.fhir_crosswalk AS xe
         ON xe.resource_type = 'Encounter' AND xe.legacy_id = e.Id AND xe.fhir_id IS NOT NULL
    ) AS providers_used_by_migrated_visits,
    (SELECT COUNT(*) FROM etl.practitioner_crosswalk WHERE fhir_id IS NOT NULL) AS practitioners_migrated;
