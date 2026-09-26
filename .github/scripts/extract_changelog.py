#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
CHANGELOG_PATH = ROOT / "CHANGELOG.md"
PYPROJECT_PATH = ROOT / "pyproject.toml"

VERSION_HEADER_RE = re.compile(r"^## Version (?P<version>\S+)", re.MULTILINE)


def resolve_version(tag: str | None) -> str:
    if tag and tag.startswith("v"):
        return tag[1:]
    elif tag == "latest":
        with PYPROJECT_PATH.open("rb") as f:
            version: str = tomllib.load(f)["tool"]["poetry"]["version"]
        return version
    return None


def extract(version: str) -> str:
    text = CHANGELOG_PATH.read_text(encoding="utf-8")
    if version:
        pattern = rf"## Version {re.escape(version)}(?=\s|$).*?\n(.*?)(?=\n## |\Z)"
    else:
        pattern = r"## WIP(?=\s|$).*?\n(.*?)(?=\n## |\Z)"
    match = re.search(pattern, text, re.DOTALL)
    if match:
        return match.group(1).strip()
    headers = VERSION_HEADER_RE.findall(text)
    print(f"::error::No changelog section for version {version!r}.", file=sys.stderr)
    print(f"::error::Found headers: {headers}", file=sys.stderr)
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract a version's section from CHANGELOG.md")
    parser.add_argument(
        "tag",
        nargs="?",
        help="Release tag (e.g. v0.1.0); default = pyproject version",
    )
    args = parser.parse_args()
    version = resolve_version(args.tag)
    body = extract(version)
    if not body:
        return 1
    print(body)
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as f:
            f.write(f"body<<EOF\n{body}\nEOF\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
