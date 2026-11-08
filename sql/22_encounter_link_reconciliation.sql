/* =============================================================================
   22_encounter_link_reconciliation.sql
   Phase 2, step 3: reconcile the visit links
   (Encounter -> Practitioner, Location, Organization).

   Run AFTER:  python src\phase2_encounter_links.py
   How to run: open in SSMS, press F5. Read the result grids top to bottom.
   Safe to re-run (CREATE OR ALTER).

   Four questions:
     1. Is every migrated visit linked?                       (coverage)
     2. Does the server hold exactly the links we sent?       (field comparison)
     3. Does each visit point to the RIGHT practitioner -
        the one in the legacy PROVIDER column?                (link correctness)
     4. Does the visit's practitioner have a role at the
        visit's location?                                     (cross-resource consistency)

   Then re-run the Phase 1 checks: python src\extract_fhir.py and
   sql\10_reconciliation.sql must still show 44,765 of 44,765 matched,
   proving the update changed ONLY the links (a regression test).
   ========================================================================== */

USE FhirMigration;
GO

IF SCHEMA_ID('rpt') IS NULL EXEC('CREATE SCHEMA rpt');
GO

/* -----------------------------------------------------------------------------
   One row per migrated encounter with its link status.
   -------------------------------------------------------------------------- */
CREATE OR ALTER VIEW rpt.v_encounter_link_status AS
WITH enc AS (
    SELECT Id, PROVIDER, source_row,
           ROW_NUMBER() OVER (PARTITION BY Id ORDER BY source_row) AS rn
    FROM cur.encounters
),
clinic AS (
    SELECT MAX(CASE WHEN resource_type = 'Organization' THEN N'Organization/' + fhir_id END) AS org_ref,
           MAX(CASE WHEN resource_type = 'Location'     THEN N'Location/'     + fhir_id END) AS loc_ref
    FROM etl.clinic_crosswalk
)
SELECT x.legacy_id AS legacy_encounter_id, x.fhir_id AS encounter_fhir_id,
       e.PROVIDER  AS legacy_provider_id,
       a.practitioner_ref, a.location_ref, a.service_provider_ref,
       CASE
           WHEN a.encounter_fhir_id IS NULL                         THEN 'Not read back from server'
           WHEN NULLIF(LTRIM(e.PROVIDER), '') IS NULL               THEN 'No provider in legacy data'
           WHEN a.practitioner_ref IS NULL                          THEN 'Practitioner link missing'
           WHEN NOT EXISTS (SELECT 1 FROM tgt.practitioners AS p
                            WHERE N'Practitioner/' + p.fhir_id = a.practitioner_ref
                              AND p.legacy_id = e.PROVIDER)         THEN 'WRONG practitioner'
           WHEN ISNULL(a.location_ref, '') <> c.loc_ref             THEN 'Location link wrong or missing'
           WHEN ISNULL(a.service_provider_ref, '') <> c.org_ref     THEN 'Organization link wrong or missing'
           ELSE 'OK'
       END AS link_status
FROM etl.fhir_crosswalk AS x
JOIN enc AS e ON e.Id = x.legacy_id AND e.rn = 1
CROSS JOIN clinic AS c
LEFT JOIN tgt.encounter_links AS a ON a.encounter_fhir_id = x.fhir_id
WHERE x.resource_type = 'Encounter' AND x.fhir_id IS NOT NULL;
GO

/* -----------------------------------------------------------------------------
   Field comparison: links sent (etl.encounter_link_expected) vs. links stored
   -------------------------------------------------------------------------- */
CREATE OR ALTER VIEW rpt.v_encounter_link_compare AS
SELECT e.legacy_encounter_id, e.encounter_fhir_id, f.field_name, f.expected_value, f.actual_value,
       CASE WHEN ISNULL(f.expected_value, N'<null>') COLLATE Latin1_General_BIN2
               = ISNULL(f.actual_value,   N'<null>') COLLATE Latin1_General_BIN2
            THEN 1 ELSE 0 END AS is_match
FROM etl.encounter_link_expected AS e
LEFT JOIN tgt.encounter_links AS t ON t.encounter_fhir_id = e.encounter_fhir_id
CROSS APPLY (VALUES
    ('practitioner_ref',     e.practitioner_ref,     t.practitioner_ref),
    ('participant_type',     e.participant_type,     t.participant_type),
    ('location_ref',         e.location_ref,         t.location_ref),
    ('service_provider_ref', e.service_provider_ref, t.service_provider_ref)
) AS f (field_name, expected_value, actual_value);
GO

/* =============================================================================
   THE REPORT
   ========================================================================== */

-- 1. Coverage by status  (expect every encounter 'OK'; 'No provider in legacy data' only if the source had none)
SELECT link_status, COUNT(*) AS encounters
FROM rpt.v_encounter_link_status
GROUP BY link_status
ORDER BY encounters DESC;

-- 1b. Summary  (expect PASS: unaccounted 0 and nothing broken)
SELECT COUNT(*)                                                         AS migrated_encounters,
       SUM(CASE WHEN link_status = 'OK' THEN 1 ELSE 0 END)              AS fully_linked,
       SUM(CASE WHEN link_status = 'No provider in legacy data' THEN 1 ELSE 0 END) AS no_provider_in_source,
       SUM(CASE WHEN link_status NOT IN ('OK', 'No provider in legacy data') THEN 1 ELSE 0 END) AS problems,
       CASE WHEN SUM(CASE WHEN link_status NOT IN ('OK', 'No provider in legacy data') THEN 1 ELSE 0 END) = 0
            THEN 'PASS' ELSE 'FAIL' END                                 AS result
FROM rpt.v_encounter_link_status;

-- 2. Field match  (expect 100.0)
SELECT COUNT(*)                 AS values_compared,
       SUM(is_match)            AS values_matched,
       COUNT(*) - SUM(is_match) AS values_mismatched,
       CAST(100.0 * SUM(is_match) / NULLIF(COUNT(*), 0) AS DECIMAL(5,1)) AS match_pct
FROM rpt.v_encounter_link_compare;

-- 2b. Any mismatches?  (expect NO rows)
SELECT TOP (50) * FROM rpt.v_encounter_link_compare WHERE is_match = 0;

-- 3. Problems in detail  (expect NO rows)
SELECT TOP (50) * FROM rpt.v_encounter_link_status
WHERE link_status NOT IN ('OK', 'No provider in legacy data');

-- 4. Consistency: the visit's practitioner has a PractitionerRole at the visit's location
--    Step 1 (CTE): every practitioner + location pair that has a role.
--    Step 2: join each visit to that list. COUNT(r.practitioner_ref) counts only
--    the visits that found a match (COUNT ignores NULLs from the LEFT JOIN).
WITH role_pairs AS (
    SELECT DISTINCT rp.actual_value AS practitioner_ref, rl.actual_value AS location_ref
    FROM tgt.clinic_resources AS rp
    JOIN tgt.clinic_resources AS rl
      ON rl.resource_type = 'PractitionerRole' AND rl.fhir_id = rp.fhir_id
     AND rl.field_name = 'location_ref'
    WHERE rp.resource_type = 'PractitionerRole' AND rp.field_name = 'practitioner_ref'
)
SELECT COUNT(*)                  AS linked_visits,
       COUNT(r.practitioner_ref) AS visits_with_matching_role
FROM rpt.v_encounter_link_status AS s
LEFT JOIN role_pairs AS r
  ON r.practitioner_ref = s.practitioner_ref AND r.location_ref = s.location_ref
WHERE s.practitioner_ref IS NOT NULL;

-- 4b. Flags and errors raised by the script
SELECT rule_id, action, COUNT(*) AS encounters
FROM etl.encounter_link_issue_log
GROUP BY rule_id, action;
