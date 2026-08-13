from __future__ import annotations

import re
import sys

from openai import OpenAI


MODEL = "gpt-5.4"


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
    descriptions may be preserved for the same source record, but they are
    not copied to a different record.
    """
    normalized_title = normalized_cache_title(prompt_job_title)
    normalized_soc_code = normalized_cache_soc_code(soc_code)

    if not normalized_title or not normalized_soc_code:
        return None

    return normalized_title, normalized_soc_code


def build_description_cache(
    existing_rows: list[dict[str, str]],
    *,
    prompt_title_field: str,
) -> dict[tuple[str, str], str]:
    """
    Build a reusable description cache from existing CSV rows.

    The first description found for a normalized prompt title and SOC code is
    retained. Conflicting existing descriptions are reported for review.

    prompt_title_field allows different description datasets to share this
    logic, for example:
    - promptJobTitle for apprenticeship postings
    - promptTradeName for registered programs
    """
    cache: dict[tuple[str, str], str] = {}

    for row in existing_rows:
        prompt_title = row[prompt_title_field]
        soc_code = row["socCode"]

        cache_key = description_cache_key(
            prompt_title,
            soc_code,
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
                f"{prompt_title} ({soc_code}). "
                "The first description will be reused.",
                file=sys.stderr,
            )
            continue

        cache[cache_key] = description

    return cache


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
        # records in this same run.
        description_cache[cache_key] = description

        return description, "generated"

    description = describe_profession(
        client=client,
        display_profession=display_job_title,
        prompt_profession=prompt_job_title,
    )

    return description, "generated"


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
    the common title shapes in the source data.
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
