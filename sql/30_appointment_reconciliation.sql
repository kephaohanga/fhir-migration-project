/* =============================================================================
   30_appointment_reconciliation.sql
   Phase 3, step 3: reconcile the appointment -> FHIR Appointment migration.

   Run AFTER:  python src\plant_appointment_errors.py
               python src\phase3_appointment.py
   How to run: open in SSMS, press F5. Read the result grids top to bottom.
   Safe to re-run (CREATE OR ALTER).

   Five questions:
     1. Did every legacy appointment row get exactly one disposition?  (row reconciliation)
     2. Does the server hold exactly what we sent?                     (field comparison)
     3. Does each appointment point to the right patient, provider,
        clinic location and prior authorization?                       (link integrity)
     4. Is every legacy status expressed correctly, including the
        patient's confirmation?                                        (status check)
     5. Is the migrated schedule safe to go live with?                 (open-work check:
        nobody deceased, clinic open, no double booking, authorized)
   ========================================================================== */

USE FhirMigration;
GO

IF SCHEMA_ID('rpt') IS NULL EXEC('CREATE SCHEMA rpt');
GO

-- 1. ROW RECONCILIATION
CREATE OR ALTER VIEW rpt.v_appointment_row_reconciliation AS
WITH d AS (
    SELECT
        (SELECT COUNT(*) FROM stg.appointments)                                    AS source_rows,
        SUM(CASE WHEN disposition = 'Migrated'          THEN 1 ELSE 0 END)         AS disposition_migrated,
        SUM(CASE WHEN disposition = 'Rejected'          THEN 1 ELSE 0 END)         AS rejected,
        SUM(CASE WHEN disposition = 'Duplicate removed' THEN 1 ELSE 0 END)         AS duplicates_removed,
        COUNT(DISTINCT source_row)                                                 AS rows_with_disposition,
        (SELECT COUNT(*) FROM etl.appointment_crosswalk WHERE fhir_id IS NOT NULL) AS confirmed_on_server
    FROM etl.appointment_disposition
)
SELECT source_rows, disposition_migrated, confirmed_on_server, rejected, duplicates_removed,
       source_rows - disposition_migrated - rejected - duplicates_removed AS unaccounted,
       CASE WHEN source_rows = disposition_migrated + rejected + duplicates_removed
             AND rows_with_disposition = source_rows
             AND confirmed_on_server = disposition_migrated
            THEN 'PASS' ELSE 'FAIL' END AS result
FROM d;
GO

-- 2. FIELD COMPARISON (long format)
CREATE OR ALTER VIEW rpt.v_appointment_field_compare AS
SELECT e.legacy_id, x.fhir_id, e.field_name, e.expected_value, t.actual_value,
       CASE WHEN ISNULL(e.expected_value, N'<null>') COLLATE Latin1_General_BIN2
               = ISNULL(t.actual_value,   N'<null>') COLLATE Latin1_General_BIN2
            THEN 1 ELSE 0 END AS is_match
FROM etl.appointment_expected AS e
JOIN etl.appointment_crosswalk AS x
  ON x.resource_type = 'Appointment' AND x.legacy_id = e.legacy_id AND x.fhir_id IS NOT NULL
LEFT JOIN tgt.appointment_resources AS t
  ON t.resource_type = 'Appointment' AND t.fhir_id = x.fhir_id AND t.field_name = e.field_name;
GO

-- One row per migrated appointment, with the fields the checks below need (as on the server)
CREATE OR ALTER VIEW rpt.v_appointment_on_server AS
SELECT s.appointment_key, s.legacy_status, s.visit_type, s.legacy_patient, s.legacy_provider,
       s.legacy_auth, s.source_row, x.fhir_id,
       MAX(CASE WHEN t.field_name = 'status'           THEN t.actual_value END) AS status,
       MAX(CASE WHEN t.field_name = 'patient_status'   THEN t.actual_value END) AS patient_status,
       MAX(CASE WHEN t.field_name = 'start'            THEN t.actual_value END) AS start_utc,
       MAX(CASE WHEN t.field_name = 'end'              THEN t.actual_value END) AS end_utc,
       MAX(CASE WHEN t.field_name = 'patient_ref'      THEN t.actual_value END) AS patient_ref,
       MAX(CASE WHEN t.field_name = 'practitioner_ref' THEN t.actual_value END) AS practitioner_ref,
       MAX(CASE WHEN t.field_name = 'location_ref'     THEN t.actual_value END) AS location_ref,
       MAX(CASE WHEN t.field_name = 'auth_ref'         THEN t.actual_value END) AS auth_ref
FROM etl.appointment_sources AS s
JOIN etl.appointment_crosswalk AS x
  ON x.resource_type = 'Appointment' AND x.legacy_id = s.appointment_key AND x.fhir_id IS NOT NULL
LEFT JOIN tgt.appointment_resources AS t ON t.fhir_id = x.fhir_id AND t.resource_type = 'Appointment'
GROUP BY s.appointment_key, s.legacy_status, s.visit_type, s.legacy_patient, s.legacy_provider,
         s.legacy_auth, s.source_row, x.fhir_id;
GO

-- 3. LINK INTEGRITY
CREATE OR ALTER VIEW rpt.v_appointment_link_check AS
SELECT a.appointment_key, a.visit_type,
       CASE WHEN a.patient_ref = N'Patient/' + px.fhir_id THEN 'OK' ELSE 'BROKEN' END     AS patient_link,
       CASE WHEN a.practitioner_ref IS NULL AND pr.fhir_id IS NULL  THEN 'No practitioner (flagged)'
            WHEN a.practitioner_ref = N'Practitioner/' + pr.fhir_id THEN 'OK'
            ELSE 'BROKEN' END                                                             AS practitioner_link,
       CASE WHEN a.location_ref = N'Location/' + loc.fhir_id THEN 'OK' ELSE 'BROKEN' END  AS location_link,
       CASE WHEN a.legacy_auth IS NULL AND a.auth_ref IS NULL       THEN 'n/a'
            WHEN a.auth_ref = N'Claim/' + ax.fhir_id                THEN 'OK'
            ELSE 'BROKEN' END                                                             AS authorization_link
FROM rpt.v_appointment_on_server AS a
LEFT JOIN etl.fhir_crosswalk AS px ON px.resource_type = 'Patient' AND px.legacy_id = a.legacy_patient
LEFT JOIN etl.practitioner_crosswalk AS pr ON pr.legacy_id = a.legacy_provider AND pr.fhir_id IS NOT NULL
LEFT JOIN etl.clinic_crosswalk AS loc ON loc.resource_type = 'Location' AND loc.fhir_id IS NOT NULL
LEFT JOIN etl.prior_auth_crosswalk AS ax
  ON ax.resource_type = 'Claim' AND ax.legacy_id = a.legacy_auth AND ax.fhir_id IS NOT NULL;
GO

-- 4. STATUS CHECK: legacy status -> appointment status + the patient's own status
CREATE OR ALTER VIEW rpt.v_appointment_status_check AS
SELECT a.legacy_status, a.status, a.patient_status,
       CASE WHEN a.status + '|' + a.patient_status =
                 CASE a.legacy_status WHEN 'SCHEDULED'             THEN 'booked|needs-action'
                                      WHEN 'CONFIRMED'             THEN 'booked|accepted'
                                      WHEN 'CANCELLED'             THEN 'cancelled|declined'
                                      WHEN 'PENDING AUTHORIZATION' THEN 'pending|needs-action' END
            THEN 'OK' ELSE 'WRONG' END AS result
FROM rpt.v_appointment_on_server AS a;
GO

-- 5. GO-LIVE SAFETY: every active (not cancelled) appointment on the server, checked again
CREATE OR ALTER VIEW rpt.v_appointment_golive_check AS
SELECT a.appointment_key, a.visit_type, a.start_utc,
       CASE WHEN p.DEATHDATE IS NOT NULL AND LEFT(p.DEATHDATE, 10) <= LEFT(a.start_utc, 10)
            THEN 'DECEASED' ELSE 'ok' END                                        AS patient_alive,
       -- 1900-01-01 was a Monday, so days since then MOD 7 = 5 / 6 means Saturday / Sunday
       -- (works whatever language or first day of the week the server is set to)
       CASE WHEN DATEDIFF(DAY, '19000101', CAST(LEFT(a.start_utc, 10) AS DATE)) % 7 IN (5, 6)
                                                                                 THEN 'WEEKEND'
            WHEN LEFT(a.start_utc, 10) IN ('2026-11-26', '2026-12-25', '2027-01-01') THEN 'HOLIDAY'
            WHEN SUBSTRING(a.start_utc, 12, 5) NOT BETWEEN '13:00' AND '20:30'   THEN 'CLOSED HOURS'
            ELSE 'ok' END                                                        AS clinic_open,
       CASE WHEN EXISTS (SELECT 1 FROM rpt.v_appointment_on_server AS b
                          WHERE b.legacy_provider = a.legacy_provider
                            AND b.appointment_key <> a.appointment_key
                            AND b.status <> 'cancelled'
                            AND b.start_utc < a.end_utc AND a.start_utc < b.end_utc)
            THEN 'DOUBLE BOOKED' ELSE 'ok' END                                   AS provider_free,
       CASE WHEN a.visit_type NOT IN ('INFUSION', 'PROCEDURE')                   THEN 'n/a'
            WHEN pa.AUTH_ID IS NULL                                              THEN 'NO AUTHORIZATION'
            WHEN ps.decision = 'PENDING' AND a.status = 'pending'                THEN 'pending (visit pending too)'
            WHEN ps.decision <> 'APPROVED'                                       THEN 'NOT APPROVED'
            WHEN LEFT(a.start_utc, 10) NOT BETWEEN pa.VALID_FROM AND pa.VALID_TO THEN 'OUTSIDE APPROVED DATES'
            ELSE 'ok' END                                                        AS authorized
FROM rpt.v_appointment_on_server AS a
-- one row per patient: cur.patients can hold a patient twice (Phase 1's planted duplicate
-- stays in the curated table and is removed only during migration)
OUTER APPLY (SELECT TOP (1) cp.DEATHDATE FROM cur.patients AS cp
             WHERE cp.Id = a.legacy_patient ORDER BY cp.source_row) AS p
LEFT JOIN etl.prior_auth_sources AS ps ON ps.auth_key = a.legacy_auth
LEFT JOIN cur.prior_auths AS pa ON pa.source_row = TRY_CAST(ps.source_row AS INT)
WHERE a.status <> 'cancelled';
GO

/* =============================================================================
   THE REPORT
   ========================================================================== */

-- 1. Every legacy appointment row accounted for  (expect PASS, unaccounted 0)
SELECT * FROM rpt.v_appointment_row_reconciliation;

-- 2. Field match  (expect 100.0)
SELECT COUNT(*) AS values_compared, SUM(is_match) AS values_matched,
       COUNT(*) - SUM(is_match) AS values_mismatched,
       CAST(100.0 * SUM(is_match) / NULLIF(COUNT(*), 0) AS DECIMAL(5,1)) AS match_pct
FROM rpt.v_appointment_field_compare;

-- 2b. Any mismatches?  (expect NO rows)
SELECT TOP (50) * FROM rpt.v_appointment_field_compare WHERE is_match = 0;

-- 3. Link integrity  (expect OK / n/a; "No practitioner (flagged)" only for the R-AP03 row)
SELECT visit_type, patient_link, practitioner_link, location_link, authorization_link,
       COUNT(*) AS appointments
FROM rpt.v_appointment_link_check
GROUP BY visit_type, patient_link, practitioner_link, location_link, authorization_link
ORDER BY visit_type;

-- 4. Status, including the patient's confirmation  (expect only OK)
SELECT legacy_status, status, patient_status, result, COUNT(*) AS appointments
FROM rpt.v_appointment_status_check
GROUP BY legacy_status, status, patient_status, result
ORDER BY legacy_status;

-- 5. Go-live safety of the active schedule  (before staff fixes: the flagged visits appear here;
--    after: one row, every column 'ok' or 'n/a')
SELECT patient_alive, clinic_open, provider_free, authorized, COUNT(*) AS active_appointments
FROM rpt.v_appointment_golive_check
GROUP BY patient_alive, clinic_open, provider_free, authorized
ORDER BY active_appointments;

-- 5b. The visits that are not safe yet, one by one
SELECT * FROM rpt.v_appointment_golive_check
WHERE patient_alive <> 'ok' OR clinic_open <> 'ok' OR provider_free <> 'ok'
   OR authorized NOT IN ('ok', 'n/a', 'pending (visit pending too)')
ORDER BY start_utc;

-- 5c. Rules triggered
SELECT rule_id, action, COUNT(*) AS rows_affected
FROM etl.appointment_issue_log
GROUP BY rule_id, action
ORDER BY rule_id, action;
