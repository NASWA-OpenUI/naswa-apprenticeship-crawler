import re
import shutil
from pathlib import Path

import scrapy
from scrapy import signals

ROOT_DIR = Path(__file__).resolve().parents[2]
HTML_DIR = ROOT_DIR / "html"
ARCHIVE_DIR = ROOT_DIR / "html-archived"

LISTINGS_URL = "https://dol.ny.gov/apprenticeship/apprenticeship-announcements"


def url_to_filename(url: str) -> str:
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    slug = re.split(r"[?#]", slug)[0]
    return f"{slug}.html"


def str_to_bool(value: str) -> bool:
    return str(value).lower() in {"1", "true", "yes", "y"}


def unique_archive_path(path: Path) -> Path:
    """Avoid overwriting an already-archived file with the same name."""
    if not path.exists():
        return path

    for i in range(1, 10_000):
        candidate = path.with_name(f"{path.stem}-{i}{path.suffix}")
        if not candidate.exists():
            return candidate

    raise RuntimeError(f"Could not find unique archive path for {path}")


class AnnouncementsSpider(scrapy.Spider):
    name = "announcements"
    start_urls = [LISTINGS_URL]

    def __init__(self, archive_missing: str = "false", *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.archive_missing = str_to_bool(archive_missing)
        self.existing_files: set[str] = set()
        self.seen_files: set[str] = set()

    @classmethod
    def from_crawler(cls, crawler, *args, **kwargs):
        spider = super().from_crawler(crawler, *args, **kwargs)

        crawler.signals.connect(spider.spider_opened, signal=signals.spider_opened)
        crawler.signals.connect(spider.spider_closed, signal=signals.spider_closed)

        return spider

    def spider_opened(self, spider):
        HTML_DIR.mkdir(parents=True, exist_ok=True)

        self.existing_files = {path.name for path in HTML_DIR.glob("*.html")}

        self.logger.info(
            "Tracking %d existing HTML files in %s",
            len(self.existing_files),
            HTML_DIR,
        )

    def parse(self, response):
        # Collect posting links from the listing cards.
        links = response.css(
            "article.teaser--type--webny-news .webny-teaser-title a::attr(href)"
        ).getall()

        self.logger.info("Found %d posting links on %s", len(links), response.url)

        for href in links:
            url = response.urljoin(href)
            filename = url_to_filename(url)

            # This is the key point:
            # mark the file as still active even if we already have it locally.
            self.seen_files.add(filename)

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

    def spider_closed(self, spider, reason):
        if reason != "finished":
            self.logger.warning(
                "Spider closed with reason=%s. Not archiving missing files.",
                reason,
            )
            return

        missing_files = sorted(self.existing_files - self.seen_files)

        if not missing_files:
            self.logger.info("No deprecated local HTML files found.")
            return

        self.logger.warning(
            "Found %d existing HTML files that were not seen on the live listing:",
            len(missing_files),
        )

        for filename in missing_files:
            self.logger.warning("Deprecated candidate: %s", filename)

        if not self.archive_missing:
            self.logger.warning(
                "Dry run only. Re-run with -a archive_missing=true to move these to %s",
                ARCHIVE_DIR,
            )
            return

        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

        for filename in missing_files:
            src = HTML_DIR / filename

            if not src.exists():
                self.logger.warning("File disappeared before archiving: %s", src)
                continue

            dest = unique_archive_path(ARCHIVE_DIR / filename)
            shutil.move(str(src), str(dest))

            self.logger.info("Archived %s -> %s", src, dest)