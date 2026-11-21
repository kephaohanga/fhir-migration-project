r"""
profile_medication_report.py
Phase 3, steps 4 and 5 (before mapping): profile the data behind two NEW resources that
are built from records already migrated in Phase 2.

  Medication       one record per DRUG (e.g. "Epogen 4000 UNT/ML injection"), built from
                   the distinct drugs in medications.csv. Prescriptions can then point to it.
  DiagnosticReport one report per lab PANEL (e.g. a lipid panel holding 4 results), built by
                   grouping the lab results in observations.csv.

Run:  python src\profile_medication_report.py
Read-only: it prints counts and changes no file.
"""
import re
from collections import Counter

import pandas as pd

LEGACY = "data/legacy/"


def read(name):
    return pd.read_csv(LEGACY + name, dtype=str, keep_default_na=False)


# =============================================================================
# PART A - MEDICATION: the drug catalogue hidden in medications.csv
# =============================================================================
med = read("medications.csv")
print("=" * 78)
print(f"A. MEDICATION   medications.csv: {len(med)} prescriptions, {med['PATIENT'].nunique()} patients")
print("=" * 78)

codes = med.groupby("CODE").agg(descriptions=("DESCRIPTION", "nunique"),
                                description=("DESCRIPTION", "first"),
                                prescriptions=("DESCRIPTION", "size"),
                                patients=("PATIENT", "nunique"))
print(f"\n1. Distinct drugs (RxNorm CODE): {len(codes)}")
print(f"   blank CODE: {(med['CODE'].str.strip() == '').sum()}   "
      f"not all digits: {(~med['CODE'].str.fullmatch(r'\d+')).sum()}")
print(f"   codes with MORE THAN ONE description: {(codes['descriptions'] > 1).sum()}")
for code, row in codes[codes["descriptions"] > 1].iterrows():
    print(f"      {code}: " + " | ".join(sorted(med.loc[med['CODE'] == code, 'DESCRIPTION'].unique()))[:150])
dup_desc = med.groupby("DESCRIPTION")["CODE"].nunique()
print(f"   descriptions with MORE THAN ONE code: {(dup_desc > 1).sum()}")

print("\n2. The 15 most prescribed drugs")
for code, row in codes.sort_values("prescriptions", ascending=False).head(15).iterrows():
    print(f"   {code:<9} {row['prescriptions']:>5} rx {row['patients']:>3} pts  {row['description'][:70]}")

print("\n3. What the descriptions contain (RxNorm naming: ingredient, strength, dose form, [brand])")
desc = codes["description"]
print(f"   with a brand name in [brackets]:      {desc.str.contains(r'\[').sum()}")
print(f"   packs  {{...}} Pack:                   {desc.str.contains(r'Pack', case=False).sum()}")
STRENGTH = r"\d+(?:\.\d+)?\s*(?:MG|MCG|UNT|ML|ACTUAT|%)"
print(f"   with a strength (MG, UNT, ML, MCG...): {desc.str.contains(STRENGTH, case=False).sum()}")
FORMS = ["Oral Tablet", "Oral Capsule", "Chewable Tablet", "Extended Release Oral Tablet", "Oral Solution",
         "Oral Suspension", "Injection", "Injectable Suspension", "Prefilled Syringe", "Auto-Injector",
         "Inhaler", "Inhalation", "Nasal Spray", "Topical", "Transdermal", "Oral Gel", "Pack", "Vaginal",
         "Ophthalmic", "Rectal", "Medicated Patch"]
found = Counter()
for d in desc:
    hit = next((f for f in FORMS if f.lower() in d.lower()), "(other)")
    found[hit] += 1
print("   dose form found by keyword (first match):")
for form, n in found.most_common():
    print(f"      {form:<30} {n}")
print("   drugs with no recognized dose form:")
for d in desc[[next((f for f in FORMS if f.lower() in x.lower()), None) is None for x in desc]].head(10):
    print(f"      {d[:100]}")

# =============================================================================
# PART B - DIAGNOSTICREPORT: lab results grouped into panels
# =============================================================================
obs = read("observations.csv")
print("\n" + "=" * 78)
print(f"B. DIAGNOSTICREPORT   observations.csv: {len(obs)} results")
print("=" * 78)
print("\n1. Results by CATEGORY")
for cat, n in obs["CATEGORY"].replace("", "(blank)").value_counts().items():
    print(f"   {cat:<16} {n}")

lab = obs[obs["CATEGORY"] == "laboratory"].copy()
lab["group"] = lab["ENCOUNTER"] + "|" + lab["DATE"]
groups = lab.groupby("group")
print(f"\n2. Lab results: {len(lab)}  grouped by visit + date-time: {groups.ngroups} groups")
print(f"   lab results with a blank ENCOUNTER: {(lab['ENCOUNTER'] == '').sum()}")
sizes = groups.size().value_counts().sort_index()
print("   results per group: " + ", ".join(f"{k} x{v}" for k, v in sizes.items()))
same_day = lab.assign(day=lab["DATE"].str[:10]).groupby(["ENCOUNTER", "day"])["DATE"].nunique()
print(f"   visits with lab results at MORE THAN ONE time on the same day: {(same_day > 1).sum()}")

print("\n3. The 12 most common sets of tests done together (these are the panels)")
sets = Counter(tuple(sorted(g["CODE"])) for _, g in groups)
names = dict(zip(lab["CODE"], lab["DESCRIPTION"]))
for n, (combo, count) in enumerate(sets.most_common(12), 1):
    print(f"   #{n:<2} {count:>5} groups, {len(combo):>2} tests: "
          + "; ".join(f"{c} {names[c][:28]}" for c in combo[:6]) + (" ..." if len(combo) > 6 else ""))
print(f"   distinct test sets: {len(sets)}   groups covered by the top 12: "
      f"{sum(c for _, c in sets.most_common(12))} of {groups.ngroups}")

print("\n4. Lab tests (LOINC) by how many groups they appear in - top 25")
appears = lab.drop_duplicates(["group", "CODE"])["CODE"].value_counts()
for code, n in appears.head(25).items():
    print(f"   {code:<9} {n:>5}  {names[code][:70]}")
