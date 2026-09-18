#!/usr/bin/env python3

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
POSTINGS_DIR = ROOT / "json"
ONET_DIR = ROOT / "onet"


def main() -> None:
    soc_postings: dict[str, list[Path]] = defaultdict(list)
    missing_soc: list[Path] = []

    posting_files = sorted(POSTINGS_DIR.glob("*/*.json"))

    for path in posting_files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"ERROR: Could not read {path}: {exc}")
            continue

        soc_code = data.get("socCode")

        if not soc_code:
            missing_soc.append(path)
            continue

        soc_postings[str(soc_code)].append(path)

    soc_codes = sorted(soc_postings)

    missing_onet = [
        soc_code
        for soc_code in soc_codes
        if not (ONET_DIR / f"{soc_code}.json").is_file()
    ]

    print(f"Posting JSON files: {len(posting_files)}")
    print(f"Unique SOC codes:    {len(soc_codes)}")
    print(f"O*NET files present: {len(soc_codes) - len(missing_onet)}")
    print(f"O*NET files missing: {len(missing_onet)}")
    print(f"Postings without SOC: {len(missing_soc)}")

    if missing_soc:
        print("\nPostings without a SOC code:")
        for path in missing_soc:
            print(f"  {path.relative_to(ROOT)}")

    if missing_onet:
        print("\nMissing O*NET files:")

        for soc_code in missing_onet:
            print(f"\n  {soc_code}")
            for path in soc_postings[soc_code]:
                print(f"    {path.relative_to(ROOT)}")

        print("\nFetch all missing O*NET occupations with:")
        print()
        print(
            "poetry run python scripts/fetch_onet_occupation.py "
            + " ".join(missing_onet)
        )
    else:
        print("\nAll posting SOC codes have corresponding O*NET files.")


if __name__ == "__main__":
    main()
