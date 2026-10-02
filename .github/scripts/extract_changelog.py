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

HEADER_RE = re.compile(r"^## (?:Version )?(?P<version>\S+)", re.MULTILINE)
TAG_RE = re.compile(r"v?(?P<version>\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?(?:\.post\d+)?(?:\.dev\d+)?)")


def resolve_version(tag: str | None) -> str | None:
    if not tag:
        return "WIP"
    if tag == "latest":
        with PYPROJECT_PATH.open("rb") as f:
            version: str = tomllib.load(f)["tool"]["poetry"]["version"]
        return version
    match = TAG_RE.fullmatch(tag)
    return match.group("version") if match else None


def report_error(message: str, text: str) -> None:
    print(f"::error::{message}", file=sys.stderr)
    print(f"::error::Found headers: {HEADER_RE.findall(text)}", file=sys.stderr)


def extract(version: str) -> str:
    text = CHANGELOG_PATH.read_text(encoding="utf-8")
    header = "WIP" if version == "WIP" else f"Version {version}"
    pattern = rf"## {re.escape(header)}(?=\s|$).*?\n(.*?)(?=\n## |\Z)"
    match = re.search(pattern, text, re.DOTALL)
    if not match:
        report_error(f"No changelog section for version {version!r}.", text)
        return ""
    body = match.group(1).strip()
    if not body:
        report_error(f"The changelog section for version {version!r} is empty.", text)
    return body


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract a version's section from CHANGELOG.md")
    parser.add_argument(
        "tag",
        nargs="?",
        help="Release tag (e.g. v0.1.0), or 'latest' for the pyproject version; default = the WIP section",
    )
    args = parser.parse_args()
    version = resolve_version(args.tag)
    if version is None:
        text = CHANGELOG_PATH.read_text(encoding="utf-8")
        report_error(f"Tag {args.tag!r} is not a version tag like v0.1.0.", text)
        return 1
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
