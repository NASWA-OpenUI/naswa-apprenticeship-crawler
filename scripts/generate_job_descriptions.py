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


JSON_ROOT = Path("./json")

OUTPUT_DIR = Path("./job-descriptions")
OUTPUT_CSV_PATH = OUTPUT_DIR / "job-descriptions-postings.csv"

CSV_FIELDNAMES = [
    "id",
    "sourceUrl",
    "jobTitle",
    "displayJobTitle",
    "promptJobTitle",
    "socCode",
    "description",
]


def main() -> None:
    load_dotenv()

    if not JSON_ROOT.exists():
        raise FileNotFoundError(
            f"JSON directory not found: {JSON_ROOT}"
        )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    job_files = get_job_files(JSON_ROOT)
    existing_rows = load_existing_description_rows(
        OUTPUT_CSV_PATH
    )

    existing_rows_by_id = {
        row["id"]: row
        for row in existing_rows
    }

    # Seed the reuse cache from every existing row before archived rows are
    # removed. This allows a new posting to reuse a suitable description from
    # an older posting with the same normalized title and SOC code.
    description_cache = build_description_cache(
        existing_rows,
        prompt_title_field="promptJobTitle",
    )

    client = OpenAI()

    output_rows: list[dict[str, str]] = []
    current_job_ids: set[str] = set()

    kept_count = 0
    reused_count = 0
    generated_count = 0
    new_job_count = 0
    changed_existing_job_count = 0

    print(
        f"Found {len(job_files)} current job file(s).",
        file=sys.stderr,
    )

    if existing_rows:
        print(
            f"Loaded {len(existing_rows)} existing description row(s).",
            file=sys.stderr,
        )
    else:
        print(
            "No existing description CSV found. "
            "Generating the initial description set.",
            file=sys.stderr,
        )

    for job_path in job_files:
        job_data = load_job_file(job_path)

        job_id = read_required_string(
            job_data,
            "id",
            job_path,
        )
        job_url = read_required_string(
            job_data,
            "sourceUrl",
            job_path,
        )
        raw_job_title = read_required_string(
            job_data,
            "jobTitle",
            job_path,
        )

        if job_id in current_job_ids:
            raise ValueError(
                f"Duplicate job id in current JSON files: {job_id}"
            )

        current_job_ids.add(job_id)

        display_job_title = normalize_display_job_title(
            raw_job_title
        )
        prompt_job_title = normalize_prompt_job_title(
            raw_job_title
        )
        soc_code = read_soc_code(job_data)
        csv_soc_code = soc_code or ""

        existing_row = existing_rows_by_id.get(job_id)

        if existing_row is None:
            new_job_count += 1

        if can_keep_existing_description(
            existing_row=existing_row,
            prompt_job_title=prompt_job_title,
            soc_code=csv_soc_code,
        ):
            description = existing_row["description"]
            action = "kept"
            kept_count += 1

        else:
            description, action = get_description(
                client=client,
                display_job_title=display_job_title,
                prompt_job_title=prompt_job_title,
                soc_code=soc_code,
                description_cache=description_cache,
            )

            if action == "reused":
                reused_count += 1
            else:
                generated_count += 1

            if existing_row is not None:
                changed_existing_job_count += 1

        output_rows.append(
            {
                "id": job_id,
                "sourceUrl": job_url,
                "jobTitle": raw_job_title,
                "displayJobTitle": display_job_title,
                "promptJobTitle": prompt_job_title,
                "socCode": csv_soc_code,
                "description": description,
            }
        )

        code_label = (
            f" ({soc_code})"
            if soc_code
            else ""
        )

        print(
            f"{action.capitalize()}: "
            f"{display_job_title}{code_label}",
            file=sys.stderr,
            flush=True,
        )

    archived_job_ids = (
        set(existing_rows_by_id)
        - current_job_ids
    )
    removed_count = len(archived_job_ids)

    output_changed = write_description_rows(
        output_rows
    )

    print("", file=sys.stderr)
    print(
        "Job description reconciliation complete:",
        file=sys.stderr,
    )
    print(
        f"  Current jobs: {len(output_rows)}",
        file=sys.stderr,
    )
    print(
        f"  New jobs: {new_job_count}",
        file=sys.stderr,
    )
    print(
        f"  Existing descriptions kept: {kept_count}",
        file=sys.stderr,
    )
    print(
        f"  Existing descriptions reused: {reused_count}",
        file=sys.stderr,
    )
    print(
        f"  New descriptions generated: {generated_count}",
        file=sys.stderr,
    )
    print(
        "  Existing jobs with changed title or SOC code: "
        f"{changed_existing_job_count}",
        file=sys.stderr,
    )
    print(
        f"  Archived rows removed: {removed_count}",
        file=sys.stderr,
    )

    if archived_job_ids:
        print(
            "  Removed job IDs:",
            file=sys.stderr,
        )

        for job_id in sorted(archived_job_ids):
            print(
                f"    - {job_id}",
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


def get_job_files(root: Path) -> list[Path]:
    """
    Return every extracted job JSON file under:

    ./json/{posting_dir}/{job}.json
    """
    return sorted(
        path
        for path in root.glob("*/*.json")
        if path.is_file()
    )


def load_job_file(
    path: Path,
) -> dict[str, Any]:
    """Load one extracted job JSON file."""
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


def load_existing_description_rows(
    path: Path,
) -> list[dict[str, str]]:
    """Load and validate the existing job-description CSV."""
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
            set(CSV_FIELDNAMES)
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
        seen_ids: set[str] = set()

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

            job_id = row["id"]

            if not job_id:
                raise ValueError(
                    f"{path} row {row_number} has no id."
                )

            if job_id in seen_ids:
                raise ValueError(
                    f"{path} contains duplicate id: "
                    f"{job_id}"
                )

            if not row["description"]:
                raise ValueError(
                    f"{path} row {row_number} "
                    "has no description."
                )

            seen_ids.add(job_id)
            rows.append(row)

    return rows


def read_required_string(
    data: dict[str, Any],
    key: str,
    path: Path,
) -> str:
    """Read a required non-empty string from one job JSON object."""
    value = data.get(key)

    if (
        not isinstance(value, str)
        or not value.strip()
    ):
        raise ValueError(
            f"{path} is missing required string field: "
            f"{key}"
        )

    return value.strip()


def read_soc_code(
    data: dict[str, Any],
) -> str | None:
    """
    Read the O*NET/SOC code if present.

    The extractor currently uses root-level socCode, but the fallbacks make
    this safe if later files are merged or enriched.
    """
    posting = data.get("posting")
    onet = data.get("onet")

    candidates = [
        data.get("socCode"),
        data.get("onetSocCode"),
        (
            posting.get("socCode")
            if isinstance(posting, dict)
            else None
        ),
        (
            onet.get("socCode")
            if isinstance(onet, dict)
            else None
        ),
        (
            onet.get("onetSocCode")
            if isinstance(onet, dict)
            else None
        ),
    ]

    for value in candidates:
        if (
            isinstance(value, str)
            and value.strip()
        ):
            return value.strip()

    return None


def can_keep_existing_description(
    *,
    existing_row: dict[str, str] | None,
    prompt_job_title: str,
    soc_code: str,
) -> bool:
    """
    Return whether a current job can keep its existing description.

    The source URL, raw title, and display title may be updated in the output
    row without forcing new generated text. A new description is needed only
    when the normalized occupation title or SOC code changes.

    A same-ID job without a SOC code may retain its own description, but that
    description is not reused for another job.
    """
    if existing_row is None:
        return False

    if not existing_row["description"]:
        return False

    existing_title = normalized_cache_title(
        existing_row["promptJobTitle"]
    )
    current_title = normalized_cache_title(
        prompt_job_title
    )

    existing_soc_code = (
        normalized_cache_soc_code(
            existing_row["socCode"]
        )
    )
    current_soc_code = (
        normalized_cache_soc_code(
            soc_code
        )
    )

    return (
        existing_title == current_title
        and existing_soc_code == current_soc_code
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
    Write the complete CSV only when its contents have changed.

    All job loading, reconciliation, reuse, and generation has already
    completed before this function is called.

    Returns True when the CSV was created or updated.
    """
    new_content = render_description_csv(
        rows
    )

    if OUTPUT_CSV_PATH.exists():
        existing_content = (
            OUTPUT_CSV_PATH.read_text(
                encoding="utf-8",
            )
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
