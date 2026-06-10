# NASWA Apprenticeship Crawler

A small Poetry project that crawls NY DOL apprenticeship announcements, converts them to Markdown, extracts structured data via OpenAI, and exports the results to CSV.

## Getting started

**Prerequisites:** Python 3.14+ and [Poetry](https://python-poetry.org/docs/#installation). An OpenAI API key is required for Step 3.

```bash
# Clone the repo
git clone <repo-url>
cd naswa-apprenticeship-crawler

# Install dependencies
poetry install

# Activate the virtual environment
poetry shell

# Copy and fill in your OpenAI API key
cp .env.example .env
```

## Usage

**Step 1 — Crawl:** Fetch all announcement pages from the [NY DOL announcements listing](https://dol.ny.gov/apprenticeship/apprenticeship-announcements) and save them as HTML files in `html/`. Already-saved files are skipped.

```bash
poetry run scrapy crawl announcements
```

If you have run the crawler in the past and would like to rerun it and automatically archive new opportunities, you can run this:

```bash
poetry run scrapy crawl announcements -a archive_missing=true
```

**Step 2 — Convert to Markdown:** Parse the saved HTML files and write them to `markdown/` with YAML frontmatter (`source_file`, `source_url`, `source_title`, `date_posted`, `location`) and a Markdown body.

```bash
poetry run python scripts/convert_to_markdown.py
```

**Step 3 — Extract structured data:** Send each Markdown file to the OpenAI Responses API (default: `gpt-5.4-mini`, reasoning effort `medium`) and save one JSON file per job listing under `json/<model>/<effort>/<posting-slug>/`. Postings that already have output are skipped; failed extractions are retried up to 3 times and reported at the end.

```bash
poetry run python scripts/extract_job_listing.py
```

To process a single file instead of the whole `markdown/` directory, set `MARKDOWN_PATH` near the top of `extract_job_listing.py`.

**Step 4 — Export to CSV:** Recursively read every JSON file under `json/gpt-5.4-mini/medium/` and write them as rows to `csv/apprenticeship-announcements.csv`. Array and object fields are serialized as compact JSON strings.

```bash
poetry run python scripts/json_to_csv.py
```

To export from a different model/effort folder, update `JSON_ROOT` near the top of `json_to_csv.py`.

## Output layout

```
html/          # raw HTML pages from the crawler (one file per announcement)
markdown/      # converted Markdown files with YAML frontmatter
json/          # extracted job listings, organized by model/effort/posting-slug/
csv/           # final CSV export
schemas/       # JSON Schema used to validate extracted job listings
```
