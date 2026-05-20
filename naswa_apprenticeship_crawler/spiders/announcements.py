import re
from pathlib import Path

import scrapy

HTML_DIR = Path(__file__).resolve().parents[2] / "html"
LISTINGS_URL = "https://dol.ny.gov/apprenticeship/apprenticeship-announcements"


def url_to_filename(url: str) -> str:
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    slug = re.split(r"[?#]", slug)[0]
    return f"{slug}.html"


class AnnouncementsSpider(scrapy.Spider):
    name = "announcements"
    start_urls = [LISTINGS_URL]

    def parse(self, response):
        # Collect posting links from the listing cards.
        links = response.css(
            "article.teaser--type--webny-news .webny-teaser-title a::attr(href)"
        ).getall()

        for href in links:
            url = response.urljoin(href)
            filename = url_to_filename(url)
            dest = HTML_DIR / filename

            if dest.exists():
                self.logger.info("Already saved, skipping: %s", filename)
                continue

            yield scrapy.Request(url, callback=self.save_page)

        # Follow the site's own pagination.
        # The page has both mobile and desktop pagers, so pick one stable selector first.
        next_href = (
            response.css("nav.pager-desktop li.pager__item--next a::attr(href)").get()
            or response.css("li.pager__item--next a[rel='next']::attr(href)").get()
            or response.css("li.pager__item--next a::attr(href)").get()
        )

        if next_href:
            yield response.follow(next_href, callback=self.parse)

    def save_page(self, response):
        filename = url_to_filename(response.url)
        dest = HTML_DIR / filename

        HTML_DIR.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(response.body)

        self.logger.info("Saved %s (%d bytes)", filename, len(response.body))
