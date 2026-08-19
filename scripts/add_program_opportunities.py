from __future__ import annotations

import copy
import csv
import json
import shutil
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


PROGRAM_OUTPUT_ROOT = Path("programs/out")
PROGRAM_OPPORTUNITIES_CSV_PATH = Path(
    "programs/ra-programs-opportunities.csv"
)
OPPORTUNITY_OUTPUT_ROOT = Path("out")

TEMP_OUTPUT_ROOT = Path(
    "programs/.out.opportunities.tmp"
)
BACKUP_OUTPUT_ROOT = Path(
    "programs/.out.opportunities.backup"
)

REQUIRED_LINK_FIELDS = {
    "PROGRAM_AK",
    "POSTING_URL",
}

SOC_MATCH_OVERRIDES = {
    "47-4099.00": {
        "47-2061.00",
    },
}


def main() -> None:
    require_directory(
        PROGRAM_OUTPUT_ROOT,
        "merged program output directory",
    )
    require_directory(
        OPPORTUNITY_OUTPUT_ROOT,
        "opportunity output directory",
    )
    require_file(
        PROGRAM_OPPORTUNITIES_CSV_PATH,
        "program-opportunity links CSV",
    )

    program_groups = load_program_groups(
        PROGRAM_OUTPUT_ROOT
    )

    if not program_groups:
        raise SystemExit(
            f"No program group JSON files found in "
            f"{PROGRAM_OUTPUT_ROOT}."
        )

    (
        programs_by_ak,
        total_program_count,
    ) = index_programs(
        program_groups
    )

    opportunities_by_source_url = (
        load_opportunities_by_source_url(
            OPPORTUNITY_OUTPUT_ROOT
        )
    )

    link_rows = load_link_rows(
        PROGRAM_OPPORTUNITIES_CSV_PATH
    )

    errors: list[str] = []

    missing_postings: list[
        tuple[int, str]
    ] = []

    soc_mismatches: list[
        tuple[int, str, str, list[str]]
    ] = []

    duplicate_links = 0
    attached_opportunities = 0

    linked_program_aks: set[int] = set()

    seen_links: set[
        tuple[int, str]
    ] = set()

    attached_ids_by_program: dict[
        int,
        set[str],
    ] = defaultdict(set)

    for (
        program_ak,
        posting_url,
    ) in link_rows:
        normalized_url = normalize_url(
            posting_url
        )

        link_key = (
            program_ak,
            normalized_url,
        )

        if link_key in seen_links:
            duplicate_links += 1
            continue

        seen_links.add(
            link_key
        )

        program_entry = programs_by_ak.get(
            program_ak
        )

        if program_entry is None:
            errors.append(
                f"PROGRAM_AK {program_ak} appears "
                f"in {PROGRAM_OPPORTUNITIES_CSV_PATH} "
                "but is not present in the merged "
                "program data."
            )
            continue

        program = program_entry["program"]
        program_soc_code = (
            program_entry["socCode"]
        )

        linked_opportunities = (
            opportunities_by_source_url.get(
                normalized_url
            )
        )

        if not linked_opportunities:
            missing_postings.append(
                (
                    program_ak,
                    posting_url,
                )
            )
            continue

        matching_opportunities = [
            opportunity
            for opportunity in linked_opportunities
            if soc_codes_match(
                program_soc_code,
                opportunity["socCode"],
            )
        ]

        if not matching_opportunities:
            available_soc_codes = sorted(
                {
                    opportunity["socCode"]
                    for opportunity
                    in linked_opportunities
                }
            )

            soc_mismatches.append(
                (
                    program_ak,
                    posting_url,
                    program_soc_code,
                    available_soc_codes,
                )
            )
            continue

        for opportunity in matching_opportunities:
            posting = opportunity["posting"]

            opportunity_id = read_required_string(
                posting,
                "id",
            )

            if (
                opportunity_id
                in attached_ids_by_program[
                    program_ak
                ]
            ):
                continue

            program["opportunities"].append(
                copy.deepcopy(
                    posting
                )
            )

            attached_ids_by_program[
                program_ak
            ].add(
                opportunity_id
            )

            attached_opportunities += 1
            linked_program_aks.add(
                program_ak
            )

    if errors:
        print(
            "Program opportunities could not be added:",
            file=sys.stderr,
        )

        for error in errors:
            print(
                f"  - {error}",
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

    sort_program_opportunities(
        program_groups
    )

    write_groups_transactionally(
        program_groups
    )

    print("")
    print(
        "Program opportunity enrichment complete:"
    )
    print(
        f"  SOC groups:                     "
        f"{len(program_groups)}"
    )
    print(
        f"  Programs:                       "
        f"{total_program_count}"
    )
    print(
        f"  Link rows read:                 "
        f"{len(link_rows)}"
    )
    print(
        f"  Unique links processed:         "
        f"{len(seen_links)}"
    )
    print(
        f"  Duplicate links ignored:        "
        f"{duplicate_links}"
    )
    print(
        f"  Programs with opportunities:    "
        f"{len(linked_program_aks)}"
    )
    print(
        f"  Opportunity records attached:   "
        f"{attached_opportunities}"
    )
    print(
        f"  Linked posting URLs not found:  "
        f"{len(missing_postings)}"
    )
    print(
        f"  Links with no matching SOC:     "
        f"{len(soc_mismatches)}"
    )

    if missing_postings:
        print("")
        print(
            "Linked posting URLs not found in out/:"
        )

        for (
            program_ak,
            posting_url,
        ) in missing_postings:
            print(
                f"  PROGRAM_AK {program_ak}"
            )
            print(
                f"    {posting_url}"
            )

    if soc_mismatches:
        print("")
        print(
            "Linked postings found, but no "
            "opportunity matched the program SOC:"
        )

        for (
            program_ak,
            posting_url,
            program_soc_code,
            available_soc_codes,
        ) in soc_mismatches:
            print(
                f"  PROGRAM_AK {program_ak}"
            )
            print(
                f"    Program SOC: "
                f"{program_soc_code}"
            )
            print(
                f"    Posting URL: "
                f"{posting_url}"
            )
            print(
                "    Opportunity SOC(s): "
                + ", ".join(
                    available_soc_codes
                )
            )

    print("")
    print(
        f"Updated: {PROGRAM_OUTPUT_ROOT}/"
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
        raise ValueError(
            f"Invalid JSON in {path}: {exc}"
        ) from exc

    if not isinstance(
        data,
        dict,
    ):
        raise ValueError(
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
        not isinstance(
            value,
            str,
        )
        or not value.strip()
    ):
        raise ValueError(
            f"Missing required string field: "
            f"{key}"
        )

    return value.strip()

def soc_codes_match(
    program_soc_code: str,
    opportunity_soc_code: str,
) -> bool:
    if opportunity_soc_code == program_soc_code:
        return True


    allowed_soc_codes = SOC_MATCH_OVERRIDES.get(
        program_soc_code,
        set(),
    )


    return opportunity_soc_code in allowed_soc_codes

def normalize_url(
    value: str,
) -> str:
    """
    Normalize a source URL for reliable comparison.

    Query strings are preserved. Fragments and trailing
    slashes are ignored.
    """
    text = value.strip()

    if not text:
        return ""

    parts = urlsplit(
        text
    )

    if (
        not parts.scheme
        and not parts.netloc
    ):
        return text.rstrip("/")

    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path.rstrip("/"),
            parts.query,
            "",
        )
    )


def load_program_groups(
    root: Path,
) -> list[dict[str, Any]]:
    """
    Load the merged program files that will be enriched.

    Every individual program is expected to already
    contain an opportunities array created by Step 6.
    Existing values are cleared so this script is safe
    to rerun.
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

    for path in paths:
        try:
            group = load_json_object(
                path
            )
        except ValueError as exc:
            errors.append(
                str(exc)
            )
            continue

        soc_code = group.get(
            "socCode"
        )

        if (
            not isinstance(
                soc_code,
                str,
            )
            or not soc_code.strip()
        ):
            errors.append(
                f"{path} has no valid socCode."
            )
            continue

        if path.stem != soc_code:
            errors.append(
                f"{path}: filename SOC "
                f"{path.stem!r} does not match "
                f"socCode {soc_code!r}."
            )

        trades = group.get(
            "trades"
        )

        if not isinstance(
            trades,
            list,
        ):
            errors.append(
                f"{path} has no valid trades array."
            )
            continue

        for trade in trades:
            if not isinstance(
                trade,
                dict,
            ):
                errors.append(
                    f"{path} contains a non-object "
                    "trade."
                )
                continue

            programs = trade.get(
                "programs"
            )

            if not isinstance(
                programs,
                list,
            ):
                errors.append(
                    f"{path} contains a trade with "
                    "no valid programs array."
                )
                continue

            for program in programs:
                if not isinstance(
                    program,
                    dict,
                ):
                    errors.append(
                        f"{path} contains a "
                        "non-object program."
                    )
                    continue

                program_ak = program.get(
                    "programAk"
                )

                opportunities = program.get(
                    "opportunities"
                )

                if not isinstance(
                    opportunities,
                    list,
                ):
                    errors.append(
                        f"{path}: PROGRAM_AK "
                        f"{program_ak!r} has no valid "
                        "opportunities array. "
                        "Run Program Step 6 with the "
                        "updated output shape first."
                    )
                    continue

                # Rebuild the relationship data from
                # the source CSV on every run.
                program["opportunities"] = []

        groups.append(
            group
        )

    if errors:
        print(
            "Existing merged program data failed "
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


def index_programs(
    groups: list[dict[str, Any]],
) -> tuple[
    dict[int, dict[str, Any]],
    int,
]:
    """
    Index every individual program by PROGRAM_AK.

    The SOC code is inherited from the containing
    program group.
    """
    programs_by_ak: dict[
        int,
        dict[str, Any],
    ] = {}

    errors: list[str] = []
    total_program_count = 0

    for group in groups:
        soc_code = read_required_string(
            group,
            "socCode",
        )

        trades = group["trades"]

        for trade in trades:
            programs = trade["programs"]

            for program in programs:
                total_program_count += 1

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
                    errors.append(
                        f"SOC {soc_code} contains "
                        f"invalid programAk "
                        f"{program_ak!r}."
                    )
                    continue

                if (
                    program_ak
                    in programs_by_ak
                ):
                    errors.append(
                        f"PROGRAM_AK {program_ak} "
                        "appears more than once in "
                        "programs/out."
                    )
                    continue

                programs_by_ak[
                    program_ak
                ] = {
                    "socCode": soc_code,
                    "program": program,
                }

    if errors:
        print(
            "Could not index merged program data:",
            file=sys.stderr,
        )

        for error in errors:
            print(
                f"  - {error}",
                file=sys.stderr,
            )

        raise SystemExit(1)

    return (
        programs_by_ak,
        total_program_count,
    )


def load_opportunities_by_source_url(
    root: Path,
) -> dict[
    str,
    list[dict[str, Any]],
]:
    """
    Load complete opportunity output files and index
    them by posting.sourceUrl.

    Only the posting object is retained for later
    inclusion in program output.
    """
    paths = sorted(
        path
        for path in root.glob("*.json")
        if path.is_file()
    )

    opportunities_by_source_url: dict[
        str,
        list[dict[str, Any]],
    ] = defaultdict(list)

    errors: list[str] = []
    seen_opportunity_ids: set[str] = set()

    for path in paths:
        try:
            opportunity = load_json_object(
                path
            )
        except ValueError as exc:
            errors.append(
                str(exc)
            )
            continue

        opportunity_id = opportunity.get(
            "id"
        )
        soc_code = opportunity.get(
            "socCode"
        )
        posting = opportunity.get(
            "posting"
        )

        if (
            not isinstance(
                opportunity_id,
                str,
            )
            or not opportunity_id.strip()
        ):
            errors.append(
                f"{path} has no valid id."
            )
            continue

        opportunity_id = (
            opportunity_id.strip()
        )

        if opportunity_id in seen_opportunity_ids:
            errors.append(
                f"Duplicate opportunity id "
                f"{opportunity_id!r} in out/."
            )
            continue

        seen_opportunity_ids.add(
            opportunity_id
        )

        if (
            not isinstance(
                soc_code,
                str,
            )
            or not soc_code.strip()
        ):
            errors.append(
                f"{path} has no valid socCode."
            )
            continue

        soc_code = soc_code.strip()

        if not isinstance(
            posting,
            dict,
        ):
            errors.append(
                f"{path} has no valid posting "
                "object."
            )
            continue

        posting_id = posting.get(
            "id"
        )

        if (
            not isinstance(
                posting_id,
                str,
            )
            or not posting_id.strip()
        ):
            errors.append(
                f"{path} posting has no valid id."
            )
            continue

        if posting_id.strip() != opportunity_id:
            errors.append(
                f"{path}: top-level id "
                f"{opportunity_id!r} does not "
                "match posting.id "
                f"{posting_id!r}."
            )
            continue

        source_url = posting.get(
            "sourceUrl"
        )

        if (
            not isinstance(
                source_url,
                str,
            )
            or not source_url.strip()
        ):
            errors.append(
                f"{path} posting has no valid "
                "sourceUrl."
            )
            continue

        posting_soc_code = posting.get(
            "socCode"
        )

        if (
            isinstance(
                posting_soc_code,
                str,
            )
            and posting_soc_code.strip()
            and posting_soc_code.strip()
            != soc_code
        ):
            errors.append(
                f"{path}: top-level socCode "
                f"{soc_code!r} does not match "
                f"posting.socCode "
                f"{posting_soc_code!r}."
            )
            continue

        normalized_url = normalize_url(
            source_url
        )

        opportunities_by_source_url[
            normalized_url
        ].append(
            {
                "id": opportunity_id,
                "socCode": soc_code,
                "posting": posting,
            }
        )

    if errors:
        print(
            "Opportunity output data failed "
            "validation:",
            file=sys.stderr,
        )

        for error in errors:
            print(
                f"  - {error}",
                file=sys.stderr,
            )

        raise SystemExit(1)

    return dict(
        opportunities_by_source_url
    )


def load_link_rows(
    path: Path,
) -> list[
    tuple[int, str]
]:
    """
    Load PROGRAM_AK -> POSTING_URL relationships.

    Missing opportunity output is handled later and
    is not considered a malformed CSV row.
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
            REQUIRED_LINK_FIELDS
            - fieldnames
        )

        if missing_fields:
            missing = ", ".join(
                sorted(
                    missing_fields
                )
            )

            raise SystemExit(
                f"{path} is missing required "
                f"field(s): {missing}"
            )

        rows: list[
            tuple[int, str]
        ] = []

        errors: list[str] = []

        for row_number, raw_row in enumerate(
            reader,
            start=2,
        ):
            raw_program_ak = (
                raw_row.get("PROGRAM_AK")
                or ""
            ).strip()

            posting_url = (
                raw_row.get("POSTING_URL")
                or ""
            ).strip()

            if not raw_program_ak:
                errors.append(
                    f"{path} row {row_number} "
                    "has no PROGRAM_AK."
                )
                continue

            try:
                program_ak = int(
                    raw_program_ak
                )
            except ValueError:
                errors.append(
                    f"{path} row {row_number} "
                    f"has invalid PROGRAM_AK "
                    f"{raw_program_ak!r}."
                )
                continue

            if program_ak < 1:
                errors.append(
                    f"{path} row {row_number} "
                    f"has invalid PROGRAM_AK "
                    f"{raw_program_ak!r}."
                )
                continue

            if not posting_url:
                errors.append(
                    f"{path} row {row_number} "
                    "has no POSTING_URL."
                )
                continue

            rows.append(
                (
                    program_ak,
                    posting_url,
                )
            )

    if errors:
        print(
            "Program-opportunity link data failed "
            "validation:",
            file=sys.stderr,
        )

        for error in errors:
            print(
                f"  - {error}",
                file=sys.stderr,
            )

        raise SystemExit(1)

    return rows


def sort_program_opportunities(
    groups: list[dict[str, Any]],
) -> None:
    """
    Give each program a stable opportunity order.

    Application end date is used first because it is
    the most useful deterministic ordering for
    recruitment postings.
    """
    for group in groups:
        for trade in group["trades"]:
            for program in trade["programs"]:
                program["opportunities"].sort(
                    key=opportunity_sort_key
                )


def opportunity_sort_key(
    posting: dict[str, Any],
) -> tuple[str, str, str]:
    return (
        str(
            posting.get(
                "applicationEndDate"
            )
            or "9999-12-31"
        ),
        str(
            posting.get(
                "applicationStartDate"
            )
            or "9999-12-31"
        ),
        str(
            posting.get("id")
            or ""
        ),
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
    Stage the complete enriched program dataset, then
    replace programs/out only after every file has
    been written successfully.
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

        if PROGRAM_OUTPUT_ROOT.exists():
            PROGRAM_OUTPUT_ROOT.rename(
                BACKUP_OUTPUT_ROOT
            )

        TEMP_OUTPUT_ROOT.rename(
            PROGRAM_OUTPUT_ROOT
        )

    except Exception:
        if (
            PROGRAM_OUTPUT_ROOT.exists()
            and BACKUP_OUTPUT_ROOT.exists()
        ):
            shutil.rmtree(
                PROGRAM_OUTPUT_ROOT
            )

        if (
            not PROGRAM_OUTPUT_ROOT.exists()
            and BACKUP_OUTPUT_ROOT.exists()
        ):
            BACKUP_OUTPUT_ROOT.rename(
                PROGRAM_OUTPUT_ROOT
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
