from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# -----------------------------
# Manually tweak this for now
# -----------------------------

# JSON_FOLDER = PROJECT_ROOT / "json/gpt-5.4-mini/medium"
JSON_FOLDER = PROJECT_ROOT / "json/manual"
OUTPUT_FILENAME = "job-listings.csv"


def csv_safe_value(value: Any) -> str:
    """
    Convert JSON values into CSV-safe values.

    - None becomes an empty string
    - lists/dicts become compact JSON strings
    - everything else becomes a plain string/value
    """
    if value is None:
        return ""

    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)

    return value


def load_json_entries(json_folder: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []

    for json_path in sorted(json_folder.glob("*.json")):
        with json_path.open("r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, dict):
            entries.append(data)
        elif isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    raise ValueError(
                        f"{json_path} contains a list item that is not a JSON object."
                    )
                entries.append(item)
        else:
            raise ValueError(
                f"{json_path} must contain a JSON object or a list of JSON objects."
            )

    return entries


def get_fieldnames(entries: list[dict[str, Any]]) -> list[str]:
    """
    Build CSV columns from all JSON keys, preserving first-seen order.
    """
    fieldnames: list[str] = []

    for entry in entries:
        for key in entry.keys():
            if key not in fieldnames:
                fieldnames.append(key)

    return fieldnames


def write_csv(entries: list[dict[str, Any]], output_path: Path) -> None:
    fieldnames = get_fieldnames(entries)

    with output_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for entry in entries:
            row = {
                fieldname: csv_safe_value(entry.get(fieldname))
                for fieldname in fieldnames
            }
            writer.writerow(row)


def main() -> None:
    if not JSON_FOLDER.exists():
        raise FileNotFoundError(f"Folder does not exist: {JSON_FOLDER}")

    if not JSON_FOLDER.is_dir():
        raise NotADirectoryError(f"Path is not a folder: {JSON_FOLDER}")

    entries = load_json_entries(JSON_FOLDER)

    if not entries:
        print(f"No JSON entries found in {JSON_FOLDER}")
        return

    output_path = JSON_FOLDER / OUTPUT_FILENAME
    write_csv(entries, output_path)

    print(f"Wrote {len(entries)} rows to {output_path}")


if __name__ == "__main__":
    main()
