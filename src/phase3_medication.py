r"""
phase3_medication.py
--------------------
Phase 3, step 4: build the drug catalogue - one FHIR Medication per drug.

Phase 2 wrote the drug (RxNorm code and name) into every one of the 2,825 prescriptions.
Here each distinct drug becomes ONE Medication record. phase2_medication.py is then run
again so every prescription POINTS to its Medication (medicationReference) instead of
repeating the code and name.

This is a DERIVED resource: it has no legacy file of its own. It is built from the curated
prescriptions (cur.medications), which already passed Phase 2's rules and staff corrections.
So there is no plant step: the test cases are the real problems the profile found.

Mapping (one Medication per distinct RxNorm CODE):
  CODE                     -> identifier (legacy drug code) and code.coding   RxNorm
  DESCRIPTION              -> code.coding.display, code.text                  (one name per drug)
  dose form in the name    -> form.text           docs/dose_form_map.csv (keyword -> dose form);
                                                  a "{...} Pack" is a Pack whatever its contents
  (always)                 -> status active

Rules:
  R-MN01 Fix   the same CODE is written with more than one DESCRIPTION. When they differ only
               in capitals, the Tall Man spelling (e.g. medroxyPROGESTERone - mixed capitals that
               mark look-alike drug names, a drug-safety convention) is kept; otherwise the most
               used description is kept. Every variant is logged.
  R-MN02 Flag  no dose form found in the name (e.g. "doxycycline hyclate 100 MG") - form omitted
  R-MN03 Flag  the same DESCRIPTION is used by more than one CODE - each code still gets its record
  Only codes that pass Phase 2's R-MD04 check (numeric, not a SNOMED CT description) are used.

Run AFTER Phase 2 (phase2_medication.py has staged and curated the prescriptions):
  python src\phase3_medication.py
  python src\phase2_medication.py      <- re-points every prescription to its Medication
Then open sql\31_medication_catalogue_reconciliation.sql in SSMS and press F5.
"""

import csv
import re
import time
from collections import Counter, defaultdict
from pathlib import Path

from phase2_clinic_structure import identifier, parse_reply, put_entry, search_all, send_bundle
from phase2_practitioner import (BUNDLE_SIZE, FHIR_SERVER, PAUSE_BETWEEN_BUNDLES, PROJECT_TAG,
                                 save_table, write_csv)
from phase2_procedure import long_rows, read_table

DOSE_FORM_MAP = Path("docs/dose_form_map.csv")
OUTPUT_DIR = Path("output/phase3_medication")

SYS_DRUG = "urn:fhirmigration:legacy-drug-code"
SYS_RXNORM = "http://www.nlm.nih.gov/research/umls/rxnorm"
SNOMED_TAG = re.compile(r"\((substance|finding|disorder|procedure|organism|product|situation)\)\s*$", re.I)

ISSUE_COLS = ["record_key", "rule_id", "action", "detail"]
CATALOGUE_COLS = ["code", "display", "dose_form", "prescriptions", "patients", "variants"]
LONG_EXPECTED = ["resource_type", "legacy_id", "field_name", "expected_value"]
LONG_ACTUAL = ["resource_type", "fhir_id", "legacy_id", "field_name", "actual_value"]
XWALK_COLS = ["resource_type", "legacy_id", "fhir_id", "server_status"]


def squash(text):
    """Lower case, no spaces: 'ExtendedRelease Oral Tablet' matches 'Extended Release Oral Tablet'."""
    return re.sub(r"\s+", "", (text or "").lower())


def read_dose_forms():
    with DOSE_FORM_MAP.open(encoding="utf-8-sig", newline="") as f:
        rows = [(squash(r["keyword"]), r["dose_form"].strip()) for r in csv.DictReader(f)]
    return sorted(rows, key=lambda kf: -len(kf[0]))        # longest keyword first


def find_form(name, forms):
    if re.match(r"\s*\{.*\}\s*Pack", name or ""):      # "{28 (... Oral Tablet) } Pack": the product is a pack
        return "Pack"
    squashed = squash(name)
    return next((form for keyword, form in forms if keyword in squashed), None)


def is_tall_man(text):
    """A word with lower case followed by 3+ capitals, e.g. 'medroxyPROGESTERone'."""
    return bool(re.search(r"[a-z][A-Z]{3,}", text))


def flatten_drug(r):
    coding = next((c for c in (r.get("code") or {}).get("coding", []) if c.get("system") == SYS_RXNORM), {})
    return {
        "status": r.get("status"),
        "code": coding.get("code"),
        "code_display": coding.get("display"),
        "code_text": (r.get("code") or {}).get("text"),
        "dose_form": (r.get("form") or {}).get("text"),
    }


# ---------------------------------------------------------------------------
# BUILD the catalogue from the curated prescriptions
# ---------------------------------------------------------------------------
def build(rows, forms, expected):
    names = defaultdict(Counter)                  # code -> {description: prescriptions}
    patients = defaultdict(set)
    for r in rows:
        code, name = (r.get("CODE") or "").strip(), (r.get("DESCRIPTION") or "").strip()
        if not code.isdigit() or SNOMED_TAG.search(name):     # same test as Phase 2 rule R-MD04
            continue
        names[code][name] += 1
        patients[code].add(r.get("PATIENT"))

    issues, catalogue, drugs = [], [], []
    codes_by_name = defaultdict(set)
    for code in sorted(names, key=int):
        variants = names[code]
        if len(variants) > 1:                                                         # R-MN01
            same_letters = len({n.lower() for n in variants}) == 1
            tall_man = [n for n in variants if is_tall_man(n)]
            chosen = tall_man[0] if same_letters and tall_man else variants.most_common(1)[0][0]
            issues.append({"record_key": code, "rule_id": "R-MN01", "action": "Fix",
                           "detail": f"{len(variants)} descriptions ({' | '.join(sorted(variants))}) - kept "
                                     f"'{chosen}'" + (" (Tall Man spelling)" if same_letters and tall_man else
                                                      " (most used)")})
        else:
            chosen = next(iter(variants))
        codes_by_name[chosen.lower()].add(code)
        form = find_form(chosen, forms)
        drug = {
            "resourceType": "Medication",
            "meta": {"tag": [PROJECT_TAG]},
            "identifier": [{"system": SYS_DRUG, "value": code}],
            "code": {"coding": [{"system": SYS_RXNORM, "code": code, "display": chosen}], "text": chosen},
            "status": "active",
        }
        if form:
            drug["form"] = {"text": form}
        else:                                                                         # R-MN02
            issues.append({"record_key": code, "rule_id": "R-MN02", "action": "Flag",
                           "detail": f"No dose form found in '{chosen}' - form omitted"})
        drugs.append((code, drug))
        catalogue.append({"code": code, "display": chosen, "dose_form": form,
                          "prescriptions": sum(variants.values()), "patients": len(patients[code]),
                          "variants": len(variants)})
        expected += long_rows("Medication", code, flatten_drug(drug), "expected_value")
    for name, codes in codes_by_name.items():                                         # R-MN03
        if len(codes) > 1:
            for code in sorted(codes):
                issues.append({"record_key": code, "rule_id": "R-MN03", "action": "Flag",
                               "detail": f"Description '{name}' is also used by code(s) "
                                         f"{', '.join(sorted(codes - {code}))}"})

    print("\n2. BUILD   one Medication per drug")
    print(f"   prescriptions read {len(rows)}; distinct drugs {len(drugs)}; "
          f"with a dose form {sum(1 for c in catalogue if c['dose_form'])}")
    for rule in sorted({i["rule_id"] + " " + i["action"] for i in issues}):
        print(f"   {rule}: {sum(1 for i in issues if i['rule_id'] + ' ' + i['action'] == rule)}")
    for i in issues:
        print(f"     {i['rule_id']} {i['record_key']}: {i['detail'][:110]}")
    forms_used = Counter(c["dose_form"] or "(none)" for c in catalogue)
    print("   dose forms: " + ", ".join(f"{f} {n}" for f, n in forms_used.most_common()))
    return drugs, issues, catalogue


# ---------------------------------------------------------------------------
# LOAD and EXTRACT
# ---------------------------------------------------------------------------
def load(drugs, load_log, xwalk):
    chunks = [drugs[i:i + BUNDLE_SIZE] for i in range(0, len(drugs), BUNDLE_SIZE)]
    print(f"\n3. LOAD   {len(chunks)} bundle(s) of Medication to {FHIR_SERVER}")
    for n, chunk in enumerate(chunks, 1):
        entries = [put_entry(d, f"Medication?identifier={SYS_DRUG}|{code}") for code, d in chunk]
        reply = send_bundle(entries, n, "Medication", load_log)
        if reply is not None:
            for (code, _), r in zip(chunk, reply):
                fhir_id, status = parse_reply(r, "Medication")
                xwalk.append({"resource_type": "Medication", "legacy_id": code, "fhir_id": fhir_id,
                              "server_status": status})
        time.sleep(PAUSE_BETWEEN_BUNDLES)
    confirmed = sum(1 for x in xwalk if x["fhir_id"])
    created = sum(1 for x in xwalk if x["server_status"].startswith("201"))
    updated = sum(1 for x in xwalk if x["server_status"].startswith("200"))
    print(f"   Sent {len(drugs)}, confirmed {confirmed} ({created} created, {updated} updated)   "
          f"{'PASS' if confirmed == len(drugs) else 'FAIL'}")


def extract(xwalk):
    print("\n4. EXTRACT")
    found = search_all("Medication")
    actual = []
    for r in found:
        actual += long_rows("Medication", identifier(r, SYS_DRUG), flatten_drug(r), "actual_value", r.get("id"))
    expected_ids = {x["fhir_id"] for x in xwalk if x["fhir_id"]}
    missing = expected_ids - {r.get("id") for r in found}
    print(f"   Medication  expected {len(expected_ids)}, on server {len(found)}, missing {len(missing)}   "
          f"{'PASS' if not missing else 'FAIL'}")
    save_table("tgt.drug_resources", actual, LONG_ACTUAL)


def main():
    print("1. READ   curated prescriptions (cur.medications, from Phase 2)")
    rows = read_table("SELECT CODE, DESCRIPTION, PATIENT FROM cur.medications;")
    if not rows:
        raise SystemExit("cur.medications is empty - run src\\phase2_medication.py first.")
    expected = []
    drugs, issues, catalogue = build(rows, read_dose_forms(), expected)
    save_table("etl.drug_issue_log", issues, ISSUE_COLS)
    save_table("etl.drug_catalogue", catalogue, CATALOGUE_COLS)
    save_table("etl.drug_expected", expected, LONG_EXPECTED)
    write_csv(OUTPUT_DIR / "drug_catalogue.csv", catalogue, CATALOGUE_COLS)

    load_log, xwalk = [], []
    load(drugs, load_log, xwalk)
    save_table("etl.drug_crosswalk", xwalk, XWALK_COLS)
    save_table("etl.drug_load_log", load_log,
               ["bundle_no", "label", "entries", "http_status", "outcome", "attempts", "seconds", "error"])
    extract(xwalk)
    print("\nNext: python src\\phase2_medication.py  (re-points the prescriptions to these Medications)")


if __name__ == "__main__":
    main()
