"""
phase2_encounter_links.py
-------------------------
Phase 2, step 3: link every migrated visit (Encounter) to
  - the practitioner who saw the patient   Encounter.participant  -> Practitioner
  - where it happened                      Encounter.location     -> the clinic Location
  - the organization responsible           Encounter.serviceProvider -> the clinic Organization

The encounters were loaded in Phase 1, before any Practitioner existed, so they
are UPDATED IN PLACE, not reloaded. The method is "read - modify - write":
  1. READ     read each encounter as the server holds it now
  2. MODIFY   add only the three links; every other field is left exactly as it is
  3. WRITE    PUT it back, in transaction bundles of 50, with ifMatch on the version
              just read. ifMatch means "save only if nobody changed this encounter
              since I read it" (optimistic locking), so no one else's change is
              overwritten without notice.
  4. EXTRACT  read the links back for reconciliation

The provider comes from the legacy encounter's PROVIDER column (cur.encounters),
translated to the FHIR Practitioner through etl.practitioner_crosswalk.

Rules:
  R-EL01 Flag   encounter has no PROVIDER in the legacy data - linked to location only
  R-EL02 Flag   PROVIDER was not migrated as a Practitioner - linked to location only
  R-EL03 Error  encounter in the crosswalk could not be read from the server

Run AFTER src\\phase2_practitioner.py and src\\phase2_clinic_structure.py.
From the project folder:
  python src\\phase2_encounter_links.py --limit 1   <- ONE bundle of 50 encounters (a test)
  python src\\phase2_encounter_links.py             <- everything
Then open sql\\22_encounter_link_reconciliation.sql in SSMS and press F5,
and re-run the Phase 1 checks (extract_fhir.py + 10_reconciliation.sql) to prove
nothing else in the encounters changed.
"""

import argparse
import copy
import time
from pathlib import Path

from phase2_practitioner import (BUNDLE_SIZE, FHIR_SERVER, PAGE_SIZE, PAUSE_BETWEEN_BUNDLES,
                                 TAG_SEARCH, connect, get_json, post_bundle, save_table, write_csv)

OUTPUT_DIR = Path("output/phase2_encounter_links")
SYS_PARTICIPATION = "http://terminology.hl7.org/CodeSystem/v3-ParticipationType"

LINK_COLS = ["legacy_encounter_id", "encounter_fhir_id", "practitioner_ref", "participant_type",
             "location_ref", "service_provider_ref"]
ISSUE_COLS = ["record_key", "source_row", "rule_id", "action", "detail"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def is_practitioner_participant(p):
    return ((p.get("individual") or {}).get("reference") or "").startswith("Practitioner/")


def flatten_links(r):
    """The link fields only - used for both expected and actual."""
    part = next((p for p in r.get("participant", []) if is_practitioner_participant(p)), {})
    part_type = next((c.get("code") for t in part.get("type", []) for c in t.get("coding", [])
                      if c.get("system") == SYS_PARTICIPATION), None)
    return {
        "practitioner_ref": (part.get("individual") or {}).get("reference"),
        "participant_type": part_type,
        "location_ref": next(((l.get("location") or {}).get("reference") for l in r.get("location", [])), None),
        "service_provider_ref": (r.get("serviceProvider") or {}).get("reference"),
    }


def add_links(encounter, practitioner_ref, location_ref, organization_ref):
    """Return (updated copy, version read). Only the three links change."""
    r = copy.deepcopy(encounter)
    if practitioner_ref:
        # Keep any non-practitioner participants; replace a practitioner link if one exists
        others = [p for p in r.get("participant", []) if not is_practitioner_participant(p)]
        r["participant"] = others + [{
            "type": [{"coding": [{"system": SYS_PARTICIPATION, "code": "PPRF",
                                  "display": "primary performer"}]}],
            "individual": {"reference": practitioner_ref},
        }]
    r["location"] = [{"location": {"reference": location_ref}}]
    r["serviceProvider"] = {"reference": organization_ref}
    meta = r.get("meta", {})
    version = meta.pop("versionId", None)          # the server sets these itself on save
    meta.pop("lastUpdated", None)
    return r, version


# ---------------------------------------------------------------------------
# 1. READ
# ---------------------------------------------------------------------------
def read_scope():
    """Migrated encounters with their legacy PROVIDER, plus the crosswalks."""
    conn = connect()
    cur = conn.cursor()
    scope = [dict(zip(["legacy_id", "fhir_id", "provider", "source_row"], row)) for row in cur.execute("""
        SELECT x.legacy_id, x.fhir_id, NULLIF(LTRIM(e.PROVIDER), ''), e.source_row
        FROM etl.fhir_crosswalk AS x
        JOIN (SELECT Id, PROVIDER, source_row,
                     ROW_NUMBER() OVER (PARTITION BY Id ORDER BY source_row) AS rn
              FROM cur.encounters) AS e
          ON e.Id = x.legacy_id AND e.rn = 1
        WHERE x.resource_type = 'Encounter' AND x.fhir_id IS NOT NULL
        ORDER BY e.source_row;""").fetchall()]
    practitioners = {r[0]: r[1] for r in cur.execute(
        "SELECT legacy_id, fhir_id FROM etl.practitioner_crosswalk WHERE fhir_id IS NOT NULL;").fetchall()}
    clinic = {r[0]: r[1] for r in cur.execute(
        "SELECT resource_type, fhir_id FROM etl.clinic_crosswalk "
        "WHERE resource_type IN ('Organization', 'Location') AND fhir_id IS NOT NULL;").fetchall()}
    conn.close()
    if "Organization" not in clinic or "Location" not in clinic:
        raise SystemExit("Clinic Organization/Location not found. Run  python src\\phase2_clinic_structure.py  first.")
    print("1. READ")
    print(f"   Migrated encounters: {len(scope)}   Migrated practitioners: {len(practitioners)}")
    print(f"   Clinic: Organization/{clinic['Organization']}  Location/{clinic['Location']}")
    return scope, practitioners, f"Organization/{clinic['Organization']}", f"Location/{clinic['Location']}"


def read_encounters(wanted_ids):
    """Read the encounters from the server: tag search, then one-by-one for any not found."""
    found = {}
    bundle = get_json(f"{FHIR_SERVER}/Encounter", params={"_tag": TAG_SEARCH, "_count": PAGE_SIZE})
    page = 1
    while True:
        for e in bundle.get("entry", []):
            r = e.get("resource", {})
            if r.get("resourceType") == "Encounter" and r.get("id") in wanted_ids:
                found[r["id"]] = r
        next_url = next((l["url"] for l in bundle.get("link", []) if l.get("relation") == "next"), None)
        if not next_url:
            break
        page += 1
        bundle = get_json(next_url)
    for fhir_id in wanted_ids - found.keys():                   # not tagged / not in the search
        try:
            found[fhir_id] = get_json(f"{FHIR_SERVER}/Encounter/{fhir_id}")
        except Exception:
            pass
    print(f"   Read from server: {len(found)} of {len(wanted_ids)} encounters ({page} search pages)")
    return found


# ---------------------------------------------------------------------------
# 2. MODIFY
# ---------------------------------------------------------------------------
def build_updates(scope, server, practitioners, org_ref, loc_ref):
    issues, updates, expected = [], [], []
    already = sum(1 for s in scope if s["fhir_id"] in server
                  and flatten_links(server[s["fhir_id"]])["practitioner_ref"])

    def log(key, src, rule, action, detail):
        issues.append({"record_key": key, "source_row": src, "rule_id": rule, "action": action, "detail": detail})

    for s in scope:
        enc = server.get(s["fhir_id"])
        if enc is None:                                                    # R-EL03
            log(s["legacy_id"], s["source_row"], "R-EL03", "Error",
                f"Encounter/{s['fhir_id']} could not be read from the server")
            continue
        prac_ref = None
        if s["provider"] is None:                                          # R-EL01
            log(s["legacy_id"], s["source_row"], "R-EL01", "Flag", "No PROVIDER in the legacy encounter")
        elif s["provider"] not in practitioners:                           # R-EL02
            log(s["legacy_id"], s["source_row"], "R-EL02", "Flag",
                f"PROVIDER {s['provider']} was not migrated as a Practitioner")
        else:
            prac_ref = f"Practitioner/{practitioners[s['provider']]}"
        updated, version = add_links(enc, prac_ref, loc_ref, org_ref)
        updates.append((s, updated, version))
        expected.append({"legacy_encounter_id": s["legacy_id"], "encounter_fhir_id": s["fhir_id"],
                         **flatten_links(updated)})

    with_prac = sum(1 for _, u, _ in updates if flatten_links(u)["practitioner_ref"])
    errors = sum(1 for i in issues if i["action"] == "Error")
    ok = len(scope) == len(updates) + errors
    print("\n2. MODIFY")
    print(f"   Encounters {len(scope)} = to update {len(updates)} + could not read {errors}   "
          f"{'PASS' if ok else 'FAIL'}")
    print(f"   With a practitioner link: {with_prac}   Location/organization only: {len(updates) - with_prac}")
    print(f"   Already linked to a practitioner before this run: {already}")
    for rule in ("R-EL01", "R-EL02", "R-EL03"):
        n = sum(1 for i in issues if i["rule_id"] == rule)
        if n:
            print(f"   {rule}: {n}")
    return updates, issues, expected


# ---------------------------------------------------------------------------
# 3. WRITE
# ---------------------------------------------------------------------------
def write(updates, limit=None):
    chunks = [updates[i:i + BUNDLE_SIZE] for i in range(0, len(updates), BUNDLE_SIZE)]
    if limit:
        chunks = chunks[:limit]
    print(f"\n3. WRITE   {len(chunks)} bundle(s) to {FHIR_SERVER}")
    load_log, sent, confirmed = [], 0, 0
    for n, chunk in enumerate(chunks, 1):
        entries = []
        for s, resource, version in chunk:
            request = {"method": "PUT", "url": f"Encounter/{s['fhir_id']}"}
            if version:
                request["ifMatch"] = f'W/"{version}"'
            entries.append({"fullUrl": f"{FHIR_SERVER}/Encounter/{s['fhir_id']}",
                            "resource": resource, "request": request})
        sent += len(entries)
        started = time.time()
        resp, attempts, error = post_bundle({"resourceType": "Bundle", "type": "transaction", "entry": entries})
        log = {"bundle_no": n, "entries": len(entries), "attempts": attempts,
               "seconds": round(time.time() - started, 2),
               "http_status": resp.status_code if resp is not None else None, "error": error}
        reply = resp.json().get("entry", []) if resp is not None and resp.status_code == 200 else []
        if len(reply) == len(entries):
            ok = sum(1 for r in reply if r.get("response", {}).get("status", "").startswith("200"))
            confirmed += ok
            log["outcome"] = "OK"
            if n % 10 == 1 or n == len(chunks):
                print(f"   bundle {n}/{len(chunks)}: OK  {ok} updated  {log['seconds']}s")
        else:
            log["outcome"] = "FAILED"
            if resp is not None and not log["error"]:
                log["error"] = resp.text[:450]
            print(f"   bundle {n}: FAILED ({log['http_status']}) {str(log['error'])[:150]}")
            if resp is not None and resp.status_code == 412:
                print("      412 = an encounter changed since it was read. Re-run the script to read it again.")
        load_log.append(log)
        time.sleep(PAUSE_BETWEEN_BUNDLES)
    print(f"   Sent {sent}, confirmed updated {confirmed}   {'PASS' if confirmed == sent else 'FAIL'}")
    save_table("etl.encounter_link_load_log", load_log,
               ["bundle_no", "entries", "http_status", "outcome", "attempts", "seconds", "error"])


# ---------------------------------------------------------------------------
# 4. EXTRACT
# ---------------------------------------------------------------------------
def extract(scope):
    print("\n4. EXTRACT")
    legacy_by_fhir = {s["fhir_id"]: s["legacy_id"] for s in scope}
    server = read_encounters(set(legacy_by_fhir))
    rows = [{"legacy_encounter_id": legacy_by_fhir[fid], "encounter_fhir_id": fid, **flatten_links(r)}
            for fid, r in server.items()]
    save_table("tgt.encounter_links", rows, LINK_COLS)
    write_csv(OUTPUT_DIR / "target_encounter_links.csv", rows, LINK_COLS)
    linked = sum(1 for r in rows if r["practitioner_ref"])
    print(f"   On server: {len(rows)} encounters, {linked} linked to a practitioner")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Phase 2: link encounters to practitioner and clinic")
    parser.add_argument("--limit", type=int, help="only send the first N bundles (try 1 first)")
    args = parser.parse_args()

    scope, practitioners, org_ref, loc_ref = read_scope()
    server = read_encounters({s["fhir_id"] for s in scope})
    updates, issues, expected = build_updates(scope, server, practitioners, org_ref, loc_ref)
    save_table("etl.encounter_link_issue_log", issues, ISSUE_COLS)
    write_csv(OUTPUT_DIR / "issue_log.csv", issues, ISSUE_COLS)

    sent_ids = None
    if args.limit:
        sent_ids = {s["fhir_id"] for s, _, _ in updates[:args.limit * BUNDLE_SIZE]}
    # Expected = what each encounter should hold after this run (only the ones actually sent)
    save_table("etl.encounter_link_expected",
               [e for e in expected if sent_ids is None or e["encounter_fhir_id"] in sent_ids], LINK_COLS)

    write(updates, args.limit)
    extract(scope)
    print("\nNext: open sql\\22_encounter_link_reconciliation.sql in SSMS and press F5.")


if __name__ == "__main__":
    main()
