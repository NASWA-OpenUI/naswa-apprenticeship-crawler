from __future__ import annotations

import csv
import io
import json
import re
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI

MODEL = "gpt-5.4"

JSON_ROOT = Path("./json")

OUTPUT_DIR = Path("./job-descriptions")
OUTPUT_CSV_PATH = OUTPUT_DIR / "job-descriptions.csv"

CSV_FIELDNAMES = [
    "id",
    "sourceUrl",
    "jobTitle",
    "displayJobTitle",
    "promptJobTitle",
    "socCode",
    "description",
]

SYSTEM_PROMPT = """
You write short, plain-language job descriptions for people exploring careers.

Rules:
- Explain what the full job does, not what an apprentice or trainee does.
- Write at about a grade 9 reading level.
- Keep it to about 50-60 words.
- In the first sentence, capitalize the job title exactly like the display job title. For example, write "A Sheet Metal Worker..." not "A sheet metal worker..."
- Use this structure:
  1. Start with what the job is.
  2. Describe the main hands-on tasks.
  3. End with why the work matters in everyday life.
- Be concrete and visual: help the reader picture the workday.
- Keep the purpose sentence grounded and practical, not heroic, exaggerated, or salesy.
- The final sentence should explain what the work protects, improves, builds, keeps running, or makes safer.
- Avoid hype, jargon, and overly formal wording.
- Return only the description text, with no heading and no bullet points.
""".strip()


def main() -> None:
    load_dotenv()

    if not JSON_ROOT.exists():
        raise FileNotFoundError(f"JSON directory not found: {JSON_ROOT}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    job_files = get_job_files(JSON_ROOT)
    existing_rows = load_existing_description_rows(OUTPUT_CSV_PATH)

    existing_rows_by_id = {
        row["id"]: row
        for row in existing_rows
    }

    # Seed the reuse cache from every existing row before archived rows are
    # removed. This allows a new posting to reuse a suitable description from
    # an older posting with the same normalized title and SOC code.
    description_cache = build_description_cache(existing_rows)

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

        job_id = read_required_string(job_data, "id", job_path)
        job_url = read_required_string(job_data, "sourceUrl", job_path)
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

        display_job_title = normalize_display_job_title(raw_job_title)
        prompt_job_title = normalize_prompt_job_title(raw_job_title)
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

        code_label = f" ({soc_code})" if soc_code else ""

        print(
            f"{action.capitalize()}: "
            f"{display_job_title}{code_label}",
            file=sys.stderr,
            flush=True,
        )

    archived_job_ids = set(existing_rows_by_id) - current_job_ids
    removed_count = len(archived_job_ids)

    output_changed = write_description_rows(output_rows)

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


def load_job_file(path: Path) -> dict[str, Any]:
    """Load one extracted job JSON file."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
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

        fieldnames = set(reader.fieldnames or [])
        missing_fields = set(CSV_FIELDNAMES) - fieldnames

        if missing_fields:
            missing = ", ".join(sorted(missing_fields))

            raise ValueError(
                f"{path} is missing required column(s): {missing}"
            )

        rows: list[dict[str, str]] = []
        seen_ids: set[str] = set()

        for row_number, raw_row in enumerate(
            reader,
            start=2,
        ):
            row = {
                field: (raw_row.get(field) or "").strip()
                for field in CSV_FIELDNAMES
            }

            job_id = row["id"]

            if not job_id:
                raise ValueError(
                    f"{path} row {row_number} has no id."
                )

            if job_id in seen_ids:
                raise ValueError(
                    f"{path} contains duplicate id: {job_id}"
                )

            if not row["description"]:
                raise ValueError(
                    f"{path} row {row_number} has no description."
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

    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"{path} is missing required string field: {key}"
        )

    return value.strip()


def read_soc_code(data: dict[str, Any]) -> str | None:
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
        if isinstance(value, str) and value.strip():
            return value.strip()

    return None


def normalized_cache_title(value: str) -> str:
    """Normalize a prompt title for case-insensitive cache comparisons."""
    return " ".join(value.strip().split()).casefold()


def normalized_cache_soc_code(value: str | None) -> str:
    """Normalize a SOC code for cache and existing-row comparisons."""
    return (value or "").strip().upper()


def description_cache_key(
    prompt_job_title: str,
    soc_code: str | None,
) -> tuple[str, str] | None:
    """
    Return the title-and-SOC key used to reuse descriptions.

    Jobs without a SOC code deliberately receive no cache key. Their
    descriptions may be preserved for the same job ID, but they are not
    copied to a different posting.
    """
    normalized_title = normalized_cache_title(prompt_job_title)
    normalized_soc_code = normalized_cache_soc_code(soc_code)

    if not normalized_title or not normalized_soc_code:
        return None

    return normalized_title, normalized_soc_code


def build_description_cache(
    existing_rows: list[dict[str, str]],
) -> dict[tuple[str, str], str]:
    """
    Build a reusable description cache from the existing CSV.

    The first description found for a normalized prompt title and SOC code is
    retained. Conflicting existing descriptions are reported for review.
    """
    cache: dict[tuple[str, str], str] = {}

    for row in existing_rows:
        cache_key = description_cache_key(
            row["promptJobTitle"],
            row["socCode"],
        )

        if cache_key is None:
            continue

        description = row["description"]
        cached_description = cache.get(cache_key)

        if (
            cached_description is not None
            and cached_description != description
        ):
            print(
                "Warning: multiple existing descriptions were found for "
                f"{row['promptJobTitle']} ({row['socCode']}). "
                "The first description will be reused.",
                file=sys.stderr,
            )
            continue

        cache[cache_key] = description

    return cache


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

    existing_soc_code = normalized_cache_soc_code(
        existing_row["socCode"]
    )
    current_soc_code = normalized_cache_soc_code(
        soc_code
    )

    return (
        existing_title == current_title
        and existing_soc_code == current_soc_code
    )


def get_description(
    *,
    client: OpenAI,
    display_job_title: str,
    prompt_job_title: str,
    soc_code: str | None,
    description_cache: dict[tuple[str, str], str],
) -> tuple[str, str]:
    """
    Return a description and the action used to obtain it.

    Actions:
    - reused: copied from an existing or earlier row with the same normalized
      prompt title and SOC code
    - generated: newly generated through the OpenAI API
    """
    cache_key = description_cache_key(
        prompt_job_title,
        soc_code,
    )

    if cache_key is not None:
        cached_description = description_cache.get(cache_key)

        if cached_description is not None:
            return cached_description, "reused"

        description = describe_profession(
            client=client,
            display_profession=display_job_title,
            prompt_profession=prompt_job_title,
        )

        # Make the newly generated description immediately available to later
        # jobs in this same run.
        description_cache[cache_key] = description

        return description, "generated"

    description = describe_profession(
        client=client,
        display_profession=display_job_title,
        prompt_profession=prompt_job_title,
    )

    return description, "generated"


def render_description_csv(
    rows: list[dict[str, str]],
) -> str:
    """Render the complete desired CSV into memory."""
    buffer = io.StringIO(newline="")

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
    new_content = render_description_csv(rows)

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


def normalize_prompt_job_title(job_title: str) -> str:
    """
    Clean the job title for the model prompt.

    Keep useful parenthetical qualifiers such as:
    - Boilermaker (Construction)
    - Ironworker (Outside)
    - Painter and Decorator (Structural Steel-Bridges)

    Remove apprenticeship and program-length wording because the description
    should explain the full occupation, not the training program.
    """
    normalized = job_title.strip()

    normalized = remove_apprenticeship_terms(normalized)
    normalized = normalize_conjunctions(normalized)
    normalized = normalize_punctuation_spacing(normalized)

    if not normalized:
        raise ValueError(
            f"Could not normalize prompt job title: {job_title}"
        )

    return normalized


def normalize_display_job_title(job_title: str) -> str:
    """
    Clean the job title for display as a page heading.

    Examples:
    - Tile Setter (3 Year) -> Tile Setter
    - Boilermaker (Construction) -> Boilermaker
    - Ironworker (Outside) -> Ironworker
    - Dry Wall Taper (Finisher) -> Drywall Taper
    - Painter, Decorator & Paperhanger ->
      Painter, Decorator, and Paperhanger
    """
    normalized = job_title.strip()

    normalized = remove_apprenticeship_terms(normalized)
    normalized = remove_parenthetical_qualifiers(normalized)
    normalized = normalize_conjunctions(normalized)
    normalized = normalize_known_title_variants(normalized)
    normalized = normalize_punctuation_spacing(normalized)

    if not normalized:
        raise ValueError(
            f"Could not normalize display job title: {job_title}"
        )

    return normalized


def remove_apprenticeship_terms(job_title: str) -> str:
    """Remove apprentice, apprenticeship, and program-length wording."""
    normalized = job_title

    normalized = re.sub(
        r"\b\d+\s*[- ]?\s*year\b",
        "",
        normalized,
        flags=re.IGNORECASE,
    )
    normalized = re.sub(
        r"\bapprenticeship\b",
        "",
        normalized,
        flags=re.IGNORECASE,
    )
    normalized = re.sub(
        r"\bapprentice\b",
        "",
        normalized,
        flags=re.IGNORECASE,
    )

    return normalized


def remove_parenthetical_qualifiers(job_title: str) -> str:
    """Remove bracketed qualifiers from the display title."""
    return re.sub(
        r"\s*\([^)]*\)",
        "",
        job_title,
    )


def normalize_conjunctions(job_title: str) -> str:
    """
    Normalize ampersands and missing Oxford commas.

    This intentionally does not try to solve every grammar case. It handles
    the common title shapes in the extracted job data.
    """
    normalized = job_title.replace("&", "and")

    # Painter, Decorator and Paperhanger
    # -> Painter, Decorator, and Paperhanger
    normalized = re.sub(
        r",\s*([^,]+?)\s+and\s+([^,()]+)$",
        r", \1, and \2",
        normalized,
        flags=re.IGNORECASE,
    )

    # Linoleum, Resilient Tile and Carpet Layer
    # -> Linoleum, Resilient Tile, and Carpet Layer
    normalized = re.sub(
        r",\s*([^,]+?)\s+and\s+([^,()]+)",
        r", \1, and \2",
        normalized,
        flags=re.IGNORECASE,
    )

    # Pointer, Caulker and Cleaner
    # -> Pointer, Caulker, and Cleaner
    normalized = re.sub(
        r"^([^,]+),\s*([^,]+?)\s+and\s+(.+)$",
        r"\1, \2, and \3",
        normalized,
        flags=re.IGNORECASE,
    )

    return normalized


def normalize_known_title_variants(job_title: str) -> str:
    """Normalize a few known title variants from the source data."""
    replacements = {
        "Dry Wall Taper": "Drywall Taper",
        "Cabinetmaker": "Cabinet Maker",
    }

    normalized = job_title

    for old, new in replacements.items():
        normalized = re.sub(
            rf"\b{re.escape(old)}\b",
            new,
            normalized,
            flags=re.IGNORECASE,
        )

    return normalized


def normalize_punctuation_spacing(job_title: str) -> str:
    """
    Clean whitespace, comma spacing, slash spacing, and dangling punctuation.
    """
    normalized = job_title

    normalized = re.sub(
        r"\s*/\s*",
        "/",
        normalized,
    )
    normalized = re.sub(
        r"\s*,\s*",
        ", ",
        normalized,
    )
    normalized = re.sub(
        r"\s+",
        " ",
        normalized,
    )

    normalized = normalized.strip(" -–—,/")

    return normalized


def describe_profession(
    *,
    client: OpenAI,
    display_profession: str,
    prompt_profession: str,
) -> str:
    """Generate one plain-language occupation description."""
    response = client.responses.create(
        model=MODEL,
        input=[
            {
                "role": "system",
                "content": SYSTEM_PROMPT,
            },
            {
                "role": "user",
                "content": build_user_prompt(
                    display_profession=display_profession,
                    prompt_profession=prompt_profession,
                ),
            },
        ],
    )

    if response.status != "completed":
        raise RuntimeError(
            f"{MODEL} did not complete for "
            f"{display_profession}. "
            f"Status: {response.status}"
        )

    if not response.output_text:
        raise RuntimeError(
            f"{MODEL} returned no output_text for "
            f"{display_profession}."
        )

    return clean_description(response.output_text)


def build_user_prompt(
    *,
    display_profession: str,
    prompt_profession: str,
) -> str:
    """Build the occupation-description request sent to the model."""
    return f"""
Question:
What is a {display_profession}?

Source job title:
{prompt_profession}

Write a plain-language answer in about 50-60 words.

Use the source job title as context. If it contains a parenthetical qualifier,
use that qualifier to understand the kind of work, but do not include
parentheses in the final description. Work the qualifier naturally into the
first sentence if it is useful.

For example:
- "Boilermaker (Construction)" can become "A Boilermaker in construction..."
- "Electrician (Housewire or Residential)" can become
  "A residential Electrician..."
- "Painter and Decorator (Structural Steel Bridges)" can become
  "A Painter and Decorator working on structural steel bridges..."
- "Ironworker (Outside)" can become "An outside Ironworker..."
- "Welder (Industrial)" can become "An industrial Welder..."

Good style examples:

An Insulation and Asbestos Worker measures, cuts, fits, and applies insulation
to pipes, boilers, ducts, and other mechanical systems. This helps control
temperature, save energy, and protect equipment. They may also safely remove
asbestos-containing materials from older buildings.

A Pointer, Caulker, and Cleaner repairs and protects the outside of buildings,
bridges, and chimneys. They remove crumbling mortar, press fresh mortar between
bricks or stone, seal joints with caulk, and wash or blast away dirt and
stains, often from scaffolds. This keeps walls strong, dry, safe, and well
maintained.

A Construction and General Building Laborer does basic physical work on
building sites. They carry and load materials, dig trenches, mix concrete, set
up and take down scaffolding, clean work areas, and use hand and power tools to
help skilled tradespeople. Their work keeps projects moving, sites safer, and
buildings taking shape.
""".strip()


def clean_description(text: str) -> str:
    """Keep each description on one line for easier CSV comparison."""
    return " ".join(text.strip().split())


if __name__ == "__main__":
    main()
