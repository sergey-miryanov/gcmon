from __future__ import annotations

import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import override
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parents[2]


class PageLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.github_doc_links: list[str] = []

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            href = dict(attrs).get("href")
            if (
                href
                and href.startswith("https://github.com/sergey-miryanov/gcmon/blob/main/")
                and urlsplit(href).path.endswith(".md")
            ):
                self.github_doc_links.append(href)


def test_documentation_build(tmp_path: Path) -> None:
    site = tmp_path / "site"
    result = subprocess.run(
        [sys.executable, "-m", "mkdocs", "build", "--strict", "--site-dir", str(site)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (site / "LICENSE").read_bytes() == (REPO_ROOT / "LICENSE").read_bytes()
    assert not [p for p in site.rglob("*") if p.suffix == ".py"]
    parser = PageLinks()
    parser.feed((site / "index.html").read_text(encoding="utf-8"))
    parser.close()
    assert not parser.github_doc_links, parser.github_doc_links
