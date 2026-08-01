from __future__ import annotations

import json
import logging
import re
import shutil
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from PIL import Image

BASE_URL = "https://weebcentral.com"
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 13; Termux) AppleWebKit/537.36",
    "Accept": "text/html,*/*",
    "Referer": BASE_URL + "/",
}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
CHAPTER_PARSER_VERSION = 3
LOGGER = logging.getLogger(__name__)


class MangaSourceError(RuntimeError):
    """Raised when the upstream site responds but cannot be parsed safely."""


class _ChapterLinkParser(HTMLParser):
    """Collect chapter anchors without depending on WeebCentral CSS classes."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._active_href: Optional[str] = None
        self._text_parts: List[str] = []
        self.links: List[Tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() != "a" or self._active_href is not None:
            return
        attr_map = {str(key).lower(): value for key, value in attrs if key}
        href = str(attr_map.get("href") or "").strip()
        if not href:
            return
        path = urllib.parse.urlparse(urllib.parse.urljoin(BASE_URL, href)).path
        if not path.startswith("/chapters/"):
            return
        self._active_href = href
        self._text_parts = []

    def handle_data(self, data: str) -> None:
        if self._active_href is not None and data:
            self._text_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or self._active_href is None:
            return
        text = " ".join(" ".join(self._text_parts).split())
        self.links.append((self._active_href, text))
        self._active_href = None
        self._text_parts = []


class _ImageLinkParser(HTMLParser):
    """Collect image candidates from current and legacy reader markup."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.urls: List[str] = []

    def _append(self, value: Optional[str]) -> None:
        value = str(value or "").strip()
        if not value:
            return
        # srcset may contain multiple candidates with width descriptors.
        for candidate in value.split(","):
            url = candidate.strip().split()[0] if candidate.strip() else ""
            if url:
                self.urls.append(url)

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() not in {"img", "source"}:
            return
        attr_map = {str(key).lower(): value for key, value in attrs if key}
        for name in ("src", "data-src", "data-original", "srcset", "data-srcset"):
            self._append(attr_map.get(name))


def _chapter_number(text: str) -> Optional[float]:
    normalized = " ".join((text or "").split())
    patterns = (
        r"(?:chapter|chap(?:ter)?|ch\.?|episode|ep\.?)\s*#?\s*([0-9]+(?:\.[0-9]+)?)",
        r"#\s*([0-9]+(?:\.[0-9]+)?)",
        r"([0-9]+(?:\.[0-9]+)?)",
    )
    for pattern in patterns:
        match = re.search(pattern, normalized, re.IGNORECASE)
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                pass
    return None


def _request(url: str, extra_headers: Optional[dict] = None) -> str:
    headers = dict(DEFAULT_HEADERS)
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def _post(url: str, data: dict, extra_headers: Optional[dict] = None) -> str:
    headers = dict(DEFAULT_HEADERS)
    headers["Content-Type"] = "application/x-www-form-urlencoded"
    headers["HX-Request"] = "true"
    if extra_headers:
        headers.update(extra_headers)
    encoded = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=encoded, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def search_manga(query: str) -> List[Dict[str, str]]:
    try:
        html = _post(f"{BASE_URL}/search/simple?location=main", {"text": query}, {"HX-Request": "true"})
    except Exception:
        return []

    results: List[Dict[str, str]] = []
    pairs = re.findall(
        r'href="(https://weebcentral\.com/series/([^/]+)/([^"]+))"[^>]*>.*?'
        r'<div[^>]*class="[^"]*flex-1[^"]*"[^>]*>\s*(.*?)\s*</div>',
        html,
        re.DOTALL,
    )
    for url, series_id, slug, raw_title in pairs:
        title = re.sub(r"<[^>]+>", "", raw_title).strip()
        if title:
            results.append({"title": title, "url": url, "series_id": series_id, "slug": slug})
    return results


def get_all_chapters(series_id: str) -> List[Tuple[float, str]]:
    url = f"{BASE_URL}/series/{series_id}/full-chapter-list"
    try:
        html = _request(url, {"HX-Request": "true"})
    except Exception as exc:
        raise MangaSourceError(f"Could not fetch the chapter list for {series_id}: {exc}") from exc

    parser = _ChapterLinkParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception as exc:
        raise MangaSourceError(f"Could not parse the chapter list for {series_id}: {exc}") from exc

    by_number: Dict[float, str] = {}
    for href, label in parser.links:
        number = _chapter_number(label)
        if number is None:
            continue
        chapter_url = urllib.parse.urljoin(BASE_URL + "/", href)
        by_number.setdefault(number, chapter_url)

    # Compatibility fallback for malformed fragments that HTMLParser could not close.
    if not by_number:
        for href, inner_html in re.findall(
            r'<a\b[^>]*href=["\']([^"\']*(?:/chapters/)[^"\']*)["\'][^>]*>(.*?)</a>',
            html,
            re.IGNORECASE | re.DOTALL,
        ):
            label = re.sub(r"<[^>]+>", " ", inner_html)
            number = _chapter_number(label)
            if number is not None:
                by_number.setdefault(number, urllib.parse.urljoin(BASE_URL + "/", href))

    if not by_number:
        content_hint = " ".join(re.sub(r"<[^>]+>", " ", html[:1000]).split())[:240]
        LOGGER.error(
            "WeebCentral returned a chapter-list response for %s, but no chapter links were recognised. Hint: %r",
            series_id,
            content_hint,
        )
        raise MangaSourceError(
            "WeebCentral returned a chapter list, but its layout was not recognised. "
            "The last known chapter cache has been retained."
        )

    return sorted(by_number.items(), key=lambda item: item[0])


def filter_chapters(chapters: List[Tuple[float, str]], chapter_range: str) -> List[Tuple[float, str]]:
    if chapter_range.strip().lower() == "all":
        return chapters
    chapter_map = {number: url for number, url in chapters}
    all_numbers = sorted(chapter_map)
    selected: set[float] = set()
    for part in chapter_range.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            low_text, high_text = part.split("-", 1)
            try:
                low, high = float(low_text), float(high_text)
            except ValueError:
                continue
            selected.update(number for number in all_numbers if low <= number <= high)
        else:
            try:
                requested = float(part)
            except ValueError:
                continue
            if not all_numbers:
                continue
            closest = min(all_numbers, key=lambda number: abs(number - requested))
            if abs(closest - requested) < 0.6:
                selected.add(closest)
    return [(number, chapter_map[number]) for number in sorted(selected)]


def get_chapter_images(chapter_url: str) -> List[str]:
    chapter_id = chapter_url.rstrip("/").split("/")[-1]
    endpoint = f"{BASE_URL}/chapters/{chapter_id}/images?is_prev=False&reading_style=long_strip"
    try:
        html = _request(endpoint, {"HX-Request": "true"})
    except Exception as exc:
        raise MangaSourceError(f"Could not fetch images for chapter {chapter_id}: {exc}") from exc

    parser = _ImageLinkParser()
    parser.feed(html)
    parser.close()

    images: List[str] = []
    seen: set[str] = set()
    for candidate in parser.urls:
        absolute = urllib.parse.urljoin(BASE_URL + "/", candidate)
        path = urllib.parse.urlparse(absolute).path.lower()
        if not any(path.endswith(ext) for ext in IMAGE_EXTS):
            continue
        if absolute not in seen:
            seen.add(absolute)
            images.append(absolute)

    if not images:
        # Keep support for image URLs with query strings or unusual attribute order.
        for candidate in re.findall(
            r'["\']((?:https?:)?//[^"\']+?\.(?:jpg|jpeg|png|webp)(?:\?[^"\']*)?)["\']',
            html,
            re.IGNORECASE,
        ):
            absolute = urllib.parse.urljoin(BASE_URL + "/", candidate)
            if absolute not in seen:
                seen.add(absolute)
                images.append(absolute)

    return images


def slugify(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return value or "manga"


def sanitize_filename(value: str) -> str:
    value = re.sub(r'[\\/:*?"<>|]+', "-", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:160] or "untitled"


def download_binary(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": DEFAULT_HEADERS["User-Agent"]})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def _image_to_rgb(image: Image.Image) -> Image.Image:
    if image.mode == "RGB":
        return image
    if image.mode in ("RGBA", "LA"):
        background = Image.new("RGB", image.size, (255, 255, 255))
        background.paste(image, mask=image.split()[-1])
        return background
    return image.convert("RGB")


def _images_to_pdf(image_paths: Sequence[Path], pdf_path: Path) -> None:
    converted: List[Image.Image] = []
    for path in image_paths:
        with Image.open(path) as image:
            converted.append(_image_to_rgb(image.copy()))
    if not converted:
        raise ValueError("No images found for PDF output")
    first, rest = converted[0], converted[1:]
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    first.save(pdf_path, format="PDF", save_all=True, append_images=rest, resolution=150.0)
    for image in converted:
        image.close()


def download_series(
    series: Dict[str, str],
    chapter_range: str,
    output_format: str,
    out_dir: Path,
    progress_cb=None,
    target_dir: Optional[Path] = None,
    chapter_numbers: Optional[Sequence[float]] = None,
) -> Dict[str, object]:
    if output_format.lower().strip() != "pdf":
        raise ValueError("This web app stores chapters as PDF only")

    series_title = series.get("title") or "Untitled"
    series_id = series.get("series_id") or ""
    series_slug = series.get("slug") or slugify(series_title)
    safe_title = sanitize_filename(series_title)

    all_chapters = get_all_chapters(series_id)
    if chapter_numbers is not None:
        wanted = {float(number) for number in chapter_numbers}
        chapters = [(number, url) for number, url in all_chapters if number in wanted]
    else:
        chapters = filter_chapters(all_chapters, chapter_range)
    if not chapters:
        raise RuntimeError("No chapters matched the selected range")

    base_dir = target_dir if target_dir is not None else out_dir / f"{safe_title} [{series_id}]"
    base_dir.mkdir(parents=True, exist_ok=True)
    temp_root = base_dir / ".mangadl-temp"
    temp_root.mkdir(parents=True, exist_ok=True)

    manifest: Dict[str, object] = {
        "source": "weebcentral",
        "series_id": series_id,
        "series_url": series.get("url"),
        "series_slug": series_slug,
        "title": series_title,
        "output_format": "pdf",
        "chapter_range": chapter_range,
        "chapters": [],
    }

    try:
        for chapter_index, (chapter_num, chapter_url) in enumerate(chapters, start=1):
            chapter_label = f"chapter-{chapter_num:g}"
            chapter_temp = temp_root / chapter_label
            shutil.rmtree(chapter_temp, ignore_errors=True)
            chapter_temp.mkdir(parents=True, exist_ok=True)
            image_urls = get_chapter_images(chapter_url)
            if not image_urls:
                raise RuntimeError(f"No page images found for chapter {chapter_num:g}")

            image_paths: List[Path] = []
            for image_index, image_url in enumerate(image_urls, start=1):
                suffix = Path(urllib.parse.urlparse(image_url).path).suffix.lower()
                if suffix not in IMAGE_EXTS:
                    suffix = ".jpg"
                image_path = chapter_temp / f"{image_index:04d}{suffix}"
                if progress_cb:
                    progress_cb(
                        {
                            "series_title": series_title,
                            "chapter_num": chapter_num,
                            "chapter_index": chapter_index,
                            "chapter_total": len(chapters),
                            "image_index": image_index,
                            "image_total": len(image_urls),
                            "message": f"Chapter {chapter_num:g}: page {image_index}/{len(image_urls)}",
                        }
                    )
                image_path.write_bytes(download_binary(image_url))
                image_paths.append(image_path)

            pdf_path = base_dir / f"{chapter_label}.pdf"
            temp_pdf = pdf_path.with_suffix(".pdf.part")
            temp_pdf.unlink(missing_ok=True)
            _images_to_pdf(image_paths, temp_pdf)
            temp_pdf.replace(pdf_path)
            shutil.rmtree(chapter_temp, ignore_errors=True)
            manifest["chapters"].append(
                {
                    "chapter": chapter_num,
                    "url": chapter_url,
                    "output": str(pdf_path),
                    "pages": len(image_paths),
                }
            )
    finally:
        if temp_root.exists() and not any(temp_root.iterdir()):
            temp_root.rmdir()

    (base_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest
