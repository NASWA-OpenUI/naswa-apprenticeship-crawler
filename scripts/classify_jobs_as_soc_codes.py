from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI

# -----------------------------
# Manually tweak these for now
# -----------------------------

POSTINGS_ROOT = Path("json/gpt-5.4-mini/medium")
SOC_CODES_CSV_PATH = Path("oesdata/soccodes.csv")
OUTPUT_CSV_PATH = Path("oesdata/job-soc-codes.csv")

MODEL = "gpt-5.4-mini"
REASONING_EFFORT = "low"

MAX_ATTEMPTS = 3

OUTPUT_FIELDNAMES = ["jobTitle", "socCode", "socTitle"]

SYSTEM_PROMPT = """
You classify apprenticeship job titles into the best matching SOC occupation.

You will receive:
- one apprenticeship job title
- a CSV containing allowed SOCCODE and SOCTITLE values

Return exactly one JSON object with:
- socCode
- socTitle

Important rules:
- Use only the SOC codes and titles provided in the CSV.
- Do not invent SOC codes.
- Do not invent SOC titles.
- Choose the most specific matching occupation available.
- Avoid broad aggregate categories such as "Total, All Occupations" or major occupation groups when a more specific occupation fits.
- Treat "Apprentice" as a training status, not as the occupation itself. For example, "Electrician Apprentice" should match the occupation for electricians.
- Prefer the occupation the apprentice is training to become, unless the title clearly describes a helper or assistant occupation.
- If several SOC titles seem plausible, choose the closest match based on the job title alone.
""".strip()


RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["socCode", "socTitle"],
    "properties": {
        "socCode": {
            "type": "string",
            "description": "The selected SOCCODE from the provided CSV.",
        },
        "socTitle": {
            "type": "string",
            "description": "The selected SOCTITLE from the provided CSV.",
        },
    },
}


def main() -> None:
    """
    Classify every unique jobTitle from extracted posting JSON files into a SOC code.

    Existing rows in oesdata/jobsSocCodes.csv are reused, so reruns only classify
    new job titles.
    """
    load_dotenv()

    if not POSTINGS_ROOT.exists():
        raise FileNotFoundError(f"Postings root not found: {POSTINGS_ROOT}")

    if not SOC_CODES_CSV_PATH.exists():
        raise FileNotFoundError(f"SOC codes CSV not found: {SOC_CODES_CSV_PATH}")

    OUTPUT_CSV_PATH.parent.mkdir(parents=True, exist_ok=True)

    soc_rows = load_soc_codes(SOC_CODES_CSV_PATH)
    soc_codes_csv_text = build_soc_codes_prompt_csv(soc_rows)
    soc_titles_by_code = {row["SOCCODE"]: row["SOCTITLE"] for row in soc_rows}

    existing_rows = load_existing_rows(OUTPUT_CSV_PATH)
    existing_job_titles = {row["jobTitle"].strip() for row in existing_rows}

    posting_files = find_posting_files(POSTINGS_ROOT)
    job_titles = collect_unique_job_titles(posting_files)

    client = OpenAI()

    processed_count = 0
    skipped_count = 0
    failed: list[tuple[str, Exception]] = []

    print(f"Found {len(posting_files)} posting JSON file(s).")
    print(f"Found {len(job_titles)} unique job title(s).")
    print(f"Found {len(existing_job_titles)} existing classified job title(s).")
    print()

    ensure_output_file_exists(OUTPUT_CSV_PATH)

    for job_title in job_titles:
        if job_title in existing_job_titles:
            print(f"Skipping existing job title: {job_title}")
            skipped_count += 1
            continue

        try:
            print(f"Classifying: {job_title}")

            result = classify_job_title(
                client=client,
                job_title=job_title,
                soc_codes_csv_text=soc_codes_csv_text,
                soc_titles_by_code=soc_titles_by_code,
            )

            row = {
                "jobTitle": job_title,
                "socCode": result["socCode"],
                "socTitle": result["socTitle"],
            }

            append_row(OUTPUT_CSV_PATH, row)
            existing_job_titles.add(job_title)
            processed_count += 1

            print(f"  → {row['socCode']} {row['socTitle']}")

        except Exception as exc:
            print(f"FAILED: {job_title}: {exc}")
            failed.append((job_title, exc))

    print()
    print("Done.")
    print(f"Processed: {processed_count}")
    print(f"Skipped:   {skipped_count}")
    print(f"Failed:    {len(failed)}")

    if failed:
        print()
        print("Failures:")
        for job_title, exc in failed:
            print(f"- {job_title}: {exc}")

        raise RuntimeError(f"{len(failed)} job title(s) failed classification.")


def find_posting_files(postings_root: Path) -> list[Path]:
    """Find all posting JSON files inside directories under the postings root."""
    return sorted(path for path in postings_root.glob("*/*.json") if path.is_file())


def collect_unique_job_titles(posting_files: list[Path]) -> list[str]:
    """Collect unique non-empty jobTitle values from posting JSON files."""
    job_titles: dict[str, None] = {}

    for posting_path in posting_files:
        with posting_path.open("r", encoding="utf-8") as file:
            posting = json.load(file)

        job_title = str(posting.get("jobTitle", "")).strip()

        if not job_title:
            print(f"WARNING: missing jobTitle in {posting_path}")
            continue

        job_titles[job_title] = None

    return sorted(job_titles.keys())


def load_soc_codes(path: Path) -> list[dict[str, str]]:
    """Load the allowed SOC code/title rows from oesdata/soccodes.csv."""
    rows: list[dict[str, str]] = []

    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)

        if reader.fieldnames:
            reader.fieldnames = [field.strip() for field in reader.fieldnames]

        for row in reader:
            soc_code = row.get("SOCCODE", "").strip()
            soc_title = row.get("SOCTITLE", "").strip()

            if not soc_code or not soc_title:
                continue

            rows.append(
                {
                    "SOCCODE": soc_code,
                    "SOCTITLE": soc_title,
                }
            )

    if not rows:
        raise ValueError(f"No SOC code rows found in {path}")

    return rows


def build_soc_codes_prompt_csv(soc_rows: list[dict[str, str]]) -> str:
    """Build a compact CSV string for the model prompt."""
    lines = ["SOCCODE,SOCTITLE"]

    for row in soc_rows:
        lines.append(f"{csv_escape(row['SOCCODE'])},{csv_escape(row['SOCTITLE'])}")

    return "\n".join(lines)


def csv_escape(value: str) -> str:
    """Escape one CSV value for inclusion in the prompt CSV."""
    if any(char in value for char in [",", '"', "\n"]):
        return '"' + value.replace('"', '""') + '"'

    return value


def load_existing_rows(path: Path) -> list[dict[str, str]]:
    """Load existing output rows so reruns can skip already-classified job titles."""
    if not path.exists():
        return []

    rows: list[dict[str, str]] = []

    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)

        if reader.fieldnames:
            reader.fieldnames = [field.strip() for field in reader.fieldnames]

        for row in reader:
            job_title = row.get("jobTitle", "").strip()
            soc_code = row.get("socCode", "").strip()
            soc_title = row.get("socTitle", "").strip()

            if not job_title:
                continue

            rows.append(
                {
                    "jobTitle": job_title,
                    "socCode": soc_code,
                    "socTitle": soc_title,
                }
            )

    return rows


def ensure_output_file_exists(path: Path) -> None:
    """Create the output CSV with headers if it does not already exist."""
    if path.exists():
        return

    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=OUTPUT_FIELDNAMES)
        writer.writeheader()


def append_row(path: Path, row: dict[str, str]) -> None:
    """Append one classified job title row to the output CSV."""
    with path.open("a", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=OUTPUT_FIELDNAMES)
        writer.writerow(row)


def classify_job_title(
    *,
    client: OpenAI,
    job_title: str,
    soc_codes_csv_text: str,
    soc_titles_by_code: dict[str, str],
) -> dict[str, str]:
    """Classify one job title into a SOC code, retrying on model or validation failures."""
    last_error: Exception | None = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            print(f"  attempt {attempt} of {MAX_ATTEMPTS}...")

            result = call_openai_for_soc_match(
                client=client,
                job_title=job_title,
                soc_codes_csv_text=soc_codes_csv_text,
            )

            return validate_and_normalize_result(
                result=result,
                soc_titles_by_code=soc_titles_by_code,
            )

        except Exception as exc:
            last_error = exc
            print(f"  attempt {attempt} failed: {exc}")

    raise RuntimeError(
        f"Failed to classify job title after {MAX_ATTEMPTS} attempts."
    ) from last_error


def call_openai_for_soc_match(
    *,
    client: OpenAI,
    job_title: str,
    soc_codes_csv_text: str,
) -> dict[str, Any]:
    """Call the OpenAI Responses API to select the best SOC match for one job title."""
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
                    job_title=job_title,
                    soc_codes_csv_text=soc_codes_csv_text,
                ),
            },
        ],
        text={
            "format": {
                "type": "json_schema",
                "name": "soc_code_match",
                "strict": True,
                "schema": RESPONSE_SCHEMA,
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


def build_user_prompt(*, job_title: str, soc_codes_csv_text: str) -> str:
    """Build the user prompt for one job title classification."""
    return f"""
Classify this apprenticeship job title into the best matching SOC code.

Job title:
{job_title}

Allowed SOC codes and titles:
<soc_codes_csv>
{soc_codes_csv_text}
</soc_codes_csv>

Return the best matching SOC code and SOC title from the CSV.
""".strip()


def validate_and_normalize_result(
    *,
    result: dict[str, Any],
    soc_titles_by_code: dict[str, str],
) -> dict[str, str]:
    """
    Validate the model result against the allowed SOC code list.

    The returned socTitle is normalized to the canonical title from the CSV.
    """
    soc_code = str(result.get("socCode", "")).strip()

    if not soc_code:
        raise ValueError("Model result is missing socCode.")

    if soc_code not in soc_titles_by_code:
        raise ValueError(f"Model returned unknown SOC code: {soc_code}")

    return {
        "socCode": soc_code,
        "socTitle": soc_titles_by_code[soc_code],
    }


if __name__ == "__main__":
    main()
