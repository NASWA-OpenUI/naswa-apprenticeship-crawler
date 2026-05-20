from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from openai import OpenAI

# Notes
# - ids should be created predictably
# - add raw text
# - test a few models
# - write a script to determine if there are 1 or more jobs


PROJECT_ROOT = Path(__file__).resolve().parent.parent


# -----------------------------
# Manually tweak these for now
# -----------------------------

MODEL = "gpt-5.4-mini"
REASONING_EFFORT = "medium"

MARKDOWN_PATH = PROJECT_ROOT / "markdown/westchester-fairfield-jeatc-lu-3-ibew-1.md"
SCHEMA_PATH = PROJECT_ROOT / "schemas/job-listing.schema.json"
OUTPUT_ROOT = PROJECT_ROOT / "json"

MAX_ATTEMPTS = 3

SYSTEM_PROMPT = """
You extract structured apprenticeship job listing data from markdown versions of public job postings.

Return exactly one JSON object that matches the provided schema.

Important rules:
- Create one browsable job listing record.
- Do not invent facts.
- Use null when a nullable field is not present or cannot be confidently extracted.
- Use [] for array fields when no values are found.
- Dates must use YYYY-MM-DD format.
- Use the source_title value from the markdown front matter for sourceTitle.
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
    load_dotenv()

    schema = load_schema(SCHEMA_PATH)
    validator = Draft202012Validator(schema)

    markdown_text = read_markdown(MARKDOWN_PATH)
    output_path = build_output_path()

    if output_path.exists():
        print(f"Skipping existing output: {output_path}")
        return

    client = OpenAI()

    last_error: Exception | None = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        print(f"Attempt {attempt} of {MAX_ATTEMPTS}...")

        try:
            data = extract_job_listing(
                client=client,
                schema=schema,
                markdown_text=markdown_text,
                source_file=MARKDOWN_PATH,
            )

            data = dedupe_lists(data)
            validate_output(validator, data)

            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            print(f"Saved: {output_path}")
            return

        except Exception as exc:
            last_error = exc
            print(f"Attempt {attempt} failed: {exc}")

    raise RuntimeError(
        f"Failed to extract valid job listing JSON after {MAX_ATTEMPTS} attempts."
    ) from last_error


def load_schema(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Schema file not found: {path}")

    schema = json.loads(path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return schema


def read_markdown(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"Markdown file not found: {path}")

    return path.read_text(encoding="utf-8")


def build_output_path() -> Path:
    return OUTPUT_ROOT / MODEL / REASONING_EFFORT / f"{MARKDOWN_PATH.stem}.json"


def dedupe_lists(data: dict[str, Any]) -> dict[str, Any]:
    for key in [
        "applicationMethods",
        "regions",
        "allRequirements",
    ]:
        values = data.get(key)
        if isinstance(values, list):
            data[key] = list(dict.fromkeys(values))
    return data


def extract_job_listing(
    *,
    client: OpenAI,
    schema: dict[str, Any],
    markdown_text: str,
    source_file: Path,
) -> dict[str, Any]:
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
                "name": "job_listing",
                "strict": True,
                "schema": schema_for_openai(schema),
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
    return f"""
Extract one apprenticeship job listing from the markdown below.

Source file name: {source_file.name}
Source file stem: {source_file.stem}

Markdown:

```markdown
{markdown_text}
```

""".strip()


def schema_for_openai(schema: dict[str, Any]) -> dict[str, Any]:
    """
    The local schema file can include metadata such as $schema, title, and description.
    The OpenAI Structured Outputs call only needs the actual schema shape.
    """
    return {
        key: value
        for key, value in schema.items()
        if key not in {"$schema", "$id", "title", "description"}
    }


def validate_output(
    validator: Draft202012Validator,
    data: dict[str, Any],
) -> None:
    try:
        validator.validate(data)
    except ValidationError as exc:
        path = ".".join(str(part) for part in exc.absolute_path)
        location = path or "<root>"
        raise ValueError(
            f"JSON failed schema validation at {location}: {exc.message}"
        ) from exc


if __name__ == "__main__":
    main()
