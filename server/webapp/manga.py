from __future__ import annotations

import json
import re
import shutil
import urllib.parse
import urllib.request
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
    try:
        html = _request(f"{BASE_URL}/series/{series_id}/full-chapter-list", {"HX-Request": "true"})
    except Exception:
        return []

    pairs = re.findall(
        r'href="(https://weebcentral\.com/chapters/[^"]+)"[^>]*>.*?'
        r'<span class="">\s*([^<]+?)\s*</span>',
        html,
        re.DOTALL,
    )
    result: List[Tuple[float, str]] = []
    for chapter_url, title_text in pairs:
        num_match = re.search(r"([\d]+(?:\.\d+)?)\s*$", title_text.strip())
        if not num_match:
            continue
        try:
            result.append((float(num_match.group(1)), chapter_url))
        except ValueError:
            continue
    result.sort(key=lambda item: item[0])
    return result


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
    try:
        html = _request(
            f"{BASE_URL}/chapters/{chapter_id}/images?is_prev=False&reading_style=long_strip",
            {"HX-Request": "true"},
        )
    except Exception:
        return []
    images = re.findall(
        r'<img\s[^>]*src="(https://[^"]+\.(?:jpg|png|webp|jpeg)[^"]*)"',
        html,
        re.IGNORECASE,
    )
    return list(dict.fromkeys(images))


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
