from __future__ import annotations

import csv
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from jsonschema import Draft202012Validator


API_BASE = "https://api-v2.onetcenter.org"

PROGRAM_JSON_DIR = Path("./programs/json")
SCHEMA_PATH = Path("./schemas/program-group.schema.json")
MAPPINGS_CSV_PATH = Path("./programs/soc-code-mappings.csv")

MAPPING_FIELDNAMES = [
    "sourceSocCode",
    "sourceSocTitle",
    "targetSocCode",
    "targetSocTitle",
    "reason",
]

REQUEST_DELAY_SECONDS = 0.5
RETRY_DELAY_SECONDS = 1
MAX_REQUEST_ATTEMPTS = 3


@dataclass(frozen=True)
class Occupation:
    code: str
    title: str


@dataclass(frozen=True)
class SocResolution:
    source_code: str
    source_title: str
    program_count: int
    status: str
    targets: tuple[Occupation, ...]
    reason: str | None = None


def main() -> None:
    load_dotenv()

    api_key = os.getenv("ONET_API_KEY")

    if not api_key:
        raise RuntimeError(
            "Missing ONET_API_KEY in your environment or .env file."
        )

    require_directory(
        PROGRAM_JSON_DIR,
        "program JSON directory",
    )
    require_file(
        SCHEMA_PATH,
        "program-group JSON schema",
    )

    schema = load_json_object(SCHEMA_PATH)

    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)

    groups = load_program_groups(
        PROGRAM_JSON_DIR,
        validator=validator,
    )

    if not groups:
        raise SystemExit(
            f"No program group JSON files found in {PROGRAM_JSON_DIR}."
        )

    existing_mapping_rows = load_existing_mapping_rows(
        MAPPINGS_CSV_PATH
    )

    existing_mappings_by_source_code = {
        row["sourceSocCode"]: row
        for row in existing_mapping_rows
    }

    resolutions: dict[str, SocResolution] = {}

    print(
        f"Checking {len(groups)} program SOC group(s) against O*NET...",
        file=sys.stderr,
    )
    print("", file=sys.stderr)

    for index, group in enumerate(
        groups,
        start=1,
    ):
        soc_code = group["socCode"]
        soc_title = group["socTitle"]
        program_count = group["programCount"]

        print(
            f"[{index}/{len(groups)}] "
            f"{soc_code} — {soc_title}",
            file=sys.stderr,
            flush=True,
        )

        resolution = resolve_soc_code(
            soc_code=soc_code,
            soc_title=soc_title,
            program_count=program_count,
            api_key=api_key,
        )

        resolutions[soc_code] = resolution

    mapping_additions, mapping_warnings = build_mapping_additions(
        resolutions=resolutions,
        existing_mappings_by_source_code=existing_mappings_by_source_code,
    )

    print_report(
        source_group_count=len(groups),
        resolutions=resolutions,
    )

    print_mapping_report(
        existing_mapping_count=len(existing_mapping_rows),
        mapping_additions=mapping_additions,
        mapping_warnings=mapping_warnings,
    )

    if mapping_additions:
        append_mapping_rows(
            MAPPINGS_CSV_PATH,
            mapping_additions,
        )

        print("")
        print(
            f"Updated: {MAPPINGS_CSV_PATH}"
        )
    else:
        print("")
        print(
            "No new SOC mappings were added."
        )

    print("")
    print(
        "No program JSON files were changed."
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


def load_program_groups(
    root: Path,
    *,
    validator: Draft202012Validator,
) -> list[dict[str, Any]]:
    """
    Load and validate all current SOC-grouped program files.

    The audit does not modify program data, but it should still refuse to
    audit malformed generated data.
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
            group = load_json_object(path)
        except SystemExit as exc:
            errors.append(str(exc))
            continue

        schema_errors = sorted(
            validator.iter_errors(group),
            key=lambda error: list(error.absolute_path),
        )

        for error in schema_errors:
            errors.append(
                f"{path} {format_json_path(error.absolute_path)}: "
                f"{error.message}"
            )

        soc_code = group.get("socCode")

        if not isinstance(soc_code, str):
            continue

        if path.stem != soc_code:
            errors.append(
                f"{path}: filename SOC {path.stem!r} does not match "
                f"the group's socCode {soc_code!r}."
            )

        if soc_code in seen_soc_codes:
            errors.append(
                f"Duplicate SOC group found: {soc_code}"
            )

        seen_soc_codes.add(soc_code)
        groups.append(group)

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


def request_json(
    url: str,
    api_key: str,
    *,
    allow_unprocessable: bool = False,
) -> dict[str, Any] | None:
    """
    Fetch one O*NET JSON endpoint.

    HTTP 422 may be treated as an expected indication that a SOC code is not
    valid for the requested endpoint.

    All other HTTP errors remain fatal.
    """
    response: requests.Response | None = None

    for attempt in range(
        1,
        MAX_REQUEST_ATTEMPTS + 1,
    ):
        response = requests.get(
            url,
            headers={
                "X-API-Key": api_key,
            },
            timeout=30,
        )

        if response.status_code != 429:
            break

        if attempt == MAX_REQUEST_ATTEMPTS:
            break

        time.sleep(
            RETRY_DELAY_SECONDS
        )

    assert response is not None

    time.sleep(
        REQUEST_DELAY_SECONDS
    )

    if (
        response.status_code == 422
        and allow_unprocessable
    ):
        return None

    response.raise_for_status()

    data = response.json()

    if not isinstance(data, dict):
        raise RuntimeError(
            f"Expected a JSON object from O*NET: {url}"
        )

    return data


def fetch_current_occupation(
    soc_code: str,
    api_key: str,
) -> Occupation | None:
    """
    Return the current O*NET occupation for an active SOC code.

    Return None when O*NET rejects the code with HTTP 422.
    """
    url = (
        f"{API_BASE}/online/occupations/"
        f"{soc_code}"
    )

    data = request_json(
        url,
        api_key,
        allow_unprocessable=True,
    )

    if data is None:
        return None

    code = data.get("code")
    title = data.get("title")

    if (
        not isinstance(code, str)
        or not code.strip()
        or not isinstance(title, str)
        or not title.strip()
    ):
        raise RuntimeError(
            f"O*NET returned an incomplete occupation response "
            f"for {soc_code}."
        )

    return Occupation(
        code=code.strip(),
        title=title.strip(),
    )


def fetch_2010_to_active_crosswalk(
    soc_code: str,
    api_key: str,
) -> tuple[Occupation, ...] | None:
    """
    Crosswalk an O*NET-SOC 2010 code to occupations currently usable by
    O*NET Web Services.

    Return None when the supplied code is not accepted by this crosswalk.
    """
    url = (
        f"{API_BASE}/taxonomy/2010/active/"
        f"{soc_code}"
    )

    data = request_json(
        url,
        api_key,
        allow_unprocessable=True,
    )

    if data is None:
        return None

    raw_occupations = data.get("occupation")

    if not isinstance(
        raw_occupations,
        list,
    ):
        raise RuntimeError(
            f"O*NET taxonomy response for {soc_code} "
            "does not contain an occupation list."
        )

    occupations: list[Occupation] = []
    seen_codes: dict[str, str] = {}

    for raw_occupation in raw_occupations:
        if not isinstance(
            raw_occupation,
            dict,
        ):
            raise RuntimeError(
                f"O*NET taxonomy response for {soc_code} "
                "contains a non-object occupation."
            )

        code = raw_occupation.get("code")
        title = raw_occupation.get("title")

        if (
            not isinstance(code, str)
            or not code.strip()
            or not isinstance(title, str)
            or not title.strip()
        ):
            raise RuntimeError(
                f"O*NET taxonomy response for {soc_code} "
                "contains an incomplete occupation."
            )

        code = code.strip()
        title = title.strip()

        previous_title = seen_codes.get(code)

        if (
            previous_title is not None
            and previous_title != title
        ):
            raise RuntimeError(
                f"O*NET taxonomy response for {soc_code} "
                f"returned conflicting titles for {code}: "
                f"{previous_title!r} and {title!r}."
            )

        if previous_title is not None:
            continue

        seen_codes[code] = title

        occupations.append(
            Occupation(
                code=code,
                title=title,
            )
        )

    return tuple(occupations)


def resolve_soc_code(
    *,
    soc_code: str,
    soc_title: str,
    program_count: int,
    api_key: str,
) -> SocResolution:
    """
    Audit one program SOC group.

    Classifications:

    current
        The code is accepted by the current O*NET occupation endpoint.

    update
        The source code is obsolete and the official crosswalk identifies
        exactly one current replacement.

    ambiguous
        The official crosswalk identifies more than one possible current
        occupation.

    unresolved
        The code is neither current nor recognized by the 2010-to-active
        crosswalk.
    """
    current_occupation = fetch_current_occupation(
        soc_code,
        api_key,
    )

    if current_occupation is not None:
        return SocResolution(
            source_code=soc_code,
            source_title=soc_title,
            program_count=program_count,
            status="current",
            targets=(
                current_occupation,
            ),
        )

    crosswalk = fetch_2010_to_active_crosswalk(
        soc_code,
        api_key,
    )

    if crosswalk is None:
        return SocResolution(
            source_code=soc_code,
            source_title=soc_title,
            program_count=program_count,
            status="unresolved",
            targets=(),
            reason=(
                "The code is not accepted by the current occupation "
                "endpoint and is not accepted by the 2010-to-active "
                "taxonomy crosswalk."
            ),
        )

    if not crosswalk:
        return SocResolution(
            source_code=soc_code,
            source_title=soc_title,
            program_count=program_count,
            status="unresolved",
            targets=(),
            reason=(
                "The 2010-to-active taxonomy crosswalk returned "
                "no occupations."
            ),
        )

    if len(crosswalk) > 1:
        return SocResolution(
            source_code=soc_code,
            source_title=soc_title,
            program_count=program_count,
            status="ambiguous",
            targets=crosswalk,
        )

    mapped_occupation = crosswalk[0]

    # Verify the returned target really is accepted by the current
    # occupation endpoint before recording it as an automatic mapping.
    verified_occupation = fetch_current_occupation(
        mapped_occupation.code,
        api_key,
    )

    if verified_occupation is None:
        raise RuntimeError(
            f"O*NET mapped {soc_code} to "
            f"{mapped_occupation.code}, but that target code is not "
            "accepted by the current occupation endpoint."
        )

    if verified_occupation.code != mapped_occupation.code:
        raise RuntimeError(
            f"O*NET crosswalk target mismatch for {soc_code}: "
            f"{mapped_occupation.code} became "
            f"{verified_occupation.code}."
        )

    return SocResolution(
        source_code=soc_code,
        source_title=soc_title,
        program_count=program_count,
        status="update",
        targets=(
            verified_occupation,
        ),
    )


def load_existing_mapping_rows(
    path: Path,
) -> list[dict[str, str]]:
    """
    Load and validate the reviewed SOC mapping CSV.

    The audit never modifies existing rows. A source SOC code may therefore
    appear only once.
    """
    if not path.exists():
        return []

    with path.open(
        newline="",
        encoding="utf-8",
    ) as csv_file:
        reader = csv.DictReader(csv_file)

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

            raise ValueError(
                f"{path} is missing required column(s): "
                f"{missing}"
            )

        rows: list[dict[str, str]] = []
        seen_source_codes: set[str] = set()

        for row_number, raw_row in enumerate(
            reader,
            start=2,
        ):
            row = {
                field: (
                    raw_row.get(field) or ""
                ).strip()
                for field in MAPPING_FIELDNAMES
            }

            source_soc_code = row[
                "sourceSocCode"
            ]
            source_soc_title = row[
                "sourceSocTitle"
            ]

            if not source_soc_code:
                raise ValueError(
                    f"{path} row {row_number} has no "
                    "sourceSocCode."
                )

            if not source_soc_title:
                raise ValueError(
                    f"{path} row {row_number} has no "
                    "sourceSocTitle."
                )

            if source_soc_code in seen_source_codes:
                raise ValueError(
                    f"{path} contains duplicate sourceSocCode: "
                    f"{source_soc_code}"
                )

            target_soc_code = row[
                "targetSocCode"
            ]
            target_soc_title = row[
                "targetSocTitle"
            ]

            # Target code/title should either both be present or both blank.
            if bool(target_soc_code) != bool(target_soc_title):
                raise ValueError(
                    f"{path} row {row_number} must either provide "
                    "both targetSocCode and targetSocTitle or leave "
                    "both blank."
                )

            seen_source_codes.add(
                source_soc_code
            )
            rows.append(row)

    return rows


def build_mapping_additions(
    *,
    resolutions: dict[str, SocResolution],
    existing_mappings_by_source_code: dict[str, dict[str, str]],
) -> tuple[
    list[dict[str, str]],
    list[str],
]:
    """
    Build rows that should be appended to soc-code-mappings.csv.

    Rules:
    - Current SOC codes are not added.
    - Unambiguous official replacements get complete mapping rows.
    - Ambiguous and unresolved SOCs get source code/title only.
    - Existing mappings are never overwritten.
    """
    additions: list[dict[str, str]] = []
    warnings: list[str] = []

    for resolution in sorted(
        resolutions.values(),
        key=lambda item: item.source_code,
    ):
        if resolution.status == "current":
            continue

        existing_row = (
            existing_mappings_by_source_code.get(
                resolution.source_code
            )
        )

        if existing_row is not None:
            if (
                existing_row["sourceSocTitle"]
                != resolution.source_title
            ):
                warnings.append(
                    f"{resolution.source_code}: mapping CSV has "
                    f"source title "
                    f"{existing_row['sourceSocTitle']!r}, "
                    f"but current program data has "
                    f"{resolution.source_title!r}. "
                    "Existing mapping was left unchanged."
                )

            continue

        if resolution.status == "update":
            target = resolution.targets[0]

            additions.append(
                {
                    "sourceSocCode": resolution.source_code,
                    "sourceSocTitle": resolution.source_title,
                    "targetSocCode": target.code,
                    "targetSocTitle": target.title,
                    "reason": (
                        "Official O*NET taxonomy replacement"
                    ),
                }
            )

        elif resolution.status in {
            "ambiguous",
            "unresolved",
        }:
            additions.append(
                {
                    "sourceSocCode": resolution.source_code,
                    "sourceSocTitle": resolution.source_title,
                    "targetSocCode": "",
                    "targetSocTitle": "",
                    "reason": "",
                }
            )

    return additions, warnings


def append_mapping_rows(
    path: Path,
    rows: list[dict[str, str]],
) -> None:
    """
    Append newly discovered mappings without changing existing rows.

    Create the CSV and its header when it does not yet exist.
    """
    if not rows:
        return

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    file_exists = path.exists()

    with path.open(
        "a",
        newline="",
        encoding="utf-8",
    ) as csv_file:
        writer = csv.DictWriter(
            csv_file,
            fieldnames=MAPPING_FIELDNAMES,
            lineterminator="\n",
        )

        if not file_exists:
            writer.writeheader()

        writer.writerows(rows)


def print_report(
    *,
    source_group_count: int,
    resolutions: dict[str, SocResolution],
) -> None:
    current = [
        resolution
        for resolution in resolutions.values()
        if resolution.status == "current"
    ]

    updates = [
        resolution
        for resolution in resolutions.values()
        if resolution.status == "update"
    ]

    ambiguous = [
        resolution
        for resolution in resolutions.values()
        if resolution.status == "ambiguous"
    ]

    unresolved = [
        resolution
        for resolution in resolutions.values()
        if resolution.status == "unresolved"
    ]

    program_count = sum(
        resolution.program_count
        for resolution in resolutions.values()
    )

    affected_program_count = sum(
        resolution.program_count
        for resolution in updates
    )

    print("")
    print("Program SOC audit:")
    print("")
    print(
        f"  SOC groups checked:              "
        f"{source_group_count}"
    )
    print(
        f"  Programs represented:            "
        f"{program_count}"
    )
    print("")
    print(
        f"  Already current:                 "
        f"{len(current)}"
    )
    print(
        f"  Unambiguous updates:             "
        f"{len(updates)}"
    )
    print(
        f"  Programs affected by updates:    "
        f"{affected_program_count}"
    )
    print(
        f"  Ambiguous mappings:              "
        f"{len(ambiguous)}"
    )
    print(
        f"  Unresolved codes:                "
        f"{len(unresolved)}"
    )

    if updates:
        print("")
        print("Unambiguous updates:")

        for resolution in sorted(
            updates,
            key=lambda item: item.source_code,
        ):
            target = resolution.targets[0]

            print(
                f"  {resolution.source_code}  "
                f"{resolution.source_title}"
            )
            print(
                f"      → {target.code}  "
                f"{target.title}"
            )
            print(
                f"        Programs: "
                f"{resolution.program_count}"
            )

    if ambiguous:
        print("")
        print(
            "Ambiguous mappings "
            "(manual review required):"
        )

        for resolution in sorted(
            ambiguous,
            key=lambda item: item.source_code,
        ):
            print(
                f"  {resolution.source_code}  "
                f"{resolution.source_title}"
            )
            print(
                f"      Programs: "
                f"{resolution.program_count}"
            )

            for target in resolution.targets:
                print(
                    f"      → {target.code}  "
                    f"{target.title}"
                )

    if unresolved:
        print("")
        print(
            "Unresolved codes "
            "(manual review required):"
        )

        for resolution in sorted(
            unresolved,
            key=lambda item: item.source_code,
        ):
            print(
                f"  {resolution.source_code}  "
                f"{resolution.source_title}"
            )
            print(
                f"      Programs: "
                f"{resolution.program_count}"
            )

            if resolution.reason:
                print(
                    f"      Reason: "
                    f"{resolution.reason}"
                )


def print_mapping_report(
    *,
    existing_mapping_count: int,
    mapping_additions: list[dict[str, str]],
    mapping_warnings: list[str],
) -> None:
    complete_additions = [
        row
        for row in mapping_additions
        if row["targetSocCode"]
    ]

    review_additions = [
        row
        for row in mapping_additions
        if not row["targetSocCode"]
    ]

    print("")
    print("SOC mapping file:")
    print("")
    print(
        f"  Existing mappings:               "
        f"{existing_mapping_count}"
    )
    print(
        f"  Official mappings added:         "
        f"{len(complete_additions)}"
    )
    print(
        f"  Manual-review rows added:        "
        f"{len(review_additions)}"
    )

    if complete_additions:
        print("")
        print("Mappings to add:")

        for row in complete_additions:
            print(
                f"  {row['sourceSocCode']}  "
                f"{row['sourceSocTitle']}"
            )
            print(
                f"      → {row['targetSocCode']}  "
                f"{row['targetSocTitle']}"
            )

    if review_additions:
        print("")
        print("Manual-review rows to add:")

        for row in review_additions:
            print(
                f"  {row['sourceSocCode']}  "
                f"{row['sourceSocTitle']}"
            )

    if mapping_warnings:
        print("")
        print("Mapping warnings:")

        for warning in mapping_warnings:
            print(
                f"  - {warning}"
            )


def format_json_path(
    path: Any,
) -> str:
    parts = ["$"]

    for value in path:
        if isinstance(value, int):
            parts.append(
                f"[{value}]"
            )
        else:
            parts.append(
                f".{value}"
            )

    return "".join(parts)


if __name__ == "__main__":
    main()
