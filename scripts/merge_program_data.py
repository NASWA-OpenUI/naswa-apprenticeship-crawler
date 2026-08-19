from __future__ import annotations

import copy
import csv
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from utils.onet_utils import load_onet_data


PROGRAM_JSON_ROOT = Path("programs/json")
PROGRAM_DESCRIPTIONS_CSV_PATH = Path(
    "job-descriptions/job-descriptions-programs.csv"
)
ONET_ROOT = Path("onet")
SCHEMA_PATH = Path("schemas/program-group.schema.json")

OUTPUT_ROOT = Path("programs/out")
TEMP_OUTPUT_ROOT = Path("programs/.out.merge.tmp")
BACKUP_OUTPUT_ROOT = Path("programs/.out.merge.backup")


REQUIRED_DESCRIPTION_FIELDS = {
    "programAk",
    "tradeName",
    "displayTradeName",
    "socCode",
    "description",
}


def main() -> None:
    require_directory(
        PROGRAM_JSON_ROOT,
        "program JSON directory",
    )
    require_directory(
        ONET_ROOT,
        "O*NET directory",
    )
    require_file(
        PROGRAM_DESCRIPTIONS_CSV_PATH,
        "program descriptions CSV",
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

    program_groups = load_program_groups(
        PROGRAM_JSON_ROOT,
        validator=validator,
    )

    if not program_groups:
        raise SystemExit(
            f"No program group JSON files found in "
            f"{PROGRAM_JSON_ROOT}."
        )

    descriptions_by_program_ak = (
        load_program_description_rows(
            PROGRAM_DESCRIPTIONS_CSV_PATH
        )
    )

    merged_groups: list[dict[str, Any]] = []
    used_program_aks: set[int] = set()

    missing_onet: list[
        tuple[str, str]
    ] = []

    title_mismatches: list[
        tuple[str, str, str]
    ] = []

    merge_errors: list[str] = []

    total_program_count = 0
    total_trade_count = 0

    for group in program_groups:
        soc_code = read_required_string(
            group,
            "socCode",
        )
        soc_title = read_required_string(
            group,
            "socTitle",
        )

        onet = load_onet_data(
            ONET_ROOT,
            soc_code,
        )

        if onet is None:
            missing_onet.append(
                (
                    soc_code,
                    soc_title,
                )
            )
            continue

        onet_title = str(
            onet.get("title") or ""
        ).strip()

        if (
            onet_title
            and onet_title != soc_title
        ):
            title_mismatches.append(
                (
                    soc_code,
                    soc_title,
                    onet_title,
                )
            )

        try:
            (
                merged_group,
                group_program_aks,
            ) = build_merged_group(
                group=group,
                onet=onet,
                descriptions_by_program_ak=(
                    descriptions_by_program_ak
                ),
            )
        except ValueError as exc:
            merge_errors.append(
                str(exc)
            )
            continue

        duplicate_program_aks = (
            used_program_aks
            & group_program_aks
        )

        if duplicate_program_aks:
            duplicate_list = ", ".join(
                str(program_ak)
                for program_ak in sorted(
                    duplicate_program_aks
                )
            )

            merge_errors.append(
                f"SOC {soc_code}: PROGRAM_AK values "
                f"appear in more than one SOC group: "
                f"{duplicate_list}"
            )
            continue

        used_program_aks.update(
            group_program_aks
        )

        merged_groups.append(
            merged_group
        )

        total_program_count += merged_group[
            "programCount"
        ]
        total_trade_count += len(
            merged_group["trades"]
        )

    unused_description_aks = (
        set(descriptions_by_program_ak)
        - used_program_aks
    )

    if missing_onet:
        merge_errors.append(
            f"{len(missing_onet)} SOC group(s) have "
            "no O*NET file."
        )

    if unused_description_aks:
        unused_list = ", ".join(
            str(program_ak)
            for program_ak in sorted(
                unused_description_aks
            )
        )

        merge_errors.append(
            "Program description CSV contains rows "
            "that are not represented in the current "
            f"program JSON: {unused_list}"
        )

    if merge_errors:
        print(
            "Program data could not be merged:",
            file=sys.stderr,
        )

        for error in merge_errors:
            print(
                f"  - {error}",
                file=sys.stderr,
            )

        if missing_onet:
            print(
                "",
                file=sys.stderr,
            )
            print(
                "Missing O*NET files:",
                file=sys.stderr,
            )

            for soc_code, soc_title in missing_onet:
                print(
                    f"  - {soc_code} — {soc_title}",
                    file=sys.stderr,
                )

        print(
            "",
            file=sys.stderr,
        )
        print(
            "No output files were changed.",
            file=sys.stderr,
        )

        raise SystemExit(1)

    merged_groups.sort(
        key=lambda group: group[
            "socCode"
        ]
    )

    write_groups_transactionally(
        merged_groups
    )

    print("")
    print(
        "Program data merge complete:"
    )
    print(
        f"  SOC groups:                 "
        f"{len(merged_groups)}"
    )
    print(
        f"  Trade groups:               "
        f"{total_trade_count}"
    )
    print(
        f"  Programs:                   "
        f"{total_program_count}"
    )
    print(
        f"  Description rows used:      "
        f"{len(used_program_aks)}"
    )
    print(
        f"  O*NET profiles attached:    "
        f"{len(merged_groups)}"
    )
    print(
        f"  O*NET title differences:    "
        f"{len(title_mismatches)}"
    )

    if title_mismatches:
        print("")
        print(
            "O*NET title differences:"
        )

        for (
            soc_code,
            program_title,
            onet_title,
        ) in title_mismatches:
            print(
                f"  {soc_code}"
            )
            print(
                f"    Program title: "
                f"{program_title}"
            )
            print(
                f"    O*NET title:   "
                f"{onet_title}"
            )

    print("")
    print(
        f"Updated: {OUTPUT_ROOT}/"
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


def read_required_string(
    data: dict[str, Any],
    key: str,
) -> str:
    value = data.get(
        key
    )

    if (
        not isinstance(value, str)
        or not value.strip()
    ):
        raise ValueError(
            f"Missing required string field: "
            f"{key}"
        )

    return value.strip()


def load_program_groups(
    root: Path,
    *,
    validator: Draft202012Validator,
) -> list[dict[str, Any]]:
    """
    Load and validate every canonical SOC-grouped program file.
    """
    paths = sorted(
        path
        for path in root.glob("*.json")
        if path.is_file()
    )

    groups: list[
        dict[str, Any]
    ] = []

    errors: list[str] = []
    seen_soc_codes: set[str] = set()
    seen_program_aks: set[int] = set()

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
            validator.iter_errors(
                group
            ),
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
                f"{path}: filename SOC "
                f"{path.stem!r} does not match "
                f"socCode {soc_code!r}."
            )

        if soc_code in seen_soc_codes:
            errors.append(
                f"Duplicate SOC group: "
                f"{soc_code}"
            )

        seen_soc_codes.add(
            soc_code
        )

        trades = group.get(
            "trades"
        )

        if not isinstance(
            trades,
            list,
        ):
            continue

        actual_program_count = 0

        for trade in trades:
            if not isinstance(
                trade,
                dict,
            ):
                continue

            trade_name = trade.get(
                "tradeName"
            )
            programs = trade.get(
                "programs"
            )

            if not isinstance(
                programs,
                list,
            ):
                continue

            actual_program_count += len(
                programs
            )

            if (
                trade.get("programCount")
                != len(programs)
            ):
                errors.append(
                    f"{path}: trade "
                    f"{trade_name!r} has "
                    f"programCount "
                    f"{trade.get('programCount')!r} "
                    f"but contains "
                    f"{len(programs)} programs."
                )

            for program in programs:
                if not isinstance(
                    program,
                    dict,
                ):
                    continue

                program_ak = program.get(
                    "programAk"
                )

                if (
                    not isinstance(
                        program_ak,
                        int,
                    )
                    or isinstance(
                        program_ak,
                        bool,
                    )
                ):
                    continue

                if program_ak in seen_program_aks:
                    errors.append(
                        f"PROGRAM_AK "
                        f"{program_ak} appears "
                        "more than once in "
                        "program JSON."
                    )

                seen_program_aks.add(
                    program_ak
                )

        if (
            group.get("programCount")
            != actual_program_count
        ):
            errors.append(
                f"{path}: programCount "
                f"{group.get('programCount')!r} "
                f"but contains "
                f"{actual_program_count} programs."
            )

        groups.append(
            group
        )

    if errors:
        print(
            "Existing program-group data failed "
            "validation:",
            file=sys.stderr,
        )

        for error in errors:
            print(
                f"  - {error}",
                file=sys.stderr,
            )

        raise SystemExit(1)

    return groups


def load_program_description_rows(
    path: Path,
) -> dict[int, dict[str, str]]:
    """
    Load generated program descriptions keyed by PROGRAM_AK.

    Although descriptions ultimately live at the trade-group level,
    job-descriptions-programs.csv intentionally has one row per program.
    """
    with path.open(
        newline="",
        encoding="utf-8-sig",
    ) as csv_file:
        reader = csv.DictReader(
            csv_file
        )

        if reader.fieldnames:
            reader.fieldnames = [
                field.strip()
                for field in reader.fieldnames
            ]

        fieldnames = set(
            reader.fieldnames or []
        )

        missing_fields = (
            REQUIRED_DESCRIPTION_FIELDS
            - fieldnames
        )

        if missing_fields:
            missing = ", ".join(
                sorted(
                    missing_fields
                )
            )

            raise ValueError(
                f"{path} is missing required "
                f"field(s): {missing}"
            )

        rows_by_program_ak: dict[
            int,
            dict[str, str],
        ] = {}

        for row_number, raw_row in enumerate(
            reader,
            start=2,
        ):
            raw_program_ak = (
                raw_row.get("programAk")
                or ""
            ).strip()

            if not raw_program_ak:
                raise ValueError(
                    f"{path} row {row_number} "
                    "has no programAk."
                )

            try:
                program_ak = int(
                    raw_program_ak
                )
            except ValueError as exc:
                raise ValueError(
                    f"{path} row {row_number} "
                    f"has invalid programAk "
                    f"{raw_program_ak!r}."
                ) from exc

            if program_ak < 1:
                raise ValueError(
                    f"{path} row {row_number} "
                    f"has invalid programAk "
                    f"{raw_program_ak!r}."
                )

            if (
                program_ak
                in rows_by_program_ak
            ):
                raise ValueError(
                    f"{path} contains duplicate "
                    f"programAk: {program_ak}"
                )

            row = {
                "tradeName": (
                    raw_row.get("tradeName")
                    or ""
                ).strip(),
                "displayTradeName": (
                    raw_row.get(
                        "displayTradeName"
                    )
                    or ""
                ).strip(),
                "socCode": (
                    raw_row.get("socCode")
                    or ""
                ).strip(),
                "description": (
                    raw_row.get("description")
                    or ""
                ).strip(),
            }

            for field in [
                "tradeName",
                "displayTradeName",
                "socCode",
                "description",
            ]:
                if not row[field]:
                    raise ValueError(
                        f"{path} row "
                        f"{row_number} has no "
                        f"{field}."
                    )

            rows_by_program_ak[
                program_ak
            ] = row

    return rows_by_program_ak


def build_merged_group(
    *,
    group: dict[str, Any],
    onet: dict[str, Any],
    descriptions_by_program_ak: dict[
        int,
        dict[str, str],
    ],
) -> tuple[
    dict[str, Any],
    set[int],
]:
    """
    Add O*NET data at SOC level and one generated description at each
    trade-group level.

    Every individual program must have a matching description CSV row.
    All description rows belonging to one trade group must agree on the
    display title and generated description.
    """
    soc_code = read_required_string(
        group,
        "socCode",
    )
    soc_title = read_required_string(
        group,
        "socTitle",
    )

    trades = group.get(
        "trades"
    )

    if not isinstance(
        trades,
        list,
    ):
        raise ValueError(
            f"SOC {soc_code} has no valid "
            "trades array."
        )

    merged_trades: list[
        dict[str, Any]
    ] = []

    used_program_aks: set[int] = set()

    for trade in trades:
        if not isinstance(
            trade,
            dict,
        ):
            raise ValueError(
                f"SOC {soc_code} contains "
                "a non-object trade."
            )

        trade_name = read_required_string(
            trade,
            "tradeName",
        )

        programs = trade.get(
            "programs"
        )

        if not isinstance(
            programs,
            list,
        ):
            raise ValueError(
                f"SOC {soc_code}, trade "
                f"{trade_name!r} has no "
                "valid programs array."
            )

        display_trade_names: set[
            str
        ] = set()

        descriptions: set[
            str
        ] = set()

        merged_programs: list[
            dict[str, Any]
        ] = []

        for program in programs:
            if not isinstance(
                program,
                dict,
            ):
                raise ValueError(
                    f"SOC {soc_code}, trade "
                    f"{trade_name!r} contains "
                    "a non-object program."
                )

            program_ak = program.get(
                "programAk"
            )

            if (
                not isinstance(
                    program_ak,
                    int,
                )
                or isinstance(
                    program_ak,
                    bool,
                )
                or program_ak < 1
            ):
                raise ValueError(
                    f"SOC {soc_code}, trade "
                    f"{trade_name!r} contains "
                    f"invalid programAk "
                    f"{program_ak!r}."
                )

            description_row = (
                descriptions_by_program_ak.get(
                    program_ak
                )
            )

            if description_row is None:
                raise ValueError(
                    f"SOC {soc_code}, trade "
                    f"{trade_name!r}, "
                    f"PROGRAM_AK {program_ak} "
                    "has no generated "
                    "description row."
                )

            if (
                description_row["socCode"]
                != soc_code
            ):
                raise ValueError(
                    f"PROGRAM_AK {program_ak}: "
                    f"description CSV has SOC "
                    f"{description_row['socCode']!r}, "
                    f"but program group has "
                    f"{soc_code!r}."
                )

            if (
                description_row["tradeName"]
                != trade_name
            ):
                raise ValueError(
                    f"PROGRAM_AK {program_ak}: "
                    "description CSV has trade "
                    f"{description_row['tradeName']!r}, "
                    f"but program group has "
                    f"{trade_name!r}."
                )

            display_trade_names.add(
                description_row[
                    "displayTradeName"
                ]
            )
            descriptions.add(
                description_row[
                    "description"
                ]
            )

            if (
                program_ak
                in used_program_aks
            ):
                raise ValueError(
                    f"PROGRAM_AK {program_ak} "
                    f"appears more than once "
                    f"inside SOC {soc_code}."
                )

            used_program_aks.add(
                program_ak
            )

            merged_program = copy.deepcopy(
                program
            )
            merged_program["opportunities"] = []

            merged_programs.append(
                merged_program
            )

        if len(
            display_trade_names
        ) != 1:
            values = ", ".join(
                repr(value)
                for value in sorted(
                    display_trade_names,
                    key=str.casefold,
                )
            )

            raise ValueError(
                f"SOC {soc_code}, trade "
                f"{trade_name!r} has "
                "inconsistent display trade "
                f"names: {values}"
            )

        if len(
            descriptions
        ) != 1:
            raise ValueError(
                f"SOC {soc_code}, trade "
                f"{trade_name!r} has more "
                "than one generated description."
            )

        display_trade_name = next(
            iter(
                display_trade_names
            )
        )
        description = next(
            iter(
                descriptions
            )
        )

        merged_trades.append(
            {
                "tradeName": trade_name,
                "displayTradeName": (
                    display_trade_name
                ),
                "description": description,
                "programCount": len(
                    merged_programs
                ),
                "programs": (
                    merged_programs
                ),
            }
        )

    actual_program_count = sum(
        trade["programCount"]
        for trade in merged_trades
    )

    if (
        actual_program_count
        != group["programCount"]
    ):
        raise ValueError(
            f"SOC {soc_code}: merged "
            f"program count is "
            f"{actual_program_count}, "
            f"but source group has "
            f"{group['programCount']}."
        )

    merged = {
        "socCode": soc_code,
        "socTitle": soc_title,
        "programCount": (
            group["programCount"]
        ),
        "regions": copy.deepcopy(
            group["regions"]
        ),
        "onet": onet,
        "trades": merged_trades,
    }

    return (
        merged,
        used_program_aks,
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


def render_json(
    data: dict[str, Any],
) -> str:
    return (
        json.dumps(
            data,
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )


def write_groups_transactionally(
    groups: list[dict[str, Any]],
) -> None:
    """
    Stage the complete enhanced program dataset, then replace programs/out
    only after every output file has been successfully generated.
    """
    if TEMP_OUTPUT_ROOT.exists():
        shutil.rmtree(
            TEMP_OUTPUT_ROOT
        )

    if BACKUP_OUTPUT_ROOT.exists():
        shutil.rmtree(
            BACKUP_OUTPUT_ROOT
        )

    TEMP_OUTPUT_ROOT.mkdir(
        parents=True
    )

    try:
        for group in groups:
            output_path = (
                TEMP_OUTPUT_ROOT
                / f"{group['socCode']}.json"
            )

            output_path.write_text(
                render_json(
                    group
                ),
                encoding="utf-8",
            )

        if OUTPUT_ROOT.exists():
            OUTPUT_ROOT.rename(
                BACKUP_OUTPUT_ROOT
            )

        TEMP_OUTPUT_ROOT.rename(
            OUTPUT_ROOT
        )

    except Exception:
        if (
            OUTPUT_ROOT.exists()
            and BACKUP_OUTPUT_ROOT.exists()
        ):
            shutil.rmtree(
                OUTPUT_ROOT
            )

        if (
            not OUTPUT_ROOT.exists()
            and BACKUP_OUTPUT_ROOT.exists()
        ):
            BACKUP_OUTPUT_ROOT.rename(
                OUTPUT_ROOT
            )

        if TEMP_OUTPUT_ROOT.exists():
            shutil.rmtree(
                TEMP_OUTPUT_ROOT
            )

        raise

    else:
        if BACKUP_OUTPUT_ROOT.exists():
            shutil.rmtree(
                BACKUP_OUTPUT_ROOT
            )


if __name__ == "__main__":
    main()
