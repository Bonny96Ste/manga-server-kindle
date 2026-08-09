#!/usr/bin/env python3
from __future__ import annotations

import ast
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEBAPP = ROOT / "webapp"
sys.path.insert(0, str(WEBAPP))

from accounts import AccountStore


def main() -> None:
    for source in WEBAPP.glob("*.py"):
        ast.parse(source.read_text(encoding="utf-8"), filename=str(source))

    try:
        from jinja2 import Environment
    except ImportError:
        print("Jinja is unavailable; skipped template parsing")
    else:
        environment = Environment()
        for template in (WEBAPP / "templates").glob("*.html"):
            environment.parse(template.read_text(encoding="utf-8"))

    app_source = (WEBAPP / "app.py").read_text(encoding="utf-8")
    endpoints = set(re.findall(r"^def\s+(\w+)\(", app_source, re.MULTILINE))
    for template in (WEBAPP / "templates").glob("*.html"):
        for endpoint in re.findall(r"url_for\(['\"]([^'\"]+)", template.read_text(encoding="utf-8")):
            if endpoint != "static" and endpoint not in endpoints:
                raise RuntimeError(f"{template.name} references missing endpoint {endpoint}")

    with tempfile.TemporaryDirectory() as directory:
        store = AccountStore(Path(directory) / "accounts.sqlite3")
        first = store.create_profile("reader-one", "Reader One", admin=True)
        second = store.create_profile("reader-two", "Reader Two", "password")
        store.add_series(first.id, "Example [123]")
        store.add_series(second.id, "Example [123]")
        store.update_progress(first.id, "Example [123]", 1.0, "complete", 10)
        space = store.create_space(first.id, "Test shelf")
        store.invite_to_space(space, second.id, first.id)
        store.respond_invite(space, second.id, True)
        assert store.series_user_count("Example [123]") == 2
        assert store.shared_series(space, first.id)
        assert store.progress_for_series(first.id, "Example [123]")["last_page"] == 10

    print("MangaBridge v2 validation passed")


if __name__ == "__main__":
    main()
