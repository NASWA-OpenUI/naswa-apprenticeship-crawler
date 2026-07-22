from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from openai import OpenAI

# -----------------------------
# Manually tweak these for now
# -----------------------------


# Set MARKDOWN_PATH to process one specific markdown file.
# Set MARKDOWN_PATH = None and MARKDOWN_DIR to process every *.md file in that directory.
# If both are set, MARKDOWN_PATH wins because single-file extraction is cheaper and useful for testing.

# MARKDOWN_PATH = Path(
#     "./markdown/bricklayers-allied-craftworkers-local-2-albany-0.md"
# )
MARKDOWN_PATH = None
MARKDOWN_DIR = Path("./markdown")

MODEL = "gpt-5.4-mini"
REASONING_EFFORT = "medium"

SCHEMA_PATH = Path("./schemas/job-listing.schema.json")
OUTPUT_ROOT = Path("./json")

MAX_ATTEMPTS = 3

SYSTEM_PROMPT = """
You extract structured apprenticeship job listing data from markdown versions of public job postings.

Return exactly one JSON object with a jobListings array. Each item in jobListings must match the JobListing schema.

Important rules:
- Create one complete JobListing object per distinct apprenticeship job title/opening listed in the posting.
- If the posting lists only one job title, return a jobListings array with one object.
- If the posting lists multiple job titles with separate opening counts, return multiple complete JobListing objects.
- For multi-job postings, duplicate the shared source, sponsor, location, recruitment, application, requirement, and contact information across each object.
- For multi-job postings, only id, jobTitle, and numberOfOpenings usually differ between records unless the posting clearly states job-specific differences.
- Do not invent facts.
- Use null when a nullable field is not present or cannot be confidently extracted.
- Use [] for array fields when no values are found.
- Dates must use YYYY-MM-DD format.
- Use the source_url value from the markdown front matter for sourceUrl.
- Use the source_title value from the markdown front matter for sourceTitle.
- Create id as a stable slug-style job listing ID. If the source posting contains multiple jobs, combine the source URL/file slug with a job-title slug.
- Set socCode to null unless an O*NET-SOC code is explicitly present in the markdown.
- Set regions to an empty array. Do not infer labor-market regions during extraction.
- If the posting does not give separate application dates, use recruitmentStartDate as applicationStartDate and recruitmentEndDate as applicationEndDate.
- applicationEndDate is the application deadline.
- Use plain-language strings for requirement fields.
- For common requirement fields such as ageRequirement, educationRequirement, residencyRequirement, transportationRequirement, drugTestRequirement, and classRequirement, write short user-facing summaries.
- For allRequirements, include every applicant requirement from the posting, including requirements that are already summarized in the common requirement fields. allRequirements may intentionally duplicate information from the summary fields.
- For applicationMethods:
  - Use online_application only for a website, web form, or online submission portal.
  - Use email_application only when the applicant requests, receives, or submits application materials by email.
  - Use mail_application only when application materials must be sent or returned by postal mail.
  - Use in_person_application only when the applicant can pick up, complete, or submit an application in person.
  - Use phone_application only when the applicant can actually apply by phone. Do not use it just because a help phone number is listed.
  - Use appointment_required when an individual scheduled appointment is required as part of applying.
  - Use information_session_required when a group information session is required before applying.
- Extract only the primary further-information contact for the apprenticeship opportunity. Do not include generic NYSDOL Career Center job-search assistance in the contact fields unless no apprenticeship-specific contact is provided.
- Keep summaries short and useful for a job listing page.
- The goal is a glanceable overview, not a full duplication of the original posting.
""".strip()


def main() -> None:
    """
    Load schemas, choose markdown files to process, and extract each posting into JSON.

    Processing continues across files so one failure does not stop the whole batch.
    """
    load_dotenv()

    job_listing_schema = load_schema(SCHEMA_PATH)
    job_listing_validator = Draft202012Validator(job_listing_schema)

    response_schema = build_response_schema(job_listing_schema)
    response_validator = Draft202012Validator(response_schema)

    markdown_files = get_markdown_files()

    client = OpenAI()

    failures: list[tuple[Path, Exception]] = []
    processed_count = 0
    skipped_count = 0

    print(f"Found {len(markdown_files)} markdown file(s) to process.")

    for markdown_path in markdown_files:
        output_dir = build_output_dir(markdown_path)

        if output_dir.exists() and any(output_dir.glob("*.json")):
            print(f"Skipping existing output directory: {output_dir}")
            skipped_count += 1
            continue

        try:
            process_markdown_file(
                client=client,
                markdown_path=markdown_path,
                output_dir=output_dir,
                response_schema=response_schema,
                response_validator=response_validator,
                job_listing_validator=job_listing_validator,
            )
            processed_count += 1

        except Exception as exc:
            print(f"FAILED: {markdown_path.name}: {exc}")
            failures.append((markdown_path, exc))

    print()
    print("Done.")
    print(f"Processed: {processed_count}")
    print(f"Skipped:   {skipped_count}")
    print(f"Failed:    {len(failures)}")

    if failures:
        print()
        print("Failures:")
        for markdown_path, exc in failures:
            print(f"- {markdown_path}: {exc}")

        raise RuntimeError(f"{len(failures)} markdown file(s) failed.")


def get_markdown_files() -> list[Path]:
    """
    Decide which markdown files to process.

    MARKDOWN_PATH wins over MARKDOWN_DIR because single-file extraction is cheaper
    and is useful for testing one posting at a time.
    """
    if MARKDOWN_PATH is not None:
        if not MARKDOWN_PATH.exists():
            raise FileNotFoundError(f"Markdown file not found: {MARKDOWN_PATH}")

        if not MARKDOWN_PATH.is_file():
            raise ValueError(f"MARKDOWN_PATH is not a file: {MARKDOWN_PATH}")

        return [MARKDOWN_PATH]

    if MARKDOWN_DIR is not None:
        if not MARKDOWN_DIR.exists():
            raise FileNotFoundError(f"Markdown directory not found: {MARKDOWN_DIR}")

        if not MARKDOWN_DIR.is_dir():
            raise ValueError(f"MARKDOWN_DIR is not a directory: {MARKDOWN_DIR}")

        markdown_files = sorted(MARKDOWN_DIR.glob("*.md"))

        if not markdown_files:
            raise FileNotFoundError(f"No markdown files found in: {MARKDOWN_DIR}")

        return markdown_files

    raise ValueError("Set either MARKDOWN_PATH or MARKDOWN_DIR.")


def process_markdown_file(
    *,
    client: OpenAI,
    markdown_path: Path,
    output_dir: Path,
    response_schema: dict[str, Any],
    response_validator: Draft202012Validator,
    job_listing_validator: Draft202012Validator,
) -> None:
    """
    Extract, validate, and save job listing JSON for one markdown file.

    The model call is retried up to MAX_ATTEMPTS times if extraction or validation fails.
    """
    markdown_text = read_markdown(markdown_path)
    frontmatter = parse_frontmatter(markdown_text)

    source_url = frontmatter.get("source_url")
    source_title = frontmatter.get("source_title", "")

    if not source_url:
        raise ValueError(f"{markdown_path.name} is missing source_url in frontmatter.")

    last_error: Exception | None = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        print(f"{markdown_path.name}: attempt {attempt} of {MAX_ATTEMPTS}...")

        try:
            response_data = extract_job_listings(
                client=client,
                response_schema=response_schema,
                markdown_text=markdown_text,
                source_file=markdown_path,
            )

            validate_response(response_validator, response_data)

            job_listings = response_data["jobListings"]

            seen_ids: dict[str, int] = {}

            for job_listing in job_listings:
                normalize_job_listing(
                    job_listing,
                    source_url=source_url,
                    source_title=source_title,
                    source_file_stem=markdown_path.stem,
                    seen_ids=seen_ids,
                )

                dedupe_lists(job_listing)
                validate_job_listing(job_listing_validator, job_listing)
                
            save_job_listings(output_dir=output_dir, job_listings=job_listings)

            print(f"Saved {len(job_listings)} job listing(s) to: {output_dir}")
            return

        except Exception as exc:
            last_error = exc
            print(f"{markdown_path.name}: attempt {attempt} failed: {exc}")

    raise RuntimeError(
        f"Failed to extract valid job listing JSON after {MAX_ATTEMPTS} attempts."
    ) from last_error


def load_schema(path: Path) -> dict[str, Any]:
    """Load a JSON Schema file and validate that the schema itself is well formed."""
    if not path.exists():
        raise FileNotFoundError(f"Schema file not found: {path}")

    schema = json.loads(path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return schema


def read_markdown(path: Path) -> str:
    """Read a markdown source file as UTF-8 text."""
    if not path.exists():
        raise FileNotFoundError(f"Markdown file not found: {path}")

    return path.read_text(encoding="utf-8")


def build_output_dir(markdown_path: Path) -> Path:
    """Build the output directory for one source markdown file."""
    return OUTPUT_ROOT / markdown_path.stem

def dedupe_lists(data: dict[str, Any]) -> dict[str, Any]:
    """Remove duplicate values from known list fields while preserving their original order."""
    for key in [
        "applicationMethods",
        "allRequirements",
    ]:
        values = data.get(key)
        if isinstance(values, list):
            data[key] = list(dict.fromkeys(values))
    return data


def extract_job_listings(
    *,
    client: OpenAI,
    response_schema: dict[str, Any],
    markdown_text: str,
    source_file: Path,
) -> dict[str, Any]:
    """
    Call the OpenAI Responses API to extract one or more job listings from markdown.

    The response is constrained to the temporary jobListings wrapper schema.
    """
    response = client.responses.create(
        model=MODEL,
        reasoning={"effort": REASONING_EFFORT},
        input=[
            {
                "role": "system",
                "content": SYSTEM_PROMPT,
            },
            {
                "role": "user",
                "content": build_user_prompt(
                    markdown_text=markdown_text,
                    source_file=source_file,
                ),
            },
        ],
        text={
            "format": {
                "type": "json_schema",
                "name": "job_listings_response",
                "strict": True,
                "schema": schema_for_openai(response_schema),
            }
        },
    )

    if response.status != "completed":
        raise RuntimeError(
            f"OpenAI response was not completed. Status: {response.status}"
        )

    if not response.output_text:
        raise RuntimeError("OpenAI response did not include output_text.")

    try:
        return json.loads(response.output_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Model returned invalid JSON: {exc}") from exc


def build_user_prompt(*, markdown_text: str, source_file: Path) -> str:
    """Build the user prompt for one source markdown file."""
    return f"""
Extract one or more complete apprenticeship job listings from the markdown below.

Source file name: {source_file.name}
Source file stem: {source_file.stem}

If the source posting lists one job title, return one item in jobListings.

If the source posting lists multiple job titles with separate opening counts, return one complete item in jobListings for each job title. Each item must be a full standalone job listing suitable for a spreadsheet row.

<markdown>
{markdown_text}
</markdown>
""".strip()


def build_response_schema(job_listing_schema: dict[str, Any]) -> dict[str, Any]:
    """
    Build the temporary model-response schema.

    The saved files still use the normal JobListing schema directly.
    This wrapper only lets one model call return one or more complete listings.
    """
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["jobListings"],
        "properties": {
            "jobListings": {
                "type": "array",
                "items": schema_for_openai(job_listing_schema),
                "description": "One or more complete JobListing records extracted from the source posting.",
            }
        },
    }


def schema_for_openai(schema: dict[str, Any]) -> dict[str, Any]:
    """
    The local schema file can include metadata such as $schema, $id, title, and description.
    The OpenAI Structured Outputs call only needs the actual schema shape.
    """
    return {
        key: value
        for key, value in schema.items()
        if key not in {"$schema", "$id", "title", "description"}
    }


def validate_response(
    validator: Draft202012Validator,
    data: dict[str, Any],
) -> None:
    """Validate the full model response against the temporary wrapper schema."""
    try:
        validator.validate(data)
    except ValidationError as exc:
        raise_validation_error("response", exc)


def validate_job_listing(
    validator: Draft202012Validator,
    data: dict[str, Any],
) -> None:
    """Validate one extracted job listing against the saved JobListing schema."""
    try:
        validator.validate(data)
    except ValidationError as exc:
        listing_id = data.get("id", "<missing id>")
        raise_validation_error(f"job listing {listing_id}", exc)


def raise_validation_error(label: str, exc: ValidationError) -> None:
    """Raise a readable validation error that includes the failing schema path."""
    path = ".".join(str(part) for part in exc.absolute_path)
    location = path or "<root>"
    raise ValueError(
        f"JSON failed schema validation for {label} at {location}: {exc.message}"
    ) from exc


def save_job_listings(
    *,
    output_dir: Path,
    job_listings: list[dict[str, Any]],
) -> None:
    """
    Save each extracted job listing as an individual JSON file.

    Existing files are not overwritten, so reruns do not accidentally replace prior output.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    for job_listing in job_listings:
        listing_id = job_listing["id"]
        filename = f"{slugify(listing_id)}.json"
        output_path = output_dir / filename

        if output_path.exists():
            raise FileExistsError(f"Refusing to overwrite existing file: {output_path}")

        output_path.write_text(
            json.dumps(job_listing, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


def parse_frontmatter(markdown_text: str) -> dict[str, str]:
    """
    Parse the simple YAML-style frontmatter used by convert_to_markdown.py.

    This intentionally handles only the simple key: value shape we generate.
    """
    if not markdown_text.startswith("---\n"):
        raise ValueError("Markdown file is missing frontmatter.")

    try:
        _, frontmatter_text, _ = markdown_text.split("---", 2)
    except ValueError as exc:
        raise ValueError("Could not parse markdown frontmatter.") from exc

    frontmatter: dict[str, str] = {}

    for line in frontmatter_text.splitlines():
        line = line.strip()

        if not line or ":" not in line:
            continue

        key, value = line.split(":", 1)
        frontmatter[key.strip()] = value.strip().strip('"').strip("'")

    return frontmatter


def normalize_job_listing(
    job_listing: dict[str, Any],
    *,
    source_url: str,
    source_title: str,
    source_file_stem: str,
    seen_ids: dict[str, int],
) -> None:
    """
    Force deterministic fields that should not be left to the model.
    """
    job_listing["sourceUrl"] = source_url

    # make sure "regions" is empty
    job_listing["regions"] = []

    if source_title:
        job_listing["sourceTitle"] = source_title

    job_title = job_listing.get("jobTitle")

    if not isinstance(job_title, str) or not job_title.strip():
        raise ValueError("Job listing is missing a usable jobTitle.")

    base_id = f"{source_file_stem}-{slugify(job_title)}"

    seen_count = seen_ids.get(base_id, 0)
    seen_ids[base_id] = seen_count + 1

    if seen_count:
        job_listing["id"] = f"{base_id}-{seen_count + 1}"
    else:
        job_listing["id"] = base_id

def slugify(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"-+", "-", value)
    value = value.strip("-")

    if not value:
        raise ValueError("Could not create a valid filename slug.")

    return value


if __name__ == "__main__":
    main()
