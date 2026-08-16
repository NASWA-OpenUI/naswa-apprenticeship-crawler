from __future__ import annotations

import copy
import csv
import json
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


PROGRAM_JSON_DIR = Path("./programs/json")
MAPPINGS_CSV_PATH = Path("./programs/soc-code-mappings.csv")
SCHEMA_PATH = Path("./schemas/program-group.schema.json")

TEMP_OUTPUT_DIR = Path("./programs/.json.soc-apply.tmp")
BACKUP_OUTPUT_DIR = Path("./programs/.json.soc-apply.backup")

SOC_CODE_PATTERN = re.compile(r"^\d{2}-\d{4}\.\d{2}$")

MAPPING_FIELDNAMES = [
    "sourceSocCode",
    "sourceSocTitle",
    "targetSocCode",
    "targetSocTitle",
    "reason",
]


@dataclass(frozen=True)
class SocMapping:
    source_code: str
    source_title: str
    target_code: str
    target_title: str
    reason: str


def main() -> None:
    require_directory(
        PROGRAM_JSON_DIR,
        "program JSON directory",
    )
    require_file(
        MAPPINGS_CSV_PATH,
        "SOC-code mapping CSV",
    )
    require_file(
        SCHEMA_PATH,
        "program-group JSON schema",
    )

    schema = load_json_object(
        SCHEMA_PATH
    )

    Draft202012Validator.check_schema(
        schema
    )
    validator = Draft202012Validator(
        schema
    )

    region_order = read_region_order(
        schema
    )

    groups = load_program_groups(
        PROGRAM_JSON_DIR,
        validator=validator,
        region_order=region_order,
    )

    if not groups:
        raise SystemExit(
            f"No program group JSON files found in "
            f"{PROGRAM_JSON_DIR}."
        )

    current_soc_codes = {
        group["socCode"]
        for group in groups
    }

    completed_mappings, incomplete_mapping_rows = (
        load_soc_mappings(
            MAPPINGS_CSV_PATH
        )
    )

    mappings_by_source_code = {
        mapping.source_code: mapping
        for mapping in completed_mappings
    }

    mapping_errors = validate_mapping_relationships(
        completed_mappings
    )

    if mapping_errors:
        print(
            "SOC mapping file failed validation:",
            file=sys.stderr,
        )

        for error in mapping_errors:
            print(
                f"  - {error}",
                file=sys.stderr,
            )

        raise SystemExit(1)

    (
        transformed_groups,
        applied_mappings,
        affected_program_count,
        transformation_errors,
    ) = apply_mappings(
        groups=groups,
        mappings_by_source_code=mappings_by_source_code,
    )

    if transformation_errors:
        print(
            "Program SOC mapping could not be applied:",
            file=sys.stderr,
        )

        for error in transformation_errors:
            print(
                f"  - {error}",
                file=sys.stderr,
            )

        print(
            "",
            file=sys.stderr,
        )
        print(
            "No files changed.",
            file=sys.stderr,
        )

        raise SystemExit(1)

    (
        already_applied_mappings,
        unused_mappings,
    ) = classify_unapplied_mappings(
        completed_mappings=completed_mappings,
        applied_mappings=applied_mappings,
        current_soc_codes=current_soc_codes,
    )

    (
        proposed_groups,
        merge_count,
    ) = merge_converging_groups(
        groups=transformed_groups,
        region_order=region_order,
    )

    validation_errors = validate_generated_dataset(
        groups=proposed_groups,
        validator=validator,
        region_order=region_order,
    )

    if validation_errors:
        print(
            "Mapped program data failed validation:",
            file=sys.stderr,
        )

        for error in validation_errors:
            print(
                f"  - {error}",
                file=sys.stderr,
            )

        print(
            "",
            file=sys.stderr,
        )
        print(
            "No files changed.",
            file=sys.stderr,
        )

        raise SystemExit(1)

    print_report(
        source_group_count=len(groups),
        proposed_group_count=len(proposed_groups),
        completed_mapping_count=len(completed_mappings),
        incomplete_mapping_rows=incomplete_mapping_rows,
        applied_mappings=applied_mappings,
        already_applied_mappings=already_applied_mappings,
        unused_mappings=unused_mappings,
        affected_program_count=affected_program_count,
        merge_count=merge_count,
    )

    if not applied_mappings:
        print("")
        print(
            "No applicable completed SOC mappings were found. "
            "No files changed."
        )
        return

    write_groups_transactionally(
        proposed_groups
    )

    print("")
    print(
        f"Updated: {PROGRAM_JSON_DIR}/"
    )


def require_file(
    path: Path,
    label: str,
) -> None:
    if not path.is_file():
        raise SystemExit(
            f"Required {label} not found:\n"
            f"  {path}"
        )


def require_directory(
    path: Path,
    label: str,
) -> None:
    if not path.is_dir():
        raise SystemExit(
            f"Required {label} not found:\n"
            f"  {path}"
        )


def load_json_object(
    path: Path,
) -> dict[str, Any]:
    try:
        data = json.loads(
            path.read_text(
                encoding="utf-8"
            )
        )
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"Invalid JSON in {path}: {exc}"
        ) from exc

    if not isinstance(data, dict):
        raise SystemExit(
            f"Expected a JSON object in {path}."
        )

    return data


def read_region_order(
    schema: dict[str, Any],
) -> list[str]:
    """
    Read canonical region order directly from program-group.schema.json.
    """
    try:
        regions = schema["$defs"]["region"]["enum"]
    except (KeyError, TypeError) as exc:
        raise SystemExit(
            "program-group.schema.json does not contain "
            "$defs.region.enum."
        ) from exc

    if (
        not isinstance(regions, list)
        or not regions
        or not all(
            isinstance(region, str)
            for region in regions
        )
    ):
        raise SystemExit(
            "$defs.region.enum in program-group.schema.json "
            "must be a non-empty list of strings."
        )

    return regions


def load_program_groups(
    root: Path,
    *,
    validator: Draft202012Validator,
    region_order: list[str],
) -> list[dict[str, Any]]:
    """
    Load and validate the existing generated program data before applying
    any reviewed SOC mappings.
    """
    paths = sorted(
        path
        for path in root.glob("*.json")
        if path.is_file()
    )

    groups: list[dict[str, Any]] = []
    errors: list[str] = []
    seen_soc_codes: set[str] = set()

    for path in paths:
        try:
            group = load_json_object(
                path
            )
        except SystemExit as exc:
            errors.append(
                str(exc)
            )
            continue

        schema_errors = sorted(
            validator.iter_errors(group),
            key=lambda error: list(
                error.absolute_path
            ),
        )

        for error in schema_errors:
            errors.append(
                f"{path} "
                f"{format_json_path(error.absolute_path)}: "
                f"{error.message}"
            )

        soc_code = group.get(
            "socCode"
        )

        if not isinstance(
            soc_code,
            str,
        ):
            continue

        if path.stem != soc_code:
            errors.append(
                f"{path}: filename SOC {path.stem!r} "
                f"does not match the group's socCode "
                f"{soc_code!r}."
            )

        if soc_code in seen_soc_codes:
            errors.append(
                f"Duplicate SOC group found: {soc_code}"
            )

        seen_soc_codes.add(
            soc_code
        )
        groups.append(
            group
        )

    errors.extend(
        validate_group_invariants(
            groups=groups,
            region_order=region_order,
        )
    )

    if errors:
        print(
            "Existing program-group data failed validation:",
            file=sys.stderr,
        )

        for error in errors:
            print(
                f"  - {error}",
                file=sys.stderr,
            )

        raise SystemExit(1)

    return groups


def load_soc_mappings(
    path: Path,
) -> tuple[
    list[SocMapping],
    list[dict[str, str]],
]:
    """
    Load reviewed SOC mappings.

    Completed rows contain both targetSocCode and targetSocTitle and are
    eligible for application.

    Rows with both target fields blank remain in the manual-review queue and
    are skipped.
    """
    with path.open(
        newline="",
        encoding="utf-8",
    ) as csv_file:
        reader = csv.DictReader(
            csv_file
        )

        fieldnames = set(
            reader.fieldnames or []
        )
        missing_fields = (
            set(MAPPING_FIELDNAMES)
            - fieldnames
        )

        if missing_fields:
            missing = ", ".join(
                sorted(missing_fields)
            )

            raise SystemExit(
                f"{path} is missing required column(s): "
                f"{missing}"
            )

        completed: list[SocMapping] = []
        incomplete: list[dict[str, str]] = []

        seen_source_codes: set[str] = set()

        for row_number, raw_row in enumerate(
            reader,
            start=2,
        ):
            row = {
                field: (
                    raw_row.get(field)
                    or ""
                ).strip()
                for field in MAPPING_FIELDNAMES
            }

            source_code = row[
                "sourceSocCode"
            ]
            source_title = row[
                "sourceSocTitle"
            ]
            target_code = row[
                "targetSocCode"
            ]
            target_title = row[
                "targetSocTitle"
            ]
            reason = row[
                "reason"
            ]

            if not source_code:
                raise SystemExit(
                    f"{path} row {row_number} "
                    "has no sourceSocCode."
                )

            if not source_title:
                raise SystemExit(
                    f"{path} row {row_number} "
                    "has no sourceSocTitle."
                )

            if not SOC_CODE_PATTERN.fullmatch(
                source_code
            ):
                raise SystemExit(
                    f"{path} row {row_number} has invalid "
                    f"sourceSocCode {source_code!r}."
                )

            if source_code in seen_source_codes:
                raise SystemExit(
                    f"{path} contains duplicate "
                    f"sourceSocCode: {source_code}"
                )

            seen_source_codes.add(
                source_code
            )

            if bool(target_code) != bool(
                target_title
            ):
                raise SystemExit(
                    f"{path} row {row_number} must either "
                    "provide both targetSocCode and "
                    "targetSocTitle or leave both blank."
                )

            if not target_code:
                incomplete.append(
                    row
                )
                continue

            if not SOC_CODE_PATTERN.fullmatch(
                target_code
            ):
                raise SystemExit(
                    f"{path} row {row_number} has invalid "
                    f"targetSocCode {target_code!r}."
                )

            if source_code == target_code:
                raise SystemExit(
                    f"{path} row {row_number} maps "
                    f"{source_code} to itself. "
                    "SOC mappings must change the code."
                )

            completed.append(
                SocMapping(
                    source_code=source_code,
                    source_title=source_title,
                    target_code=target_code,
                    target_title=target_title,
                    reason=reason,
                )
            )

    return completed, incomplete


def validate_mapping_relationships(
    mappings: list[SocMapping],
) -> list[str]:
    """
    Validate relationships between completed mapping rows.

    A target should represent a final current SOC code. Mapping chains such as
    A -> B and B -> C are rejected so application remains one-step and
    deterministic.
    """
    errors: list[str] = []

    source_codes = {
        mapping.source_code
        for mapping in mappings
    }

    for mapping in mappings:
        if mapping.target_code in source_codes:
            errors.append(
                f"{mapping.source_code} maps to "
                f"{mapping.target_code}, but "
                f"{mapping.target_code} is also a source code "
                "in the mapping file. Replace mapping chains "
                "with direct mappings to the final SOC code."
            )

    target_titles: dict[
        str,
        str,
    ] = {}

    for mapping in mappings:
        previous_title = target_titles.get(
            mapping.target_code
        )

        if (
            previous_title is not None
            and previous_title
            != mapping.target_title
        ):
            errors.append(
                f"Mappings targeting {mapping.target_code} "
                "use conflicting target titles: "
                f"{previous_title!r} and "
                f"{mapping.target_title!r}."
            )
            continue

        target_titles[
            mapping.target_code
        ] = mapping.target_title

    return errors


def apply_mappings(
    *,
    groups: list[dict[str, Any]],
    mappings_by_source_code: dict[str, SocMapping],
) -> tuple[
    list[dict[str, Any]],
    list[SocMapping],
    int,
    list[str],
]:
    """
    Apply every completed mapping whose source SOC exists in program data.

    The source title must exactly match the reviewed mapping before any
    transformation is allowed.
    """
    transformed_groups: list[
        dict[str, Any]
    ] = []

    applied_mappings: list[
        SocMapping
    ] = []

    affected_program_count = 0
    errors: list[str] = []

    for group in groups:
        source_code = group[
            "socCode"
        ]
        source_title = group[
            "socTitle"
        ]

        mapping = (
            mappings_by_source_code.get(
                source_code
            )
        )

        if mapping is None:
            transformed_groups.append(
                copy.deepcopy(group)
            )
            continue

        if (
            source_title
            != mapping.source_title
        ):
            errors.append(
                f"SOC {source_code}: mapping expects "
                f"source title {mapping.source_title!r}, "
                f"but current program data contains "
                f"{source_title!r}."
            )

            transformed_groups.append(
                copy.deepcopy(group)
            )
            continue

        transformed_groups.append(
            remap_group(
                group,
                mapping=mapping,
            )
        )

        applied_mappings.append(
            mapping
        )

        affected_program_count += group[
            "programCount"
        ]

    return (
        transformed_groups,
        applied_mappings,
        affected_program_count,
        errors,
    )

def classify_unapplied_mappings(
    *,
    completed_mappings: list[SocMapping],
    applied_mappings: list[SocMapping],
    current_soc_codes: set[str],
) -> tuple[
    list[SocMapping],
    list[SocMapping],
]:
    """
    Classify completed mappings that were not applied during this run.

    A mapping is treated as already applied when:
    - its old source SOC is no longer present, and
    - its target SOC is present in the current program data.

    A mapping is treated as unused when neither its source nor target SOC is
    represented in the current program data. This can happen when a program
    occupation disappears from a newer source dataset.
    """
    applied_source_codes = {
        mapping.source_code
        for mapping in applied_mappings
    }

    already_applied: list[
        SocMapping
    ] = []

    unused: list[
        SocMapping
    ] = []

    for mapping in completed_mappings:
        if (
            mapping.source_code
            in applied_source_codes
        ):
            continue

        source_present = (
            mapping.source_code
            in current_soc_codes
        )
        target_present = (
            mapping.target_code
            in current_soc_codes
        )

        if (
            not source_present
            and target_present
        ):
            already_applied.append(
                mapping
            )

        elif (
            not source_present
            and not target_present
        ):
            unused.append(
                mapping
            )

    return (
        already_applied,
        unused,
    )


def remap_group(
    group: dict[str, Any],
    *,
    mapping: SocMapping,
) -> dict[str, Any]:
    """
    Apply one reviewed SOC code/title mapping to a complete group.
    """
    updated = copy.deepcopy(
        group
    )

    updated[
        "socCode"
    ] = mapping.target_code
    updated[
        "socTitle"
    ] = mapping.target_title

    for trade in updated[
        "trades"
    ]:
        for program in trade[
            "programs"
        ]:
            program[
                "socCode"
            ] = mapping.target_code
            program[
                "socTitle"
            ] = mapping.target_title

    return updated


def merge_converging_groups(
    *,
    groups: list[dict[str, Any]],
    region_order: list[str],
) -> tuple[
    list[dict[str, Any]],
    int,
]:
    """
    Merge program groups that now share the same SOC after mappings.

    Exact matching trade names are combined.
    """
    groups_by_soc: dict[
        str,
        list[dict[str, Any]],
    ] = {}

    for group in groups:
        groups_by_soc.setdefault(
            group["socCode"],
            [],
        ).append(
            group
        )

    proposed_groups: list[
        dict[str, Any]
    ] = []

    merge_count = 0

    for soc_code in sorted(
        groups_by_soc
    ):
        matching_groups = groups_by_soc[
            soc_code
        ]

        if len(matching_groups) == 1:
            proposed_groups.append(
                matching_groups[0]
            )
            continue

        merge_count += 1

        proposed_groups.append(
            merge_soc_groups(
                soc_code=soc_code,
                groups=matching_groups,
                region_order=region_order,
            )
        )

    return (
        proposed_groups,
        merge_count,
    )


def merge_soc_groups(
    *,
    soc_code: str,
    groups: list[dict[str, Any]],
    region_order: list[str],
) -> dict[str, Any]:
    """
    Merge multiple groups sharing one final SOC code.

    All groups must agree on the final SOC title.
    """
    soc_titles = {
        group["socTitle"]
        for group in groups
    }

    if len(soc_titles) != 1:
        titles = ", ".join(
            repr(title)
            for title in sorted(
                soc_titles,
                key=str.casefold,
            )
        )

        raise RuntimeError(
            f"Cannot merge SOC {soc_code}: "
            f"groups have conflicting final titles: "
            f"{titles}."
        )

    soc_title = next(
        iter(soc_titles)
    )

    programs_by_trade: dict[
        str,
        list[dict[str, Any]],
    ] = {}

    seen_program_aks: set[
        int
    ] = set()

    for group in groups:
        for trade in group[
            "trades"
        ]:
            trade_name = trade[
                "tradeName"
            ]

            trade_programs = (
                programs_by_trade.setdefault(
                    trade_name,
                    [],
                )
            )

            for source_program in trade[
                "programs"
            ]:
                program = copy.deepcopy(
                    source_program
                )

                program_ak = program[
                    "programAk"
                ]

                if (
                    program_ak
                    in seen_program_aks
                ):
                    raise RuntimeError(
                        f"PROGRAM_AK {program_ak} "
                        "appears more than once while "
                        f"merging SOC {soc_code}."
                    )

                seen_program_aks.add(
                    program_ak
                )

                program[
                    "socCode"
                ] = soc_code
                program[
                    "socTitle"
                ] = soc_title

                trade_programs.append(
                    program
                )

    trades: list[
        dict[str, Any]
    ] = []

    for trade_name in sorted(
        programs_by_trade,
        key=str.casefold,
    ):
        programs = sorted(
            programs_by_trade[
                trade_name
            ],
            key=lambda program: program[
                "programAk"
            ],
        )

        trades.append(
            {
                "tradeName": trade_name,
                "programCount": len(
                    programs
                ),
                "programs": programs,
            }
        )

    all_programs = [
        program
        for trade in trades
        for program in trade[
            "programs"
        ]
    ]

    region_index = {
        region: index
        for index, region in enumerate(
            region_order
        )
    }

    regions = sorted(
        {
            program["region"]
            for program in all_programs
        },
        key=lambda region: region_index[
            region
        ],
    )

    return {
        "socCode": soc_code,
        "socTitle": soc_title,
        "programCount": len(
            all_programs
        ),
        "regions": regions,
        "trades": trades,
    }


def validate_generated_dataset(
    *,
    groups: list[dict[str, Any]],
    validator: Draft202012Validator,
    region_order: list[str],
) -> list[str]:
    """
    Validate schema and cross-field invariants for the complete proposed
    output dataset.
    """
    errors: list[str] = []

    errors.extend(
        validate_groups_against_schema(
            groups=groups,
            validator=validator,
        )
    )

    errors.extend(
        validate_group_invariants(
            groups=groups,
            region_order=region_order,
        )
    )

    return errors


def validate_groups_against_schema(
    *,
    groups: list[dict[str, Any]],
    validator: Draft202012Validator,
) -> list[str]:
    errors: list[str] = []

    for group in groups:
        soc_code = group.get(
            "socCode",
            "<unknown>",
        )

        schema_errors = sorted(
            validator.iter_errors(group),
            key=lambda error: list(
                error.absolute_path
            ),
        )

        for error in schema_errors:
            errors.append(
                f"{soc_code}.json "
                f"{format_json_path(error.absolute_path)}: "
                f"{error.message}"
            )

    return errors


def validate_group_invariants(
    *,
    groups: list[dict[str, Any]],
    region_order: list[str],
) -> list[str]:
    """
    Check relationships JSON Schema cannot conveniently express.
    """
    errors: list[str] = []

    region_index = {
        region: index
        for index, region in enumerate(
            region_order
        )
    }

    seen_soc_codes: set[
        str
    ] = set()

    seen_program_aks: set[
        int
    ] = set()

    for group in groups:
        soc_code = group[
            "socCode"
        ]
        soc_title = group[
            "socTitle"
        ]

        if soc_code in seen_soc_codes:
            errors.append(
                f"Duplicate generated SOC group: "
                f"{soc_code}."
            )

        seen_soc_codes.add(
            soc_code
        )

        actual_program_count = sum(
            len(
                trade["programs"]
            )
            for trade in group[
                "trades"
            ]
        )

        if (
            group["programCount"]
            != actual_program_count
        ):
            errors.append(
                f"SOC {soc_code}: programCount "
                f"is {group['programCount']} "
                f"but contains "
                f"{actual_program_count} programs."
            )

        expected_regions = sorted(
            {
                program["region"]
                for trade in group[
                    "trades"
                ]
                for program in trade[
                    "programs"
                ]
            },
            key=lambda region: region_index[
                region
            ],
        )

        if (
            group["regions"]
            != expected_regions
        ):
            errors.append(
                f"SOC {soc_code}: regions does "
                "not match the regions represented "
                "by its programs."
            )

        seen_trade_names: set[
            str
        ] = set()

        for trade in group[
            "trades"
        ]:
            trade_name = trade[
                "tradeName"
            ]

            if (
                trade_name
                in seen_trade_names
            ):
                errors.append(
                    f"SOC {soc_code}: duplicate "
                    f"trade group {trade_name!r}."
                )

            seen_trade_names.add(
                trade_name
            )

            if (
                trade["programCount"]
                != len(
                    trade["programs"]
                )
            ):
                errors.append(
                    f"SOC {soc_code}, trade "
                    f"{trade_name!r}: programCount "
                    f"is {trade['programCount']} "
                    f"but contains "
                    f"{len(trade['programs'])} "
                    "programs."
                )

            for program in trade[
                "programs"
            ]:
                program_ak = program[
                    "programAk"
                ]

                if (
                    program_ak
                    in seen_program_aks
                ):
                    errors.append(
                        f"PROGRAM_AK {program_ak} "
                        "appears more than once in "
                        "generated groups."
                    )

                seen_program_aks.add(
                    program_ak
                )

                if (
                    program["socCode"]
                    != soc_code
                ):
                    errors.append(
                        f"PROGRAM_AK {program_ak}: "
                        f"child socCode "
                        f"{program['socCode']!r} "
                        f"does not match parent SOC "
                        f"{soc_code!r}."
                    )

                if (
                    program["socTitle"]
                    != soc_title
                ):
                    errors.append(
                        f"PROGRAM_AK {program_ak}: "
                        f"child socTitle "
                        f"{program['socTitle']!r} "
                        f"does not match parent title "
                        f"{soc_title!r}."
                    )

                if (
                    program["tradeName"]
                    != trade_name
                ):
                    errors.append(
                        f"PROGRAM_AK {program_ak}: "
                        f"child tradeName "
                        f"{program['tradeName']!r} "
                        f"does not match parent trade "
                        f"{trade_name!r}."
                    )

    return errors


def print_report(
    *,
    source_group_count: int,
    proposed_group_count: int,
    completed_mapping_count: int,
    incomplete_mapping_rows: list[dict[str, str]],
    applied_mappings: list[SocMapping],
    already_applied_mappings: list[SocMapping],
    unused_mappings: list[SocMapping],
    affected_program_count: int,
    merge_count: int,
) -> None:
    print("")
    print("Program SOC mappings:")
    print("")
    print(
        f"  SOC groups read:                  "
        f"{source_group_count}"
    )
    print(
        f"  Completed mappings available:     "
        f"{completed_mapping_count}"
    )
    print(
        f"  Incomplete mappings skipped:      "
        f"{len(incomplete_mapping_rows)}"
    )
    print("")
    print(
        f"  SOC groups changed:               "
        f"{len(applied_mappings)}"
    )
    print(
        f"  Programs affected:                "
        f"{affected_program_count}"
    )
    print(
        f"  SOC group merges:                 "
        f"{merge_count}"
    )
    print(
        f"  Resulting SOC groups:             "
        f"{proposed_group_count}"
    )
    print("")
    print(
        f"  Mappings already applied:         "
        f"{len(already_applied_mappings)}"
    )
    print(
        f"  Mappings not represented:         "
        f"{len(unused_mappings)}"
    )

    if applied_mappings:
        print("")
        print("Mappings applied this run:")

        for mapping in sorted(
            applied_mappings,
            key=lambda item: item.source_code,
        ):
            print(
                f"  {mapping.source_code}  "
                f"{mapping.source_title}"
            )
            print(
                f"      → {mapping.target_code}  "
                f"{mapping.target_title}"
            )

            if mapping.reason:
                print(
                    f"        Reason: "
                    f"{mapping.reason}"
                )

    if already_applied_mappings:
        print("")
        print("Mappings already applied:")

        for mapping in sorted(
            already_applied_mappings,
            key=lambda item: item.source_code,
        ):
            print(
                f"  {mapping.source_code}"
                f" → {mapping.target_code}"
            )

    if incomplete_mapping_rows:
        print("")
        print(
            "Incomplete mappings skipped "
            "(manual review still required):"
        )

        for row in sorted(
            incomplete_mapping_rows,
            key=lambda item: item[
                "sourceSocCode"
            ],
        ):
            print(
                f"  {row['sourceSocCode']}  "
                f"{row['sourceSocTitle']}"
            )

    if unused_mappings:
        print("")
        print(
            "Completed mappings not represented "
            "in current program data:"
        )

        for mapping in sorted(
            unused_mappings,
            key=lambda item: item.source_code,
        ):
            print(
                f"  {mapping.source_code}"
                f" → {mapping.target_code}"
            )


def format_json_path(
    path: Any,
) -> str:
    parts = [
        "$"
    ]

    for value in path:
        if isinstance(
            value,
            int,
        ):
            parts.append(
                f"[{value}]"
            )
        else:
            parts.append(
                f".{value}"
            )

    return "".join(
        parts
    )


def render_group_json(
    group: dict[str, Any],
) -> str:
    return (
        json.dumps(
            group,
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )


def write_groups_transactionally(
    groups: list[dict[str, Any]],
) -> None:
    """
    Stage the complete corrected dataset and replace programs/json only after
    every resulting group has already passed validation.

    If the final directory swap fails, restore the previous output.
    """
    if TEMP_OUTPUT_DIR.exists():
        shutil.rmtree(
            TEMP_OUTPUT_DIR
        )

    if BACKUP_OUTPUT_DIR.exists():
        shutil.rmtree(
            BACKUP_OUTPUT_DIR
        )

    TEMP_OUTPUT_DIR.mkdir(
        parents=True
    )

    try:
        for group in groups:
            output_path = (
                TEMP_OUTPUT_DIR
                / f"{group['socCode']}.json"
            )

            output_path.write_text(
                render_group_json(
                    group
                ),
                encoding="utf-8",
            )

        if PROGRAM_JSON_DIR.exists():
            PROGRAM_JSON_DIR.rename(
                BACKUP_OUTPUT_DIR
            )

        TEMP_OUTPUT_DIR.rename(
            PROGRAM_JSON_DIR
        )

    except Exception:
        if (
            PROGRAM_JSON_DIR.exists()
            and BACKUP_OUTPUT_DIR.exists()
        ):
            shutil.rmtree(
                PROGRAM_JSON_DIR
            )

        if (
            not PROGRAM_JSON_DIR.exists()
            and BACKUP_OUTPUT_DIR.exists()
        ):
            BACKUP_OUTPUT_DIR.rename(
                PROGRAM_JSON_DIR
            )

        if TEMP_OUTPUT_DIR.exists():
            shutil.rmtree(
                TEMP_OUTPUT_DIR
            )

        raise

    else:
        if BACKUP_OUTPUT_DIR.exists():
            shutil.rmtree(
                BACKUP_OUTPUT_DIR
            )


if __name__ == "__main__":
    main()
