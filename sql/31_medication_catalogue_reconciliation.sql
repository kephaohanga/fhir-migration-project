/* =============================================================================
   31_medication_catalogue_reconciliation.sql
   Phase 3, step 4: reconcile the drug catalogue (FHIR Medication) and the re-pointed
   prescriptions (MedicationRequest -> Medication).

   Run AFTER:  python src\phase3_medication.py
               python src\phase2_medication.py      (re-points the prescriptions)
   How to run: open in SSMS, press F5. Read the result grids top to bottom.
   Then also re-run sql\26_medication_reconciliation.sql: the prescriptions changed, so
   their own reconciliation must still PASS at 100%.
   Safe to re-run (CREATE OR ALTER).

   Four questions:
     1. Is there exactly one Medication per prescribed drug, and is each on the server?
     2. Does the server hold exactly what we sent?                       (field comparison)
     3. Does every migrated prescription point to the Medication of ITS OWN drug?
     4. What is in the catalogue: dose forms, most prescribed drugs, rules
   ========================================================================== */

USE FhirMigration;
GO

IF SCHEMA_ID('rpt') IS NULL EXEC('CREATE SCHEMA rpt');
GO

-- 3 (used by 1). Each migrated prescription: the Medication it SHOULD point to (by its legacy
-- drug code) and the one it DOES point to on the server.
CREATE OR ALTER VIEW rpt.v_prescription_drug_link AS
WITH ref AS (
    SELECT fhir_id, MAX(CASE WHEN field_name = 'medication_ref' THEN actual_value END) AS medication_ref
    FROM tgt.medication_resources
    WHERE resource_type = 'MedicationRequest'
    GROUP BY fhir_id
)
SELECT s.medication_key, x.fhir_id AS prescription_fhir_id, LTRIM(RTRIM(m.CODE)) AS rxnorm_code,
       r.medication_ref,
       CASE WHEN dx.fhir_id IS NULL                              THEN 'DRUG NOT IN CATALOGUE'
            WHEN r.medication_ref = N'Medication/' + dx.fhir_id  THEN 'OK'
            WHEN r.medication_ref IS NULL                        THEN 'NO REFERENCE'
            ELSE 'BROKEN' END AS drug_link
FROM etl.medication_sources AS s
JOIN etl.medication_crosswalk AS x
  ON x.resource_type = 'MedicationRequest' AND x.legacy_id = s.medication_key AND x.fhir_id IS NOT NULL
JOIN cur.medications AS m ON m.source_row = TRY_CAST(s.source_row AS INT)
LEFT JOIN ref AS r ON r.fhir_id = x.fhir_id
LEFT JOIN etl.drug_crosswalk AS dx ON dx.legacy_id = LTRIM(RTRIM(m.CODE)) AND dx.fhir_id IS NOT NULL;
GO

-- 1. CATALOGUE COMPLETENESS
CREATE OR ALTER VIEW rpt.v_drug_catalogue_check AS
WITH d AS (
    SELECT (SELECT COUNT(*) FROM etl.drug_catalogue)                                     AS drugs_in_catalogue,
           (SELECT COUNT(DISTINCT code) FROM etl.drug_catalogue)                         AS distinct_codes,
           (SELECT COUNT(*) FROM etl.drug_crosswalk WHERE fhir_id IS NOT NULL)           AS confirmed_on_server,
           (SELECT COUNT(DISTINCT legacy_id) FROM tgt.drug_resources)                    AS found_on_server,
           (SELECT COUNT(DISTINCT rxnorm_code) FROM rpt.v_prescription_drug_link)        AS drugs_prescribed,
           (SELECT COUNT(*) FROM rpt.v_prescription_drug_link
             WHERE drug_link = 'DRUG NOT IN CATALOGUE')                                  AS prescriptions_without_drug
)
SELECT *,
       CASE WHEN drugs_in_catalogue = distinct_codes
             AND confirmed_on_server = drugs_in_catalogue
             AND found_on_server = drugs_in_catalogue
             AND prescriptions_without_drug = 0
            THEN 'PASS' ELSE 'FAIL' END AS result
FROM d;
GO

-- 2. FIELD COMPARISON (long format)
CREATE OR ALTER VIEW rpt.v_drug_field_compare AS
SELECT e.legacy_id, x.fhir_id, e.field_name, e.expected_value, t.actual_value,
       CASE WHEN ISNULL(e.expected_value, N'<null>') COLLATE Latin1_General_BIN2
               = ISNULL(t.actual_value,   N'<null>') COLLATE Latin1_General_BIN2
            THEN 1 ELSE 0 END AS is_match
FROM etl.drug_expected AS e
JOIN etl.drug_crosswalk AS x ON x.legacy_id = e.legacy_id AND x.fhir_id IS NOT NULL
LEFT JOIN tgt.drug_resources AS t ON t.fhir_id = x.fhir_id AND t.field_name = e.field_name;
GO

/* =============================================================================
   THE REPORT
   ========================================================================== */

-- 1. One Medication per prescribed drug, all on the server  (expect PASS)
--    drugs_prescribed can be LOWER than drugs_in_catalogue: the catalogue is built from every
--    valid drug in the legacy file, including drugs whose only prescriptions were rejected.
SELECT * FROM rpt.v_drug_catalogue_check;

-- 2. Field match  (expect 100.0)
SELECT COUNT(*) AS values_compared, SUM(is_match) AS values_matched,
       COUNT(*) - SUM(is_match) AS values_mismatched,
       CAST(100.0 * SUM(is_match) / NULLIF(COUNT(*), 0) AS DECIMAL(5,1)) AS match_pct
FROM rpt.v_drug_field_compare;

-- 2b. Any mismatches?  (expect NO rows)
SELECT TOP (50) * FROM rpt.v_drug_field_compare WHERE is_match = 0;

-- 3. Every prescription points to the Medication of its own drug  (expect only OK)
SELECT drug_link, COUNT(*) AS prescriptions
FROM rpt.v_prescription_drug_link
GROUP BY drug_link;

-- 4. Dose forms in the catalogue
SELECT ISNULL(dose_form, '(none - flagged R-MN02)') AS dose_form, COUNT(*) AS drugs
FROM etl.drug_catalogue
GROUP BY dose_form
ORDER BY drugs DESC;

-- 4b. The most prescribed drugs, counted through the references on the server
SELECT TOP (10) l.rxnorm_code, c.display AS drug, c.dose_form, COUNT(*) AS prescriptions
FROM rpt.v_prescription_drug_link AS l
JOIN etl.drug_catalogue AS c ON c.code = l.rxnorm_code
GROUP BY l.rxnorm_code, c.display, c.dose_form
ORDER BY prescriptions DESC;

-- 4c. Rules triggered (catalogue)
SELECT rule_id, action, record_key AS rxnorm_code, detail
FROM etl.drug_issue_log
ORDER BY rule_id, record_key;
