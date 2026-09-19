from __future__ import annotations

import csv
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit


SOURCE_CSV_PATH = Path(
    "programs/ra-programs-opportunities.csv"
)

OUTPUT_CSV_PATH = Path(
    "programs/ra-programs-opportunities-verified.csv"
)

OPPORTUNITY_OUTPUT_ROOT = Path("out")

REDIRECT_STATUSES = {
    301,
    302,
    303,
    307,
    308,
}

MAX_REDIRECTS = 10


class NoRedirectHandler(
    urllib.request.HTTPRedirectHandler
):
    def redirect_request(
        self,
        req,
        fp,
        code,
        msg,
        headers,
        newurl,
    ):
        return None


OPENER = urllib.request.build_opener(
    NoRedirectHandler
)


def normalize_url(
    value: str,
) -> str:
    text = value.strip()

    if not text:
        return ""

    parts = urlsplit(text)

    if (
        not parts.scheme
        and not parts.netloc
    ):
        return text.rstrip("/")

    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path.rstrip("/"),
            parts.query,
            "",
        )
    )


def load_current_opportunity_urls() -> set[str]:
    urls: set[str] = set()

    for path in sorted(
        OPPORTUNITY_OUTPUT_ROOT.glob("*.json")
    ):
        try:
            data = json.loads(
                path.read_text(
                    encoding="utf-8"
                )
            )
        except (
            OSError,
            json.JSONDecodeError,
        ) as exc:
            raise SystemExit(
                f"Could not read {path}: {exc}"
            ) from exc

        posting = data.get("posting")

        if not isinstance(
            posting,
            dict,
        ):
            continue

        source_url = posting.get(
            "sourceUrl"
        )

        if (
            isinstance(
                source_url,
                str,
            )
            and source_url.strip()
        ):
            urls.add(
                normalize_url(
                    source_url
                )
            )

    return urls


def request_url(
    url: str,
) -> tuple[int | str, str | None]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 "
                "(compatible; "
                "NASWA-Apprenticeship-Crawler/1.0)"
            ),
        },
        method="GET",
    )

    try:
        with OPENER.open(
            request,
            timeout=20,
        ) as response:
            return (
                response.status,
                response.headers.get(
                    "Location"
                ),
            )

    except urllib.error.HTTPError as exc:
        return (
            exc.code,
            exc.headers.get(
                "Location"
            ),
        )

    except urllib.error.URLError as exc:
        print(
            f"Could not request {url}: "
            f"{exc.reason}",
            file=sys.stderr,
        )
        return (
            "ERROR",
            None,
        )

    except TimeoutError:
        print(
            f"Request timed out: {url}",
            file=sys.stderr,
        )
        return (
            "TIMEOUT",
            None,
        )


def trace_url(
    program_ak: str,
    starting_url: str,
    current_urls: set[str],
) -> list[dict[str, str]]:
    rows: list[
        dict[str, str]
    ] = []

    current_url = normalize_url(
        starting_url
    )

    visited: set[str] = set()

    for _ in range(
        MAX_REDIRECTS + 1
    ):
        if current_url in visited:
            rows.append(
                {
                    "PROGRAM_AK": program_ak,
                    "POSTING_URL": current_url,
                    "STATUS": "REDIRECT_LOOP",
                }
            )
            break

        visited.add(
            current_url
        )

        # If today's crawler output contains this
        # exact source URL, consider it current.
        if current_url in current_urls:
            rows.append(
                {
                    "PROGRAM_AK": program_ak,
                    "POSTING_URL": current_url,
                    "STATUS": "200",
                }
            )
            break

        status, location = request_url(
            current_url
        )

        rows.append(
            {
                "PROGRAM_AK": program_ak,
                "POSTING_URL": current_url,
                "STATUS": str(status),
            }
        )

        if (
            not isinstance(
                status,
                int,
            )
            or status
            not in REDIRECT_STATUSES
        ):
            break

        if not location:
            break

        current_url = normalize_url(
            urljoin(
                current_url,
                location,
            )
        )

    else:
        rows.append(
            {
                "PROGRAM_AK": program_ak,
                "POSTING_URL": current_url,
                "STATUS": "TOO_MANY_REDIRECTS",
            }
        )

    return rows


def load_source_rows() -> list[
    tuple[str, str]
]:
    with SOURCE_CSV_PATH.open(
        newline="",
        encoding="utf-8-sig",
    ) as csv_file:
        reader = csv.DictReader(
            csv_file
        )

        rows: list[
            tuple[str, str]
        ] = []

        for row_number, row in enumerate(
            reader,
            start=2,
        ):
            program_ak = (
                row.get("PROGRAM_AK")
                or ""
            ).strip()

            posting_url = (
                row.get("POSTING_URL")
                or ""
            ).strip()

            if (
                not program_ak
                or not posting_url
            ):
                raise SystemExit(
                    f"{SOURCE_CSV_PATH} "
                    f"row {row_number} "
                    "is missing PROGRAM_AK "
                    "or POSTING_URL."
                )

            rows.append(
                (
                    program_ak,
                    posting_url,
                )
            )

    return rows


def main() -> None:
    if not SOURCE_CSV_PATH.is_file():
        raise SystemExit(
            f"Not found: {SOURCE_CSV_PATH}"
        )

    if not OPPORTUNITY_OUTPUT_ROOT.is_dir():
        raise SystemExit(
            f"Not found: "
            f"{OPPORTUNITY_OUTPUT_ROOT}"
        )

    current_urls = (
        load_current_opportunity_urls()
    )

    source_rows = load_source_rows()

    output_rows: list[
        dict[str, str]
    ] = []

    for (
        program_ak,
        posting_url,
    ) in source_rows:
        print(
            f"Checking PROGRAM_AK "
            f"{program_ak}: "
            f"{posting_url}"
        )

        traced_rows = trace_url(
            program_ak,
            posting_url,
            current_urls,
        )

        output_rows.extend(
            traced_rows
        )

        for traced_row in traced_rows:
            print(
                f"  "
                f"{traced_row['STATUS']} "
                f"{traced_row['POSTING_URL']}"
            )

    with OUTPUT_CSV_PATH.open(
        "w",
        newline="",
        encoding="utf-8",
    ) as csv_file:
        writer = csv.DictWriter(
            csv_file,
            fieldnames=[
                "PROGRAM_AK",
                "POSTING_URL",
                "STATUS",
            ],
        )

        writer.writeheader()
        writer.writerows(
            output_rows
        )

    print("")
    print(
        "Program opportunity URL "
        "verification complete:"
    )
    print(
        f"  Source links: "
        f"{len(source_rows)}"
    )
    print(
        f"  Trace rows written: "
        f"{len(output_rows)}"
    )
    print(
        f"  Current opportunity URLs: "
        f"{len(current_urls)}"
    )
    print("")
    print(
        f"Wrote: {OUTPUT_CSV_PATH}"
    )


if __name__ == "__main__":
    main()
