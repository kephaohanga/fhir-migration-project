r"""
list_lab_tests.py
Lists EVERY distinct lab test (LOINC code) in observations.csv, so each one can be assigned
to its lab panel in docs/lab_panel_map.csv (the next step).

Run:  python src\list_lab_tests.py
Read-only: it prints a list and changes no file.
"""
import pandas as pd

obs = pd.read_csv("data/legacy/observations.csv", dtype=str, keep_default_na=False)
lab = obs[obs["CATEGORY"] == "laboratory"].copy()
lab["group"] = lab["ENCOUNTER"] + "|" + lab["DATE"]

tests = (lab.groupby("CODE")
            .agg(description=("DESCRIPTION", "first"), unit=("UNITS", "first"),
                 results=("CODE", "size"), groups=("group", "nunique"))
            .sort_values(["groups", "description"], ascending=[False, True]))
print(f"{len(tests)} distinct lab tests in {lab['group'].nunique()} visit+time groups\n")
print(f"{'CODE':<9} {'GROUPS':>6} {'RESULTS':>7}  {'UNIT':<14} DESCRIPTION")
for code, t in tests.iterrows():
    print(f"{code:<9} {t['groups']:>6} {t['results']:>7}  {t['unit'][:14]:<14} {t['description']}")
