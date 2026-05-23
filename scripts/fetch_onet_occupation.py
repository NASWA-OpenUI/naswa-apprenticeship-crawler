from __future__ import annotations

import argparse
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

API_BASE = "https://api-v2.onetcenter.org"
OUTPUT_DIR = Path("./onet")

# O*NET asks clients to delay and retry when a 429 response is returned.
# Their docs say at least 200ms; this is intentionally a bit more conservative.
RETRY_DELAY_SECONDS = 1

# Small delay between successful requests so batch fetching stays polite.
REQUEST_DELAY_SECONDS = 0.5


def slugify(value: str) -> str:
    """Convert endpoint titles like 'Work Activities' into stable JSON keys."""
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "_", value)
    return value.strip("_")


def get_section_key(item: dict[str, str]) -> str:
    """
    Build a readable key for a linked O*NET report section.

    Prefer the title because it is stable and human-readable. Fall back to the
    final URL segment if a title is unavailable.
    """
    if item.get("title"):
        return slugify(item["title"])

    path = urlparse(item["href"]).path
    return slugify(Path(path).name)


def get_json(url: str, api_key: str) -> dict[str, Any]:
    """Fetch JSON from O*NET using the required X-API-Key header."""
    response = requests.get(
        url,
        headers={"X-API-Key": api_key},
        timeout=30,
    )

    if response.status_code == 429:
        time.sleep(RETRY_DELAY_SECONDS)
        response = requests.get(
            url,
            headers={"X-API-Key": api_key},
            timeout=30,
        )

    response.raise_for_status()
    return response.json()


def merge_page_results(merged: dict[str, Any], page: dict[str, Any]) -> None:
    """
    Merge list fields from a paginated response into the first response object.

    O*NET paginated endpoints include metadata like start, end, total, prev, and
    next. The actual result lists vary by endpoint, so this generic merge keeps
    the original response shape and appends any list fields found on later pages.
    """
    for key, value in page.items():
        if isinstance(value, list):
            merged.setdefault(key, [])
            merged[key].extend(value)

    for key in ("start", "end", "total"):
        if key in page:
            merged[key] = page[key]


def fetch_paginated(url: str, api_key: str) -> dict[str, Any]:
    """
    Fetch an O*NET endpoint and follow any `next` links until all pages are read.
    """
    first_page = get_json(url, api_key)
    merged = dict(first_page)

    next_url = first_page.get("next")

    while next_url:
        time.sleep(REQUEST_DELAY_SECONDS)
        page = get_json(next_url, api_key)
        merge_page_results(merged, page)
        next_url = page.get("next")

    # These links are useful while fetching, but noisy in a saved local cache.
    merged.pop("next", None)
    merged.pop("prev", None)

    return merged


def fetch_linked_sections(
    items: list[dict[str, str]],
    api_key: str,
) -> dict[str, Any]:
    """Fetch each linked report section from an O*NET contents list."""
    sections: dict[str, Any] = {}

    for item in items:
        href = item["href"]
        title = item.get("title", href)
        key = get_section_key(item)

        print(f"Fetching {title}: {href}")
        sections[key] = {
            "title": title,
            "href": href,
            "data": fetch_paginated(href, api_key),
        }

        time.sleep(REQUEST_DELAY_SECONDS)

    return sections


def fetch_occupation_profile(
    soc_code: str,
    api_key: str,
    include_summary: bool = False,
) -> dict[str, Any]:
    """
    Fetch a rich O*NET occupation profile and return a local-cache-friendly JSON object.
    """
    overview_url = f"{API_BASE}/online/occupations/{soc_code}"
    print(f"Fetching occupation overview: {overview_url}")

    overview = get_json(overview_url, api_key)

    profile: dict[str, Any] = {
        "socCode": soc_code,
        "onetSocCode": overview.get("code"),
        "title": overview.get("title"),
        "description": overview.get("description"),
        "fetchedAt": datetime.now(timezone.utc).isoformat(),
        "source": {
            "name": "O*NET Web Services API v2",
            "overviewUrl": overview_url,
        },
        "overview": overview,
        "details": fetch_linked_sections(
            overview.get("details_contents", []),
            api_key,
        ),
        "custom": fetch_linked_sections(
            overview.get("custom_contents", []),
            api_key,
        ),
    }

    if include_summary:
        profile["summary"] = fetch_linked_sections(
            overview.get("summary_contents", []),
            api_key,
        )

    return profile


def save_profile(profile: dict[str, Any], soc_code: str) -> Path:
    """Save a fetched occupation profile to ./onet/{SOC_CODE}.json."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    output_path = OUTPUT_DIR / f"{soc_code}.json"
    output_path.write_text(
        json.dumps(profile, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    return output_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch rich O*NET occupation JSON for one or more SOC codes."
    )
    parser.add_argument(
        "soc_codes",
        nargs="+",
        help="One or more O*NET-SOC codes, for example: 47-2111.00",
    )
    parser.add_argument(
        "--include-summary",
        action="store_true",
        help="Also fetch summary_contents. By default, only details_contents and custom_contents are fetched.",
    )
    return parser.parse_args()


def main() -> None:
    load_dotenv()

    api_key = os.getenv("ONET_API_KEY")

    if not api_key:
        raise RuntimeError("Missing ONET_API_KEY in your environment or .env file.")

    args = parse_args()

    for soc_code in args.soc_codes:
        profile = fetch_occupation_profile(
            soc_code=soc_code,
            api_key=api_key,
            include_summary=args.include_summary,
        )
        output_path = save_profile(profile, soc_code)
        print(f"Wrote {output_path}")


if __name__ == "__main__":
    main()
