"""
Reads current HTML files listed in manifests/announcements.json and converts each
one to a Markdown file in markdown/.

Archived manifest records are skipped. The markdown frontmatter uses the DOL
page URL captured in the manifest as source_url.

Usage:
    poetry run python scripts/convert_to_markdown.py
"""

import json
from pathlib import Path

from bs4 import BeautifulSoup
from markdownify import markdownify

PROJECT_ROOT = Path(__file__).resolve().parent.parent

HTML_DIR = PROJECT_ROOT / "html"
MD_DIR = PROJECT_ROOT / "markdown"
MANIFEST_PATH = PROJECT_ROOT / "manifests" / "announcements.json"

BASE_URL = "https://dol.ny.gov/news"

def clear_markdown_dir() -> int:
    """
    Delete existing Markdown files so markdown/ reflects the current html/ folder.

    Only deletes top-level *.md files in markdown/.
    """
    deleted_count = 0

    for md_path in MD_DIR.glob("*.md"):
        if md_path.is_file():
            md_path.unlink()
            deleted_count += 1

    return deleted_count


def load_manifest() -> dict:
    if not MANIFEST_PATH.exists():
        raise FileNotFoundError(
            f"Manifest not found: {MANIFEST_PATH}\n"
            "Run the announcements spider first."
        )

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    if not isinstance(manifest.get("postings"), dict):
        raise ValueError(f"Manifest is missing a valid postings object: {MANIFEST_PATH}")

    return manifest


def get_current_html_files(manifest: dict) -> list[tuple[Path, dict]]:
    """
    Return only non-archived HTML files from the manifest.

    Archived postings are intentionally not converted to markdown, so they do not
    continue down the pipeline.
    """
    records = []

    for filename, record in sorted(manifest["postings"].items()):
        if record.get("archived"):
            continue

        html_path = HTML_DIR / filename

        if not html_path.exists():
            print(f"Skipping missing HTML file from manifest: {filename}")
            continue

        records.append((html_path, record))

    return records


def extract_fields(soup: BeautifulSoup) -> dict:
    hero = soup.select_one("div.hero-news-wrapper")

    date_posted = ""
    location = ""
    source_title = ""

    if hero:
        date_el = hero.select_one(".webny-card-date")
        if date_el:
            date_posted = date_el.get_text(strip=True)

        loc_el = hero.select_one(".hero-news-location")
        if loc_el:
            location = loc_el.get_text(strip=True)

        title_el = hero.select_one(".hero-news-title")
        if title_el:
            source_title = title_el.get_text(strip=True)

    news_body = soup.select_one("div.news-body")
    announcement_heading = ""
    body_html = ""

    if news_body:
        teaser = news_body.select_one("div.press-teaser")
        if teaser:
            announcement_heading = teaser.get_text(strip=True)

        press_body = news_body.select_one("div.press-body")
        if press_body:
            if not announcement_heading:
                # Fall back to first heading inside press-body.
                heading_el = press_body.find(["h1", "h2", "h3"])
                if heading_el:
                    announcement_heading = heading_el.get_text(strip=True)
                    heading_el.decompose()

            body_html = str(press_body)

    return {
        "source_title": source_title,
        "date_posted": date_posted,
        "location": location,
        "announcement_heading": announcement_heading,
        "body_html": body_html,
    }


def build_frontmatter(source_file: str, fields: dict, manifest_record: dict) -> str:
    slug = source_file.replace(".html", "")

    # Prefer the URL captured by the crawler manifest.
    source_url = manifest_record.get("url") or f"{BASE_URL}/{slug}"

    lines = [
        "---",
        f"source_file: {source_file}",
        f"source_url: {source_url}",
        f"source_title: {fields['source_title']}",
        f"date_posted: {fields['date_posted']}",
        f"location: {fields['location']}",
        "---",
    ]

    return "\n".join(lines)


def convert_file(html_path: Path, md_path: Path, manifest_record: dict) -> None:
    with html_path.open("r", encoding="utf-8") as f:
        soup = BeautifulSoup(f, "html.parser")

    fields = extract_fields(soup)
    frontmatter = build_frontmatter(html_path.name, fields, manifest_record)

    body_md = markdownify(
        fields["body_html"],
        heading_style="ATX",
        strip=["script", "style"],
    ).strip()

    heading = (
        f"**{fields['announcement_heading']}**"
        if fields["announcement_heading"]
        else ""
    )

    parts = [frontmatter, heading, body_md]
    output = "\n\n".join(p for p in parts if p)

    md_path.write_text(output + "\n", encoding="utf-8")
    print(f"Converted: {html_path.name} -> {md_path.name}")


def main() -> None:
    MD_DIR.mkdir(exist_ok=True)

    deleted_count = clear_markdown_dir()
    print(f"Deleted {deleted_count} existing Markdown file(s) from {MD_DIR}")

    manifest = load_manifest()
    current_files = get_current_html_files(manifest)

    if not current_files:
        print("No current HTML files found in manifest.")
        return

    for html_path, manifest_record in current_files:
        md_path = MD_DIR / html_path.name.replace(".html", ".md")
        convert_file(html_path, md_path, manifest_record)

    print(f"\nDone. {len(current_files)} current file(s) converted to {MD_DIR}")


if __name__ == "__main__":
    main()