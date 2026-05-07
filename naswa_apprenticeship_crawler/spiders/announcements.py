import re
from pathlib import Path

import scrapy

HTML_DIR = Path(__file__).resolve().parents[2] / "html"
LISTINGS_URL = "https://dol.ny.gov/apprenticeship/apprenticeship-announcements"


def url_to_filename(url: str) -> str:
    # Use the last non-empty path segment as the filename, e.g. "electricians-jac-watertown-local-910-0.html"
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    # Strip any query string or fragment
    slug = re.split(r"[?#]", slug)[0]
    return f"{slug}.html"


class AnnouncementsSpider(scrapy.Spider):
    name = "announcements"
    start_urls = [LISTINGS_URL]

    def parse(self, response):
        # Collect all links that look like individual posting pages (/news/<slug>)
        links = response.css("div.view-content a[href*='/news/']::attr(href)").getall()
        seen = set()
        for href in links:
            url = response.urljoin(href)
            if url in seen:
                continue
            seen.add(url)

            filename = url_to_filename(url)
            dest = HTML_DIR / filename
            if dest.exists():
                self.logger.info("Already saved, skipping: %s", filename)
                continue

            yield scrapy.Request(url, callback=self.save_page)

    def save_page(self, response):
        filename = url_to_filename(response.url)
        dest = HTML_DIR / filename
        HTML_DIR.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(response.body)
        self.logger.info("Saved %s (%d bytes)", filename, len(response.body))
