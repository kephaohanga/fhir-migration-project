r"""
phase3_prior_auth.py
--------------------
Phase 3, step 2: migrate legacy prior authorizations to FHIR Claim + ClaimResponse.

One legacy row holds two things, so it becomes two resources:
  Claim (use = preauthorization)  the clinic's REQUEST to the payer
  ClaimResponse                   the payer's DECISION, pointing back to its Claim
A PENDING authorization has no decision yet, so it gets a Claim and no ClaimResponse:
a Claim without a response IS the pending state.

Mapping - Claim:
  AUTH_ID                  -> identifier
  (always)                 -> status active, use preauthorization, priority normal
  SERVICE_TYPE             -> type  MEDICATION pharmacy, PROCEDURE institutional, REFERRAL professional
  PATIENT                  -> patient                     Patient (must be migrated)
  REQUEST_DATE             -> created
  (clinic)                 -> provider                    the clinic Organization (Phase 2)
  PAYER                    -> insurer                     the payer Organization (Phase 2)
  PATIENT+PAYER+MEMBERID   -> insurance.coverage          the patient's migrated primary Coverage
  SERVICE_CODE             -> item.productOrService       RxNorm drug / SNOMED CT surgery /
                                                          SNOMED CT 3457005 Patient referral
  REFERRAL_ID              -> referral                    ServiceRequest (Phase 3 step 1)
Mapping - ClaimResponse (APPROVED and DENIED only):
  AUTH_ID                  -> identifier, request = the Claim
  DECISION_DATE            -> created; outcome complete
  DECISION, DENIAL_REASON  -> disposition                 "Approved" / "Denied: <reason>"
  AUTH_NUMBER              -> preAuthRef                  (the number a later claim must quote)
  VALID_FROM, VALID_TO     -> preAuthPeriod

Rules:
  R-PA01 Reject  PATIENT is not a migrated patient
  R-PA02 Reject  PATIENT + PAYER + MEMBERID is not the patient's migrated policy
  R-PA03 Reject  referral authorization: REFERRAL_ID blank, not migrated, or another patient's referral
  R-PA04 Fix     DECISION written with other capitals or spaces - tidied
  R-PA04 Reject  DECISION is not APPROVED, DENIED or PENDING
  R-PA05 Flag    APPROVED but AUTH_NUMBER blank - preAuthRef omitted, sent to the worklist
  R-PA06 Reject  dates invalid: REQUEST_DATE blank, DECISION_DATE missing or before the request,
                 or an approval without a valid VALID_FROM <= VALID_TO
  R-PA07 Flag    expired authorization: APPROVED but VALID_TO before the extract date -
                 migrated as history, sent to the worklist (renew or extend if still needed)
  R-PA08 Fix     exact duplicate row (EVERY column identical) removed, first copy kept

Run AFTER phase3_referral.py and python src\plant_prior_auth_errors.py. From the project folder:
  python src\phase3_prior_auth.py
Then open sql\29_prior_auth_reconciliation.sql in SSMS and press F5.
"""

import argparse
import csv
import time
from collections import defaultdict
from datetime import date
from pathlib import Path

import pandas as pd

import apply_corrections
from phase2_clinic_structure import identifier, parse_reply, put_entry, search_all, send_bundle
from phase2_practitioner import (BUNDLE_SIZE, FHIR_SERVER, PAUSE_BETWEEN_BUNDLES, PROJECT_TAG,
                                 connect, save_table, v, write_csv)
from phase2_procedure import long_rows, read_table

SOURCE_FILE = Path("data/legacy_dirty/prior_auths.csv")
ANSWER_KEY = Path("docs/planted_prior_auth_errors_answer_key.csv")
OUTPUT_DIR = Path("output/phase3_prior_auth")

SYS_CLAIM = "urn:fhirmigration:legacy-prior-auth-id"
SYS_DECISION = "urn:fhirmigration:legacy-prior-auth-decision-id"
SYS_CLAIM_TYPE = "http://terminology.hl7.org/CodeSystem/claim-type"
SYS_PRIORITY = "http://terminology.hl7.org/CodeSystem/processpriority"
SYS_RXNORM = "http://www.nlm.nih.gov/research/umls/rxnorm"
SYS_SNOMED = "http://snomed.info/sct"

CLAIM_TYPE = {"MEDICATION": "pharmacy", "PROCEDURE": "institutional", "REFERRAL": "professional"}
SERVICE_SYSTEM = {"MEDICATION": SYS_RXNORM, "PROCEDURE": SYS_SNOMED}
DECISIONS = {"APPROVED", "DENIED", "PENDING"}

ISSUE_COLS = ["record_key", "source_row", "rule_id", "action", "detail"]
DISPOSITION_COLS = ["record_key", "source_row", "disposition"]
SOURCE_COLS = ["auth_key", "record_key", "source_row", "decision", "service_type", "legacy_patient",
               "legacy_payer", "legacy_referral", "service_code"]
AUDIT_COLS = {"source_row", "loaded_at"}
LONG_EXPECTED = ["resource_type", "legacy_id", "field_name", "expected_value"]
LONG_ACTUAL = ["resource_type", "fhir_id", "legacy_id", "field_name", "actual_value"]
XWALK_COLS = ["resource_type", "legacy_id", "fhir_id", "server_status"]


def ref(r, field):
    return (r.get(field) or {}).get("reference")


def flatten_claim(r):
    item = (r.get("item") or [{}])[0]
    service = next(iter((item.get("productOrService") or {}).get("coding", [])), {})
    return {
        "status": r.get("status"),
        "use": r.get("use"),
        "type": next(iter((r.get("type") or {}).get("coding", [])), {}).get("code"),
        "priority": next(iter((r.get("priority") or {}).get("coding", [])), {}).get("code"),
        "patient_ref": ref(r, "patient"),
        "created": r.get("created"),
        "provider_ref": ref(r, "provider"),
        "insurer_ref": ref(r, "insurer"),
        "coverage_ref": ((r.get("insurance") or [{}])[0].get("coverage") or {}).get("reference"),
        "service_system": service.get("system"),
        "service_code": service.get("code"),
        "service_text": (item.get("productOrService") or {}).get("text"),
        "referral_ref": ref(r, "referral"),
    }


def flatten_response(r):
    period = r.get("preAuthPeriod") or {}
    return {
        "status": r.get("status"),
        "use": r.get("use"),
        "type": next(iter((r.get("type") or {}).get("coding", [])), {}).get("code"),
        "patient_ref": ref(r, "patient"),
        "created": r.get("created"),
        "insurer_ref": ref(r, "insurer"),
        "request_ref": ref(r, "request"),
        "outcome": r.get("outcome"),
        "disposition": r.get("disposition"),
        "pre_auth_ref": r.get("preAuthRef"),
        "pre_auth_start": period.get("start"),
        "pre_auth_end": period.get("end"),
    }


def iso(text):
    try:
        return date.fromisoformat((text or "").strip()[:10]) if len((text or "").strip()) == 10 else None
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 1. STAGE
# ---------------------------------------------------------------------------
def stage():
    if not SOURCE_FILE.exists():
        raise SystemExit(f"{SOURCE_FILE} not found. Run  python src\\plant_prior_auth_errors.py  first.")
    df = pd.read_csv(SOURCE_FILE, dtype=str, keep_default_na=False)
    columns = list(df.columns)
    data = [[None if x == "" else x for x in values] + [n + 2]
            for n, values in enumerate(df.itertuples(index=False, name=None))]
    conn = connect()
    cur = conn.cursor()
    cur.execute("IF SCHEMA_ID('stg') IS NULL EXEC('CREATE SCHEMA stg');")
    cur.execute("DROP TABLE IF EXISTS stg.prior_auths;")
    col_defs = ", ".join(f"[{c}] NVARCHAR(500) NULL" for c in columns)
    cur.execute(f"CREATE TABLE stg.prior_auths ({col_defs}, [source_row] INT NOT NULL, "
                f"[loaded_at] DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME());")
    cur.fast_executemany = True
    cur.executemany(
        f"INSERT INTO stg.prior_auths ({', '.join(f'[{c}]' for c in columns)}, [source_row]) "
        f"VALUES ({', '.join('?' for _ in range(len(columns) + 1))});", data)
    sql_rows = cur.execute("SELECT COUNT(*) FROM stg.prior_auths;").fetchone()[0]
    conn.close()
    print("1. STAGE")
    print(f"   CSV rows {len(df)}   stg.prior_auths rows {sql_rows}   {'PASS' if sql_rows == len(df) else 'FAIL'}")


# ---------------------------------------------------------------------------
# 3. TRANSFORM
# ---------------------------------------------------------------------------
def transform(rows, ctx, expected):
    """ctx: patients, payers, clinic, coverage {(patient, payer, member): id},
    referrals {legacy id: (fhir id, legacy patient)}, as_of (date)."""
    issues, dispositions, claims, sources = [], [], [], []
    seen_rows = set()

    def log(key, src, rule, action, detail):
        issues.append({"record_key": key, "source_row": src, "rule_id": rule, "action": action, "detail": detail})

    def dispose(key, src, disposition):
        dispositions.append({"record_key": key, "source_row": src, "disposition": disposition})

    def reject(key, src, rule, detail):
        log(key, src, rule, "Reject", detail)
        dispose(key, src, "Rejected")

    for r in rows:
        key, src = v(r, "AUTH_ID"), r["source_row"]
        patient, payer, member = v(r, "PATIENT"), v(r, "PAYER"), v(r, "MEMBERID")
        service_type, referral_id = v(r, "SERVICE_TYPE"), v(r, "REFERRAL_ID")

        signature = tuple((c, r[c]) for c in sorted(r) if c not in AUDIT_COLS)
        if signature in seen_rows:                                                    # R-PA08
            log(key, src, "R-PA08", "Fix", "Exact duplicate row (every column identical) removed - first copy kept")
            dispose(key, src, "Duplicate removed")
            continue
        seen_rows.add(signature)

        if patient not in ctx["patients"]:                                            # R-PA01
            reject(key, src, "R-PA01", f"PATIENT {patient} is not a migrated patient")
            continue
        raw = r.get("DECISION") or ""
        decision = raw.strip().upper()
        if decision not in DECISIONS:                                                 # R-PA04 Reject
            reject(key, src, "R-PA04", f"DECISION '{raw}' is not APPROVED, DENIED or PENDING")
            continue
        if decision != raw:                                                           # R-PA04 Fix
            log(key, src, "R-PA04", "Fix", f"DECISION '{raw}' tidied to '{decision}'")
        coverage = ctx["coverage"].get((patient, payer, member))
        if coverage is None or payer not in ctx["payers"]:                            # R-PA02
            reject(key, src, "R-PA02", f"PAYER {payer} + MEMBERID {member} is not this patient's migrated policy")
            continue
        referral = None
        if service_type == "REFERRAL":
            referral = ctx["referrals"].get(referral_id)
            if referral is None or referral[1] != patient:                            # R-PA03
                reject(key, src, "R-PA03",
                       "Referral authorization without a referral" if not referral_id else
                       f"REFERRAL_ID {referral_id} is not a migrated referral" if referral is None else
                       f"REFERRAL_ID {referral_id} belongs to another patient ({referral[1]})")
                continue
        if service_type not in CLAIM_TYPE:                                            # unknown type
            reject(key, src, "R-PA04", f"SERVICE_TYPE '{service_type}' is not MEDICATION, PROCEDURE or REFERRAL")
            continue
        requested, decided = iso(r.get("REQUEST_DATE")), iso(r.get("DECISION_DATE"))
        valid_from, valid_to = iso(r.get("VALID_FROM")), iso(r.get("VALID_TO"))
        bad = (requested is None
               or (decision != "PENDING" and (decided is None or decided < requested))
               or (decision == "APPROVED" and (valid_from is None or valid_to is None or valid_to < valid_from)))
        if bad:                                                                       # R-PA06
            reject(key, src, "R-PA06",
                   f"Dates invalid: REQUEST_DATE {r.get('REQUEST_DATE')}, DECISION_DATE {r.get('DECISION_DATE')}, "
                   f"VALID_FROM {r.get('VALID_FROM')}, VALID_TO {r.get('VALID_TO')}")
            continue

        claim_type = {"coding": [{"system": SYS_CLAIM_TYPE, "code": CLAIM_TYPE[service_type]}]}
        if service_type == "REFERRAL":
            service = {"coding": [{"system": SYS_SNOMED, "code": "3457005", "display": "Patient referral"}],
                       "text": v(r, "SERVICE_DESCRIPTION")}
        else:
            service = {"coding": [{"system": SERVICE_SYSTEM[service_type], "code": v(r, "SERVICE_CODE"),
                                   "display": v(r, "SERVICE_DESCRIPTION")}],
                       "text": v(r, "SERVICE_DESCRIPTION")}
        claim = {
            "resourceType": "Claim",
            "meta": {"tag": [PROJECT_TAG]},
            "identifier": [{"system": SYS_CLAIM, "value": key}],
            "status": "active",
            "type": claim_type,
            "use": "preauthorization",
            "patient": {"reference": f"Patient/{ctx['patients'][patient]}"},
            "created": requested.isoformat(),
            "provider": {"reference": f"Organization/{ctx['clinic']}"},
            "insurer": {"reference": f"Organization/{ctx['payers'][payer]}"},
            "priority": {"coding": [{"system": SYS_PRIORITY, "code": "normal"}]},
            "insurance": [{"sequence": 1, "focal": True, "coverage": {"reference": f"Coverage/{coverage}"}}],
            "item": [{"sequence": 1, "productOrService": service}],
        }
        if referral:
            claim["referral"] = {"reference": f"ServiceRequest/{referral[0]}"}

        response = None
        if decision != "PENDING":
            response = {
                "resourceType": "ClaimResponse",
                "meta": {"tag": [PROJECT_TAG]},
                "identifier": [{"system": SYS_DECISION, "value": key}],
                "status": "active",
                "type": claim_type,
                "use": "preauthorization",
                "patient": claim["patient"],
                "created": decided.isoformat(),
                "insurer": claim["insurer"],
                "outcome": "complete",
                "disposition": "Approved" if decision == "APPROVED"
                               else "Denied" + (f": {v(r, 'DENIAL_REASON')}" if v(r, "DENIAL_REASON") else ""),
            }
            if decision == "APPROVED":
                response["preAuthPeriod"] = {"start": valid_from.isoformat(), "end": valid_to.isoformat()}
                if v(r, "AUTH_NUMBER"):
                    response["preAuthRef"] = v(r, "AUTH_NUMBER")
                else:                                                                 # R-PA05
                    log(key, src, "R-PA05", "Flag",
                        "APPROVED but AUTH_NUMBER is blank - preAuthRef omitted (a claim for this service would be denied)")
                if valid_to < ctx["as_of"]:                                           # R-PA07
                    log(key, src, "R-PA07", "Flag",
                        f"Expired authorization: approved until {valid_to}, before the extract date {ctx['as_of']} "
                        f"- migrated as history; renew or extend if the service is still planned")

        claims.append((key, claim, response))
        sources.append({"auth_key": key, "record_key": key, "source_row": src, "decision": decision,
                        "service_type": service_type, "legacy_patient": patient, "legacy_payer": payer,
                        "legacy_referral": referral_id, "service_code": v(r, "SERVICE_CODE")})
        expected += long_rows("Claim", key, flatten_claim(claim), "expected_value")
        if response:
            # request_ref is only known after the Claim is loaded; it is added in load()
            fields = {k: val for k, val in flatten_response(response).items() if k != "request_ref"}
            expected += long_rows("ClaimResponse", key, fields, "expected_value")
        dispose(key, src, "Migrated")

    counts = defaultdict(int)
    for d in dispositions:
        counts[d["disposition"]] += 1
    ok = len(rows) == len(dispositions)
    print("\n3. TRANSFORM   Claim (preauthorization) + ClaimResponse")
    print(f"   Legacy rows {len(rows)} = " + " + ".join(f"{n} {d.lower()}" for d, n in sorted(counts.items()))
          + f"   {'PASS' if ok else 'FAIL'}")
    by_decision = defaultdict(int)
    for s in sources:
        by_decision[s["decision"]] += 1
    print(f"   Claims built: {len(claims)} (" + ", ".join(f"{n} {d}" for d, n in sorted(by_decision.items()))
          + f"); ClaimResponses: {sum(1 for c in claims if c[2])}")
    for rule in sorted({i["rule_id"] + " " + i["action"] for i in issues}):
        print(f"   {rule}: {sum(1 for i in issues if i['rule_id'] + ' ' + i['action'] == rule)}")
    check_answer_key(issues)
    return claims, issues, dispositions, sources


def check_answer_key(issues):
    if not ANSWER_KEY.exists():
        return
    with ANSWER_KEY.open(encoding="utf-8") as f:
        planted = list(csv.DictReader(f))
    worked = {r["record_key"]: r["disposition"] for r in read_table(
        "SELECT record_key, disposition FROM etl.corrections WHERE table_name = 'prior_auths';")}
    done = {"Correct": "corrected", "Archive": "archived"}
    caught = 0
    print("   Planted-defect check:")
    for p in planted:
        hit = any(i["record_key"] == p["record_id"] and i["rule_id"] == p["expected_rule"]
                  and i["action"] == p["expected_action"] for i in issues)
        if hit:
            result = "CAUGHT"
        elif p["record_id"] in worked:
            result = f"CAUGHT earlier, {done.get(worked[p['record_id']], 'worked')} by staff"
        else:
            result = "MISSED"
        caught += result != "MISSED"
        print(f"     {p['error_type']:<24} expected {p['expected_rule']} {p['expected_action']:<7} {result}")
    print(f"   Result: {caught} of {len(planted)} planted defects caught")


# ---------------------------------------------------------------------------
# 4. LOAD (Claims, then ClaimResponses that point to them) and 5. EXTRACT
# ---------------------------------------------------------------------------
def load_set(resource_type, system, items, load_log, xwalk):
    """items: [(key, resource)] -> sends them in bundles, fills xwalk; returns {key: fhir id}."""
    ids = {}
    chunks = [items[i:i + BUNDLE_SIZE] for i in range(0, len(items), BUNDLE_SIZE)]
    for n, chunk in enumerate(chunks, 1):
        entries = [put_entry(res, f"{resource_type}?identifier={system}|{key}") for key, res in chunk]
        reply = send_bundle(entries, n, resource_type, load_log)
        if reply is not None:
            for (key, _), r in zip(chunk, reply):
                fhir_id, status = parse_reply(r, resource_type)
                ids[key] = fhir_id
                xwalk.append({"resource_type": resource_type, "legacy_id": key, "fhir_id": fhir_id,
                              "server_status": status})
        time.sleep(PAUSE_BETWEEN_BUNDLES)
    return ids


def load(claims, load_log, xwalk, expected):
    print(f"\n4. LOAD   to {FHIR_SERVER}")
    claim_ids = load_set("Claim", SYS_CLAIM, [(k, c) for k, c, _ in claims], load_log, xwalk)
    responses = []
    for key, _, response in claims:
        if response and claim_ids.get(key):
            response["request"] = {"reference": f"Claim/{claim_ids[key]}"}
            responses.append((key, response))
            expected.append({"resource_type": "ClaimResponse", "legacy_id": key, "field_name": "request_ref",
                             "expected_value": response["request"]["reference"]})
    response_ids = load_set("ClaimResponse", SYS_DECISION, responses, load_log, xwalk)
    for resource_type, sent, ids in (("Claim", len(claims), claim_ids),
                                     ("ClaimResponse", sum(1 for c in claims if c[2]), response_ids)):
        rows = [x for x in xwalk if x["resource_type"] == resource_type]
        confirmed = sum(1 for x in rows if x["fhir_id"])
        created = sum(1 for x in rows if x["server_status"].startswith("201"))
        updated = sum(1 for x in rows if x["server_status"].startswith("200"))
        print(f"   {resource_type:<14} sent {sent}, confirmed {confirmed} ({created} created, {updated} updated)"
              f"   {'PASS' if confirmed == sent else 'FAIL'}")
    failed = sum(1 for l in load_log if l["outcome"] == "FAILED")
    print(f"   failed bundles {failed}")


def extract(xwalk):
    print("\n5. EXTRACT")
    actual = []
    for resource_type, system, flatten in (("Claim", SYS_CLAIM, flatten_claim),
                                           ("ClaimResponse", SYS_DECISION, flatten_response)):
        found = search_all(resource_type)
        for r in found:
            actual += long_rows(resource_type, identifier(r, system), flatten(r), "actual_value", r.get("id"))
        expected_ids = {x["fhir_id"] for x in xwalk if x["resource_type"] == resource_type and x["fhir_id"]}
        on_server = {r.get("id") for r in found}
        missing = expected_ids - on_server
        print(f"   {resource_type:<14} expected {len(expected_ids)}, on server {len(on_server)}, "
              f"missing {len(missing)}   {'PASS' if not missing else 'FAIL'}")
    save_table("tgt.prior_auth_resources", actual, LONG_ACTUAL)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def read_context():
    lookup = lambda sql: {r["legacy_id"]: r["fhir_id"] for r in read_table(sql)}
    clinic = read_table("SELECT fhir_id FROM etl.clinic_crosswalk "
                        "WHERE resource_type = 'Organization' AND fhir_id IS NOT NULL;")
    if not clinic:
        raise SystemExit("Clinic Organization not found - run src\\phase2_clinic_structure.py first.")
    coverage = {(r["PATIENT"], r["PAYER"], r["MEMBERID"]): r["fhir_id"] for r in read_table("""
        SELECT DISTINCT s.legacy_patient AS PATIENT, s.legacy_payer AS PAYER, pt.MEMBERID, x.fhir_id
        FROM etl.coverage_sources AS s
        JOIN etl.coverage_crosswalk AS x
          ON x.resource_type = 'Coverage' AND x.legacy_id = s.coverage_key AND x.fhir_id IS NOT NULL
        JOIN cur.payer_transitions AS pt ON CONCAT(pt.PATIENT, '_', pt.START_DATE) = s.record_key
        WHERE s.coverage_order = '1';""")}
    referrals = {r["legacy_id"]: (r["fhir_id"], r["legacy_patient"]) for r in read_table("""
        SELECT x.legacy_id, x.fhir_id, s.legacy_patient
        FROM etl.referral_crosswalk AS x
        JOIN etl.referral_sources AS s ON s.referral_key = x.legacy_id
        WHERE x.fhir_id IS NOT NULL;""")}
    as_of = read_table("SELECT MAX(START_DATE) AS as_of FROM cur.payer_transitions;")[0]["as_of"]
    return {
        "patients": lookup("SELECT legacy_id, fhir_id FROM etl.fhir_crosswalk "
                           "WHERE resource_type = 'Patient' AND fhir_id IS NOT NULL;"),
        "payers": lookup("SELECT legacy_id, fhir_id FROM etl.coverage_crosswalk "
                         "WHERE resource_type = 'Organization' AND fhir_id IS NOT NULL;"),
        "clinic": clinic[0]["fhir_id"],
        "coverage": coverage,
        "referrals": referrals,
        "as_of": date.fromisoformat(as_of[:10]),
    }


def main():
    argparse.ArgumentParser(description="Phase 3: migrate prior authorizations to Claim + ClaimResponse").parse_args()

    stage()
    print("\n2. CURATE   staff corrections from remediation/corrections.csv")
    if not apply_corrections.main(show_next=False):
        raise SystemExit("Stopped: fix remediation/corrections.csv, then run this script again.")
    rows = read_table("SELECT * FROM cur.prior_auths ORDER BY source_row;")
    ctx = read_context()
    print(f"   cur.prior_auths {len(rows)} rows; extract date {ctx['as_of']}; migrated patients "
          f"{len(ctx['patients'])}, payers {len(ctx['payers'])}, primary policies {len(ctx['coverage'])}, "
          f"referrals {len(ctx['referrals'])}")

    expected = []
    claims, issues, dispositions, sources = transform(rows, ctx, expected)
    save_table("etl.prior_auth_issue_log", issues, ISSUE_COLS)
    save_table("etl.prior_auth_disposition", dispositions, DISPOSITION_COLS)
    save_table("etl.prior_auth_sources", sources, SOURCE_COLS)
    write_csv(OUTPUT_DIR / "issue_log.csv", issues, ISSUE_COLS)

    load_log, xwalk = [], []
    load(claims, load_log, xwalk, expected)
    save_table("etl.prior_auth_expected", expected, LONG_EXPECTED)
    save_table("etl.prior_auth_crosswalk", xwalk, XWALK_COLS)
    save_table("etl.prior_auth_load_log", load_log,
               ["bundle_no", "label", "entries", "http_status", "outcome", "attempts", "seconds", "error"])
    write_csv(OUTPUT_DIR / "crosswalk.csv", xwalk, XWALK_COLS)

    extract(xwalk)
    print("\nNext: open sql\\29_prior_auth_reconciliation.sql in SSMS and press F5.")


if __name__ == "__main__":
    main()
