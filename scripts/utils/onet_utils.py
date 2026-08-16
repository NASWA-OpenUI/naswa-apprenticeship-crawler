from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ONET_DETAIL_KEYS = [
    "tasks",
    "detailed_work_activities",
    "skills",
    "abilities",
    "work_styles",
]


def load_onet_data(
    onet_root: Path,
    soc_code: str,
) -> dict[str, Any] | None:
    """
    Load the selected O*NET sections used by merged application data.

    Returns None when no cached O*NET file exists for the SOC code.

    The raw cached O*NET profile contains many linked sections. The merged
    application data intentionally keeps only the occupation metadata and
    sections used by the matcher/UI.
    """
    soc_code = soc_code.strip()

    if not soc_code:
        return None

    onet_path = onet_root / f"{soc_code}.json"

    if not onet_path.exists():
        return None

    try:
        source_data = json.loads(
            onet_path.read_text(
                encoding="utf-8"
            )
        )
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Invalid O*NET JSON in {onet_path}: {exc}"
        ) from exc

    if not isinstance(source_data, dict):
        raise ValueError(
            f"Expected O*NET JSON object in {onet_path}."
        )

    file_soc_code = str(
        source_data.get("socCode") or ""
    ).strip()

    if not file_soc_code:
        raise ValueError(
            f"{onet_path} is missing socCode."
        )

    if file_soc_code != soc_code:
        raise ValueError(
            f"{onet_path} contains socCode {file_soc_code!r}, "
            f"but was loaded for {soc_code!r}."
        )

    details = source_data.get("details")

    if not isinstance(details, dict):
        raise ValueError(
            f"{onet_path} is missing a valid details object."
        )

    onet_data: dict[str, Any] = {
        "socCode": file_soc_code,
        "onetSocCode": str(
            source_data.get("onetSocCode") or ""
        ).strip(),
        "title": str(
            source_data.get("title") or ""
        ).strip(),
        "description": str(
            source_data.get("description") or ""
        ).strip(),
        "fetchedAt": str(
            source_data.get("fetchedAt") or ""
        ).strip(),
        "source": source_data.get("source"),
    }

    for key in ONET_DETAIL_KEYS:
        onet_data[key] = details.get(key)

    return onet_data
