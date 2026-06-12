from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

POSTINGS_ROOT = Path("json/gpt-5.4-mini/medium")
OES_CSV_PATH = Path("oesdata/oesdata.csv")
ONET_ROOT = Path("onet")
OUTPUT_ROOT = Path("out")

ONET_DETAIL_KEYS = [
    "tasks",
    "detailed_work_activities",
    "skills",
    "abilities",
    "work_styles",
]


def normalize_soc_code_for_oes(soc_code: str) -> str:
    """Convert an O*NET-SOC code like '51-7011.00' to an OES SOC code like '51-7011'."""
    return soc_code.strip().split(".")[0]


def parse_int(value: str) -> int | None:
    """Parse an integer field from CSV, returning None for blank or invalid values."""
    value = value.strip()
    if not value:
        return None

    try:
        return int(value)
    except ValueError:
        return None


def parse_float(value: str) -> float | None:
    """Parse a float field from CSV, returning None for blank or invalid values."""
    value = value.strip()
    if not value:
        return None

    try:
        return float(value)
    except ValueError:
        return None


def load_oes_rows(csv_path: Path) -> dict[str, dict[str, Any]]:
    """Load OES wage rows keyed by normalized SOC code."""
    if not csv_path.exists():
        raise FileNotFoundError(f"OES CSV not found: {csv_path}")

    rows_by_soc_code: dict[str, dict[str, Any]] = {}

    with csv_path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)

        if reader.fieldnames:
            reader.fieldnames = [field.strip() for field in reader.fieldnames]

        for row in reader:
            soc_code = row.get("SOCCODE", "").strip()
            if not soc_code:
                continue

            rows_by_soc_code[soc_code] = {
                "areaType": row.get("AREATYPE", "").strip(),
                "area": row.get("AREA", "").strip(),
                "areaName": row.get("AREANAME", "").strip(),
                "socCode": soc_code,
                "socTitle": row.get("SOCTITLE", "").strip(),
                "socDescription": row.get("SOCDESC", "").strip(),
                "employment": parse_int(row.get("EMPLOYMENT", "")),
                "meanAnnualWage": parse_float(row.get("MEAN", "")),
                "medianAnnualWage": parse_float(row.get("MEDIAN", "")),
                "entryAnnualWage": parse_float(row.get("ENTRYWG", "")),
                "experiencedAnnualWage": parse_float(row.get("EXPERIENCE", "")),
            }

    return rows_by_soc_code


def load_onet_data(onet_root: Path, soc_code: str) -> dict[str, Any] | None:
    """Load selected O*NET sections for a SOC code, preserving each selected section as-is."""
    onet_path = onet_root / f"{soc_code}.json"

    if not onet_path.exists():
        return None

    with onet_path.open("r", encoding="utf-8") as file:
        source_data = json.load(file)

    details = source_data.get("details", {})

    onet_data: dict[str, Any] = {
        "socCode": source_data.get("socCode", ""),
        "onetSocCode": source_data.get("onetSocCode", ""),
        "title": source_data.get("title", ""),
        "description": source_data.get("description", ""),
        "fetchedAt": source_data.get("fetchedAt", ""),
        "source": source_data.get("source"),
    }

    for key in ONET_DETAIL_KEYS:
        onet_data[key] = details.get(key)

    return onet_data


def find_posting_files(postings_root: Path) -> list[Path]:
    """Find all JSON posting files inside directories under the postings root."""
    if not postings_root.exists():
        raise FileNotFoundError(f"Postings root not found: {postings_root}")

    return sorted(path for path in postings_root.glob("*/*.json") if path.is_file())


def build_merged_posting(
    posting: dict[str, Any],
    oes_rows_by_soc_code: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Build one merged posting record from the original posting plus OES and O*NET data."""
    posting_id = posting.get("id", "").strip()
    if not posting_id:
        raise ValueError("Posting is missing required 'id' field.")

    soc_code = str(posting.get("socCode") or "").strip()

    merged: dict[str, Any] = {
        "id": posting_id,
        "socCode": soc_code,
        "posting": posting,
        "oes": None,
        "onet": None,
    }

    if not soc_code:
        return merged

    oes_soc_code = normalize_soc_code_for_oes(soc_code)
    merged["oes"] = oes_rows_by_soc_code.get(oes_soc_code)
    merged["onet"] = load_onet_data(ONET_ROOT, soc_code)

    return merged


def write_json(output_path: Path, data: dict[str, Any]) -> None:
    """Write a dictionary to a pretty-printed JSON file."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, ensure_ascii=False)
        file.write("\n")


def print_missing_section(
    *,
    title: str,
    rows: list[dict[str, str]],
    include_oes_soc_code: bool = False,
) -> None:
    """Print a readable list of missing merge inputs."""
    if not rows:
        return

    print()
    print(title)
    print("-" * len(title))

    for row in rows:
        print(f"- {row['path']}")
        print(f"  id:        {row['id']}")
        print(f"  jobTitle:  {row['jobTitle']}")
        print(f"  sourceUrl: {row['sourceUrl']}")
        print(f"  socCode:   {row['socCode'] or '<empty>'}")

        if include_oes_soc_code:
            print(f"  OES lookup code: {row['oesSocCode'] or '<empty>'}")

        print()

def main() -> None:
    """Merge posting JSON files with OES wage data and selected O*NET sections."""
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

    oes_rows_by_soc_code = load_oes_rows(OES_CSV_PATH)
    posting_files = find_posting_files(POSTINGS_ROOT)

    created_count = 0

    missing_soc_code: list[dict[str, str]] = []
    missing_oes: list[dict[str, str]] = []
    missing_onet: list[dict[str, str]] = []

    for posting_path in posting_files:
        with posting_path.open("r", encoding="utf-8") as file:
            posting = json.load(file)

        merged = build_merged_posting(posting, oes_rows_by_soc_code)

        posting_info = {
            "path": str(posting_path),
            "id": str(posting.get("id") or ""),
            "jobTitle": str(posting.get("jobTitle") or ""),
            "sourceUrl": str(posting.get("sourceUrl") or ""),
            "socCode": str(merged.get("socCode") or ""),
            "oesSocCode": normalize_soc_code_for_oes(str(merged.get("socCode") or "")),
        }

        if not merged["socCode"]:
            missing_soc_code.append(posting_info)
        elif merged["oes"] is None:
            missing_oes.append(posting_info)

        if merged["socCode"] and merged["onet"] is None:
            missing_onet.append(posting_info)

        output_path = OUTPUT_ROOT / f"{merged['id']}.json"
        write_json(output_path, merged)

        created_count += 1

    print(f"Created {created_count} merged posting files in {OUTPUT_ROOT}/")
    print(f"Postings without SOC code: {len(missing_soc_code)}")
    print(f"Postings with SOC code but no OES match: {len(missing_oes)}")
    print(f"Postings with SOC code but no O*NET file: {len(missing_onet)}")

    print_missing_section(
        title="Postings without SOC code",
        rows=missing_soc_code,
    )

    print_missing_section(
        title="Postings with SOC code but no OES match",
        rows=missing_oes,
        include_oes_soc_code=True,
    )

    print_missing_section(
        title="Postings with SOC code but no O*NET file",
        rows=missing_onet,
    )


if __name__ == "__main__":
    main()
