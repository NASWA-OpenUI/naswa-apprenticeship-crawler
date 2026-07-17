import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

import scrapy
from scrapy import signals

ROOT_DIR = Path(__file__).resolve().parents[2]
HTML_DIR = ROOT_DIR / "html"
ARCHIVE_DIR = ROOT_DIR / "html-archived"

MANIFEST_DIR = ROOT_DIR / "manifests"
MANIFEST_PATH = MANIFEST_DIR / "announcements.json"

LISTINGS_URL = "https://dol.ny.gov/apprenticeship/apprenticeship-announcements"


def utc_now() -> str:
    """Return a stable UTC timestamp for manifest metadata."""
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


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


def load_manifest() -> dict:
    """Load the crawler manifest, or create a new empty one."""
    if not MANIFEST_PATH.exists():
        return {
            "version": 1,
            "source": LISTINGS_URL,
            "postings": {},
        }

    data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    data.setdefault("version", 1)
    data.setdefault("source", LISTINGS_URL)
    data.setdefault("postings", {})

    return data


def save_manifest(manifest: dict) -> None:
    """Write the manifest atomically."""
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)

    # Keep the file stable and diff-friendly.
    manifest = dict(manifest)
    postings = manifest.get("postings", {})
    manifest["postings"] = {
        filename: postings[filename]
        for filename in sorted(postings)
    }

    tmp_path = MANIFEST_PATH.with_suffix(".tmp")
    tmp_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    tmp_path.replace(MANIFEST_PATH)


class AnnouncementsSpider(scrapy.Spider):
    name = "announcements"
    start_urls = [LISTINGS_URL]

    def __init__(
        self,
        archive_missing: str = "false",
        refresh_existing: str = "false",
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)

        self.archive_missing = str_to_bool(archive_missing)

        self.refresh_existing = str_to_bool(refresh_existing)

        self.existing_files: set[str] = set()
        self.seen_files: set[str] = set()
        self.new_files: set[str] = set()
        self.archived_files: set[str] = set()

        self.manifest: dict = {}
        self.run_started_at = ""

    @classmethod
    def from_crawler(cls, crawler, *args, **kwargs):
        spider = super().from_crawler(crawler, *args, **kwargs)

        crawler.signals.connect(spider.spider_opened, signal=signals.spider_opened)
        crawler.signals.connect(spider.spider_closed, signal=signals.spider_closed)

        return spider

    def spider_opened(self, spider):
        HTML_DIR.mkdir(parents=True, exist_ok=True)

        self.run_started_at = utc_now()
        self.manifest = load_manifest()
        self.manifest["source"] = LISTINGS_URL
        self.manifest["last_run_started_at"] = self.run_started_at

        self.existing_files = {path.name for path in HTML_DIR.glob("*.html")}

        self.logger.info(
            "Tracking %d existing HTML files in %s",
            len(self.existing_files),
            HTML_DIR,
        )

        self.logger.info("Using manifest: %s", MANIFEST_PATH)

    def record_current_posting(
        self,
        *,
        filename: str,
        url: str,
        listing_page_url: str,
    ) -> None:
        """Record one posting as current in the manifest."""
        postings = self.manifest.setdefault("postings", {})
        existing = postings.get(filename, {})

        record = {
            "filename": filename,
            "url": url,
            "listing_page_url": listing_page_url,
            "archived": False,
            "first_seen_at": existing.get("first_seen_at") or self.run_started_at,
            "last_seen_at": self.run_started_at,
        }

        postings[filename] = record
        self.seen_files.add(filename)

    def parse(self, response):
        # Collect posting links from the listing cards.
        links = response.css(
            "article.teaser--type--webny-news .webny-teaser-title a::attr(href)"
        ).getall()

        self.logger.info("Found %d posting links on %s", len(links), response.url)

        for href in links:
            request_url = response.urljoin(href)
            filename = url_to_filename(request_url)

            dest = HTML_DIR / filename

            if dest.exists() and not self.refresh_existing:
                existing_record = self.manifest.get("postings", {}).get(filename, {})

                self.record_current_posting(
                    filename=filename,
                    url=existing_record.get("url") or request_url,
                    listing_page_url=response.url,
                )

                self.logger.info("Already saved, skipping: %s", filename)
                continue

            yield scrapy.Request(
                request_url,
                callback=self.save_page,
                meta={
                    "request_url": request_url,
                    "request_filename": filename,
                    "listing_page_url": response.url,
                },
            )

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
        request_url = response.meta.get("request_url", response.url)
        request_filename = response.meta.get(
            "request_filename",
            url_to_filename(request_url),
        )
        listing_page_url = response.meta.get("listing_page_url", "")

        url = response.url
        filename = url_to_filename(url)
        dest = HTML_DIR / filename

        is_new = filename not in self.existing_files

        HTML_DIR.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(response.body)

        self.record_current_posting(
            filename=filename,
            url=url,
            listing_page_url=listing_page_url,
        )

        if is_new:
            self.new_files.add(filename)

        if filename != request_filename or url.rstrip("/") != request_url.rstrip("/"):
            self.logger.info(
                "Redirected while saving: requested=%s final=%s",
                request_url,
                url,
            )

        self.logger.info("Saved %s (%d bytes)", filename, len(response.body))

    def mark_archived_manifest_records(self, finished_at: str) -> None:
        """Mark manifest records archived if they were not seen in this completed run."""
        postings = self.manifest.setdefault("postings", {})

        for filename, record in postings.items():
            if filename in self.seen_files:
                continue

            if not record.get("archived"):
                record["archived"] = True
                record["archived_at"] = finished_at

    def spider_closed(self, spider, reason):
        if reason != "finished":
            self.logger.warning(
                "Spider closed with reason=%s. Not archiving missing files or saving manifest changes.",
                reason,
            )
            return

        finished_at = utc_now()

        missing_files = sorted(self.existing_files - self.seen_files)

        # This updates the manifest even if archive_missing=false.
        # archive_missing only controls whether the physical html file is moved.
        self.mark_archived_manifest_records(finished_at)

        if not missing_files:
            self.logger.info("No deprecated local HTML files found.")
        else:
            self.logger.warning(
                "Found %d existing HTML files that were not seen on the live listing:",
                len(missing_files),
            )

            for filename in missing_files:
                self.logger.warning("Deprecated candidate: %s", filename)

        if missing_files and not self.archive_missing:
            self.logger.warning(
                "Dry run only. Manifest was updated, but files were not moved. "
                "Re-run with -a archive_missing=true to move these to %s",
                ARCHIVE_DIR,
            )

        if missing_files and self.archive_missing:
            ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

            for filename in missing_files:
                src = HTML_DIR / filename

                if not src.exists():
                    self.logger.warning("File disappeared before archiving: %s", src)
                    continue

                dest = unique_archive_path(ARCHIVE_DIR / filename)
                shutil.move(str(src), str(dest))

                self.archived_files.add(filename)

                self.logger.info("Archived %s -> %s", src, dest)

        postings = self.manifest.setdefault("postings", {})
        self.manifest["last_run_finished_at"] = finished_at
        self.manifest["last_run_reason"] = reason
        self.manifest["current_count"] = len(self.seen_files)
        self.manifest["archived_count"] = sum(
            1 for record in postings.values() if record.get("archived")
        )

        save_manifest(self.manifest)

        self.logger.info("")
        self.logger.info("=" * 60)
        self.logger.info("CRAWL SUMMARY")
        self.logger.info("=" * 60)
        self.logger.info(
            "Jobs crawled:  %d",
            len(self.seen_files),
        )
        self.logger.info(
            "New jobs:      %d",
            len(self.new_files),
        )
        self.logger.info(
            "Jobs archived: %d",
            len(self.archived_files),
        )
        self.logger.info(
            "Archive candidates: %d",
            len(missing_files),
        )
        self.logger.info("=" * 60)

        self.logger.info("Manifest saved to %s", MANIFEST_PATH)