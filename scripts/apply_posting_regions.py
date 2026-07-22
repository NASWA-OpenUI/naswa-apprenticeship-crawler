"""Infer and apply canonical NY labor-market regions to posting JSON files.

Postings are grouped by ``sourceUrl`` because jobs extracted from the same
announcement share geographic coverage. Existing rows in
``data/locations/posting_regions.csv`` are reused; missing rows are inferred
from ``sourceTitle``, ``locationSummary``, ``residencyRequirement``, and
``allRequirements`` using the official NYS county and locality CSV files plus
an app-owned alias CSV for common or ambiguous place names.

The current ``regions`` value is used only for reporting and write decisions.
It is never used as geographic evidence.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

CANONICAL_REGIONS = (
    "Capital Region",
    "Central New York",
    "Finger Lakes",
    "Hudson Valley",
    "Long Island",
    "Mohawk Valley",
    "New York City",
    "North Country",
    "Southern Tier",
    "Western New York",
)
REGION_ORDER = {region: index for index, region in enumerate(CANONICAL_REGIONS)}
CANONICAL_REGION_SET = frozenset(CANONICAL_REGIONS)

# These labels are the canonical names and legacy variants found in the
# extracted announcements. Short forms are handled separately because words
# such as "central" are unsafe outside an explicit region list.
REGION_ALIASES = {
    "capital district": "Capital Region",
    "capital region": "Capital Region",
    "central new york": "Central New York",
    "central ny": "Central New York",
    "finger lakes": "Finger Lakes",
    "hudson valley": "Hudson Valley",
    "long island": "Long Island",
    "mohawk valley": "Mohawk Valley",
    "new york city": "New York City",
    "north country": "North Country",
    "southern tier": "Southern Tier",
    "western new york": "Western New York",
    "western ny": "Western New York",
}
SHORT_REGION_ALIASES = {
    "central": "Central New York",
    "southern": "Southern Tier",
    "western": "Western New York",
}
REGION_CONTEXT = re.compile(r"\bregions?\b", re.IGNORECASE)

# The official locality hierarchy contains legitimate municipalities whose
# names are unsafe when matched as standalone words in apprenticeship prose.
# For example, "western" may describe a part of the state, "union" usually
# refers to a labor union, and "York" commonly appears inside "New York".
# Exact multi-word region names such as "Western New York" remain valid because
# they are matched separately through REGION_ALIASES before locality matching.
UNSAFE_STANDALONE_LOCATION_TERMS = frozenset(
    {
        "capital",
        "central",
        "east",
        "eastern",
        "new york",
        "north",
        "northern",
        "south",
        "southern",
        "union",
        "west",
        "western",
        "york",
    }
)

# Only geographic entries from allRequirements are considered. This prevents
# unrelated requirement text from introducing accidental place-name matches.
GEOGRAPHIC_REQUIREMENT = re.compile(
    r"\b(?:resid(?:e|es|ed|ence|ency|ent|ents)|live|county|counties|"
    r"region|regions|jurisdiction|job\s*sites?|work\s*sites?|"
    r"geograph(?:ic|ical)|travel\s+(?:to|within))\b",
    re.IGNORECASE,
)
LOCALITY_COLUMNS = ("City Name", "Town Name", "Village Name", "Municipality")


def normalize(value: str) -> str:
    """Normalize punctuation and spacing for phrase matching."""

    value = html.unescape(str(value)).casefold().replace("&", " and ")
    value = re.sub(r"[’'`]", "", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def sort_regions(regions: Iterable[str], *, context: str) -> tuple[str, ...]:
    """Validate, deduplicate, and consistently order canonical regions."""

    unique = set(regions)
    unknown = unique - CANONICAL_REGION_SET
    if unknown:
        raise ValueError(f"{context}: unknown regions: {sorted(unknown)}")
    return tuple(sorted(unique, key=REGION_ORDER.__getitem__))


def read_county_regions(path: Path) -> dict[str, str]:
    """Read the official county-to-labor-market-region mapping."""

    result: dict[str, str] = {}
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for row_number, row in enumerate(csv.DictReader(handle), start=2):
            county = (row.get("County") or "").strip()
            region = (row.get("Region") or "").strip()
            if not county or not region:
                continue
            if region not in CANONICAL_REGION_SET:
                raise ValueError(f"{path}:{row_number}: unknown region {region!r}")
            result[county] = region
    if not result:
        raise ValueError(f"No county mappings found in {path}")
    return result


def read_locality_regions(
    path: Path,
    county_to_region: Mapping[str, str],
) -> dict[str, str]:
    """Map unambiguous NYS cities, towns, and villages to one region."""

    possible: dict[str, set[str]] = defaultdict(set)
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            region = county_to_region.get((row.get("County Name") or "").strip())
            if not region:
                continue
            for column in LOCALITY_COLUMNS:
                name = normalize(row.get(column) or "")
                if name and name not in UNSAFE_STANDALONE_LOCATION_TERMS:
                    possible[name].add(region)

    # Ambiguous locality names are excluded. A reviewer can resolve those by
    # adding the source URL directly to posting_regions.csv. Generic standalone
    # names such as "western", "union", and "york" are also excluded above
    # because they are much more likely to be ordinary prose than locations.
    return {
        locality: next(iter(regions))
        for locality, regions in possible.items()
        if len(regions) == 1
    }


def read_location_aliases(path: Path) -> dict[str, str]:
    """Read app-owned aliases for common places missing or ambiguous in NYS data.

    The official hierarchy contains both the City of Rochester in Monroe County
    and the Town of Rochester in Ulster County, so a name-only locality lookup
    correctly treats "Rochester" as ambiguous. The reviewed alias file lets the
    crawler interpret the common posting phrase "Rochester, NY" as the Finger
    Lakes while keeping that policy decision outside the Python code.
    """

    if not path.exists():
        raise FileNotFoundError(
            f"Missing location alias CSV: {path}. "
            "It is required for reviewed place names such as Rochester."
        )

    result: dict[str, str] = {}
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for row_number, row in enumerate(csv.DictReader(handle), start=2):
            alias = normalize(row.get("Alias") or "")
            region = (row.get("Region") or "").strip()
            if not alias or not region:
                continue
            if region not in CANONICAL_REGION_SET:
                raise ValueError(f"{path}:{row_number}: unknown region {region!r}")

            # Unsafe one-word aliases such as "Central" and "Western" are
            # intentionally ignored. Their exact region phrases remain valid.
            if alias in UNSAFE_STANDALONE_LOCATION_TERMS:
                continue
            previous = result.get(alias)
            if previous is not None and previous != region:
                raise ValueError(f"{path}:{row_number}: conflicting alias {alias!r}")
            result[alias] = region
    return result


def build_terms(
    county_to_region: Mapping[str, str],
    locality_to_region: Mapping[str, str],
    location_aliases: Mapping[str, str],
) -> tuple[tuple[str, str, str], ...]:
    """Build longest-first searchable terms as (text, region, kind)."""

    terms: dict[tuple[str, str], tuple[str, str, str]] = {}

    for alias, region in REGION_ALIASES.items():
        text = normalize(alias)
        terms[(text, "region")] = (text, region, "region")

    # Add both "Erie County" and bare "Erie". They use different kinds so
    # jurisdiction text may recognize a list such as "Erie, Niagara, and..."
    # without treating a city-like value such as "Albany area" as strong county
    # evidence before the location-summary fallback runs. Bare "New York" is
    # excluded so ordinary references to New York State do not imply NYC.
    for county, region in county_to_region.items():
        text = normalize(county)
        terms[(f"{text} county", "county")] = (
            f"{text} county",
            region,
            "county",
        )
        if text != "new york":
            terms[(text, "county_name")] = (text, region, "county_name")

    for alias, region in location_aliases.items():
        if alias in UNSAFE_STANDALONE_LOCATION_TERMS:
            continue
        # Canonical and legacy region phrases retain the stronger "region"
        # kind when the alias CSV happens to contain the same phrase.
        if alias not in {normalize(value) for value in REGION_ALIASES}:
            terms[(alias, "alias")] = (alias, region, "alias")

    for locality, region in locality_to_region.items():
        if locality not in UNSAFE_STANDALONE_LOCATION_TERMS:
            terms[(locality, "locality")] = (locality, region, "locality")

    # Longest terms run first so "Long Island City" wins over "Long Island".
    return tuple(
        sorted(
            terms.values(),
            key=lambda item: (-len(item[0].split()), -len(item[0]), item[2]),
        )
    )


def find_regions(
    text: str,
    terms: Sequence[tuple[str, str, str]],
    *,
    allowed_kinds: frozenset[str] | None = None,
) -> list[tuple[str, str, str]]:
    """Find non-overlapping region, county, and locality matches."""

    normalized = normalize(text)
    matches: list[tuple[str, str, str]] = []
    occupied: list[tuple[int, int]] = []

    for term, region, kind in terms:
        if allowed_kinds is not None and kind not in allowed_kinds:
            continue
        pattern = re.compile(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])")
        for match in pattern.finditer(normalized):
            span = match.span()
            if any(span[0] < end and start < span[1] for start, end in occupied):
                continue
            matches.append((region, kind, term))
            occupied.append(span)

    # The data contains list phrases such as "Central, Finger Lakes, and
    # Western regions". Accept those short aliases only when "region(s)" is
    # explicitly present in the original text.
    if (
        REGION_CONTEXT.search(text)
        and (allowed_kinds is None or "region" in allowed_kinds)
    ):
        matched_regions = {region for region, _, _ in matches}
        for alias, region in SHORT_REGION_ALIASES.items():
            if region in matched_regions:
                continue
            if re.search(
                rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])",
                normalized,
            ):
                matches.append((region, "region", alias))

    return matches


def read_region_map(path: Path) -> dict[str, tuple[tuple[str, ...], str]]:
    """Read saved source URL mappings; an empty starter CSV is valid."""

    if not path.exists():
        return {}

    result: dict[str, tuple[tuple[str, ...], str]] = {}
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for row_number, row in enumerate(csv.DictReader(handle), start=2):
            source_url = (row.get("sourceUrl") or "").strip()
            raw_regions = (row.get("regions") or "").strip()
            notes = (row.get("notes") or "").strip()
            if not source_url and not raw_regions:
                continue
            if not source_url:
                raise ValueError(f"{path}:{row_number}: sourceUrl is required")
            if source_url in result:
                raise ValueError(f"{path}:{row_number}: duplicate sourceUrl")
            try:
                regions = json.loads(raw_regions)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"{path}:{row_number}: regions must be a JSON array"
                ) from exc
            if not isinstance(regions, list) or not all(
                isinstance(region, str) for region in regions
            ):
                raise ValueError(
                    f"{path}:{row_number}: regions must be an array of strings"
                )
            result[source_url] = (
                sort_regions(regions, context=f"{path}:{row_number}"),
                notes,
            )
    return result


def write_region_map(
    path: Path,
    mappings: Mapping[str, tuple[tuple[str, ...], str]],
) -> None:
    """Persist reviewed and newly inferred announcement mappings."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=("sourceUrl", "regions", "notes"))
        writer.writeheader()
        for source_url in sorted(mappings):
            regions, notes = mappings[source_url]
            writer.writerow(
                {
                    "sourceUrl": source_url,
                    "regions": json.dumps(regions),
                    "notes": notes,
                }
            )


def load_postings(json_dir: Path) -> dict[str, list[tuple[Path, dict[str, Any]]]]:
    """Load postings and group every job from the same announcement."""

    groups: dict[str, list[tuple[Path, dict[str, Any]]]] = defaultdict(list)
    for path in sorted(json_dir.glob("*/*.json")):
        posting = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(posting, dict):
            raise ValueError(f"{path}: expected a JSON object")
        source_url = str(posting.get("sourceUrl") or "").strip()
        if not source_url:
            raise ValueError(f"{path}: sourceUrl is required")
        groups[source_url].append((path, posting))
    return dict(groups)


def geographic_sources(
    postings: Sequence[tuple[Path, dict[str, Any]]],
) -> list[tuple[str, str]]:
    """Collect unique geographic evidence across one announcement."""

    sources: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for _, posting in postings:
        for field in ("locationSummary", "residencyRequirement"):
            value = posting.get(field)
            if isinstance(value, str) and value.strip():
                item = (field, value.strip())
                if item not in seen:
                    seen.add(item)
                    sources.append(item)

        requirements = posting.get("allRequirements")
        if isinstance(requirements, list):
            for value in requirements:
                if not isinstance(value, str) or not value.strip():
                    continue
                if not GEOGRAPHIC_REQUIREMENT.search(value):
                    continue
                item = ("allRequirements", value.strip())
                if item not in seen:
                    seen.add(item)
                    sources.append(item)

    # sourceTitle is checked last as a fallback. It can locate simple postings
    # such as Rochester or Elmira but should not override stated jurisdiction.
    for _, posting in postings:
        value = posting.get("sourceTitle")
        if isinstance(value, str) and value.strip():
            item = ("sourceTitle", value.strip())
            if item not in seen:
                seen.add(item)
                sources.append(item)

    return sources


def infer_regions(
    postings: Sequence[tuple[Path, dict[str, Any]]],
    terms: Sequence[tuple[str, str, str]],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Infer one region set and evidence list for a source announcement."""

    regions: set[str] = set()
    evidence: list[str] = []
    sources = geographic_sources(postings)

    # First use explicit region phrases and county jurisdiction evidence. Bare
    # county names are accepted in residency/allRequirements because those
    # fields commonly contain lists such as "Erie, Niagara, and Orleans
    # counties". Location summaries require either an explicit region phrase
    # or a phrase ending in "County" at this stage.
    for field, text in sources:
        if field == "sourceTitle":
            continue
        if field == "locationSummary":
            allowed_kinds = frozenset({"region", "county"})
        else:
            allowed_kinds = frozenset({"region", "county", "county_name"})
        for region, kind, term in find_regions(
            text,
            terms,
            allowed_kinds=allowed_kinds,
        ):
            regions.add(region)
            evidence.append(f"{field}: {kind} {term!r} -> {region}")

    # When no explicit region or county jurisdiction was found, fall back to
    # concrete places in locationSummary. This resolves simple postings such as
    # "Rochester, NY" and multi-place summaries such as "Albany area and Utica
    # area" without allowing incidental locality words from requirement prose.
    if not regions:
        for field, text in sources:
            if field != "locationSummary":
                continue
            for region, kind, term in find_regions(
                text,
                terms,
                allowed_kinds=frozenset(
                    {"region", "county", "county_name", "alias", "locality"}
                ),
            ):
                regions.add(region)
                evidence.append(f"{field}: {kind} {term!r} -> {region}")

    # Use sourceTitle only when all posting-level geographic fields produced no
    # region. Titles can locate simple announcements but should not expand an
    # explicitly stated jurisdiction or a concrete location summary.
    if not regions:
        for field, text in sources:
            if field != "sourceTitle":
                continue
            for region, kind, term in find_regions(
                text,
                terms,
                allowed_kinds=frozenset({"region", "alias", "locality"}),
            ):
                regions.add(region)
                evidence.append(f"{field}: {kind} {term!r} -> {region}")

    return (
        sort_regions(regions, context="inferred posting regions"),
        tuple(dict.fromkeys(evidence)),
    )


def current_regions(posting: Mapping[str, Any]) -> list[str]:
    """Read current values for comparison only, never for inference."""

    value = posting.get("regions")
    if not isinstance(value, list):
        return []
    return [str(region).strip() for region in value if str(region).strip()]


def proposed_status(
    old_regions: list[str],
    new_regions: Sequence[str],
    *,
    overwrite: bool,
) -> str:
    if old_regions == list(new_regions):
        return "unchanged"
    if not new_regions:
        return "unresolved"
    if not old_regions:
        return "fill-missing"
    if overwrite:
        return "replace-existing"
    return "skip-existing"


def write_report(path: Path, rows: Sequence[dict[str, str]]) -> None:
    """Write posting-level proposed and completed changes for review."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fields = (
        "path",
        "id",
        "sourceUrl",
        "mappingSource",
        "oldRegions",
        "newRegions",
        "status",
        "evidence",
    )
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-dir", type=Path, default=Path("json"))
    parser.add_argument(
        "--labor-market-regions-csv",
        type=Path,
        default=Path("data/locations/Labor_Market_Regions.csv"),
    )
    parser.add_argument(
        "--locality-hierarchy-csv",
        type=Path,
        default=Path(
            "data/locations/New_York_State_Locality_Hierarchy_with_Websites.csv"
        ),
    )
    parser.add_argument(
        "--location-aliases-csv",
        type=Path,
        default=Path("data/locations/location_aliases.csv"),
        help="reviewed aliases for common or ambiguous place names",
    )
    parser.add_argument(
        "--region-map",
        type=Path,
        default=Path("data/locations/posting_regions.csv"),
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("reports/posting-region-normalization.csv"),
    )
    parser.add_argument(
        "--mode",
        choices=("audit", "apply"),
        default="audit",
        help="audit previews changes; apply writes permitted changes",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="allow apply mode to replace existing non-empty regions",
    )
    parser.add_argument(
        "--refresh-mappings",
        action="store_true",
        help=(
            "ignore saved sourceUrl mappings and infer them again; apply mode "
            "updates posting_regions.csv with the refreshed results"
        ),
    )
    parser.add_argument(
        "--fail-on-unresolved",
        action="store_true",
        help="exit with status 1 if an announcement cannot be resolved",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)

    county_to_region = read_county_regions(args.labor_market_regions_csv)
    locality_to_region = read_locality_regions(
        args.locality_hierarchy_csv,
        county_to_region,
    )
    location_aliases = read_location_aliases(args.location_aliases_csv)
    terms = build_terms(county_to_region, locality_to_region, location_aliases)
    region_map = read_region_map(args.region_map)
    groups = load_postings(args.json_dir)

    if not groups:
        print(f"No posting files found under {args.json_dir}/*/*.json", file=sys.stderr)
        return 1

    rows: list[dict[str, str]] = []
    counts: dict[str, int] = defaultdict(int)
    unresolved_sources = 0
    saved_mappings = 0

    for source_url, postings in sorted(groups.items()):
        # --refresh-mappings is useful after inference rules change. Audit mode
        # previews the refreshed result; apply mode also replaces the cached row.
        saved = None if args.refresh_mappings else region_map.get(source_url)
        if saved is not None:
            new_regions, notes = saved
            evidence = (notes or "reused posting_regions.csv",)
            mapping_source = "saved"
        else:
            new_regions, evidence = infer_regions(postings, terms)
            mapping_source = "inferred"
            if not new_regions:
                unresolved_sources += 1
            elif args.mode == "apply":
                # Successful announcement-level results are cached so future
                # runs can skip inference. With --refresh-mappings, an existing
                # cached row is deliberately replaced by the new inference.
                refreshed = (new_regions, " | ".join(evidence))
                if region_map.get(source_url) != refreshed:
                    region_map[source_url] = refreshed
                    saved_mappings += 1

        for path, posting in postings:
            old_regions = current_regions(posting)
            status = proposed_status(
                old_regions,
                new_regions,
                overwrite=args.overwrite,
            )

            # Audit never writes. Apply fills empty values by default and only
            # replaces existing values when --overwrite is explicit.
            if args.mode == "apply" and status in {
                "fill-missing",
                "replace-existing",
            }:
                posting["regions"] = list(new_regions)
                path.write_text(
                    json.dumps(posting, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )

            counts[status] += 1
            rows.append(
                {
                    "path": str(path),
                    "id": str(posting.get("id") or ""),
                    "sourceUrl": source_url,
                    "mappingSource": mapping_source,
                    "oldRegions": json.dumps(old_regions),
                    "newRegions": json.dumps(new_regions),
                    "status": status,
                    "evidence": " | ".join(evidence),
                }
            )

    write_report(args.report, rows)
    if args.mode == "apply" and saved_mappings:
        write_region_map(args.region_map, region_map)

    print(f"Mode: {args.mode}")
    print(f"Overwrite existing values: {args.overwrite}")
    print(f"Refresh saved mappings: {args.refresh_mappings}")
    print(f"Announcements processed: {len(groups)}")
    print(f"Postings processed: {len(rows)}")
    print(f"New source mappings saved: {saved_mappings}")
    for status in (
        "fill-missing",
        "replace-existing",
        "skip-existing",
        "unchanged",
        "unresolved",
    ):
        if counts.get(status):
            print(f"{status.replace('-', ' ').title()}: {counts[status]}")
    print(f"Report written: {args.report}")

    if args.fail_on_unresolved and unresolved_sources:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
