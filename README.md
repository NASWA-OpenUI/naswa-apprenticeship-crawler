# NASWA Apprenticeship Crawler

A Poetry project for preparing two related New York apprenticeship datasets:

- **Posted opportunities** — crawl NY DOL apprenticeship announcements, extract structured posting data, assign labor-market regions and SOC codes, enrich records with O*NET and OES data, and generate plain-language job descriptions.
- **Registered programs** — process the NY registered apprenticeship program dataset, group programs by SOC code and trade, normalize outdated SOC codes, enrich groups with O*NET data, and generate plain-language trade descriptions.

The two workflows share O*NET occupation data and description-generation utilities but produce separate final datasets for use by the apprenticeship matcher.

## Getting started

**Prerequisites:** 

- Python 3.14+ and [Poetry](https://python-poetry.org/docs/#installation). 
- An OpenAI API key is required for structured-data extraction and description generation. 
- An O*NET API key is required for fetching occupation profiles.
  - Register for O*NET Web Services at [services.onetcenter.org/developer](https://services.onetcenter.org/developer/).

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

## Posted opportunities workflow

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

- `displayJobTitle` is the normalized user-facing title.
- `promptJobTitle` is the normalized title used for description generation and reuse.
- Existing descriptions are kept when the posting's normalized title and SOC code have not changed.
- Descriptions can be reused across postings with the same normalized prompt title and SOC code.
- New descriptions are generated only when no reusable description exists.
- Rows for postings that no longer exist are removed.

The CSV is rewritten only when its contents have changed.

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

## Registered program workflow

Registered apprenticeship program data follows a separate pipeline from recruitment opportunities. The source file is `programs/ra-program-data.csv`, and the final output is one enriched JSON file per O*NET-SOC code under `programs/out/`.

**Program Step 1 — Build program groups:** Read `programs/ra-program-data.csv`, normalize the source data, and group programs first by O*NET-SOC code and then by trade name. Active and probation programs are included; inactive and out-of-state programs are excluded.

```bash
poetry run python scripts/build_program_groups.py
```

The generated files are written to:

```text
programs/json/<SOC_CODE>.json
```

Each file contains one SOC group with its canonical regions, trade groups, and individual registered programs.

**Program Step 2 — Audit SOC codes:** Check the SOC codes in `programs/json/` against the current O*NET taxonomy.

```bash
poetry run python scripts/audit_program_soc_codes.py
```

The audit does not modify the program JSON files. Obsolete, ambiguous, or unresolved source codes are added to:

```text
programs/soc-code-mappings.csv
```

Unambiguous official O*NET replacements are filled in automatically. Ambiguous or unresolved rows are left incomplete for manual review.

**Program Step 3 — Apply reviewed SOC mappings:** Apply the completed mappings in `programs/soc-code-mappings.csv` to the grouped program JSON.

```bash
poetry run python scripts/apply_program_soc_codes.py
```

The script updates SOC codes and titles throughout each affected group, renames the output group where necessary, and merges groups that resolve to the same current SOC code. The resulting `programs/json/` directory contains the canonical SOC data used by later steps.

**Program Step 4 — Audit O*NET coverage:** Compare the SOC groups in `programs/json/` with the cached occupation profiles under `onet/` and list any SOC codes that still need O*NET data.

```bash
poetry run python scripts/audit_program_onet.py
```

Fetch any missing occupation profiles with the same O*NET fetcher used by the opportunity workflow:

```bash
poetry run python scripts/fetch_onet_occupation.py 17-3024.00 21-1093.00
```

O*NET profiles are shared between the opportunity and program pipelines and are stored as:

```text
onet/<SOC_CODE>.json
```

**Program Step 5 — Generate trade descriptions:** Read the canonical program groups and generate short, plain-language descriptions for their apprenticeship trades.

```bash
poetry run python scripts/generate_program_descriptions.py
```

The output is written to:

```text
job-descriptions/job-descriptions-programs.csv
```

The CSV contains one row per registered program:

```text
programAk
tradeName
displayTradeName
promptTradeName
socCode
description
```

Existing descriptions are kept when still valid, descriptions can be reused across programs with the same normalized trade and SOC code, and posting descriptions are also reused when an equivalent description already exists. Reviewed SOC-code changes are accounted for so descriptions are not regenerated solely because an obsolete SOC code was replaced.

**Program Step 6 — Merge program data:** Combine each canonical SOC group with its generated trade descriptions and selected O*NET occupation data.

```bash
poetry run python scripts/merge_program_data.py
```

One final JSON file per SOC code is written to:

```text
programs/out/<SOC_CODE>.json
```

Each merged file includes:

```text
socCode        # canonical O*NET-SOC code
socTitle       # program occupation title
programCount   # number of registered programs in the SOC group
regions        # combined canonical New York regions
onet           # selected O*NET occupation profile sections
trades         # trade groups with descriptions and individual programs
```

O*NET data is stored once at the SOC-group level, while generated descriptions are stored once per trade group.


## Output layout

```text
html/              # raw apprenticeship announcement HTML from the crawler
markdown/          # converted announcement Markdown with YAML frontmatter
json/              # extracted opportunity postings, grouped by announcement
out/               # final enriched opportunity records
csv/               # CSV exports of opportunity posting data

programs/          # registered apprenticeship program data
  ra-program-data.csv       # source registered-program dataset
  soc-code-mappings.csv     # reviewed obsolete/invalid SOC-code mappings
  json/                      # normalized program groups, one file per SOC code
  out/                       # final enriched program groups, one file per SOC code

data/locations/    # region reference data and reviewed posting-region mappings
reports/           # generated audit reports
job-descriptions/  # generated posting and program description CSVs
onet/              # shared O*NET occupation profiles, one JSON file per SOC code
oesdata/           # BLS OES data and manual opportunity-to-SOC mappings
schemas/           # JSON Schemas for opportunity and program data
```