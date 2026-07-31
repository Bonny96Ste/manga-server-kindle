from __future__ import annotations

import html
from functools import lru_cache
from typing import Any, Dict, List, Optional

import requests

ANILIST_URL = "https://graphql.anilist.co"
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36",
    "Accept": "application/json",
    "Content-Type": "application/json",
})

SEARCH_QUERY = """
query ($search: String, $page: Int, $perPage: Int) {
  Page(page: $page, perPage: $perPage) {
    media(search: $search, type: MANGA) {
      id
      title {
        romaji
        english
        native
      }
      description(asHtml: false)
      status
      chapters
      volumes
      averageScore
      siteUrl
      genres
      coverImage {
        large
      }
    }
  }
}
"""

DETAIL_QUERY = """
query ($id: Int) {
  Media(id: $id, type: MANGA) {
    id
    title {
      romaji
      english
      native
    }
    description(asHtml: false)
    status
    chapters
    volumes
    averageScore
    siteUrl
    genres
    coverImage {
      large
    }
  }
}
"""


def _post(query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
    response = SESSION.post(ANILIST_URL, json={"query": query, "variables": variables}, timeout=30)
    response.raise_for_status()
    payload = response.json()
    if payload.get("errors"):
        raise RuntimeError(payload["errors"][0].get("message", "AniList error"))
    return payload["data"]


@lru_cache(maxsize=64)
def search(title: str, per_page: int = 8) -> List[Dict[str, Any]]:
    data = _post(SEARCH_QUERY, {"search": title, "page": 1, "perPage": per_page})
    return data.get("Page", {}).get("media", []) or []


@lru_cache(maxsize=128)
def fetch(media_id: int) -> Dict[str, Any]:
    data = _post(DETAIL_QUERY, {"id": int(media_id)})
    return data.get("Media") or {}


def best_title(item: Dict[str, Any]) -> str:
    title = item.get("title") or {}
    return title.get("english") or title.get("romaji") or title.get("native") or "Untitled"


def safe_text(value: Optional[str]) -> str:
    if not value:
        return ""
    return html.unescape(value).strip()
