from __future__ import annotations

import csv
import json
import re
import shutil
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


PROGRAMS_DIR = Path("./programs")
SOURCE_CSV_PATH = PROGRAMS_DIR / "ra-program-data.csv"
OUTPUT_DIR = PROGRAMS_DIR / "json"
TEMP_OUTPUT_DIR = PROGRAMS_DIR / ".json.tmp"
BACKUP_OUTPUT_DIR = PROGRAMS_DIR / ".json.backup"

SCHEMA_PATH = Path("./schemas/program-group.schema.json")
LABOR_MARKET_REGIONS_PATH = (
    Path("./data/locations/Labor_Market_Regions.csv")
)

SOURCE_FIELDNAMES = {
    "PROGRAM_AK",
    "SPONSOR_NAME",
    "TRADE_NAME",
    "ADDRESS_LINE1",
    "ADDRESS_LINE2",
    "ADDRESS_CITY",
    "ADDRESS_STATE",
    "ADDRESS_POSTAL_CODE",
    "SPONSOR_REDC_REGION",
    "PROGRAM_STATUS",
    "PROGRAM_APPROVAL_DATE",
    "PROGRAM_GROUP_TYPE",
    "PROGRAM_APPROACH",
    "PROGRAM_LENGTH",
    "RATIO",
    "ONET_SOC_CODE",
    "ONET_TITLE",
}

INCLUDED_STATUSES = {
    "Active",
    "Probation",
}

INACTIVE_STATUSES = {
    "Inactive",
    "Program Inactive",
}

SOC_CODE_PATTERN = re.compile(r"^\d{2}-\d{4}\.\d{2}$")

# The source uses REDC terminology, while the application uses the official
# labor-market-region names from Labor_Market_Regions.csv.
#
# Values already matching an official labor-market region do not need to
# appear here.
SOURCE_REGION_ALIASES = {
    "Capital": "Capital Region",
    "Central": "Central New York",
    "Mid-Hudson": "Hudson Valley",
}

EXCLUDED_REGIONS = {
    "Out of State",
}


def main() -> None:
    require_file(SOURCE_CSV_PATH, "program CSV")
    require_file(SCHEMA_PATH, "program-group JSON schema")
    require_file(
        LABOR_MARKET_REGIONS_PATH,
        "labor-market-region CSV",
    )

    schema = load_json_object(SCHEMA_PATH)
    validator = Draft202012Validator(schema)

    region_order = load_official_regions(
        LABOR_MARKET_REGIONS_PATH
    )
    official_regions = set(region_order)

    validate_region_aliases(official_regions)

    source_rows = load_source_rows(SOURCE_CSV_PATH)

    errors: list[str] = []
    soc_warnings: list[str] = []
    out_of_state_warnings: list[str] = []

    inactive_count = 0
    out_of_state_count = 0
    included_active_count = 0
    included_probation_count = 0

    programs: list[dict[str, Any]] = []
    seen_program_aks: dict[int, int] = {}

    for row_number, row in source_rows:
        status = clean_string(row.get("PROGRAM_STATUS"))

        if status in INACTIVE_STATUSES:
            inactive_count += 1
            continue

        if status not in INCLUDED_STATUSES:
            errors.append(
                f"CSV row {row_number}: unexpected PROGRAM_STATUS "
                f"{status!r}. Expected Active, Probation, or an "
                "inactive status."
            )
            continue

        source_region = clean_string(
            row.get("SPONSOR_REDC_REGION")
        )

        if source_region in EXCLUDED_REGIONS:
            out_of_state_count += 1

            program_ak = (
                clean_string(row.get("PROGRAM_AK"))
                or "unknown"
            )
            sponsor_name = (
                clean_string(row.get("SPONSOR_NAME"))
                or "Unknown sponsor"
            )
            trade_name = (
                clean_string(row.get("TRADE_NAME"))
                or "Unknown trade"
            )

            out_of_state_warnings.append(
                f"CSV row {row_number}, PROGRAM_AK {program_ak}: "
                f"{sponsor_name} — {trade_name}"
            )
            continue

        soc_code = clean_string(row.get("ONET_SOC_CODE"))

        if not soc_code:
            soc_warnings.append(
                missing_soc_warning(
                    row=row,
                    row_number=row_number,
                    reason="missing ONET_SOC_CODE",
                )
            )
            continue

        if not SOC_CODE_PATTERN.fullmatch(soc_code):
            soc_warnings.append(
                missing_soc_warning(
                    row=row,
                    row_number=row_number,
                    reason=(
                        "unusable ONET_SOC_CODE "
                        f"{soc_code!r}"
                    ),
                )
            )
            continue

        try:
            program = build_program(
                row=row,
                row_number=row_number,
                official_regions=official_regions,
            )
        except ValueError as exc:
            errors.append(str(exc))
            continue

        program_ak = program["programAk"]

        previous_row = seen_program_aks.get(program_ak)

        if previous_row is not None:
            errors.append(
                f"CSV row {row_number}: duplicate PROGRAM_AK "
                f"{program_ak}; first seen on CSV row "
                f"{previous_row}."
            )
            continue

        seen_program_aks[program_ak] = row_number
        programs.append(program)

        if status == "Active":
            included_active_count += 1
        elif status == "Probation":
            included_probation_count += 1

    groups, grouping_errors = build_groups(
        programs=programs,
        region_order=region_order,
    )
    errors.extend(grouping_errors)

    errors.extend(
        validate_group_invariants(
            groups,
            region_order=region_order,
        )
    )

    errors.extend(
        validate_groups_against_schema(
            groups=groups,
            validator=validator,
        )
    )

    if not groups and not errors:
        errors.append(
            "No program groups were produced. Refusing to replace "
            "the existing output directory."
        )

    if errors:
        print_failure_report(
            source_row_count=len(source_rows),
            inactive_count=inactive_count,
            soc_warnings=soc_warnings,
            errors=errors,
        )
        raise SystemExit(1)

    write_groups(groups)

    print_success_report(
        source_row_count=len(source_rows),
        included_active_count=included_active_count,
        included_probation_count=included_probation_count,
        inactive_count=inactive_count,
        soc_warnings=soc_warnings,
        out_of_state_warnings=out_of_state_warnings,
        out_of_state_count=out_of_state_count,
        groups=groups,
    )


def require_file(path: Path, label: str) -> None:
    """Require one input file to exist."""
    if not path.is_file():
        raise SystemExit(
            f"Required {label} not found:\n"
            f"  {path}"
        )


def load_json_object(path: Path) -> dict[str, Any]:
    """Load a JSON file and require a top-level object."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"Invalid JSON in {path}: {exc}"
        ) from exc

    if not isinstance(data, dict):
        raise SystemExit(
            f"Expected a JSON object in {path}."
        )

    return data


def clean_string(value: object) -> str:
    """Return stripped source text, or an empty string."""
    if value is None:
        return ""

    return str(value).strip()


def optional_string(value: object) -> str | None:
    """Return stripped source text, or None when blank."""
    cleaned = clean_string(value)
    return cleaned or None


def required_string(
    row: dict[str, str],
    field: str,
    *,
    row_number: int,
) -> str:
    """Read a required non-empty source string."""
    value = clean_string(row.get(field))

    if not value:
        raise ValueError(
            f"CSV row {row_number}: {field} is required."
        )

    return value


def parse_required_positive_integer(
    value: object,
    *,
    field: str,
    row_number: int,
) -> int:
    """Parse a required positive integer source value."""
    text = clean_string(value)

    if not text:
        raise ValueError(
            f"CSV row {row_number}: {field} is required."
        )

    try:
        parsed = int(text)
    except ValueError as exc:
        raise ValueError(
            f"CSV row {row_number}: {field} must be an integer; "
            f"found {text!r}."
        ) from exc

    if parsed < 1:
        raise ValueError(
            f"CSV row {row_number}: {field} must be greater "
            f"than zero; found {parsed}."
        )

    return parsed


def parse_optional_positive_integer(
    value: object,
    *,
    field: str,
    row_number: int,
) -> int | None:
    """Parse an optional positive integer source value."""
    text = clean_string(value)

    if not text:
        return None

    try:
        parsed = int(text)
    except ValueError as exc:
        raise ValueError(
            f"CSV row {row_number}: {field} must be an integer "
            f"or blank; found {text!r}."
        ) from exc

    if parsed < 1:
        raise ValueError(
            f"CSV row {row_number}: {field} must be greater "
            f"than zero when present; found {parsed}."
        )

    return parsed


def parse_optional_iso_date(
    value: object,
    *,
    field: str,
    row_number: int,
) -> str | None:
    """Parse an optional ISO 8601 YYYY-MM-DD date."""
    text = clean_string(value)

    if not text:
        return None

    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(
            f"CSV row {row_number}: {field} must use YYYY-MM-DD; "
            f"found {text!r}."
        ) from exc

    return parsed.isoformat()


def load_source_rows(
    path: Path,
) -> list[tuple[int, dict[str, str]]]:
    """Load and validate the program source CSV."""
    with path.open(
        newline="",
        encoding="utf-8-sig",
    ) as csv_file:
        reader = csv.DictReader(csv_file)

        if reader.fieldnames is None:
            raise SystemExit(
                f"The source CSV has no header row: {path}"
            )

        actual_fields = {
            clean_string(field)
            for field in reader.fieldnames
            if field
        }

        missing_fields = SOURCE_FIELDNAMES - actual_fields

        if missing_fields:
            missing = ", ".join(sorted(missing_fields))

            raise SystemExit(
                f"{path} is missing required source column(s): "
                f"{missing}"
            )

        rows: list[tuple[int, dict[str, str]]] = []

        for row_number, raw_row in enumerate(
            reader,
            start=2,
        ):
            row = {
                clean_string(key): value
                for key, value in raw_row.items()
                if key is not None
            }

            rows.append((row_number, row))

    return rows


def load_official_regions(path: Path) -> list[str]:
    """
    Load unique official labor-market-region names in source order.

    Labor_Market_Regions.csv contains one row per county, so region
    names naturally repeat.
    """
    regions: list[str] = []
    seen: set[str] = set()

    with path.open(
        newline="",
        encoding="utf-8-sig",
    ) as csv_file:
        reader = csv.DictReader(csv_file)

        if reader.fieldnames is None or "Region" not in reader.fieldnames:
            raise SystemExit(
                f"{path} must contain a Region column."
            )

        for row in reader:
            region = clean_string(row.get("Region"))

            if not region or region in seen:
                continue

            seen.add(region)
            regions.append(region)

    if not regions:
        raise SystemExit(
            f"No labor-market regions found in {path}."
        )

    return regions


def validate_region_aliases(
    official_regions: set[str],
) -> None:
    """Ensure every configured source alias maps to an official region."""
    invalid = {
        source: target
        for source, target in SOURCE_REGION_ALIASES.items()
        if target not in official_regions
    }

    if not invalid:
        return

    details = "\n".join(
        f"  {source!r} -> {target!r}"
        for source, target in sorted(invalid.items())
    )

    raise SystemExit(
        "SOURCE_REGION_ALIASES contains target values that are "
        "not present in Labor_Market_Regions.csv:\n"
        f"{details}"
    )


def normalize_region(
    value: object,
    *,
    row_number: int,
    official_regions: set[str],
) -> str:
    """Normalize one REDC region to the app's labor-market-region name."""
    source_region = clean_string(value)

    if not source_region:
        raise ValueError(
            f"CSV row {row_number}: SPONSOR_REDC_REGION is required."
        )

    if source_region in official_regions:
        return source_region

    normalized = SOURCE_REGION_ALIASES.get(source_region)

    if normalized is not None:
        return normalized

    raise ValueError(
        f"CSV row {row_number}: unknown SPONSOR_REDC_REGION "
        f"{source_region!r}."
    )


def build_program(
    *,
    row: dict[str, str],
    row_number: int,
    official_regions: set[str],
) -> dict[str, Any]:
    """Transform one included source row into the program JSON shape."""
    program_ak = parse_required_positive_integer(
        row.get("PROGRAM_AK"),
        field="PROGRAM_AK",
        row_number=row_number,
    )

    sponsor_name = required_string(
        row,
        "SPONSOR_NAME",
        row_number=row_number,
    )
    trade_name = required_string(
        row,
        "TRADE_NAME",
        row_number=row_number,
    )
    soc_code = required_string(
        row,
        "ONET_SOC_CODE",
        row_number=row_number,
    )
    soc_title = required_string(
        row,
        "ONET_TITLE",
        row_number=row_number,
    )

    region = normalize_region(
        row.get("SPONSOR_REDC_REGION"),
        row_number=row_number,
        official_regions=official_regions,
    )

    program_status = required_string(
        row,
        "PROGRAM_STATUS",
        row_number=row_number,
    )

    program_approval_date = parse_optional_iso_date(
        row.get("PROGRAM_APPROVAL_DATE"),
        field="PROGRAM_APPROVAL_DATE",
        row_number=row_number,
    )

    program_length = parse_optional_positive_integer(
        row.get("PROGRAM_LENGTH"),
        field="PROGRAM_LENGTH",
        row_number=row_number,
    )

    return {
        "programAk": program_ak,
        "sponsorName": sponsor_name,
        "tradeName": trade_name,
        "addressLine1": optional_string(
            row.get("ADDRESS_LINE1")
        ),
        "addressLine2": optional_string(
            row.get("ADDRESS_LINE2")
        ),
        "addressCity": optional_string(
            row.get("ADDRESS_CITY")
        ),
        "addressState": optional_string(
            row.get("ADDRESS_STATE")
        ),
        "addressPostalCode": optional_string(
            row.get("ADDRESS_POSTAL_CODE")
        ),
        "region": region,
        "programStatus": program_status,
        "programApprovalDate": program_approval_date,
        "programGroupType": optional_string(
            row.get("PROGRAM_GROUP_TYPE")
        ),
        "programApproach": optional_string(
            row.get("PROGRAM_APPROACH")
        ),
        "programLength": program_length,
        "ratio": optional_string(
            row.get("RATIO")
        ),
        "socCode": soc_code,
        "socTitle": soc_title,
    }


def build_groups(
    *,
    programs: list[dict[str, Any]],
    region_order: list[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Group programs first by SOC code, then by exact trade name."""
    grouped_programs: defaultdict[
        str,
        list[dict[str, Any]],
    ] = defaultdict(list)

    for program in programs:
        grouped_programs[program["socCode"]].append(program)

    region_index = {
        region: index
        for index, region in enumerate(region_order)
    }

    groups: list[dict[str, Any]] = []
    errors: list[str] = []

    for soc_code in sorted(grouped_programs):
        soc_programs = grouped_programs[soc_code]

        soc_titles = sorted(
            {
                program["socTitle"]
                for program in soc_programs
            },
            key=str.casefold,
        )

        if len(soc_titles) != 1:
            title_list = ", ".join(
                repr(title)
                for title in soc_titles
            )
            errors.append(
                f"SOC {soc_code}: conflicting ONET_TITLE values: "
                f"{title_list}."
            )
            continue

        soc_title = soc_titles[0]

        programs_by_trade: defaultdict[
            str,
            list[dict[str, Any]],
        ] = defaultdict(list)

        for program in soc_programs:
            programs_by_trade[program["tradeName"]].append(
                program
            )

        trades: list[dict[str, Any]] = []

        for trade_name in sorted(
            programs_by_trade,
            key=str.casefold,
        ):
            trade_programs = sorted(
                programs_by_trade[trade_name],
                key=lambda program: program["programAk"],
            )

            trades.append(
                {
                    "tradeName": trade_name,
                    "programCount": len(trade_programs),
                    "programs": trade_programs,
                }
            )

        regions = sorted(
            {
                program["region"]
                for program in soc_programs
            },
            key=lambda region: region_index[region],
        )

        groups.append(
            {
                "socCode": soc_code,
                "socTitle": soc_title,
                "programCount": len(soc_programs),
                "regions": regions,
                "trades": trades,
            }
        )

    return groups, errors


def validate_group_invariants(
    groups: list[dict[str, Any]],
    *,
    region_order: list[str],
) -> list[str]:
    """
    Validate relationships JSON Schema cannot conveniently express.

    These checks protect the redundant child fields we intentionally retain.
    """
    errors: list[str] = []
    all_program_aks: set[int] = set()

    region_index = {
        region: index
        for index, region in enumerate(region_order)
    }

    seen_soc_codes: set[str] = set()

    for group in groups:
        soc_code = group["socCode"]
        soc_title = group["socTitle"]

        if soc_code in seen_soc_codes:
            errors.append(
                f"SOC {soc_code}: duplicate top-level SOC group."
            )

        seen_soc_codes.add(soc_code)

        actual_program_count = sum(
            len(trade["programs"])
            for trade in group["trades"]
        )

        if group["programCount"] != actual_program_count:
            errors.append(
                f"SOC {soc_code}: programCount is "
                f"{group['programCount']} but contains "
                f"{actual_program_count} programs."
            )

        expected_regions = sorted(
            {
                program["region"]
                for trade in group["trades"]
                for program in trade["programs"]
            },
            key=lambda region: region_index[region],
        )

        if group["regions"] != expected_regions:
            errors.append(
                f"SOC {soc_code}: regions does not match the "
                "regions represented by its programs."
            )

        seen_trade_names: set[str] = set()

        for trade in group["trades"]:
            trade_name = trade["tradeName"]

            if trade_name in seen_trade_names:
                errors.append(
                    f"SOC {soc_code}: duplicate trade group "
                    f"{trade_name!r}."
                )

            seen_trade_names.add(trade_name)

            if trade["programCount"] != len(trade["programs"]):
                errors.append(
                    f"SOC {soc_code}, trade {trade_name!r}: "
                    f"programCount is {trade['programCount']} "
                    f"but contains {len(trade['programs'])} "
                    "programs."
                )

            for program in trade["programs"]:
                program_ak = program["programAk"]

                if program_ak in all_program_aks:
                    errors.append(
                        f"PROGRAM_AK {program_ak} appears more "
                        "than once in generated groups."
                    )

                all_program_aks.add(program_ak)

                if program["socCode"] != soc_code:
                    errors.append(
                        f"PROGRAM_AK {program_ak}: child socCode "
                        f"{program['socCode']!r} does not match "
                        f"parent SOC {soc_code!r}."
                    )

                if program["socTitle"] != soc_title:
                    errors.append(
                        f"PROGRAM_AK {program_ak}: child socTitle "
                        f"{program['socTitle']!r} does not match "
                        f"parent title {soc_title!r}."
                    )

                if program["tradeName"] != trade_name:
                    errors.append(
                        f"PROGRAM_AK {program_ak}: child tradeName "
                        f"{program['tradeName']!r} does not match "
                        f"parent trade {trade_name!r}."
                    )

    return errors


def validate_groups_against_schema(
    *,
    groups: list[dict[str, Any]],
    validator: Draft202012Validator,
) -> list[str]:
    """Return every JSON Schema validation error across all groups."""
    errors: list[str] = []

    for group in groups:
        soc_code = group.get("socCode", "<unknown>")

        validation_errors = sorted(
            validator.iter_errors(group),
            key=lambda error: list(error.absolute_path),
        )

        for error in validation_errors:
            json_path = format_json_path(
                error.absolute_path
            )

            errors.append(
                f"{soc_code}.json {json_path}: "
                f"{error.message}"
            )

    return errors


def format_json_path(path: Any) -> str:
    """Format a jsonschema path as a readable JSON-style path."""
    parts = ["$"]

    for value in path:
        if isinstance(value, int):
            parts.append(f"[{value}]")
        else:
            parts.append(f".{value}")

    return "".join(parts)


def missing_soc_warning(
    *,
    row: dict[str, str],
    row_number: int,
    reason: str,
) -> str:
    """Build a readable warning for an excluded SOC-less program."""
    program_ak = clean_string(row.get("PROGRAM_AK")) or "unknown"
    sponsor_name = (
        clean_string(row.get("SPONSOR_NAME"))
        or "Unknown sponsor"
    )
    trade_name = (
        clean_string(row.get("TRADE_NAME"))
        or "Unknown trade"
    )

    return (
        f"CSV row {row_number}, PROGRAM_AK {program_ak}: "
        f"{sponsor_name} — {trade_name} ({reason})"
    )


def render_group_json(group: dict[str, Any]) -> str:
    """Render one generated program-group JSON document."""
    return (
        json.dumps(
            group,
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )


def write_groups(
    groups: list[dict[str, Any]],
) -> None:
    """
    Stage the complete output and replace programs/json only after success.

    If the directory swap fails, restore the previous output when possible.
    """
    if TEMP_OUTPUT_DIR.exists():
        shutil.rmtree(TEMP_OUTPUT_DIR)

    if BACKUP_OUTPUT_DIR.exists():
        shutil.rmtree(BACKUP_OUTPUT_DIR)

    TEMP_OUTPUT_DIR.mkdir(parents=True)

    try:
        for group in groups:
            output_path = (
                TEMP_OUTPUT_DIR
                / f"{group['socCode']}.json"
            )

            output_path.write_text(
                render_group_json(group),
                encoding="utf-8",
            )

        if OUTPUT_DIR.exists():
            OUTPUT_DIR.rename(BACKUP_OUTPUT_DIR)

        TEMP_OUTPUT_DIR.rename(OUTPUT_DIR)

    except Exception:
        if OUTPUT_DIR.exists() and BACKUP_OUTPUT_DIR.exists():
            shutil.rmtree(OUTPUT_DIR)

        if (
            not OUTPUT_DIR.exists()
            and BACKUP_OUTPUT_DIR.exists()
        ):
            BACKUP_OUTPUT_DIR.rename(OUTPUT_DIR)

        if TEMP_OUTPUT_DIR.exists():
            shutil.rmtree(TEMP_OUTPUT_DIR)

        raise

    else:
        if BACKUP_OUTPUT_DIR.exists():
            shutil.rmtree(BACKUP_OUTPUT_DIR)


def print_failure_report(
    *,
    source_row_count: int,
    inactive_count: int,
    soc_warnings: list[str],
    errors: list[str],
) -> None:
    """Print all warnings and errors without changing generated output."""
    print("", file=sys.stderr)
    print(
        "Program processing failed.",
        file=sys.stderr,
    )
    print(
        f"Source rows: {source_row_count}",
        file=sys.stderr,
    )
    print(
        f"  Excluded inactive programs:        "
        f"{inactive_count}"
    )
    print(
        f"  Excluded out-of-state programs:    "
        f"{out_of_state_count}"
    )
    print(
        f"  Excluded without usable SOC code:  "
        f"{len(soc_warnings)}"
    )

    if soc_warnings:
        print("", file=sys.stderr)
        print(
            "SOC warnings:",
            file=sys.stderr,
        )

        for warning in soc_warnings:
            print(
                f"  - {warning}",
                file=sys.stderr,
            )

    print("", file=sys.stderr)
    print(
        f"Errors ({len(errors)}):",
        file=sys.stderr,
    )

    for error in errors:
        print(
            f"  - {error}",
            file=sys.stderr,
        )

    print("", file=sys.stderr)
    print(
        f"Output unchanged: {OUTPUT_DIR}",
        file=sys.stderr,
    )


def print_success_report(
    *,
    source_row_count: int,
    included_active_count: int,
    included_probation_count: int,
    inactive_count: int,
    out_of_state_count: int,
    out_of_state_warnings: list[str],
    soc_warnings: list[str],
    groups: list[dict[str, Any]],
) -> None:
    """Print a compact summary of the completed transformation."""
    included_count = (
        included_active_count
        + included_probation_count
    )

    trade_group_count = sum(
        len(group["trades"])
        for group in groups
    )

    print("")
    print("Program processing complete:")
    print("")
    print(
        f"  Source rows:                       "
        f"{source_row_count}"
    )
    print(
        f"  Included programs:                 "
        f"{included_count}"
    )
    print(
        f"    Active:                          "
        f"{included_active_count}"
    )
    print(
        f"    Probation:                       "
        f"{included_probation_count}"
    )
    print("")
    print(
        f"  Excluded inactive programs:        "
        f"{inactive_count}"
    )
    print(
        f"  Excluded out-of-state programs:    "
        f"{out_of_state_count}"
    )
    print(
        f"  Excluded without usable SOC code:  "
        f"{len(soc_warnings)}"
    )
    print("")
    print(
        f"  SOC groups written:                "
        f"{len(groups)}"
    )
    print(
        f"  Trade groups:                      "
        f"{trade_group_count}"
    )

    if soc_warnings:
        print("")
        print("Warnings:")

        for warning in soc_warnings:
            print(f"  - {warning}")

    if out_of_state_warnings:
        print("")
        print("Out-of-state programs excluded:")

        for warning in out_of_state_warnings:
            print(f"  - {warning}")
    print("")
    print(f"Wrote: {OUTPUT_DIR}/")


if __name__ == "__main__":
    main()
