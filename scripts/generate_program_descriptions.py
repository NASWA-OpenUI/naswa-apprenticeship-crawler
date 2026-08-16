from __future__ import annotations

import csv
import io
import json
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI

from utils.job_descriptions_utils import (
    build_description_cache,
    get_description,
    normalize_display_job_title,
    normalize_prompt_job_title,
    normalized_cache_soc_code,
    normalized_cache_title,
)


PROGRAM_JSON_DIR = Path("./programs/json")

OUTPUT_DIR = Path("./job-descriptions")
OUTPUT_CSV_PATH = OUTPUT_DIR / "job-descriptions-programs.csv"

POSTING_DESCRIPTIONS_CSV_PATH = (
    OUTPUT_DIR / "job-descriptions-postings.csv"
)

CSV_FIELDNAMES = [
    "programAk",
    "tradeName",
    "displayTradeName",
    "promptTradeName",
    "socCode",
    "description",
]

POSTING_CACHE_FIELDNAMES = [
    "promptJobTitle",
    "socCode",
    "description",
]

SOC_MAPPINGS_CSV_PATH = Path(
    "./programs/soc-code-mappings.csv"
)

SOC_MAPPING_FIELDNAMES = [
    "sourceSocCode",
    "sourceSocTitle",
    "targetSocCode",
    "targetSocTitle",
    "reason",
]


def main() -> None:
    load_dotenv()

    if not PROGRAM_JSON_DIR.exists():
        raise FileNotFoundError(
            f"Program JSON directory not found: {PROGRAM_JSON_DIR}"
        )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    program_files = get_program_files(PROGRAM_JSON_DIR)

    existing_rows = load_existing_program_description_rows(
        OUTPUT_CSV_PATH
    )
    posting_rows = load_posting_description_rows(
        POSTING_DESCRIPTIONS_CSV_PATH
    )
    soc_code_mappings = load_soc_code_mappings(
        SOC_MAPPINGS_CSV_PATH
    )

    existing_rows_by_program_ak = {
        row["programAk"]: row
        for row in existing_rows
    }

    # Existing program descriptions take precedence so rerunning this script
    # does not unexpectedly replace previously generated program text.
    posting_cache = build_description_cache(
        posting_rows,
        prompt_title_field="promptJobTitle",
    )
    program_cache = build_description_cache(
        existing_rows,
        prompt_title_field="promptTradeName",
    )

    add_mapped_program_cache_entries(
        description_cache=program_cache,
        existing_rows=existing_rows,
        soc_code_mappings=soc_code_mappings,
    )

    description_cache = {
        **posting_cache,
        **program_cache,
    }

    client = OpenAI()

    output_rows: list[dict[str, str]] = []
    current_program_aks: set[str] = set()

    kept_count = 0
    reused_count = 0
    generated_count = 0
    new_program_count = 0
    changed_existing_program_count = 0

    unique_trade_keys: set[tuple[str, str]] = set()

    print(
        f"Found {len(program_files)} program group file(s).",
        file=sys.stderr,
    )

    if existing_rows:
        print(
            f"Loaded {len(existing_rows)} existing program description row(s).",
            file=sys.stderr,
        )
    else:
        print(
            "No existing program description CSV found. "
            "Generating the initial program description set.",
            file=sys.stderr,
        )

    if posting_rows:
        print(
            f"Loaded {len(posting_rows)} posting description row(s) "
            "for reuse.",
            file=sys.stderr,
        )
    else:
        print(
            "No posting description CSV found. "
            "Program descriptions cannot reuse posting descriptions.",
            file=sys.stderr,
        )

    for program_path in program_files:
        group = load_program_group(program_path)

        group_soc_code = read_required_string(
            group,
            "socCode",
            program_path,
        )

        trades = group.get("trades")

        if not isinstance(trades, list):
            raise ValueError(
                f"{program_path} is missing a trades array."
            )

        for trade in trades:
            if not isinstance(trade, dict):
                raise ValueError(
                    f"{program_path} contains a non-object trade."
                )

            raw_trade_name = read_required_string(
                trade,
                "tradeName",
                program_path,
            )

            display_trade_name = normalize_display_job_title(
                raw_trade_name
            )
            prompt_trade_name = normalize_prompt_job_title(
                raw_trade_name
            )

            programs = trade.get("programs")

            if not isinstance(programs, list):
                raise ValueError(
                    f"{program_path}: trade {raw_trade_name!r} "
                    "is missing a programs array."
                )

            unique_trade_keys.add(
                (
                    normalized_cache_title(prompt_trade_name),
                    normalized_cache_soc_code(group_soc_code),
                )
            )

            for program in programs:
                if not isinstance(program, dict):
                    raise ValueError(
                        f"{program_path}: trade {raw_trade_name!r} "
                        "contains a non-object program."
                    )

                program_ak = read_program_ak(
                    program,
                    program_path,
                )
                csv_program_ak = str(program_ak)

                program_soc_code = read_required_string(
                    program,
                    "socCode",
                    program_path,
                )
                program_trade_name = read_required_string(
                    program,
                    "tradeName",
                    program_path,
                )

                if program_soc_code != group_soc_code:
                    raise ValueError(
                        f"{program_path}: PROGRAM_AK {program_ak} "
                        f"has SOC {program_soc_code!r}, but its "
                        f"group has SOC {group_soc_code!r}."
                    )

                if program_trade_name != raw_trade_name:
                    raise ValueError(
                        f"{program_path}: PROGRAM_AK {program_ak} "
                        f"has tradeName {program_trade_name!r}, but "
                        f"its trade group is {raw_trade_name!r}."
                    )

                if csv_program_ak in current_program_aks:
                    raise ValueError(
                        "Duplicate PROGRAM_AK in current program JSON: "
                        f"{program_ak}"
                    )

                current_program_aks.add(csv_program_ak)

                existing_row = existing_rows_by_program_ak.get(
                    csv_program_ak
                )

                if existing_row is None:
                    new_program_count += 1

                if can_keep_existing_description(
                    existing_row=existing_row,
                    prompt_trade_name=prompt_trade_name,
                    soc_code=program_soc_code,
                    soc_code_mappings=soc_code_mappings,
                ):
                    description = existing_row["description"]
                    action = "kept"
                    kept_count += 1

                else:
                    description, action = get_description(
                        client=client,
                        display_job_title=display_trade_name,
                        prompt_job_title=prompt_trade_name,
                        soc_code=program_soc_code,
                        description_cache=description_cache,
                    )

                    if action == "reused":
                        reused_count += 1
                    else:
                        generated_count += 1

                    if existing_row is not None:
                        changed_existing_program_count += 1

                output_rows.append(
                    {
                        "programAk": csv_program_ak,
                        "tradeName": raw_trade_name,
                        "displayTradeName": display_trade_name,
                        "promptTradeName": prompt_trade_name,
                        "socCode": program_soc_code,
                        "description": description,
                    }
                )

                print(
                    f"{action.capitalize()}: "
                    f"PROGRAM_AK {program_ak} — "
                    f"{display_trade_name} "
                    f"({program_soc_code})",
                    file=sys.stderr,
                    flush=True,
                )

    archived_program_aks = (
        set(existing_rows_by_program_ak)
        - current_program_aks
    )
    removed_count = len(archived_program_aks)

    output_rows.sort(
        key=lambda row: int(row["programAk"])
    )

    output_changed = write_description_rows(
        output_rows
    )

    print("", file=sys.stderr)
    print(
        "Program description reconciliation complete:",
        file=sys.stderr,
    )
    print(
        f"  Current programs: {len(output_rows)}",
        file=sys.stderr,
    )
    print(
        f"  Unique trade/SOC combinations: {len(unique_trade_keys)}",
        file=sys.stderr,
    )
    print(
        f"  New programs: {new_program_count}",
        file=sys.stderr,
    )
    print(
        f"  Existing descriptions kept: {kept_count}",
        file=sys.stderr,
    )
    print(
        f"  Descriptions reused: {reused_count}",
        file=sys.stderr,
    )
    print(
        f"  New descriptions generated: {generated_count}",
        file=sys.stderr,
    )
    print(
        "  Existing programs with changed trade or SOC code: "
        f"{changed_existing_program_count}",
        file=sys.stderr,
    )
    print(
        f"  Archived rows removed: {removed_count}",
        file=sys.stderr,
    )

    if archived_program_aks:
        print(
            "  Removed PROGRAM_AKs:",
            file=sys.stderr,
        )

        for program_ak in sorted(
            archived_program_aks,
            key=int,
        ):
            print(
                f"    - {program_ak}",
                file=sys.stderr,
            )

    if output_changed:
        print(
            f"Updated: {OUTPUT_CSV_PATH}",
            file=sys.stderr,
        )
    else:
        print(
            "No CSV changes were needed.",
            file=sys.stderr,
        )


def get_program_files(
    root: Path,
) -> list[Path]:
    """Return every SOC-grouped program JSON file."""
    return sorted(
        path
        for path in root.glob("*.json")
        if path.is_file()
    )


def load_program_group(
    path: Path,
) -> dict[str, Any]:
    """Load one SOC-grouped program JSON file."""
    try:
        data = json.loads(
            path.read_text(encoding="utf-8")
        )
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Invalid JSON in {path}: {exc}"
        ) from exc

    if not isinstance(data, dict):
        raise ValueError(
            f"Expected JSON object in {path}"
        )

    return data


def read_required_string(
    data: dict[str, Any],
    key: str,
    path: Path,
) -> str:
    """Read a required non-empty string from a JSON object."""
    value = data.get(key)

    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"{path} is missing required string field: {key}"
        )

    return value.strip()


def read_program_ak(
    program: dict[str, Any],
    path: Path,
) -> int:
    """Read and validate PROGRAM_AK from an individual program record."""
    value = program.get("programAk")

    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(
            f"{path} contains a program with invalid programAk: "
            f"{value!r}"
        )

    if value < 1:
        raise ValueError(
            f"{path} contains a program with invalid programAk: "
            f"{value!r}"
        )

    return value


def load_existing_program_description_rows(
    path: Path,
) -> list[dict[str, str]]:
    """Load and validate the existing program-description CSV."""
    if not path.exists():
        return []

    with path.open(
        newline="",
        encoding="utf-8",
    ) as csv_file:
        reader = csv.DictReader(csv_file)

        fieldnames = set(reader.fieldnames or [])
        missing_fields = set(CSV_FIELDNAMES) - fieldnames

        if missing_fields:
            missing = ", ".join(
                sorted(missing_fields)
            )

            raise ValueError(
                f"{path} is missing required column(s): "
                f"{missing}"
            )

        rows: list[dict[str, str]] = []
        seen_program_aks: set[str] = set()

        for row_number, raw_row in enumerate(
            reader,
            start=2,
        ):
            row = {
                field: (
                    raw_row.get(field) or ""
                ).strip()
                for field in CSV_FIELDNAMES
            }

            program_ak = row["programAk"]

            if not program_ak:
                raise ValueError(
                    f"{path} row {row_number} has no programAk."
                )

            try:
                parsed_program_ak = int(program_ak)
            except ValueError as exc:
                raise ValueError(
                    f"{path} row {row_number} has invalid "
                    f"programAk {program_ak!r}."
                ) from exc

            if parsed_program_ak < 1:
                raise ValueError(
                    f"{path} row {row_number} has invalid "
                    f"programAk {program_ak!r}."
                )

            if program_ak in seen_program_aks:
                raise ValueError(
                    f"{path} contains duplicate programAk: "
                    f"{program_ak}"
                )

            if not row["promptTradeName"]:
                raise ValueError(
                    f"{path} row {row_number} has no "
                    "promptTradeName."
                )

            if not row["socCode"]:
                raise ValueError(
                    f"{path} row {row_number} has no socCode."
                )

            if not row["description"]:
                raise ValueError(
                    f"{path} row {row_number} has no description."
                )

            seen_program_aks.add(program_ak)
            rows.append(row)

    return rows


def load_posting_description_rows(
    path: Path,
) -> list[dict[str, str]]:
    """
    Load the existing posting descriptions used as a reuse cache.

    The posting file is optional. If it does not exist, program descriptions
    can still be generated normally.
    """
    if not path.exists():
        return []

    with path.open(
        newline="",
        encoding="utf-8",
    ) as csv_file:
        reader = csv.DictReader(csv_file)

        fieldnames = set(reader.fieldnames or [])
        missing_fields = (
            set(POSTING_CACHE_FIELDNAMES)
            - fieldnames
        )

        if missing_fields:
            missing = ", ".join(
                sorted(missing_fields)
            )

            raise ValueError(
                f"{path} is missing required column(s) "
                f"for description reuse: {missing}"
            )

        rows: list[dict[str, str]] = []

        for row_number, raw_row in enumerate(
            reader,
            start=2,
        ):
            row = {
                field: (
                    raw_row.get(field) or ""
                ).strip()
                for field in POSTING_CACHE_FIELDNAMES
            }

            if not row["promptJobTitle"]:
                raise ValueError(
                    f"{path} row {row_number} has no "
                    "promptJobTitle."
                )

            if not row["description"]:
                raise ValueError(
                    f"{path} row {row_number} has no description."
                )

            rows.append(row)

    return rows


def load_soc_code_mappings(
    path: Path,
) -> dict[str, str]:
    """
    Load completed reviewed SOC mappings as:

        old SOC code -> current SOC code

    Incomplete manual-review rows are ignored.
    """
    if not path.exists():
        return {}

    with path.open(
        newline="",
        encoding="utf-8",
    ) as csv_file:
        reader = csv.DictReader(csv_file)

        fieldnames = set(reader.fieldnames or [])
        missing_fields = (
            set(SOC_MAPPING_FIELDNAMES)
            - fieldnames
        )

        if missing_fields:
            missing = ", ".join(
                sorted(missing_fields)
            )

            raise ValueError(
                f"{path} is missing required column(s): "
                f"{missing}"
            )

        mappings: dict[str, str] = {}

        for row_number, raw_row in enumerate(
            reader,
            start=2,
        ):
            source_code = (
                raw_row.get("sourceSocCode") or ""
            ).strip()
            target_code = (
                raw_row.get("targetSocCode") or ""
            ).strip()

            if not source_code:
                raise ValueError(
                    f"{path} row {row_number} "
                    "has no sourceSocCode."
                )

            # Incomplete audit rows are deliberately ignored.
            if not target_code:
                continue

            if source_code in mappings:
                raise ValueError(
                    f"{path} contains duplicate "
                    f"sourceSocCode: {source_code}"
                )

            mappings[source_code] = target_code

    return mappings


def can_keep_existing_description(
    *,
    existing_row: dict[str, str] | None,
    prompt_trade_name: str,
    soc_code: str,
    soc_code_mappings: dict[str, str],
) -> bool:
    """
    Return whether a current program can keep its existing description.

    The normalized trade title must be unchanged.

    The SOC code may either:
    - be unchanged, or
    - have changed through a reviewed source -> target SOC mapping.
    """
    if existing_row is None:
        return False

    if not existing_row["description"]:
        return False

    existing_title = normalized_cache_title(
        existing_row["promptTradeName"]
    )
    current_title = normalized_cache_title(
        prompt_trade_name
    )

    if existing_title != current_title:
        return False

    existing_soc_code = normalized_cache_soc_code(
        existing_row["socCode"]
    )
    current_soc_code = normalized_cache_soc_code(
        soc_code
    )

    if existing_soc_code == current_soc_code:
        return True

    mapped_soc_code = normalized_cache_soc_code(
        soc_code_mappings.get(
            existing_soc_code
        )
    )

    return (
        bool(mapped_soc_code)
        and mapped_soc_code == current_soc_code
    )


def add_mapped_program_cache_entries(
    *,
    description_cache: dict[tuple[str, str], str],
    existing_rows: list[dict[str, str]],
    soc_code_mappings: dict[str, str],
) -> None:
    """
    Make descriptions cached under an old reviewed SOC code available under
    its mapped current SOC code as well.

    This avoids regenerating descriptions solely because the underlying
    taxonomy code was corrected.
    """
    for row in existing_rows:
        old_soc_code = normalized_cache_soc_code(
            row["socCode"]
        )

        mapped_soc_code = soc_code_mappings.get(
            old_soc_code
        )

        if not mapped_soc_code:
            continue

        title = normalized_cache_title(
            row["promptTradeName"]
        )
        new_soc_code = normalized_cache_soc_code(
            mapped_soc_code
        )

        if not title or not new_soc_code:
            continue

        mapped_key = (
            title,
            new_soc_code,
        )

        description_cache.setdefault(
            mapped_key,
            row["description"],
        )


def render_description_csv(
    rows: list[dict[str, str]],
) -> str:
    """Render the complete desired CSV into memory."""
    buffer = io.StringIO(
        newline=""
    )

    writer = csv.DictWriter(
        buffer,
        fieldnames=CSV_FIELDNAMES,
        lineterminator="\n",
    )
    writer.writeheader()
    writer.writerows(rows)

    return buffer.getvalue()


def write_description_rows(
    rows: list[dict[str, str]],
) -> bool:
    """
    Write the complete program-description CSV only when it changed.

    Returns True when the CSV was created or updated.
    """
    new_content = render_description_csv(
        rows
    )

    if OUTPUT_CSV_PATH.exists():
        existing_content = OUTPUT_CSV_PATH.read_text(
            encoding="utf-8",
        )

        if existing_content == new_content:
            return False

    OUTPUT_CSV_PATH.write_text(
        new_content,
        encoding="utf-8",
        newline="",
    )

    return True


if __name__ == "__main__":
    main()
