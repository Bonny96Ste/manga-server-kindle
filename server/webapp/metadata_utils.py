from __future__ import annotations

from typing import Any, Dict


def normalize_metadata_block(raw: Any) -> Dict[str, Any]:
    """Return template-safe metadata without trusting older/malformed JSON shapes."""
    block = dict(raw) if isinstance(raw, dict) else {}
    for key in ("description", "siteUrl", "bannerImage", "coverImage", "source"):
        value = block.get(key)
        if value is None:
            block[key] = ""
        elif not isinstance(value, str):
            block[key] = str(value) if isinstance(value, (int, float, bool)) else ""

    for key in ("genres", "tags"):
        values = block.get(key)
        if not isinstance(values, list):
            values = []
        block[key] = [str(value) for value in values if isinstance(value, (str, int, float)) and str(value).strip()]

    creators = []
    raw_creators = block.get("creators")
    if isinstance(raw_creators, list):
        for value in raw_creators:
            if not isinstance(value, dict):
                continue
            try:
                staff_id = int(value.get("id"))
            except (TypeError, ValueError):
                continue
            name = value.get("name")
            role = value.get("role")
            creators.append({
                "id": staff_id,
                "name": str(name).strip() if isinstance(name, (str, int, float)) and str(name).strip() else f"Staff {staff_id}",
                "role": str(role).strip() if isinstance(role, (str, int, float)) and str(role).strip() else "Creator",
            })
    block["creators"] = creators
    return block
