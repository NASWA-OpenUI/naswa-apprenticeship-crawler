from __future__ import annotations

from pathlib import Path


PROGRAM_JSON_DIR = Path("./programs/json")
ONET_DIR = Path("./onet")


def soc_codes_in(directory: Path) -> set[str]:
    """Return SOC codes represented by JSON filenames in a directory."""
    return {
        path.stem
        for path in directory.glob("*.json")
        if path.is_file()
    }


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

    missing_soc_codes = sorted(
        program_soc_codes - onet_soc_codes
    )

    available_soc_codes = (
        program_soc_codes & onet_soc_codes
    )

    print("Program O*NET audit:")
    print("")
    print(
        f"  Program SOC codes:             "
        f"{len(program_soc_codes)}"
    )
    print(
        f"  Already available in onet/:    "
        f"{len(available_soc_codes)}"
    )
    print(
        f"  Missing O*NET profiles:        "
        f"{len(missing_soc_codes)}"
    )

    if missing_soc_codes:
        print("")
        print("Missing O*NET profiles:")

        for soc_code in missing_soc_codes:
            print(f"  {soc_code}")
    else:
        print("")
        print("All program SOC codes have O*NET profiles.")


if __name__ == "__main__":
    main()