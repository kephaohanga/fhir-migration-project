# Legacy EHR to FHIR R4 Migration and Reconciliation Pipeline

An end-to-end healthcare data migration project: extract records from a simulated legacy EHR, validate and clean them, convert them to **HL7 FHIR R4**, load them to a FHIR server over a **REST API**, read everything back, **prove with reconciliation that nothing was lost or changed**, and then **work every rejected record to a documented outcome**.

**Result, Phase 1:** 5,329 source rows, 100% accounted for · **5,327 migrated** (the other two: 1 duplicate removed, 1 record archived with a documented reason) · **44,765 of 44,765 field values matched** · 9 of 9 planted data defects caught, then corrected or archived.

**Result, Phase 2 (Practitioner):** 185 legacy provider rows, 100% accounted for · **184 migrated** (1 duplicate removed) · **2,208 of 2,208 field values matched** · 7 of 7 planted defects caught · **0 visits waiting** for a provider.

**Result, Phase 2 (clinic structure):** 1 Organization, 1 Location and **184 PractitionerRoles** (one per migrated provider) · **1,126 of 1,126 field values matched** · every role linked to its own practitioner and to the clinic · 184 of 184 specialties coded to the NUCC provider taxonomy.

> **Scope:** **Phase 1** (Patient, Encounter, Condition) is complete. **Phase 2** has started: Practitioner and the clinic structure (Organization, Location, PractitionerRole) are complete; linking each visit to its provider and location is next. Later phases are planned below in [Scope and phases](#scope-and-phases).

![Reconciliation dashboard after remediation](docs/dashboard.png)

> **All data is synthetic.** Patients were generated with [Synthea](https://github.com/synthetichealth/synthea), and no real patient information (PHI) was used at any point. The legacy files in `data/` include SSN, driver's license and passport columns because Synthea generates them; their values are fake, and the pipeline deliberately drops these columns at ingestion (see *Data minimization* below).

---

## The problem

When a healthcare organization (a physician practice, a multi-site clinic, a hospital or an entire health system) replaces its electronic health record (EHR) system, merges with another organization that uses a different one, or joins a larger health system's shared EHR, years of patient history must be converted from the old system into the new one. The hard part is not moving the data. It is **proving** that every record arrived complete and correct, and explaining every record that did not.

This project simulates that conversion and answers five questions a migration team must answer before go-live:

1. Did every source row end up somewhere: migrated, rejected for a documented reason, or removed as a duplicate?
2. Were bad records caught before they reached the new system?
3. Does the target system hold exactly what was sent, field by field?
4. What happened to every rejected record: was it corrected and migrated, or archived with a reason?
5. Can the results be audited later?

---

## Architecture

```mermaid
flowchart LR
    A["Synthea<br/>synthetic legacy CSVs"] --> B["plant_errors.py<br/>9 known defects"]
    B --> C[("SQL Server<br/>stg schema<br/>(raw, never edited)")]
    C --> R["apply_corrections.py"]
    W["remediation/<br/>corrections.csv"] --> R
    R --> CU[("cur schema<br/>stg + approved<br/>corrections")]
    CU --> D["transform.py<br/>validate + map"]
    D -->|exceptions| E[("etl.issue_log")]
    E --> X["export_worklist.py<br/>prioritized worklist"]
    X -.->|"staff fix or archive"| W
    D --> F["FHIR transaction<br/>bundles (JSON)"]
    F -->|"REST POST"| G["FHIR R4 server<br/>(HAPI)"]
    G -->|"crosswalk"| H[("etl.fhir_crosswalk<br/>etl.load_log")]
    G -->|"REST GET, paginated"| I["extract_fhir.py"]
    I --> J[("tgt schema")]
    CU --> K[("rpt reconciliation<br/>views")]
    J --> K
    K --> L["Power BI<br/>reconciliation report"]
```

**Database schemas (SQL Server `FhirMigration`)**

| Schema | Holds | Why it exists |
| --- | --- | --- |
| `stg` | Raw legacy rows exactly as received, plus `source_row` and `loaded_at` audit columns | A faithful, never-edited copy of the source |
| `cur` | Curated copy: `stg` plus approved corrections | Fixes are applied here, so the raw data stays intact |
| `etl` | Issue log, corrections audit trail, load log, legacy-ID-to-FHIR-ID crosswalk | Record of every decision, every fix and every server response |
| `tgt` | Records read back from the FHIR server, flattened to rows | What the target system actually stored |
| `rpt` | Reconciliation, remediation and reporting views | Row-level and field-level proof, the staff worklist and the dashboard's data |

---

## Pipeline steps

| Step | Script / file | What it does |
| --- | --- | --- |
| 1 | `requirements.txt` | Python environment setup |
| 2 | `src/first_call.py` | First REST API call to the FHIR server (connectivity check) |
| 3 | Synthea | Generates the synthetic legacy EHR export (seed 12345, North Carolina) |
| 4 | `src/plant_errors.py` | Plants 9 known defects and writes an answer key to `docs/` |
| 5 | `src/load_staging.py` | Loads the CSVs into SQL Server staging and drops SSN, driver's license and passport columns |
| 6 | `docs/source_to_target_mapping.xlsx` | Mapping spec: 59 field mappings, 19 validation rules, 7 design decisions |
| 7 | `src/apply_corrections.py` | Builds the curated layer (`cur`) from raw staging plus approved corrections |
| 8 | `src/transform.py` | Applies reject / fix / flag rules, builds one FHIR transaction bundle per patient |
| 9 | `src/load_fhir.py` | POSTs bundles to the server with retries; records the ID crosswalk and load log |
| 10 | `src/extract_fhir.py` | Reads every resource back by project tag, following pagination links |
| 11 | `sql/10_reconciliation.sql` | Row-level and field-level reconciliation views and reports |
| 12 | `sql/11_dashboard_views.sql`, `powerbi/` | Dashboard views and the Power BI reconciliation report |
| 13 | `src/export_worklist.py`, `sql/12_remediation.sql` | One remediation worklist for staff (Phase 1 and Phase 2) and the log of every correction's outcome |
| 14 | `src/plant_provider_errors.py` | Phase 2: plants 7 provider defects (misspelt and wrong names, invalid gender and NPI, duplicate) |
| 15 | `src/phase2_practitioner.py` | Phase 2: stages, applies staff corrections (curated layer), validates against the credentialing roster, loads (conditional update) and reads back Practitioners |
| 16 | `sql/20_practitioner_reconciliation.sql` | Phase 2: row, field and visit-coverage reconciliation |
| 17 | `src/phase2_clinic_structure.py`, `docs/specialty_map.csv` | Phase 2: loads the clinic Organization and Location, then one PractitionerRole per provider with the specialty mapped to the NUCC taxonomy |
| 18 | `sql/21_clinic_structure_reconciliation.sql` | Phase 2: role row reconciliation, field comparison, link integrity and specialty-coding coverage |

---

## Results

### Row reconciliation: first pass vs. after remediation

`source rows = migrated + rejected (direct) + rejected (cascade) + duplicates removed`

| Table | Source | Migrated, first pass | Migrated, after remediation | Not migrated, and why |
| --- | ---: | ---: | ---: | --- |
| Patients | 56 | 52 | **55** | 1 duplicate removed |
| Encounters | 3,564 | 3,436 | **3,563** | 1 archived: patient cannot be identified |
| Conditions | 1,709 | 1,639 | **1,709** | none |
| **Total** | **5,329** | **5,127** | **5,327** | **2, both documented** |

On the first pass, 5 records were rejected directly, and they blocked **196 others** as cascade rejects. A *cascade* reject is a child record whose parent was rejected. For example, an encounter for a patient with no valid birth date cannot be loaded without its patient.

### Remediation

Instead of treating rejection as the end state, every rejected record was worked to a documented outcome:

| # | Problem | Records it blocked | Owner | Outcome |
| ---: | --- | ---: | --- | --- |
| 1 | Patient birth date blank | 84 | Registration | Corrected, migrated |
| 2 | Patient gender code invalid (`U`) | 63 | Registration | Corrected, migrated |
| 3 | Patient birth date blank | 49 | Registration | Corrected, migrated |
| 4 | Encounter points to a placeholder patient ID | 0 | HIM | Archived, not migrated |
| 5 | Diagnosis code blank | 0 | Coding | Corrected, migrated |

The worklist ranks problems by how many records each one blocks, so the **5 root-cause fixes released all 196 blocked records**. Every correction is recorded in `etl.corrections` with the original value, the new value, who made it, when and why.

### Field reconciliation after remediation

| Resource | Fields compared | Values compared | Mismatches | Match |
| --- | ---: | ---: | ---: | ---: |
| Patient | 16 | 880 | 0 | 100.0% |
| Encounter | 8 | 28,504 | 0 | 100.0% |
| Condition | 9 | 15,381 | 0 | 100.0% |
| **Total** | | **44,765** | **0** | **100.0%** |

**Load:** every bundle accepted, 0 failures. On the re-run, conditional create (`ifNoneExist`) recognized the records already loaded and created only the newly corrected ones, with no duplicates.

<details>
<summary>Dashboard before remediation (first pass)</summary>

![Reconciliation dashboard before remediation](docs/dashboard_before_remediation.png)

</details>

### Phase 2, step 1: Practitioner (providers)

Provider names were checked against a credentialing roster, the trusted source for provider data. Seven realistic defects were planted in the legacy provider export. Practitioner then went through the same remediation workflow as Phase 1, using the same worklist, the same `corrections.csv` and the same audit trail.

| Check | First pass | After remediation |
|---|---|---|
| Row reconciliation | 185 = 183 migrated + 1 rejected + 1 duplicate removed: **PASS** | 185 = **184 migrated + 0 rejected** + 1 duplicate removed: **PASS** |
| Planted defects | **7 of 7 caught** | **7 of 7 caught** (3 fixed automatically, 3 corrected by staff, 1 duplicate removed) |
| Field reconciliation | 2,196 of 2,196 values matched | **2,208 of 2,208** values matched (12 fields, including NPI) |
| Visit coverage | 2 visits waiting for the rejected provider | **0 visits waiting**: every provider used by a migrated visit is on the server |

**Two kinds of fixes.** Small, high-confidence errors are fixed automatically; uncertain ones go to a person:

| Problem | How it was found | Who fixed it | Outcome |
|---|---|---|---|
| 3 misspelt names (letters swapped, missing, doubled) | Similarity to the roster name 0.95–0.98, above the 0.85 threshold | Pipeline, automatically (logged as R-PR02 Fix) | Roster spelling used, migrated |
| 1 name belonging to a different provider | Similarity 0.14: possibly the wrong person, so the pipeline refused to guess | Credentialing (staff correction) | Name confirmed, migrated; its 2 visits now link to it |
| 1 invalid gender code (`X`) | Not M or F | Credentialing (staff correction) | Corrected from the credentialing file, migrated |
| 1 invalid NPI (9 digits) | Not 10 digits | Credentialing (staff correction) | Corrected from the credentialing file, migrated |
| 1 duplicate row | Same provider ID twice | Pipeline, automatically (R-PR04 Fix) | First copy kept |

The NPI is stored as the provider's official identifier (`http://hl7.org/fhir/sid/us-npi`). Loads use **conditional update**: the re-run after remediation created exactly 1 practitioner (the released one) and updated the other 183 in place, with no duplicates.

### Phase 2, step 2: Clinic structure (Organization, Location, PractitionerRole)

**Practitioner** says *who* a provider is (name, NPI). **PractitionerRole** says *where and as what* they work: this clinic, this location, this specialty. Keeping the two apart means a provider who works in more than one place is still one person with several roles, never a duplicate record.

| Check | Result |
|---|---|
| Row reconciliation | 184 distinct providers = **184 roles** + 0 rejected; roles = migrated practitioners; 1 Organization; 1 Location: **PASS** |
| Field reconciliation | Organization 10 of 10, Location 12 of 12, PractitionerRole 1,104 of 1,104: **1,126 of 1,126** values matched |
| Link integrity | Every role points to the practitioner migrated **from the same legacy provider** (not just any practitioner), and to the clinic's Organization and Location: **184 of 184 OK** for each link |
| Specialty coding | Legacy free text mapped to the **NUCC provider taxonomy** through `docs/specialty_map.csv`; unmapped values would load as text and be flagged. Profiling showed all 184 providers are `GENERAL PRACTICE` → `208D00000X`: **184 of 184 coded**, 0 flags |
| Legacy organizations | 184 legacy organization values documented as **not migrated** (one-clinic decision, below) |

The clinic's name, address and phone number are **fictional** reference data defined in the script, since all data in this project is synthetic.

---
## Scope and phases

Real EHR conversions are usually done in phases: the core records are moved and proven first, then other record types are added on top of them. This project completes **Phase 1** and proves it fully before going further. **Phase 2** has started: Practitioner (complete and remediated) and the clinic structure are done.

| Phase | FHIR resources | Source data | Status |
|---|---|---|---|
| **1** | Patient, Encounter, Condition | Synthea `patients.csv`, `encounters.csv`, `conditions.csv` | **Done**: 100% of rows accounted for, 44,765 of 44,765 values matched |
| **2** | **Clinic structure:** Practitioner, PractitionerRole, Organization, Location<br>**Insurance:** Coverage<br>**Clinical:** AllergyIntolerance, Observation, Procedure, MedicationRequest | Synthea `providers.csv`, `organizations.csv`, `payers.csv`, `payer_transitions.csv`, `allergies.csv`, `observations.csv`, `procedures.csv`, `medications.csv` | **In progress:** Practitioner done (185 rows → 184 migrated, 1 duplicate removed; 2,208 of 2,208 values matched). Clinic structure done (1 Organization, 1 Location, 184 PractitionerRoles; 1,126 of 1,126 values matched). Visit links next |
| **3** | **Scheduling:** Appointment<br>**Referrals and orders:** ServiceRequest<br>**Prior authorization:** Claim (use = preauthorization), ClaimResponse<br>**Documents and results:** DocumentReference, DiagnosticReport<br>**Medications:** MedicationAdministration, Medication (as a separate resource) | Not in Synthea's CSV export; would need synthetic data or the source EHR's scheduling, referral, authorization, document, lab and nursing data | Planned |

**Why Phase 1 comes first:** every other resource points back to a patient, and most point to a visit. They cannot be loaded until those exist. This is the same dependency that causes cascade rejects in this project.

**Why Phase 2 adds clinic structure and insurance:** scheduling and billing depend on them. Appointments need the provider, department and location they belong to (Practitioner, PractitionerRole, Organization, Location). Pre-registration and clean claims need the patient's insurance (Coverage).

**Why Phase 3 adds appointments, referrals and authorizations:** these are the "open work" a clinic must carry across on go-live day: future appointments, referrals still in progress, and prior authorizations already approved or pending. A missing prior authorization is a leading cause of claim denials, so authorizations must not be lost in the switch.

**How later phases will work:** each new resource follows the same pattern as Phase 1: map it, validate it, load it with conditional create or update, read it back, reconcile it and work every reject to a documented outcome. Practitioner came first because each encounter's `PROVIDER` field already points to a provider. The clinic structure followed, modeled as **one clinic** (see *Design decisions*): one Organization, one Location, and one PractitionerRole per migrated provider. Next, the Phase 1 encounters are updated in place so each visit points to its practitioner (`Encounter.participant`) and to the clinic location (`Encounter.location`), with a reconciliation proving every visit links to the right provider.

### Billing and balances

FHIR has separate financial resources (Account, ChargeItem, Claim, ClaimResponse, ExplanationOfBenefit, PaymentReconciliation, Invoice). In a real conversion, however, open charges and balances are usually **not** migrated. Claims from before go-live are worked to zero in the old billing system (**A/R runout**), and new charges start in the new system. This avoids double billing and broken payment histories.

Planned for this project:

- **A/R runout report:** legacy open balances by payer and by age (0–30, 31–60, 61–90, 90+ days), tracked until they reach zero.
- **Optional billing history:** load Synthea's `claims.csv` and `claims_transactions.csv` as read-only **ExplanationOfBenefit** resources, so past billing is visible. This is history, not open balances.
- **Closed claims only:** if billing history is loaded, only claims that are **closed** in the legacy system move: paid, adjusted to a zero balance, or final-denied and written off (for example, after the timely filing limit). A claim that is still open, such as one denied for a credentialing issue while the appeal or rebilling window is still open, stays in the legacy system until it closes. A reconciliation check confirms that no open balance was migrated.
- **A migration reject is not a payer denial:** a record rejected by this pipeline (for example, a provider whose name does not match the credentialing roster) has a **data** problem that is fixed or archived. It does not change whether any claim is paid.

### Next: converting into a system that already has data

Phase 1 loads into an **empty** FHIR server. Many real conversions are different: a practice joining a larger health system's shared EHR (for example, an Epic Community Connect arrangement) moves into a system that **already holds** many of the same patients and providers. The next steps make the project reflect that. They are planned work, not yet built.

| Step | What changes | Why it matters |
|---|---|---|
| **1. Patient matching** | Pre-load "existing" patients on the server, some of them the same people as legacy patients with realistic differences (nickname, typo, new address). Before loading, match each legacy patient on name, birth date, sex, address and phone: **confident match** → link to the existing record; **possible match** → send to an HIM review worklist; **no match** → create a new patient. | Linking to the existing record prevents duplicate charts. Sending uncertain matches to a person prevents **overlays** (two different people merged into one chart). |
| **2. Provider matching by NPI** | Pre-load "existing" providers on the server, then match each legacy provider by NPI (already migrated as the official identifier) and link to the existing record instead of creating a new one, using a crosswalk from legacy provider ID to NPI to target provider. | Every encounter and appointment points to a provider. A missing or wrong provider breaks scheduling and billing. |
| **3. Appointment conversion** | Add synthetic future appointments (FHIR Appointment) with a **visit-type mapping table** (legacy type → new type) and a report comparing appointment counts per provider per day, old vs. new. | Schedulers need to check converted schedules before go-live. A count report shows them exactly where to look. |
| **4. Mock conversion tracking** | Add a `run_id` to the load and issue logs, and a view comparing runs (for example, Mock 1: 9 issues → Mock 2: 4 → Mock 3: 0). | Real conversions are rehearsed several times. Tracking each run shows the data getting cleaner before go-live. |
| **5. Conversion cut-off rule** | Convert active and recent data (for example, the last 3 years), and report older history as **kept in the legacy system**, not missing. | When the old systems stay available for historical data, not everything needs to move. The reconciliation must still account for every row. |

**Reconciliation stays the same idea:** for patients, *linked + created + under review = all legacy patients*, with no unexplained rows.

---

## How to run it

### Prerequisites

- Windows with Python 3.12, SQL Server (Express or Developer) and SSMS
- Microsoft ODBC Driver 17 for SQL Server
- Java 17 or later (for Synthea)
- Power BI Desktop (to open the report)

### 1. Set up Python

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2. Check the FHIR server connection

```powershell
python src\first_call.py
```

### 3. Generate the legacy data with Synthea

```powershell
java -jar synthea-with-dependencies.jar -s 12345 -p 50 --exporter.csv.export true "North Carolina"
```

Copy `patients.csv`, `encounters.csv`, `conditions.csv` and `providers.csv` from Synthea's `output\csv` folder into `data\legacy\`. The clean `providers.csv` also serves as the credentialing roster in Phase 2.

### 4–10. Run the pipeline

```powershell
python src\plant_errors.py        # plant 9 known defects
python src\load_staging.py        # load SQL Server staging (stg)
python src\apply_corrections.py   # build the curated layer (cur)
python src\transform.py           # validate, map, build FHIR bundles
python src\load_fhir.py --limit 1 # test with one patient first
python src\load_fhir.py           # load all patients
python src\extract_fhir.py        # read everything back
```

Each script prints its own reconciliation check and a PASS / FAIL result.

### 11–12. Reconcile and report

1. In SSMS, open and run `sql\10_reconciliation.sql`, then `sql\11_dashboard_views.sql`.
2. Open `powerbi\fhir_migration_reconciliation.pbix` in Power BI Desktop and click **Refresh**.

### 13. Remediate rejected records

```powershell
python src\export_worklist.py     # writes remediation\worklist.csv, highest impact first
```

1. Copy the **Open** rows of the worklist into `remediation\corrections.csv`, **below any rows already there**, and fill in the last five columns (`corrected_value`, `disposition` = Correct or Archive, `reason`, `corrected_by`, `corrected_date`). Take every value from the source of truth (registration record, clinical record or credentialing file). Never replace the file with the worklist: earlier decisions live in it.
2. Re-run `apply_corrections.py`, `transform.py`, `load_fhir.py` and `extract_fhir.py`, then the SQL files in step 11. For providers, re-run `phase2_practitioner.py` (it applies the corrections itself).
3. Run `export_worklist.py` again: each correction shows **Resolved - migrated** or **Archived - not migrated**.

> Close `worklist.csv` and `corrections.csv` in Excel before running the scripts: Windows locks a file that Excel has open.

### 14–16. Phase 2: Practitioner

```powershell
python src\plant_provider_errors.py               # plant 7 provider defects + answer key
python src\phase2_practitioner.py --limit 1       # test with one bundle first
python src\phase2_practitioner.py                 # stage, curate, validate, load, read back
```

Then open `sql\20_practitioner_reconciliation.sql` in SSMS and press **F5**, and run `export_worklist.py` to see provider problems on the shared worklist.

### 17–18. Phase 2: Clinic structure

```powershell
python src\phase2_clinic_structure.py --limit 1   # clinic + one bundle of roles (a test)
python src\phase2_clinic_structure.py             # clinic + all roles, then read back
```

Then open `sql\21_clinic_structure_reconciliation.sql` in SSMS and press **F5**. Run this step after Practitioner: every role points to a migrated practitioner.

> **Note:** the pipeline uses the public HAPI FHIR test server (`https://hapi.fhir.org/baseR4`). It is shared and reset from time to time, so FHIR IDs will differ on every run. Only load synthetic data to a public server.

---

## Repository structure

```
fhir-migration-project/
├── data/
│   ├── legacy/              Synthea export (clean); providers.csv is also the credentialing roster
│   └── legacy_dirty/        Same data with planted defects (9 in Phase 1, 7 in providers.csv)
├── docs/
│   ├── dashboard.png
│   ├── dashboard_before_remediation.png
│   ├── planted_errors_answer_key.csv
│   ├── planted_provider_errors_answer_key.csv
│   ├── source_to_target_mapping.xlsx
│   └── specialty_map.csv    Legacy specialty text -> NUCC provider taxonomy code
├── powerbi/
│   ├── fhir_migration_reconciliation.pbix
│   └── fhir_migration_reconciliation_before_remediation.pbix
├── remediation/
│   ├── worklist.csv         Generated: problems ranked by impact, with owner
│   └── corrections.csv      Staff decisions: Correct or Archive, with reason
├── sql/
│   ├── 10_reconciliation.sql
│   ├── 11_dashboard_views.sql
│   ├── 12_remediation.sql
│   ├── 20_practitioner_reconciliation.sql
│   ├── 21_clinic_structure_reconciliation.sql
│   └── diagnosis_count_check.sql   Data profiling: distinct diagnoses per patient, with the list (raw stg data)
├── src/
│   ├── first_call.py
│   ├── plant_errors.py
│   ├── load_staging.py
│   ├── apply_corrections.py
│   ├── transform.py
│   ├── load_fhir.py
│   ├── extract_fhir.py
│   ├── export_worklist.py
│   ├── plant_provider_errors.py
│   ├── phase2_practitioner.py
│   └── phase2_clinic_structure.py
├── requirements.txt
└── README.md
```

`output/` (bundles, logs, crosswalk) is created when the pipeline runs and is not stored in the repository.

---

## Design decisions worth noting

- **Stage everything as text first.** Staging columns are `NVARCHAR` so no source value is lost or silently converted before it is validated.
- **Reject, fix or flag.** Each validation rule has one documented action. Unfixable records are rejected with a rule ID, safe corrections are made and logged, and minor gaps are flagged but loaded.
- **Never edit the source.** Corrections are applied to a separate curated layer, so the raw legacy data stays intact and every change can be traced back to its original value.
- **Fix root causes, ranked by impact.** The worklist shows each direct reject once, with the number of cascade records it blocks, so staff fix the 5 problems that matter instead of 201 symptoms.
- **Every reject gets a disposition.** A record is either corrected and migrated, or archived with a reason. Nothing is silently left behind.
- **Automatic fix or human review, decided by confidence.** A provider name within 0.85 similarity of the credentialing roster is treated as a typo and fixed automatically; anything less alike could be a different person, so it is rejected and routed to Credentialing rather than guessed.
- **One worklist, one audit trail.** Provider problems join the same worklist and `etl.corrections` log as Phase 1, so staff work from a single list for the whole migration.
- **Idempotent loads.** Conditional create (Phase 1) and conditional update (Practitioner) let the load be rerun safely after a failure or after corrections, without duplicates.
- **One clinic.** Synthea assigns each provider to their own organization (184 providers, 184 organizations, at most 1 provider per organization, verified in SQL). These values are simulator artifacts, so all providers are mapped to a single clinic Organization and Location. Legacy organization IDs are kept in `stg`/`cur` for audit only.
- **Profile before mapping.** Specialty values were profiled in SQL before the code mapping was finalized (all 184 providers are `GENERAL PRACTICE`). The mapping lives in a reviewable table (`docs/specialty_map.csv`), not in code, and any value missing from it is loaded as text and flagged rather than dropped.
- **Prove links, not just counts.** The clinic-structure reconciliation checks that each role points to the practitioner migrated from the *same* legacy provider, so a role attached to the wrong person would fail even if every count matched.
- **Exact comparison.** Field reconciliation uses a binary collation and a NULL-safe comparison, and compares against the corrected values, so differences in case, whitespace or unapproved changes are not hidden.
- **Data minimization (HIPAA).** SSN, driver's license and passport numbers are dropped at ingestion because the target does not need them.
- **Auditability.** Every run records when the pipeline ran and when the report was refreshed, so each PASS is tied to a specific run.

---

## Skills demonstrated

| Area | Where |
| --- | --- |
| ETL pipeline design (Python, SQL Server) | `src/`, staging, curated and target schemas |
| REST API integration, FHIR R4 | `load_fhir.py`, `extract_fhir.py` |
| Source-to-target mapping, legacy data conversion | `docs/source_to_target_mapping.xlsx` |
| Data-quality rules, exception reporting | `transform.py`, `phase2_practitioner.py`, `etl.issue_log` |
| Validation against a reference source (fuzzy name matching) | `phase2_practitioner.py` (credentialing roster, similarity threshold) |
| Code mapping to a national terminology (NUCC provider taxonomy) | `phase2_clinic_structure.py`, `docs/specialty_map.csv` |
| Referential integrity across FHIR resources | `sql/21_clinic_structure_reconciliation.sql` (role → practitioner, organization, location) |
| Exception remediation workflow, audit trail | `export_worklist.py`, `apply_corrections.py`, `etl.corrections` |
| Reconciliation and validation in T-SQL | `sql/10_reconciliation.sql`, `sql/12_remediation.sql`, `sql/20_practitioner_reconciliation.sql`, `sql/21_clinic_structure_reconciliation.sql` |
| Power BI (DAX, data modeling, reporting) | `powerbi/` |
| HIPAA-aware design, audit logging | Data minimization, corrections log, load log, run timestamps |

---

**Author:** Kepha Ohanga · [LinkedIn](https://www.linkedin.com/in/kepha-ohanga-102b2141b)
