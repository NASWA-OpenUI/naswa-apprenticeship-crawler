# NASWA Apprenticeship Crawler

A small Poetry project that crawls NY DOL apprenticeship announcements, converts them to Markdown, extracts structured data via OpenAI, and exports the results to CSV.

## Getting started

**Prerequisites:** Python 3.14+ and [Poetry](https://python-poetry.org/docs/#installation). An OpenAI API key is required for Step 3 and an O\*NET API key is required for Step 5. Register for O\*NET Web Services at [services.onetcenter.org/developer](https://services.onetcenter.org/developer/).

```bash
# Clone the repo
git clone <repo-url>
cd naswa-apprenticeship-crawler

# Install dependencies
poetry install

# Activate the virtual environment
poetry shell

# Copy and fill in your API keys
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

**Step 2 — Prune archived extracted JSON:** After running the crawler with `archive_missing=true`, remove extracted JSON folders for postings that are now marked archived in `manifests/announcements.json`.

First list what would be removed:

```bash
poetry run python scripts/prune_archived_json.py --mode list
```

Then delete those archived extracted folders:

```bash
poetry run python scripts/prune_archived_json.py --mode delete
```

**Step 3 — Convert to Markdown:** Parse the saved HTML files and write them to `markdown/` with YAML frontmatter (`source_file`, `source_url`, `source_title`, `date_posted`, `location`) and a Markdown body.

```bash
poetry run python scripts/convert_to_markdown.py
```

**Step 4 — Extract structured data:** Send each Markdown file to the OpenAI Responses API (default: `gpt-5.4-mini`, reasoning effort `medium`) and save one JSON file per job listing under `json/<posting-slug>/`. Postings that already have output are skipped; failed extractions are retried up to 3 times and reported at the end.

```bash
poetry run python scripts/extract_job_listing.py
```

To process a single file instead of the whole `markdown/` directory, set `MARKDOWN_PATH` near the top of `extract_job_listing.py`.

**Step 5 — Apply SOC codes:** Write O\*NET-SOC codes into the extracted posting JSON files. The source of truth is `oesdata/postings-soc-codes.csv`, a manually maintained CSV that maps each posting URL to its SOC code (`URL`, `ONETSOC_CODE`, `ONETSOC_TITLE` columns).

Start by listing which postings still lack a SOC code:

```bash
poetry run python scripts/apply_posting_soc_codes.py --mode missing
```

Once the CSV is populated, audit for disagreements between the JSON and the CSV before writing anything:

```bash
poetry run python scripts/apply_posting_soc_codes.py --mode audit
```

When the audit looks clean, apply the SOC codes to the JSON files:

```bash
poetry run python scripts/apply_posting_soc_codes.py --mode apply
```

**Step 6 — Fetch O\*NET data:** Download the full O\*NET occupation profile for each SOC code that appears in your postings and save it to `onet/<SOC_CODE>.json`. Pass one or more O\*NET-SOC codes as arguments. Requires `ONET_API_KEY` in your `.env`.

```bash
poetry run python scripts/fetch_onet_occupation.py 47-2111.00 51-7011.00
```

Add `--include-summary` to also pull the summary section (omitted by default to keep responses smaller).

**Step 7 — Merge data:** Join each posting JSON file with BLS OES wage data (`oesdata/oesdata.csv`) and the O\*NET occupation profile fetched in Step 5. One merged JSON file per posting is written to `out/`. The script prints a summary of postings that are missing a SOC code, an OES match, or an O\*NET file so you can spot gaps before exporting.

```bash
poetry run python scripts/merge_job_data.py
```

**Step 8 — Export to CSV:** Recursively read every JSON file under `json/*/*.json` and write them as rows to `csv/apprenticeship-announcements.csv`. Array and object fields are serialized as compact JSON strings.

```bash
poetry run python scripts/json_to_csv.py
```

To export from a different model/effort folder, update `JSON_ROOT` near the top of `json_to_csv.py`.

## Output layout

```
html/          # raw HTML pages from the crawler (one file per announcement)
markdown/      # converted Markdown files with YAML frontmatter
json/          # extracted job listings, organized by model/effort/posting-slug/
onet/          # O*NET occupation profiles, one JSON file per SOC code
out/           # merged posting records (posting + OES wages + O*NET data)
csv/           # final CSV export
oesdata/       # BLS OES wage CSV and the manual postings-soc-codes.csv mapping
schemas/       # JSON Schema used to validate extracted job listings
```
