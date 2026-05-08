# NASWA Apprenticeship Crawler

A small Poetry project for using Scrapy to crawl apprenticeship announcement pages and save source content locally for later parsing and analysis.

## Getting started

**Prerequisites:** Python 3.14+ and [Poetry](https://python-poetry.org/docs/#installation).

```bash
# Clone the repo
git clone <repo-url>
cd naswa-apprenticeship-crawler

# Install dependencies
poetry install

# Activate the virtual environment
poetry shell
```

## Usage

**Step 1 — Crawl:** Run the Scrapy spider to fetch posting pages and save them as HTML files in `html/`.

```bash
scrapy crawl announcements
```

**Step 2 — Convert:** Parse the saved HTML files and convert them to Markdown in `markdown/`.

```bash
python convert_to_markdown.py
```

## What this app does

1. **Crawls apprenticeship announcements** — a Scrapy spider hits the NY DOL apprenticeship announcements listing page, follows links to individual posting pages, and saves each page as an HTML file in `html/`.

2. **Converts HTML to Markdown** — `convert_to_markdown.py` reads each saved HTML file, extracts structured fields from the page (date posted, location, announcement title, job heading) into YAML frontmatter, converts the posting body to Markdown, and writes the result to `markdown/`.

The end result is a local collection of clean, readable Markdown files — one per posting.

## Coming soon

**Structured data extraction** — parse the Markdown postings and pull out specific data elements into a structured format (CSV or JSON). This will let us treat the postings as tabular data rather than text blobs, making it easier to filter, sort, and analyze across all listings.

Which fields to extract and the best approach for doing so are currently being worked out.
