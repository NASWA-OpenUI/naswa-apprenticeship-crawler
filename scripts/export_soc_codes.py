from __future__ import annotations

import csv
from pathlib import Path

OES_CSV_PATH = Path("oesdata/oesdata.csv")
OUTPUT_CSV_PATH = Path("oesdata/soccodes.csv")
TARGET_AREA_NAME = "New York State"


def main() -> None:
    """Export SOCCODE and SOCTITLE rows for New York State from the OES data."""
    if not OES_CSV_PATH.exists():
        raise FileNotFoundError(f"OES CSV not found: {OES_CSV_PATH}")

    OUTPUT_CSV_PATH.parent.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, str]] = []

    with OES_CSV_PATH.open("r", encoding="utf-8-sig", newline="") as input_file:
        reader = csv.DictReader(input_file)

        # Some OES headers have leading spaces, like " SOCCODE".
        # Strip headers once so the rest of the code can use clean names.
        if reader.fieldnames:
            reader.fieldnames = [field.strip() for field in reader.fieldnames]

        for row in reader:
            area_name = row.get("AREANAME", "").strip()

            if area_name != TARGET_AREA_NAME:
                continue

            soc_code = row.get("SOCCODE", "").strip()
            soc_title = row.get("SOCTITLE", "").strip()

            if not soc_code:
                continue

            rows.append(
                {
                    "SOCCODE": soc_code,
                    "SOCTITLE": soc_title,
                }
            )

    rows.sort(key=lambda row: row["SOCCODE"])

    with OUTPUT_CSV_PATH.open("w", encoding="utf-8", newline="") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=["SOCCODE", "SOCTITLE"])
        writer.writeheader()
        writer.writerows(rows)

    print(f"Wrote {len(rows)} SOC codes to {OUTPUT_CSV_PATH}")


if __name__ == "__main__":
    main()
