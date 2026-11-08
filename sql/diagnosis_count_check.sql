-- diagnosis_count_check.sql
-- Number of different diagnoses per patient, with the list of diagnoses.
-- Reads the staging tables (stg), the raw legacy data.

SELECT
    d.FullName,
    COUNT(*) AS DiagnosisCount,
    STRING_AGG(d.DESCRIPTION, '; ') AS Diagnoses
FROM (
    SELECT DISTINCT
        p.id,
        p.FIRST + ' ' + p.LAST AS FullName,
        c.DESCRIPTION
    FROM [stg].[patients] p
    INNER JOIN [stg].[conditions] c
        ON p.id = c.PATIENT
) d
GROUP BY d.id, d.FullName
ORDER BY DiagnosisCount DESC;