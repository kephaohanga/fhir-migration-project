"""
plant_provider_errors.py
------------------------
Phase 2 test data: plant known, realistic defects in the legacy provider file.

Two versions of the same provider list are used, as in a real clinic:
  data/legacy/providers.csv        = the CREDENTIALING ROSTER (clean, the source of truth
                                     for provider names - copied from Synthea)
  data/legacy_dirty/providers.csv  = the LEGACY EHR EXPORT (what this script writes),
                                     with the defects below planted in it

Planted defects (fixed seed, so the same ones every run):
  3 x misspelt name        (two letters swapped, a letter missing, a letter doubled)
                           -> expected: R-PR02 Fix, name corrected from the roster
  1 x wrong name           (another provider's name under this Id)
                           -> expected: R-PR02 Reject, sent to Credentialing for review
  1 x duplicate row        -> expected: R-PR04 Fix, first copy kept
  1 x invalid gender 'X'   -> expected: R-PR03 Flag, gender omitted
  1 x broken NPI (9 digits) -> expected: R-PR06 Flag, NPI omitted

Output: data/legacy_dirty/providers.csv
        docs/planted_provider_errors_answer_key.csv
Run from the project folder:  python src\\plant_provider_errors.py
"""

import csv
import random
from pathlib import Path

import pandas as pd

ROSTER_FILE = Path("data/legacy/providers.csv")
DIRTY_FILE = Path("data/legacy_dirty/providers.csv")
ANSWER_KEY = Path("docs/planted_provider_errors_answer_key.csv")
SEED = 2026

rng = random.Random(SEED)
answer_key = []


def misspell(name, kind):
    """Change one word of the name so it looks like a typing error."""
    words = name.split()
    # pick a word with at least 4 letters, so the change stays realistic
    candidates = [i for i, w in enumerate(words) if sum(ch.isalpha() for ch in w) >= 4]
    i = rng.choice(candidates)
    letters = [k for k, ch in enumerate(words[i]) if ch.isalpha()]
    w = list(words[i])
    if kind == "swap":                       # 'Mayer' -> 'Maeyr'
        k = rng.choice(letters[1:-1])
        w[k], w[k + 1] = w[k + 1], w[k]
    elif kind == "drop":                     # 'Mayer' -> 'Myer'
        k = rng.choice(letters[1:])
        del w[k]
    else:                                    # 'Mayer' -> 'Mayyer'
        k = rng.choice(letters[1:])
        w.insert(k, w[k])
    words[i] = "".join(w)
    return " ".join(words)


def main():
    df = pd.read_csv(ROSTER_FILE, dtype=str, keep_default_na=False)
    rows = rng.sample(range(len(df)), 6)      # six different providers

    # 1-3. Misspelt names
    for row, kind in zip(rows[:3], ("swap", "drop", "double")):
        original = df.at[row, "NAME"]
        planted = misspell(original, kind)
        df.at[row, "NAME"] = planted
        answer_key.append({"record_id": df.at[row, "Id"], "error_type": f"misspelt_name_{kind}",
                           "column": "NAME", "original": original, "planted": planted,
                           "expected_rule": "R-PR02", "expected_action": "Fix"})

    # 4. Wrong name: another provider's name under this Id
    row, other = rows[3], rows[4]
    original = df.at[row, "NAME"]
    df.at[row, "NAME"] = df.at[other, "NAME"]
    answer_key.append({"record_id": df.at[row, "Id"], "error_type": "wrong_name",
                       "column": "NAME", "original": original, "planted": df.at[other, "NAME"],
                       "expected_rule": "R-PR02", "expected_action": "Reject"})

    # 5. Invalid gender
    row = rows[5]
    answer_key.append({"record_id": df.at[row, "Id"], "error_type": "invalid_gender",
                       "column": "GENDER", "original": df.at[row, "GENDER"], "planted": "X",
                       "expected_rule": "R-PR03", "expected_action": "Flag"})
    df.at[row, "GENDER"] = "X"

    # 6. Duplicate row: an exact copy of a clean provider's row
    dup = df.loc[[rows[4]]]
    n_original = len(df)
    df = pd.concat([df, dup], ignore_index=True)
    answer_key.append({"record_id": dup.iloc[0]["Id"], "error_type": "duplicate_record",
                       "column": "(whole row)", "original": "", "planted": "row appears twice",
                       "expected_rule": "R-PR04", "expected_action": "Fix"})

    # 7. Broken NPI: last digit dropped (9 digits instead of 10).
    #    Picked last, so defects 1-6 stay the same as in earlier runs.
    if "NPI" in df.columns:
        row = rng.choice([i for i in range(n_original) if i not in rows])
        original = df.at[row, "NPI"]
        df.at[row, "NPI"] = original[:-1]
        answer_key.append({"record_id": df.at[row, "Id"], "error_type": "invalid_npi",
                           "column": "NPI", "original": original, "planted": original[:-1],
                           "expected_rule": "R-PR06", "expected_action": "Flag"})

    DIRTY_FILE.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(DIRTY_FILE, index=False)
    ANSWER_KEY.parent.mkdir(parents=True, exist_ok=True)
    with ANSWER_KEY.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(answer_key[0].keys()))
        writer.writeheader()
        writer.writerows(answer_key)

    print(f"Roster: {ROSTER_FILE} ({len(df) - 1} providers)")
    print(f"Legacy export with planted defects: {DIRTY_FILE} ({len(df)} rows)")
    for a in answer_key:
        print(f"  {a['error_type']:<22} {a['record_id'][:12]}...  {a['original']!r} -> {a['planted']!r}")
    print(f"Answer key: {ANSWER_KEY}")


if __name__ == "__main__":
    main()
