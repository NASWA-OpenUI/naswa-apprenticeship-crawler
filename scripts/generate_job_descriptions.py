from __future__ import annotations

import csv
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

    if OUTPUT_CSV_PATH.exists():
        print(
            f"Output file already exists: {OUTPUT_CSV_PATH}",
            file=sys.stderr,
        )
        print(
            "Delete it manually before regenerating job descriptions.",
            file=sys.stderr,
        )
        sys.exit(1)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    job_files = get_job_files(JSON_ROOT)
    client = OpenAI()

    # Reuse descriptions for exact matches on normalized title + SOC/O*NET code.
    # If a job has no code, we intentionally do not cache/reuse it.
    description_cache: dict[tuple[str, str], str] = {}

    print(f"Found {len(job_files)} job file(s).", file=sys.stderr)
    print(f"Writing job descriptions to: {OUTPUT_CSV_PATH}", file=sys.stderr)

    with OUTPUT_CSV_PATH.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(
            csv_file,
            fieldnames=[
                "id",
                "sourceUrl",
                "jobTitle",
                "displayJobTitle",
                "promptJobTitle",
                "socCode",
                "description",
            ],
        )
        writer.writeheader()
        csv_file.flush()

        row_count = 0

        for job_path in job_files:
            job_data = load_job_file(job_path)

            job_id = read_required_string(job_data, "id", job_path)
            job_url = read_required_string(job_data, "sourceUrl", job_path)
            raw_job_title = read_required_string(job_data, "jobTitle", job_path)
            display_job_title = normalize_display_job_title(raw_job_title)
            prompt_job_title = normalize_prompt_job_title(raw_job_title)
            soc_code = read_soc_code(job_data)

            description = get_description(
                client=client,
                display_job_title=display_job_title,
                prompt_job_title=prompt_job_title,
                soc_code=soc_code,
                description_cache=description_cache,
            )

            writer.writerow(
                {
                    "id": job_id,
                    "sourceUrl": job_url,
                    "jobTitle": raw_job_title,
                    "displayJobTitle": display_job_title,
                    "promptJobTitle": prompt_job_title,
                    "socCode": soc_code or "",
                    "description": description,
                }
            )
            csv_file.flush()

            row_count += 1
            print(
                f"Added row {row_count}: {display_job_title}",
                file=sys.stderr,
                flush=True,
            )

    print(f"Done. Saved {row_count} row(s).", file=sys.stderr)


def get_job_files(root: Path) -> list[Path]:
    """Return every extracted job JSON file under ./json/{posting_dir}/{job}.json."""
    return sorted(path for path in root.glob("*/*.json") if path.is_file())


def load_job_file(path: Path) -> dict[str, Any]:
    """Load one extracted job JSON file."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object in {path}")

    return data


def read_required_string(data: dict[str, Any], key: str, path: Path) -> str:
    """Read a required non-empty string from one job JSON object."""
    value = data.get(key)

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path} is missing required string field: {key}")

    return value.strip()


def read_soc_code(data: dict[str, Any]) -> str | None:
    """
    Read the O*NET/SOC code if present.

    The extractor currently uses root-level socCode, but the fallbacks make this
    safe if later files are merged/enriched.
    """
    candidates = [
        data.get("socCode"),
        data.get("onetSocCode"),
        data.get("posting", {}).get("socCode")
        if isinstance(data.get("posting"), dict)
        else None,
        data.get("onet", {}).get("socCode") if isinstance(data.get("onet"), dict) else None,
        data.get("onet", {}).get("onetSocCode")
        if isinstance(data.get("onet"), dict)
        else None,
    ]

    for value in candidates:
        if isinstance(value, str) and value.strip():
            return value.strip()

    return None


def normalize_prompt_job_title(job_title: str) -> str:
    """
    Clean the job title for the model prompt.

    Keep useful parenthetical qualifiers such as:
    - Boilermaker (Construction)
    - Ironworker (Outside)
    - Painter and Decorator (Structural Steel-Bridges)

    Remove apprenticeship/program-length wording because the description should
    explain the full occupation, not the training program.
    """
    normalized = job_title.strip()

    normalized = remove_apprenticeship_terms(normalized)
    normalized = normalize_conjunctions(normalized)
    normalized = normalize_punctuation_spacing(normalized)

    if not normalized:
        raise ValueError(f"Could not normalize prompt job title: {job_title}")

    return normalized


def normalize_display_job_title(job_title: str) -> str:
    """
    Clean the job title for display as a page heading.

    Examples:
    - Tile Setter (3 Year) -> Tile Setter
    - Boilermaker (Construction) -> Boilermaker
    - Ironworker (Outside) -> Ironworker
    - Dry Wall Taper (Finisher) -> Drywall Taper
    - Painter, Decorator & Paperhanger -> Painter, Decorator, and Paperhanger
    """
    normalized = job_title.strip()

    normalized = remove_apprenticeship_terms(normalized)
    normalized = remove_parenthetical_qualifiers(normalized)
    normalized = normalize_conjunctions(normalized)
    normalized = normalize_known_title_variants(normalized)
    normalized = normalize_punctuation_spacing(normalized)

    if not normalized:
        raise ValueError(f"Could not normalize display job title: {job_title}")

    return normalized


def remove_apprenticeship_terms(job_title: str) -> str:
    """Remove apprentice/apprenticeship/program-length wording."""
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
    return re.sub(r"\s*\([^)]*\)", "", job_title)


def normalize_conjunctions(job_title: str) -> str:
    """
    Normalize ampersands and missing Oxford commas.

    This intentionally does not try to solve every grammar case. It handles the
    common title shapes in the extracted job data.
    """
    normalized = job_title.replace("&", "and")

    # Painter, Decorator and Paperhanger -> Painter, Decorator, and Paperhanger
    normalized = re.sub(
        r",\s*([^,]+?)\s+and\s+([^,()]+)$",
        r", \1, and \2",
        normalized,
        flags=re.IGNORECASE,
    )

    # Linoleum, Resilient Tile and Carpet Layer -> Linoleum, Resilient Tile, and Carpet Layer
    normalized = re.sub(
        r",\s*([^,]+?)\s+and\s+([^,()]+)",
        r", \1, and \2",
        normalized,
        flags=re.IGNORECASE,
    )

    # Pointer, Caulker and Cleaner -> Pointer, Caulker, and Cleaner
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
    """Clean up whitespace, comma spacing, slash spacing, and dangling punctuation."""
    normalized = job_title

    normalized = re.sub(r"\s*/\s*", "/", normalized)
    normalized = re.sub(r"\s*,\s*", ", ", normalized)
    normalized = re.sub(r"\s+", " ", normalized)

    normalized = normalized.strip(" -–—,/")

    return normalized

def get_description(
    *,
    client: OpenAI,
    display_job_title: str,
    prompt_job_title: str,
    soc_code: str | None,
    description_cache: dict[tuple[str, str], str],
) -> str:
    """
    Return a generated or cached description.

    Cache only when a SOC/O*NET code is present. Use the prompt title in the
    cache key so useful qualifiers still affect description reuse.
    """
    if soc_code:
        cache_key = (prompt_job_title, soc_code)

        if cache_key in description_cache:
            print(
                f"Reusing description for: {display_job_title} ({soc_code})",
                file=sys.stderr,
                flush=True,
            )
            return description_cache[cache_key]

        description = describe_profession(
            client=client,
            display_profession=display_job_title,
            prompt_profession=prompt_job_title,
        )
        description_cache[cache_key] = description
        return description

    return describe_profession(
        client=client,
        display_profession=display_job_title,
        prompt_profession=prompt_job_title,
    )


def describe_profession(
    *,
    client: OpenAI,
    display_profession: str,
    prompt_profession: str,
) -> str:
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
            f"{MODEL} did not complete for {display_profession}. Status: {response.status}"
        )

    if not response.output_text:
        raise RuntimeError(f"{MODEL} returned no output_text for {display_profession}.")

    return clean_description(response.output_text)


def build_user_prompt(
    *,
    display_profession: str,
    prompt_profession: str,
) -> str:
    return f"""
Question:
What is a {display_profession}?

Source job title:
{prompt_profession}

Write a plain-language answer in about 50-60 words.

Use the source job title as context. If it contains a parenthetical qualifier, use that qualifier to understand the kind of work, but do not include parentheses in the final description. Work the qualifier naturally into the first sentence if it is useful.

For example:
- "Boilermaker (Construction)" can become "A Boilermaker in construction..."
- "Electrician (Housewire or Residential)" can become "A residential Electrician..."
- "Painter and Decorator (Structural Steel Bridges)" can become "A Painter and Decorator working on structural steel bridges..."
- "Ironworker (Outside)" can become "An outside Ironworker..."
- "Welder (Industrial)" can become "An industrial Welder..."

Good style examples:

An Insulation and Asbestos Worker measures, cuts, fits, and applies insulation to pipes, boilers, ducts, and other mechanical systems. This helps control temperature, save energy, and protect equipment. They may also safely remove asbestos-containing materials from older buildings.

A Pointer, Caulker, and Cleaner repairs and protects the outside of buildings, bridges, and chimneys. They remove crumbling mortar, press fresh mortar between bricks or stone, seal joints with caulk, and wash or blast away dirt and stains, often from scaffolds. This keeps walls strong, dry, safe, and well maintained.

A Construction and General Building Laborer does basic physical work on building sites. They carry and load materials, dig trenches, mix concrete, set up and take down scaffolding, clean work areas, and use hand and power tools to help skilled tradespeople. Their work keeps projects moving, sites safer, and buildings taking shape.
""".strip()


def clean_description(text: str) -> str:
    """Make CSV output easier to compare by keeping each description on one line."""
    return " ".join(text.strip().split())


if __name__ == "__main__":
    main()
