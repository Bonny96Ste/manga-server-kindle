#!/usr/bin/env python3
"""Build server and Kindle release ZIPs from the repository source tree."""

from __future__ import annotations

import hashlib
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
SERVER_VERSION = "1.0.2"
KINDLE_VERSION = "1.0.5"


def add_tree(archive: zipfile.ZipFile, source: Path, prefix: Path) -> None:
    for path in sorted(source.rglob("*")):
        relative_source = path.relative_to(source)
        if "__pycache__" in relative_source.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        relative = prefix / relative_source
        if path.is_dir():
            info = zipfile.ZipInfo(relative.as_posix().rstrip("/") + "/")
            info.external_attr = (stat.S_IFDIR | 0o755) << 16
            archive.writestr(info, b"")
            continue
        info = zipfile.ZipInfo.from_file(path, relative.as_posix())
        mode = 0o755 if path.suffix == ".sh" else 0o644
        info.external_attr = (stat.S_IFREG | mode) << 16
        with path.open("rb") as handle:
            archive.writestr(info, handle.read(), compress_type=zipfile.ZIP_DEFLATED)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_server() -> Path:
    output = DIST / f"mangabridge-server-v{SERVER_VERSION}.zip"
    with zipfile.ZipFile(output, "w") as archive:
        add_tree(archive, ROOT / "server", Path(f"mangabridge-server-v{SERVER_VERSION}"))
    return output


def build_kindle() -> Path:
    output = DIST / f"mangabridge-kindle-usb-v{KINDLE_VERSION}.zip"
    with tempfile.TemporaryDirectory() as temporary:
        stage = Path(temporary)
        extension = stage / "extensions" / "mangabridge"
        plugin = stage / "koreader" / "plugins" / "mangabridge.koplugin"
        data = stage / "mangabridge"

        shutil.copytree(ROOT / "kindle" / "extension", extension)
        shutil.copytree(ROOT / "kindle" / "plugin", plugin)
        shutil.copytree(ROOT / "kindle" / "plugin", extension / "payload" / "mangabridge.koplugin")
        data.mkdir(parents=True)
        (data / "cache").mkdir()
        (data / "library").mkdir()
        shutil.copy2(ROOT / "kindle" / "data" / "README.txt", data / "README.txt")
        shutil.copy2(ROOT / "kindle" / "data" / "config.example.lua", data / "config.example.lua")
        shutil.copy2(ROOT / "kindle" / "data" / "config.example.lua", data / "config.lua")

        with zipfile.ZipFile(output, "w") as archive:
            add_tree(archive, stage, Path(""))
    return output


def main() -> None:
    DIST.mkdir(exist_ok=True)
    outputs = [build_server(), build_kindle()]
    checksum_file = DIST / "SHA256SUMS.txt"
    checksum_file.write_text(
        "".join(f"{sha256(path)}  {path.name}\n" for path in outputs),
        encoding="utf-8",
    )
    for path in outputs:
        print(path.relative_to(ROOT))
    print(checksum_file.relative_to(ROOT))


if __name__ == "__main__":
    main()
