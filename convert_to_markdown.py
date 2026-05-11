"""
Reads HTML files from html/ and converts each one to a Markdown file in markdown/.
Extracts structured frontmatter from the page hero and news body, then markdownifies
the press-body content.

Usage:
    poetry run python convert_to_markdown.py
"""

import os
from pathlib import Path

from bs4 import BeautifulSoup
from markdownify import markdownify

HTML_DIR = Path("html")
MD_DIR = Path("markdown")
BASE_URL = "https://dol.ny.gov/news"


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
                # Fall back to first heading inside press-body
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


def build_frontmatter(source_file: str, fields: dict) -> str:
    slug = source_file.replace(".html", "")
    source_url = f"{BASE_URL}/{slug}"
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


def convert_file(html_path: Path, md_path: Path) -> None:
    with open(html_path, encoding="utf-8") as f:
        soup = BeautifulSoup(f, "html.parser")

    fields = extract_fields(soup)
    frontmatter = build_frontmatter(html_path.name, fields)

    body_md = markdownify(
        fields["body_html"], heading_style="ATX", strip=["script", "style"]
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


def main():
    MD_DIR.mkdir(exist_ok=True)
    html_files = sorted(HTML_DIR.glob("*.html"))
    if not html_files:
        print("No HTML files found in html/")
        return

    for html_path in html_files:
        md_path = MD_DIR / html_path.name.replace(".html", ".md")
        convert_file(html_path, md_path)

    print(f"\nDone. {len(html_files)} file(s) converted to markdown/")


if __name__ == "__main__":
    main()
