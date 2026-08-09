from __future__ import annotations

import html
import re
from functools import lru_cache
from typing import Any, Dict, List, Optional

import requests

ANILIST_URL = "https://graphql.anilist.co"
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "MangaBridge/2.0 (+self-hosted manga library)",
    "Accept": "application/json",
    "Content-Type": "application/json",
})

MEDIA_FIELDS = """
      id
      idMal
      type
      isAdult
      title { romaji english native }
      description(asHtml: false)
      status
      format
      chapters
      volumes
      averageScore
      popularity
      trending
      favourites
      siteUrl
      bannerImage
      genres
      tags { name rank isMediaSpoiler }
      coverImage { extraLarge large medium color }
"""

SEARCH_QUERY = f"""
query ($search: String, $page: Int, $perPage: Int) {{
  Page(page: $page, perPage: $perPage) {{
    media(search: $search, type: MANGA, isAdult: false) {{
{MEDIA_FIELDS}
    }}
  }}
}}
"""

DETAIL_QUERY = f"""
query ($id: Int) {{
  Media(id: $id, type: MANGA) {{
{MEDIA_FIELDS}
    relations {{ edges {{ relationType node {{ {MEDIA_FIELDS} }} }} }}
    staff(perPage: 24) {{
      edges {{
        role
        node {{
          id
          name {{ full native }}
          image {{ large medium }}
          siteUrl
          primaryOccupations
        }}
      }}
    }}
    recommendations(sort: RATING_DESC, perPage: 18) {{
      nodes {{ rating mediaRecommendation {{ {MEDIA_FIELDS} }} }}
    }}
  }}
}}
"""

EXPLORE_QUERY = f"""
query ($page: Int, $perPage: Int, $sort: [MediaSort]) {{
  Page(page: $page, perPage: $perPage) {{
    media(type: MANGA, isAdult: false, sort: $sort) {{
{MEDIA_FIELDS}
    }}
  }}
}}
"""


STAFF_QUERY = f"""
query ($id: Int, $page: Int, $perPage: Int) {{
  Staff(id: $id) {{
    id
    name {{ first middle last full native alternative userPreferred }}
    image {{ large medium }}
    description(asHtml: false)
    siteUrl
    primaryOccupations
    gender
    age
    homeTown
    yearsActive
    dateOfBirth {{ year month day }}
    dateOfDeath {{ year month day }}
    staffMedia(page: $page, perPage: $perPage, type: MANGA, sort: [POPULARITY_DESC]) {{
      pageInfo {{ currentPage lastPage hasNextPage total }}
      nodes {{
{MEDIA_FIELDS}
      }}
    }}
  }}
}}
"""

RECOMMENDATIONS_QUERY = f"""
query ($id: Int) {{
  Media(id: $id, type: MANGA) {{
    recommendations(sort: RATING_DESC, perPage: 20) {{
      nodes {{ rating mediaRecommendation {{ {MEDIA_FIELDS} }} }}
    }}
  }}
}}
"""


def _post(query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
    response = SESSION.post(ANILIST_URL, json={"query": query, "variables": variables}, timeout=30)
    response.raise_for_status()
    payload = response.json()
    if payload.get("errors"):
        raise RuntimeError(payload["errors"][0].get("message", "AniList error"))
    return payload["data"]


@lru_cache(maxsize=128)
def search(title: str, per_page: int = 8) -> List[Dict[str, Any]]:
    data = _post(SEARCH_QUERY, {"search": title, "page": 1, "perPage": per_page})
    return data.get("Page", {}).get("media", []) or []


@lru_cache(maxsize=256)
def fetch(media_id: int) -> Dict[str, Any]:
    data = _post(DETAIL_QUERY, {"id": int(media_id)})
    return data.get("Media") or {}


@lru_cache(maxsize=8)
def explore_popular(per_page: int = 18) -> List[Dict[str, Any]]:
    data = _post(EXPLORE_QUERY, {"page": 1, "perPage": per_page, "sort": ["TRENDING_DESC", "POPULARITY_DESC"]})
    return data.get("Page", {}).get("media", []) or []


@lru_cache(maxsize=128)
def recommendations(media_id: int) -> List[Dict[str, Any]]:
    data = _post(RECOMMENDATIONS_QUERY, {"id": int(media_id)})
    nodes = data.get("Media", {}).get("recommendations", {}).get("nodes", []) or []
    output: List[Dict[str, Any]] = []
    for node in nodes:
        media = node.get("mediaRecommendation") if isinstance(node, dict) else None
        if isinstance(media, dict):
            item = dict(media)
            item["recommendationRating"] = node.get("rating")
            output.append(item)
    return output


def cover_images(item: Dict[str, Any]) -> Dict[str, str]:
    raw = item.get("coverImage") if isinstance(item, dict) else None
    if not isinstance(raw, dict):
        return {}
    result: Dict[str, str] = {}
    for key in ("extraLarge", "large", "medium", "color"):
        value = raw.get(key)
        if value:
            result[key] = str(value)
    return result


def best_cover(item: Dict[str, Any]) -> str:
    images = cover_images(item)
    return images.get("extraLarge") or images.get("large") or images.get("medium") or ""


def best_title(item: Dict[str, Any]) -> str:
    title = item.get("title") or {}
    return title.get("english") or title.get("romaji") or title.get("native") or "Untitled"


def safe_text(value: Optional[str]) -> str:
    if not value:
        return ""
    return html.unescape(value).strip()


def creator_credits(item: Dict[str, Any]) -> List[Dict[str, Any]]:
    staff = item.get("staff") if isinstance(item, dict) else None
    edges = staff.get("edges") if isinstance(staff, dict) else None
    if not isinstance(edges, list):
        return []
    credits: List[Dict[str, Any]] = []
    by_id: Dict[int, Dict[str, Any]] = {}
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        role = str(edge.get("role") or "").strip()
        role_lower = role.lower()
        if "assistant" in role_lower or not re.search(r"\b(?:story|art|original creator)\b", role_lower):
            continue
        node = edge.get("node") if isinstance(edge.get("node"), dict) else {}
        try:
            staff_id = int(node.get("id"))
        except (TypeError, ValueError):
            continue
        if staff_id in by_id:
            existing = by_id[staff_id]
            roles = [part.strip() for part in str(existing.get("role") or "").split(" / ") if part.strip()]
            if role and role not in roles:
                roles.append(role)
                existing["role"] = " / ".join(roles)
            continue
        name = node.get("name") if isinstance(node.get("name"), dict) else {}
        image = node.get("image") if isinstance(node.get("image"), dict) else {}
        credit = {
            "id": staff_id,
            "name": name.get("full") or name.get("native") or f"Staff {staff_id}",
            "native_name": name.get("native") or "",
            "role": role or "Creator",
            "image": image.get("large") or image.get("medium") or "",
            "site_url": node.get("siteUrl") or "",
            "occupations": node.get("primaryOccupations") or [],
        }
        credits.append(credit)
        by_id[staff_id] = credit
    return credits


@lru_cache(maxsize=128)
def staff(staff_id: int) -> Dict[str, Any]:
    page = 1
    per_page = 25
    profile: Dict[str, Any] = {}
    works: List[Dict[str, Any]] = []
    seen_media: set[int] = set()
    while page <= 40:
        data = _post(STAFF_QUERY, {"id": int(staff_id), "page": page, "perPage": per_page})
        current = data.get("Staff") or {}
        if not profile:
            profile = dict(current)
        connection = current.get("staffMedia") if isinstance(current.get("staffMedia"), dict) else {}
        for media in connection.get("nodes") or []:
            if not isinstance(media, dict) or media.get("isAdult"):
                continue
            try:
                media_id = int(media.get("id"))
            except (TypeError, ValueError):
                continue
            if media_id in seen_media:
                continue
            seen_media.add(media_id)
            works.append(media)
        page_info = connection.get("pageInfo") if isinstance(connection.get("pageInfo"), dict) else {}
        if not page_info.get("hasNextPage"):
            break
        page += 1
    profile.pop("staffMedia", None)
    profile["works"] = works
    return profile
