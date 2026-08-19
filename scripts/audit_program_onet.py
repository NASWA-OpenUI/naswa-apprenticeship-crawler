from __future__ import annotations

import csv
from pathlib import Path


PROGRAM_JSON_DIR = Path("./programs/json")
ONET_DIR = Path("./onet")
SOC_CODE_MAPPINGS_FILE = Path("./programs/soc-code-mappings.csv")


def soc_codes_in(directory: Path) -> set[str]:
    """Return SOC codes represented by JSON filenames in a directory."""
    return {
        path.stem
        for path in directory.glob("*.json")
        if path.is_file()
    }


def load_soc_code_mappings(path: Path) -> dict[str, str]:
    """Return source SOC code -> target SOC code mappings."""
    if not path.is_file():
        raise SystemExit(
            f"SOC code mappings file not found: {path}"
        )

    mappings: dict[str, str] = {}

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)

        required_columns = {
            "sourceSocCode",
            "targetSocCode",
        }

        if not reader.fieldnames:
            raise SystemExit(
                f"SOC code mappings file has no header row: {path}"
            )

        missing_columns = required_columns - set(reader.fieldnames)

        if missing_columns:
            raise SystemExit(
                "SOC code mappings file is missing required columns: "
                + ", ".join(sorted(missing_columns))
            )

        for row_number, row in enumerate(reader, start=2):
            source_soc_code = row["sourceSocCode"].strip()
            target_soc_code = row["targetSocCode"].strip()

            if not source_soc_code or not target_soc_code:
                raise SystemExit(
                    f"Invalid SOC code mapping on row {row_number}: "
                    "sourceSocCode and targetSocCode are required."
                )

            if source_soc_code in mappings:
                raise SystemExit(
                    f"Duplicate source SOC code mapping on row "
                    f"{row_number}: {source_soc_code}"
                )

            mappings[source_soc_code] = target_soc_code

    return mappings


def main() -> None:
    if not PROGRAM_JSON_DIR.is_dir():
        raise SystemExit(
            f"Program JSON directory not found: {PROGRAM_JSON_DIR}"
        )

    if not ONET_DIR.is_dir():
        raise SystemExit(
            f"O*NET directory not found: {ONET_DIR}"
        )

    program_soc_codes = soc_codes_in(PROGRAM_JSON_DIR)
    onet_soc_codes = soc_codes_in(ONET_DIR)
    soc_code_mappings = load_soc_code_mappings(
        SOC_CODE_MAPPINGS_FILE
    )

    directly_available_soc_codes = (
        program_soc_codes & onet_soc_codes
    )

    mapped_soc_codes: dict[str, str] = {}
    mapped_but_missing_soc_codes: dict[str, str] = {}

    for source_soc_code in program_soc_codes - onet_soc_codes:
        target_soc_code = soc_code_mappings.get(source_soc_code)

        if not target_soc_code:
            continue

        if target_soc_code in onet_soc_codes:
            mapped_soc_codes[source_soc_code] = target_soc_code
        else:
            mapped_but_missing_soc_codes[source_soc_code] = (
                target_soc_code
            )

    resolved_soc_codes = (
        directly_available_soc_codes
        | set(mapped_soc_codes)
    )

    missing_soc_codes = sorted(
        program_soc_codes - resolved_soc_codes
    )

    print("Program O*NET audit:")
    print("")
    print(
        f"  Program SOC codes:             "
        f"{len(program_soc_codes)}"
    )
    print(
        f"  Directly available in onet/:   "
        f"{len(directly_available_soc_codes)}"
    )
    print(
        f"  Resolved through SOC mapping:  "
        f"{len(mapped_soc_codes)}"
    )
    print(
        f"  Missing O*NET profiles:        "
        f"{len(missing_soc_codes)}"
    )

    if mapped_soc_codes:
        print("")
        print("Resolved through SOC code mappings:")

        for source_soc_code in sorted(mapped_soc_codes):
            target_soc_code = mapped_soc_codes[source_soc_code]
            print(
                f"  {source_soc_code} -> {target_soc_code}"
            )

    if mapped_but_missing_soc_codes:
        print("")
        print("Mapped SOC codes with missing target profiles:")

        for source_soc_code in sorted(
            mapped_but_missing_soc_codes
        ):
            target_soc_code = (
                mapped_but_missing_soc_codes[source_soc_code]
            )
            print(
                f"  {source_soc_code} -> {target_soc_code}"
            )

    if missing_soc_codes:
        print("")
        print("Missing O*NET profiles:")

        for soc_code in missing_soc_codes:
            print(f"  {soc_code}")
    else:
        print("")
        print(
            "All program SOC codes have O*NET profiles "
            "or valid replacement mappings."
        )


if __name__ == "__main__":
    main()
