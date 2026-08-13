# NASWA Apprenticeship Crawler

A small Poetry project that crawls NY DOL apprenticeship announcements, converts them to Markdown, extracts structured data via OpenAI, assigns official New York labor-market regions, enriches postings with SOC/O\*NET/OES data, generates plain-language job descriptions, and exports the results.

## Getting started

**Prerequisites:** Python 3.14+ and [Poetry](https://python-poetry.org/docs/#installation). An OpenAI API key is required for Steps 4 and 7. An O\*NET API key is required for Step 6. Register for O\*NET Web Services at [services.onetcenter.org/developer](https://services.onetcenter.org/developer/).

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

The extraction step leaves `regions` as an empty array. Official New York labor-market regions are assigned deterministically in Step 5 rather than inferred by the language model.

**Step 5 — Normalize labor-market regions:** Read every extracted posting JSON file under `json/*/*.json` and assign canonical New York labor-market regions based on the announcement’s geographic coverage.

Jobs from the same announcement are grouped by `sourceUrl` because they normally share the same recruitment area or union jurisdiction. The script uses geographic evidence from fields such as:

```text
locationSummary
residencyRequirement
allRequirements
sourceTitle
```

It maps stated regions, counties, cities, towns, villages, and reviewed location aliases to the official New York labor-market regions. Existing regions values are used only for comparison and are not treated as geographic evidence.

Start by auditing the proposed changes without modifying any posting files:

```
poetry run python scripts/apply_posting_regions.py --mode audit
```

Review the generated report:

```
reports/posting-region-normalization.csv
```

When the proposed regions look correct, apply them to postings whose regions arrays are currently empty:

```
poetry run python scripts/apply_posting_regions.py --mode apply
```

Successful announcement-level mappings are saved to:

```
data/locations/posting_regions.csv
```

After changing the inference rules or refreshing the location reference data, use --refresh-mappings to ignore saved mappings and infer them again:

```
poetry run python scripts/apply_posting_regions.py \
  --mode audit \
  --refresh-mappings
```

To apply and save the refreshed mappings:

```
poetry run python scripts/apply_posting_regions.py \
  --mode apply \
  --refresh-mappings \
  --overwrite
```

The script uses these location reference files:

```
data/locations/Labor_Market_Regions.csv
data/locations/New_York_State_Locality_Hierarchy_with_Websites.csv
data/locations/location_aliases.csv
data/locations/posting_regions.csv
```

**Step 6 — Apply SOC codes:** Write O\*NET-SOC codes into the extracted posting JSON files. The source of truth is `oesdata/postings-soc-codes.csv`, a manually maintained CSV that maps each posting URL to its SOC code (`URL`, `ONETSOC_CODE`, `ONETSOC_TITLE` columns).

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

**Step 7 — Fetch O\*NET data:** Download the full O\*NET occupation profile for each SOC code that appears in your postings and save it to `onet/<SOC_CODE>.json`. Pass one or more O\*NET-SOC codes as arguments. Requires `ONET_API_KEY` in your `.env`.

```bash
poetry run python scripts/fetch_onet_occupation.py 47-2111.00 51-7011.00
```

Add `--include-summary` to also pull the summary section (omitted by default to keep responses smaller).

**Step 8 — Generate job descriptions:** Read every extracted posting JSON file under `json/*/*.json`, normalize the job title, and use the OpenAI Responses API to generate a short, plain-language job description. The output is written to `job-descriptions/job-descriptions-postings.csv`.

```bash
poetry run python scripts/generate_job_descriptions.py
```

The generated CSV includes:

```text
id
sourceUrl
jobTitle
displayJobTitle
promptJobTitle
socCode
description
```

- `displayJobTitle` is the user-facing title we can use on the job posting page.
- `description` is reused when there is an exact match on normalized prompt title and SOC code. If a posting has no SOC code, the script generates a fresh description.

The script will exit early if `job_descriptions/job-descriptions-postings.csv` already exists. To regenerate the file, delete it manually first.

**Step 9 — Merge data:** Join each posting JSON file with BLS OES wage data (`oesdata/oesdata.csv`), the O\*NET occupation profile fetched in Step 6, and the generated job descriptions from Step 7. One merged JSON file per posting is written to `out/`.

```bash
poetry run python scripts/merge_job_data.py
```

Each merged output file includes:

```text
posting           # original extracted posting data
jobDescription    # generated display title and plain-language description
oes               # statewide OES wage data, when available
onet              # selected O\*NET occupation profile sections, when available
```

The script prints a summary of postings that are missing a SOC code, an OES match, an O\*NET file, or a generated job description so you can spot gaps before exporting.

**Step 10 — Export to CSV:** Recursively read every JSON file under `json/*/*.json` and write them as rows to `csv/apprenticeship-announcements.csv`. Array and object fields are serialized as compact JSON strings.

```bash
poetry run python scripts/json_to_csv.py
```

To export from a different JSON root, update `JSON_ROOT` near the top of `json_to_csv.py`.

## Output layout

```text
html/              # raw HTML pages from the crawler
markdown/          # converted Markdown files with YAML frontmatter
json/              # extracted job listings, one folder per announcement
data/locations/    # region reference data and reviewed posting-region mappings
reports/           # audit reports, including posting-region-normalization.csv
job-descriptions/  # generated plain-language job descriptions CSV
onet/              # O*NET occupation profiles, one JSON file per SOC code
out/               # merged posting records
csv/               # final CSV export
oesdata/           # BLS OES data and the manual posting-to-SOC mapping
schemas/           # JSON Schema used to validate extracted job listings
```
