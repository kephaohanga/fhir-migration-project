/* =============================================================================
   21_clinic_structure_reconciliation.sql
   Phase 2, step 2: reconcile the clinic structure
   (Organization, Location, PractitionerRole).

   Run AFTER:  python src\phase2_clinic_structure.py
   How to run: open in SSMS, press F5. Read the result grids top to bottom.
   Safe to re-run (CREATE OR ALTER).

   Five questions:
     1. Does every provider have exactly one role?              (row reconciliation)
     2. Does the server hold exactly what we sent?              (field comparison)
     3. Does every role point to the RIGHT practitioner and to
        the clinic's Organization and Location?                 (link integrity)
     4. How were specialties coded?                             (code mapping coverage)
     5. What happened to the legacy organization values?        (documented decision)
   ========================================================================== */

USE FhirMigration;
GO

IF SCHEMA_ID('rpt') IS NULL EXEC('CREATE SCHEMA rpt');
GO

/* -----------------------------------------------------------------------------
   1. ROW RECONCILIATION
   distinct providers = roles migrated + roles rejected   (unaccounted must be 0)
   and roles migrated = practitioners migrated            (one role per practitioner)
   -------------------------------------------------------------------------- */
CREATE OR ALTER VIEW rpt.v_role_row_reconciliation AS
WITH c AS (
    SELECT
        (SELECT COUNT(DISTINCT Id) FROM cur.providers WHERE NULLIF(LTRIM(Id), '') IS NOT NULL) AS distinct_providers,
        (SELECT COUNT(*) FROM etl.clinic_crosswalk
          WHERE resource_type = 'PractitionerRole' AND fhir_id IS NOT NULL)                    AS roles_migrated,
        (SELECT COUNT(*) FROM etl.clinic_issue_log WHERE action = 'Reject')                    AS roles_rejected,
        (SELECT COUNT(*) FROM etl.practitioner_crosswalk WHERE fhir_id IS NOT NULL)            AS practitioners_migrated,
        (SELECT COUNT(*) FROM etl.clinic_crosswalk
          WHERE resource_type = 'Organization' AND fhir_id IS NOT NULL)                        AS organizations,
        (SELECT COUNT(*) FROM etl.clinic_crosswalk
          WHERE resource_type = 'Location' AND fhir_id IS NOT NULL)                            AS locations
)
SELECT distinct_providers, roles_migrated, roles_rejected,
       distinct_providers - roles_migrated - roles_rejected AS unaccounted,
       practitioners_migrated, organizations, locations,
       CASE WHEN distinct_providers - roles_migrated - roles_rejected = 0
             AND roles_migrated = practitioners_migrated
             AND organizations = 1 AND locations = 1
            THEN 'PASS' ELSE 'FAIL' END AS result
FROM c;
GO

/* -----------------------------------------------------------------------------
   2. FIELD COMPARISON
   Expected and actual are both stored one row per field ("long" format), so a
   single JOIN compares every field of every resource type. LEFT JOIN: a field
   missing on the server shows up as a mismatch instead of disappearing.
   -------------------------------------------------------------------------- */
CREATE OR ALTER VIEW rpt.v_clinic_field_compare AS
SELECT e.resource_type, e.legacy_id, x.fhir_id, e.field_name,
       e.expected_value, t.actual_value,
       CASE WHEN ISNULL(e.expected_value, N'<null>') COLLATE Latin1_General_BIN2
               = ISNULL(t.actual_value,   N'<null>') COLLATE Latin1_General_BIN2
            THEN 1 ELSE 0 END AS is_match
FROM etl.clinic_expected AS e
JOIN etl.clinic_crosswalk AS x
  ON x.resource_type = e.resource_type AND x.legacy_id = e.legacy_id AND x.fhir_id IS NOT NULL
LEFT JOIN tgt.clinic_resources AS t
  ON t.resource_type = e.resource_type AND t.fhir_id = x.fhir_id AND t.field_name = e.field_name;
GO

/* -----------------------------------------------------------------------------
   3. LINK INTEGRITY: each role must point to
      - the practitioner migrated FROM THE SAME legacy provider (not just any practitioner)
      - the clinic's one Organization and one Location
   -------------------------------------------------------------------------- */
CREATE OR ALTER VIEW rpt.v_role_link_check AS
SELECT x.legacy_id, x.fhir_id AS role_fhir_id, t.field_name AS link, t.actual_value AS points_to,
       CASE t.field_name
           WHEN 'practitioner_ref' THEN
               CASE WHEN EXISTS (SELECT 1 FROM tgt.practitioners AS p
                                 WHERE N'Practitioner/' + p.fhir_id = t.actual_value
                                   AND p.legacy_id = x.legacy_id)
                    THEN 'OK' ELSE 'BROKEN - not the practitioner of this provider' END
           WHEN 'organization_ref' THEN
               CASE WHEN t.actual_value = (SELECT TOP (1) N'Organization/' + fhir_id FROM etl.clinic_crosswalk
                                           WHERE resource_type = 'Organization')
                    THEN 'OK' ELSE 'BROKEN - not the clinic Organization' END
           WHEN 'location_ref' THEN
               CASE WHEN t.actual_value = (SELECT TOP (1) N'Location/' + fhir_id FROM etl.clinic_crosswalk
                                           WHERE resource_type = 'Location')
                    THEN 'OK' ELSE 'BROKEN - not the clinic Location' END
       END AS link_status
FROM etl.clinic_crosswalk AS x
JOIN tgt.clinic_resources AS t
  ON t.resource_type = 'PractitionerRole' AND t.fhir_id = x.fhir_id
 AND t.field_name IN ('practitioner_ref', 'organization_ref', 'location_ref')
WHERE x.resource_type = 'PractitionerRole' AND x.fhir_id IS NOT NULL;
GO

/* =============================================================================
   THE REPORT
   ========================================================================== */

-- 1. One role per provider, one clinic  (expect PASS, unaccounted = 0, 1 organization, 1 location)
SELECT * FROM rpt.v_role_row_reconciliation;

-- 2. Field match rate by resource type  (expect 100.0 on every row)
SELECT resource_type,
       COUNT(*)                 AS values_compared,
       SUM(is_match)            AS values_matched,
       COUNT(*) - SUM(is_match) AS values_mismatched,
       CAST(100.0 * SUM(is_match) / NULLIF(COUNT(*), 0) AS DECIMAL(5,1)) AS match_pct
FROM rpt.v_clinic_field_compare
GROUP BY resource_type
ORDER BY resource_type;

-- 2b. Any mismatches?  (expect NO rows)
SELECT TOP (50) * FROM rpt.v_clinic_field_compare WHERE is_match = 0;

-- 3. Link integrity  (expect only 'OK', 3 links per role)
SELECT link, link_status, COUNT(*) AS roles
FROM rpt.v_role_link_check
GROUP BY link, link_status
ORDER BY link, link_status;

-- 4. Specialty coding: legacy text -> NUCC code  (a blank code = not in docs\specialty_map.csv)
WITH roles AS (
    SELECT fhir_id,
           MAX(CASE WHEN field_name = 'specialty_text' THEN actual_value END) AS specialty_text,
           MAX(CASE WHEN field_name = 'specialty_code' THEN actual_value END) AS nucc_code
    FROM tgt.clinic_resources
    WHERE resource_type = 'PractitionerRole'
    GROUP BY fhir_id
)
SELECT ISNULL(specialty_text, '(blank)') AS legacy_specialty, nucc_code, COUNT(*) AS roles
FROM roles
GROUP BY specialty_text, nucc_code
ORDER BY roles DESC;

-- 4b. Flags raised  (R-RL02 blank specialty, R-RL03 specialty not in the map)
SELECT rule_id, action, COUNT(*) AS roles
FROM etl.clinic_issue_log
GROUP BY rule_id, action;

-- 5. Legacy organizations: documented as NOT migrated (one-clinic design decision)
SELECT COUNT(DISTINCT ORGANIZATION) AS legacy_organizations_not_migrated,
       (SELECT COUNT(*) FROM etl.clinic_crosswalk
         WHERE resource_type = 'Organization' AND fhir_id IS NOT NULL) AS clinic_organizations_migrated
FROM cur.providers;
