from __future__ import annotations

import argparse
import csv
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

POSTINGS_ROOT = Path("json")
SOC_CODES_CSV_PATH = Path("oesdata/postings-soc-codes.csv")

SEPARATOR = "-" * 80


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit or apply SOC codes from postings-soc-codes.csv to extracted posting JSON files."
    )
    parser.add_argument(
        "--mode",
        choices=["missing", "audit", "apply"],
        default="missing",
        help=(
            "missing: list JSON files without socCode. "
            "audit: print null/missing socCodes and disagreements with CSV. "
            "apply: replace null/empty socCode values using CSV for single-file folders."
        ),
    )

    args = parser.parse_args()

    posting_files = find_posting_files(POSTINGS_ROOT)
    folders = group_by_parent(posting_files)

    if args.mode == "missing":
        print_missing_soc_codes(posting_files)
        return

    soc_rows_by_url = load_soc_rows_by_url(SOC_CODES_CSV_PATH)

    skipped_multi_file_count = 0
    checked_count = 0
    updated_count = 0
    warning_count = 0
    no_csv_match_count = 0
    ambiguous_csv_match_count = 0

    for folder, files in sorted(folders.items()):
        if len(files) != 1:
            skipped_multi_file_count += len(files)
            print_multi_file_folder(folder, files)
            continue

        posting_path = files[0]
        posting = load_json(posting_path)

        checked_count += 1

        source_url = str(posting.get("sourceUrl") or "").strip()
        existing_soc_code = normalize_existing_value(posting.get("socCode"))

        csv_matches = soc_rows_by_url.get(normalize_url(source_url), [])

        if not csv_matches:
            no_csv_match_count += 1

            if is_blank_soc_code(existing_soc_code):
                print_record(
                    posting_path=posting_path,
                    source_url=source_url,
                    json_soc_code=existing_soc_code,
                    csv_soc_code="",
                    message="Missing JSON socCode, but no matching CSV row was found.",
                )

            continue

        if len(csv_matches) > 1:
            ambiguous_csv_match_count += 1

            print_record(
                posting_path=posting_path,
                source_url=source_url,
                json_soc_code=existing_soc_code,
                csv_soc_code=" | ".join(row["ONETSOC_CODE"] for row in csv_matches),
                message="Multiple CSV rows matched this URL. Skipping for manual reconciliation.",
            )
            continue

        csv_row = csv_matches[0]
        csv_soc_code = csv_row["ONETSOC_CODE"]

        if is_blank_soc_code(existing_soc_code):
            print_record(
                posting_path=posting_path,
                source_url=source_url,
                json_soc_code=existing_soc_code,
                csv_soc_code=csv_soc_code,
                message="Missing JSON socCode.",
            )

            if args.mode == "apply":
                posting["socCode"] = csv_soc_code
                write_json(posting_path, posting)
                updated_count += 1
                print(f"UPDATED: {posting_path}")
                print(SEPARATOR)

            continue

        if not soc_codes_agree(existing_soc_code, csv_soc_code):
            warning_count += 1

            print_record(
                posting_path=posting_path,
                source_url=source_url,
                json_soc_code=existing_soc_code,
                csv_soc_code=csv_soc_code,
                message="WARNING: JSON socCode does not agree with CSV socCode. Not overwriting.",
            )
            continue

    print()
    print("Done.")
    print(f"Mode:                         {args.mode}")
    print(f"Posting JSON files found:     {len(posting_files)}")
    print(f"Single-file folders checked:  {checked_count}")
    print(f"Multi-file postings skipped:  {skipped_multi_file_count}")
    print(f"No CSV match:                 {no_csv_match_count}")
    print(f"Ambiguous CSV URL matches:    {ambiguous_csv_match_count}")
    print(f"Disagreement warnings:        {warning_count}")
    print(f"Updated JSON files:           {updated_count}")

    print()
    print_missing_soc_codes(posting_files)


def find_posting_files(postings_root: Path) -> list[Path]:
    if not postings_root.exists():
        raise FileNotFoundError(f"Postings root not found: {postings_root}")

    return sorted(path for path in postings_root.glob("*/*.json") if path.is_file())


def group_by_parent(paths: list[Path]) -> dict[Path, list[Path]]:
    grouped: dict[Path, list[Path]] = defaultdict(list)

    for path in paths:
        grouped[path.parent].append(path)

    return dict(grouped)


def load_soc_rows_by_url(csv_path: Path) -> dict[str, list[dict[str, str]]]:
    if not csv_path.exists():
        raise FileNotFoundError(f"SOC CSV not found: {csv_path}")

    rows_by_url: dict[str, list[dict[str, str]]] = defaultdict(list)

    with csv_path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)

        if reader.fieldnames:
            reader.fieldnames = [field.strip() for field in reader.fieldnames]

        for row in reader:
            url = str(row.get("URL") or "").strip()
            soc_code = str(row.get("ONETSOC_CODE") or "").strip()
            soc_title = str(row.get("ONETSOC_TITLE") or "").strip()

            if not url or not soc_code:
                continue

            rows_by_url[normalize_url(url)].append(
                {
                    "URL": url,
                    "ONETSOC_CODE": soc_code,
                    "ONETSOC_TITLE": soc_title,
                }
            )

    return dict(rows_by_url)


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)

    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object in {path}")

    return data


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def normalize_url(url: str) -> str:
    return url.strip().rstrip("/")


def normalize_existing_value(value: Any) -> str:
    if value is None:
        return ""

    return str(value).strip()


def is_blank_soc_code(value: str) -> bool:
    return not value.strip()


def normalize_soc_code_for_comparison(code: str) -> str:
    """
    Compare 47-2021 and 47-2021.00 as the same occupation.

    The script still writes the full CSV ONETSOC_CODE value, such as 47-2021.00.
    """
    code = code.strip()

    match = re.fullmatch(r"(\d{2}-\d{4})(?:\.\d{2})?", code)
    if match:
        return match.group(1)

    return code


def soc_codes_agree(json_soc_code: str, csv_soc_code: str) -> bool:
    return (
        normalize_soc_code_for_comparison(json_soc_code)
        == normalize_soc_code_for_comparison(csv_soc_code)
    )


def print_multi_file_folder(folder: Path, files: list[Path]) -> None:
    print(f"SKIPPING MULTI-FILE POSTING FOLDER: {folder}")
    for path in sorted(files):
        print(f"  - {path.name}")
    print(SEPARATOR)


def print_record(
    *,
    posting_path: Path,
    source_url: str,
    json_soc_code: str,
    csv_soc_code: str,
    message: str,
) -> None:
    print(posting_path)
    print(f"sourceUrl:   {source_url}")
    print(f"JSON socCode: {json_soc_code or '<empty>'}")
    print(f"CSV socCode:  {csv_soc_code or '<none>'}")
    print(message)
    print(SEPARATOR)


def print_missing_soc_codes(posting_files: list[Path]) -> None:
    missing = []

    for posting_path in posting_files:
        posting = load_json(posting_path)
        soc_code = normalize_existing_value(posting.get("socCode"))

        if is_blank_soc_code(soc_code):
            missing.append(posting_path)

    print("JSON files without socCode:")
    if not missing:
        print("None.")
        return

    for path in missing:
        print(f"- {path}")

    print()
    print(f"Total without socCode: {len(missing)}")


if __name__ == "__main__":
    main()