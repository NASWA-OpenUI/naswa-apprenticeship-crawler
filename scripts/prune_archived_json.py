from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any

MANIFEST_PATH = Path("manifests/announcements.json")
JSON_ROOT = Path("json")


def load_manifest(path: Path) -> dict[str, Any]:
    """Load the announcements manifest."""
    if not path.exists():
        raise FileNotFoundError(f"Manifest not found: {path}")

    manifest = json.loads(path.read_text(encoding="utf-8"))

    postings = manifest.get("postings")
    if not isinstance(postings, dict):
        raise ValueError(f"Manifest is missing a valid postings object: {path}")

    return manifest


def archived_posting_rows(manifest: dict[str, Any]) -> list[dict[str, str]]:
    """Return manifest records for archived source postings."""
    rows: list[dict[str, str]] = []

    for manifest_key, record in sorted(manifest["postings"].items()):
        if not isinstance(record, dict):
            continue

        if not record.get("archived"):
            continue

        filename = str(record.get("filename") or manifest_key)
        source_slug = Path(filename).stem

        rows.append(
            {
                "filename": filename,
                "source_slug": source_slug,
                "url": str(record.get("url") or ""),
                "archived_at": str(record.get("archived_at") or ""),
            }
        )

    return rows


def find_archived_json_dirs(
    *,
    json_root: Path,
    archived_rows: list[dict[str, str]],
) -> list[dict[str, Any]]:
    """Find json/<source-slug>/ folders that correspond to archived postings."""
    if not json_root.exists():
        raise FileNotFoundError(f"JSON root not found: {json_root}")

    if not json_root.is_dir():
        raise ValueError(f"JSON root is not a directory: {json_root}")

    matches: list[dict[str, Any]] = []

    for row in archived_rows:
        json_dir = json_root / row["source_slug"]

        if not json_dir.exists():
            continue

        if not json_dir.is_dir():
            print(f"Skipping non-directory JSON path: {json_dir}")
            continue

        json_files = sorted(json_dir.glob("*.json"))

        matches.append(
            {
                **row,
                "json_dir": json_dir,
                "json_file_count": len(json_files),
            }
        )

    return matches


def print_matches(matches: list[dict[str, Any]]) -> None:
    """Print archived JSON folders in a readable format."""
    if not matches:
        print("No archived JSON folders found.")
        return

    print(f"Archived JSON folders found: {len(matches)}")
    print()

    for match in matches:
        print(f"- {match['json_dir']}")
        print(f"  source file: {match['filename']}")
        print(f"  json files:   {match['json_file_count']}")
        print(f"  archived at:  {match['archived_at'] or '<unknown>'}")
        print(f"  url:          {match['url']}")
        print()


def delete_matches(matches: list[dict[str, Any]]) -> int:
    """Delete matched archived JSON folders."""
    deleted_count = 0

    for match in matches:
        json_dir = match["json_dir"]

        if not isinstance(json_dir, Path):
            raise TypeError(f"Expected Path for json_dir, got: {type(json_dir)}")

        if not json_dir.exists():
            continue

        if not json_dir.is_dir():
            print(f"Skipping non-directory JSON path: {json_dir}")
            continue

        shutil.rmtree(json_dir)
        deleted_count += 1
        print(f"Deleted: {json_dir}")

    return deleted_count


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "List or delete json/<source-slug>/ folders for postings marked "
            "archived in manifests/announcements.json."
        )
    )

    parser.add_argument(
        "--mode",
        choices=["list", "delete"],
        default="list",
        help="Use 'list' to audit archived JSON folders, or 'delete' to remove them.",
    )

    parser.add_argument(
        "--manifest-path",
        type=Path,
        default=MANIFEST_PATH,
        help=f"Path to announcements manifest. Default: {MANIFEST_PATH}",
    )

    parser.add_argument(
        "--json-root",
        type=Path,
        default=JSON_ROOT,
        help=f"Path to extracted JSON root. Default: {JSON_ROOT}",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    manifest = load_manifest(args.manifest_path)
    archived_rows = archived_posting_rows(manifest)

    matches = find_archived_json_dirs(
        json_root=args.json_root,
        archived_rows=archived_rows,
    )

    print(f"Archived manifest records: {len(archived_rows)}")
    print(f"Matching JSON folders:     {len(matches)}")
    print()

    if args.mode == "list":
        print_matches(matches)
        return

    deleted_count = delete_matches(matches)
    print()
    print(f"Deleted {deleted_count} archived JSON folder(s).")


if __name__ == "__main__":
    main()
    