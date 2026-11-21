r"""
phase3_diagnostic_report.py
---------------------------
Phase 3, step 5: build lab reports - one FHIR DiagnosticReport per lab panel.

A DiagnosticReport is what a lab sends back: "Lipid panel, 2024-03-02, results: cholesterol,
HDL, LDL, triglycerides". Its results are the Observations migrated in Phase 2; the report
points to them (result = Observation references) and does not copy their values.

This is a DERIVED resource: it has no legacy file of its own. It is built from the lab
results (CATEGORY laboratory) that Phase 2 migrated. So there is no plant step: the test
cases are the real findings of the profile.

How results become reports:
  1. Lab results with the same visit and the same date-time form one GROUP (one blood draw).
  2. Each test is assigned to its panel with docs/lab_panel_map.csv (test LOINC -> panel LOINC).
     A test in no panel gets its OWN single-test report (decision 2A), e.g. an HbA1c.
  3. Panels ordered together are combined with docs/lab_panel_combine.csv - e.g. a basic
     metabolic panel + a hepatic function panel = ONE comprehensive metabolic panel (24323-8),
     not two (reporting them separately would be unbundling, as in billing).
  4. One DiagnosticReport per group and panel. Every migrated lab result ends up in exactly
     one report, or is flagged.

Mapping (one DiagnosticReport per visit + date-time + panel):
  visit + date-time + panel -> identifier
  (always)                  -> status final, category LAB (HL7 v2-0074)
  panel LOINC code          -> code      (the test's own code for a single-test report)
  the results' patient      -> subject   Patient
  the results' visit        -> encounter Encounter
  DATE                      -> effectiveDateTime
  the group's Observations  -> result    Observation references (from Phase 2's crosswalk)

Rules:
  R-DR01 Flag  a measurement labelled "laboratory" that is not a lab test (the map says NOT-LAB:
               a diastolic blood pressure is a vital sign, an ejection fraction comes from
               imaging) - left out of the lab reports
  R-DR02 Fix   panels ordered together combined into one (docs/lab_panel_combine.csv)
  R-DR03 Flag  a lab result whose code is not a LOINC code - left out (a report code must be LOINC)

Run AFTER Phase 2's phase2_observation.py. From the project folder:
  python src\phase3_diagnostic_report.py --limit 1   <- ONE bundle of 50 (a test)
  python src\phase3_diagnostic_report.py
Then open sql\32_diagnostic_report_reconciliation.sql in SSMS and press F5.
"""

import argparse
import csv
import re
import time
from collections import Counter, defaultdict
from pathlib import Path

from phase2_clinic_structure import identifier, parse_reply, put_entry, search_all, send_bundle
from phase2_practitioner import (BUNDLE_SIZE, FHIR_SERVER, PAUSE_BETWEEN_BUNDLES, PROJECT_TAG,
                                 save_table, write_csv)
from phase2_procedure import long_rows, read_table

PANEL_MAP = Path("docs/lab_panel_map.csv")
PANEL_COMBINE = Path("docs/lab_panel_combine.csv")
OUTPUT_DIR = Path("output/phase3_diagnostic_report")

SYS_REPORT = "urn:fhirmigration:lab-report-id"
SYS_LOINC = "http://loinc.org"
SYS_V2_0074 = "http://terminology.hl7.org/CodeSystem/v2-0074"
LOINC_SHAPE = re.compile(r"^\d{1,7}-\d$")
NOT_LAB = "NOT-LAB"

ISSUE_COLS = ["record_key", "rule_id", "action", "detail"]
MEMBER_COLS = ["report_key", "observation_key", "observation_fhir_id", "test_code"]
SOURCE_COLS = ["report_key", "panel_code", "legacy_patient", "legacy_encounter", "effective", "members", "kind"]
COVERAGE_COLS = ["observation_key", "disposition"]
LONG_EXPECTED = ["resource_type", "legacy_id", "field_name", "expected_value"]
LONG_ACTUAL = ["resource_type", "fhir_id", "legacy_id", "field_name", "actual_value"]
RESULT_COLS = ["report_fhir_id", "report_key", "result_ref"]
XWALK_COLS = ["resource_type", "legacy_id", "fhir_id", "server_status"]


def flatten_report(r):
    category = next((c for cat in r.get("category", []) for c in cat.get("coding", [])), {})
    code = next(iter((r.get("code") or {}).get("coding", [])), {})
    return {
        "status": r.get("status"),
        "category": category.get("code"),
        "code": code.get("code"),
        "code_display": code.get("display"),
        "subject_ref": (r.get("subject") or {}).get("reference"),
        "encounter_ref": (r.get("encounter") or {}).get("reference"),
        "effective": r.get("effectiveDateTime"),
        "result_count": str(len(r.get("result", []))),
    }


def read_maps():
    with PANEL_MAP.open(encoding="utf-8-sig", newline="") as f:
        panel_of = {r["test_code"].strip(): (r["panel_code"].strip(), r["panel_display"].strip())
                    for r in csv.DictReader(f)}
    with PANEL_COMBINE.open(encoding="utf-8-sig", newline="") as f:
        combine = [(r["panel_a"].strip(), r["panel_b"].strip(), r["combined_panel"].strip(),
                    r["combined_display"].strip()) for r in csv.DictReader(f)]
    return panel_of, combine


# ---------------------------------------------------------------------------
# BUILD the reports
# ---------------------------------------------------------------------------
def build(rows, ctx, expected):
    """rows: the migrated lab results (one per Observation). ctx: patients, encounters (legacy -> FHIR),
    panel_of, combine."""
    issues, coverage, members, sources, reports = [], [], [], [], []
    groups = defaultdict(list)
    for r in rows:
        groups[(r["ENCOUNTER"], r["DATE"])].append(r)

    combined_count = Counter()
    for (encounter, when), results in sorted(groups.items()):
        panels = defaultdict(list)                     # panel code -> results
        display = {}
        for r in results:
            code = (r["CODE"] or "").strip()
            panel, name = ctx["panel_of"].get(code, (code, r["DESCRIPTION"]))      # 2A: own report
            if panel == NOT_LAB:                                                      # R-DR01
                issues.append({"record_key": r["observation_key"], "rule_id": "R-DR01", "action": "Flag",
                               "detail": f"{code} {r['DESCRIPTION']} is labelled laboratory but is not a lab test "
                                         f"- left out of the lab reports"})
                coverage.append({"observation_key": r["observation_key"], "disposition": "Not a lab test (flagged)"})
                continue
            if not LOINC_SHAPE.match(code):                                           # R-DR03
                issues.append({"record_key": r["observation_key"], "rule_id": "R-DR03", "action": "Flag",
                               "detail": f"Lab result code '{code}' is not a LOINC code - left out"})
                coverage.append({"observation_key": r["observation_key"], "disposition": "Not LOINC (flagged)"})
                continue
            panels[panel].append(r)
            display[panel] = name

        changed = True                                                                # R-DR02
        while changed:
            changed = False
            for a, b, merged, merged_name in ctx["combine"]:
                if a in panels and b in panels and a != b:
                    joined = panels.pop(a) + panels.pop(b)
                    panels[merged] = panels.get(merged, []) + joined
                    display[merged] = merged_name
                    combined_count[f"{a} + {b} -> {merged}"] += 1
                    changed = True

        patient = results[0]["PATIENT"]
        stamp = re.sub(r"[^0-9]", "", when)
        for panel, panel_results in sorted(panels.items()):
            key = f"{encounter}_{stamp}_{panel}"
            in_map = panel in {p for p, _ in ctx["panel_of"].values()} or \
                     panel in {c[2] for c in ctx["combine"]}
            kind = "panel" if in_map else "single test"
            panel_results.sort(key=lambda r: r["observation_key"])
            report = {
                "resourceType": "DiagnosticReport",
                "meta": {"tag": [PROJECT_TAG]},
                "identifier": [{"system": SYS_REPORT, "value": key}],
                "status": "final",
                "category": [{"coding": [{"system": SYS_V2_0074, "code": "LAB", "display": "Laboratory"}]}],
                "code": {"coding": [{"system": SYS_LOINC, "code": panel, "display": display[panel]}],
                         "text": display[panel]},
                "subject": {"reference": f"Patient/{ctx['patients'][patient]}"},
                "encounter": {"reference": f"Encounter/{ctx['encounters'][encounter]}"},
                "effectiveDateTime": when,
                "result": [{"reference": f"Observation/{r['fhir_id']}"} for r in panel_results],
            }
            reports.append((key, report))
            sources.append({"report_key": key, "panel_code": panel, "legacy_patient": patient,
                            "legacy_encounter": encounter, "effective": when,
                            "members": len(panel_results), "kind": kind})
            for r in panel_results:
                members.append({"report_key": key, "observation_key": r["observation_key"],
                                "observation_fhir_id": r["fhir_id"], "test_code": r["CODE"]})
                coverage.append({"observation_key": r["observation_key"], "disposition": "In a report"})
            expected += long_rows("DiagnosticReport", key, flatten_report(report), "expected_value")
    for combo, n in combined_count.items():
        issues.append({"record_key": combo, "rule_id": "R-DR02", "action": "Fix",
                       "detail": f"{n} groups: panels ordered together combined into one report ({combo})"})

    kinds = Counter(s["kind"] for s in sources)
    print("\n2. BUILD   one DiagnosticReport per visit + time + panel")
    print(f"   lab results {len(rows)} in {len(groups)} groups -> {len(reports)} reports "
          f"({kinds['panel']} panels, {kinds['single test']} single-test reports)")
    in_report = sum(1 for c in coverage if c["disposition"] == "In a report")
    ok = len(coverage) == len(rows) and len({c["observation_key"] for c in coverage}) == len(rows)
    print(f"   every lab result placed exactly once: {in_report} in a report + "
          f"{len(coverage) - in_report} flagged = {len(coverage)} of {len(rows)}   {'PASS' if ok else 'FAIL'}")
    for rule in sorted({i["rule_id"] + " " + i["action"] for i in issues}):
        print(f"   {rule}: {sum(1 for i in issues if i['rule_id'] + ' ' + i['action'] == rule)}")
    for combo, n in combined_count.most_common():
        print(f"     combined {combo}: {n}")
    top = Counter(s["panel_code"] for s in sources if s["kind"] == "panel")
    print("   panels: " + ", ".join(f"{p} x{n}" for p, n in top.most_common()))
    return reports, issues, coverage, members, sources


# ---------------------------------------------------------------------------
# LOAD and EXTRACT
# ---------------------------------------------------------------------------
def load(reports, load_log, xwalk, limit=None):
    chunks = [reports[i:i + BUNDLE_SIZE] for i in range(0, len(reports), BUNDLE_SIZE)]
    if limit:
        chunks = chunks[:limit]
    print(f"\n3. LOAD   {len(chunks)} bundle(s) of DiagnosticReport to {FHIR_SERVER}")
    sent = 0
    for n, chunk in enumerate(chunks, 1):
        entries = [put_entry(rep, f"DiagnosticReport?identifier={SYS_REPORT}|{key}") for key, rep in chunk]
        sent += len(entries)
        reply = send_bundle(entries, n, "DiagnosticReport", load_log)
        if reply is not None:
            for (key, _), r in zip(chunk, reply):
                fhir_id, status = parse_reply(r, "DiagnosticReport")
                xwalk.append({"resource_type": "DiagnosticReport", "legacy_id": key, "fhir_id": fhir_id,
                              "server_status": status})
        time.sleep(PAUSE_BETWEEN_BUNDLES)
    confirmed = sum(1 for x in xwalk if x["fhir_id"])
    created = sum(1 for x in xwalk if x["server_status"].startswith("201"))
    updated = sum(1 for x in xwalk if x["server_status"].startswith("200"))
    failed = sum(1 for l in load_log if l["outcome"] == "FAILED")
    print(f"   Sent {sent}, confirmed {confirmed} ({created} created, {updated} updated), "
          f"failed bundles {failed}   {'PASS' if confirmed == sent else 'FAIL'}")


def extract(xwalk):
    print("\n4. EXTRACT")
    found = search_all("DiagnosticReport")
    actual, results = [], []
    for r in found:
        key = identifier(r, SYS_REPORT)
        actual += long_rows("DiagnosticReport", key, flatten_report(r), "actual_value", r.get("id"))
        results += [{"report_fhir_id": r.get("id"), "report_key": key, "result_ref": x.get("reference")}
                    for x in r.get("result", [])]
    expected_ids = {x["fhir_id"] for x in xwalk if x["fhir_id"]}
    missing = expected_ids - {r.get("id") for r in found}
    print(f"   DiagnosticReport  expected {len(expected_ids)}, on server {len(found)}, "
          f"missing {len(missing)}   {'PASS' if not missing else 'FAIL'}")
    save_table("tgt.report_resources", actual, LONG_ACTUAL)
    save_table("tgt.report_results", results, RESULT_COLS)


def read_context():
    rows = read_table("""
        SELECT s.observation_key, x.fhir_id, o.PATIENT, o.ENCOUNTER, o.DATE, o.CODE, o.DESCRIPTION
        FROM etl.observation_sources AS s
        JOIN etl.observation_crosswalk AS x
          ON x.resource_type = 'Observation' AND x.legacy_id = s.observation_key AND x.fhir_id IS NOT NULL
        JOIN cur.observations AS o ON o.source_row = TRY_CAST(s.source_row AS INT)
        WHERE LOWER(LTRIM(RTRIM(o.CATEGORY))) = 'laboratory';""")
    if not rows:
        raise SystemExit("No migrated lab results found - run src\\phase2_observation.py first.")
    lookup = lambda kind: {r["legacy_id"]: r["fhir_id"] for r in read_table(
        f"SELECT legacy_id, fhir_id FROM etl.fhir_crosswalk WHERE resource_type = '{kind}' AND fhir_id IS NOT NULL;")}
    panel_of, combine = read_maps()
    return rows, {"patients": lookup("Patient"), "encounters": lookup("Encounter"),
                  "panel_of": panel_of, "combine": combine}


def main():
    parser = argparse.ArgumentParser(description="Phase 3: build lab DiagnosticReports from migrated results")
    parser.add_argument("--limit", type=int, help="only send the first N bundles (try 1 first)")
    args = parser.parse_args()

    print("1. READ   migrated lab results (Phase 2 Observations, CATEGORY laboratory)")
    rows, ctx = read_context()
    print(f"   {len(rows)} lab results; tests in the panel map {len(ctx['panel_of'])}; "
          f"combine rules {len(ctx['combine'])}")
    expected = []
    reports, issues, coverage, members, sources = build(rows, ctx, expected)
    save_table("etl.report_issue_log", issues, ISSUE_COLS)
    save_table("etl.report_coverage", coverage, COVERAGE_COLS)
    save_table("etl.report_members", members, MEMBER_COLS)
    save_table("etl.report_sources", sources, SOURCE_COLS)
    save_table("etl.report_expected", expected, LONG_EXPECTED)
    write_csv(OUTPUT_DIR / "reports.csv", sources, SOURCE_COLS)

    load_log, xwalk = [], []
    load(reports, load_log, xwalk, args.limit)
    save_table("etl.report_crosswalk", xwalk, XWALK_COLS)
    save_table("etl.report_load_log", load_log,
               ["bundle_no", "label", "entries", "http_status", "outcome", "attempts", "seconds", "error"])
    extract(xwalk)
    print("\nNext: open sql\\32_diagnostic_report_reconciliation.sql in SSMS and press F5.")


if __name__ == "__main__":
    main()
