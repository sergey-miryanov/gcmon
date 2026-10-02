#!/usr/bin/env python3
from __future__ import annotations

import argparse
import itertools
import os
import sys
import tomllib
from collections import Counter
from pathlib import Path

from markdown_it import MarkdownIt
from packaging.version import InvalidVersion, Version

ROOT = Path(__file__).resolve().parent.parent.parent
CHANGELOG_PATH = ROOT / "CHANGELOG.md"
PYPROJECT_PATH = ROOT / "pyproject.toml"


def resolve_version(tag: str | None) -> str | None:
    if not tag:
        return "WIP"
    if tag == "latest":
        with PYPROJECT_PATH.open("rb") as f:
            version: str = tomllib.load(f)["tool"]["poetry"]["version"]
        return version
    text = tag.removeprefix("v")
    try:
        parsed = Version(text)
    except InvalidVersion:
        return None
    # Version also takes `0.6`, `0.6.0-rc1`, `1!0.6.0` and `0.6.0+local`. A tag
    # names a three-part release spelled the way its changelog heading is.
    if str(parsed) != text or len(parsed.release) != 3 or parsed.epoch or parsed.local:
        return None
    return text


def _version(title: str) -> str | None:
    """The version a `##` heading names, WIP included; None for any other heading."""
    words = title.split()
    if words[:1] == ["WIP"]:
        return "WIP"
    if words[:1] == ["Version"] and len(words) > 1:
        return words[1]
    return None


def sections(text: str) -> list[tuple[str, str]]:
    """Each version section as (version or WIP, body as written), in file order.

    `## Version 0.1.0 (2026-05-22)` is keyed `0.1.0`. Any other top-level `##`
    heading ends the section above it and opens none.
    """
    lines = text.split("\n")
    tokens = MarkdownIt().parse(text)
    heads = [
        (opening.map, title.content)
        for opening, title in itertools.pairwise(tokens)
        if opening.type == "heading_open" and opening.tag == "h2" and opening.level == 0 and opening.map
    ]
    ends = [span[0] for span, _ in heads[1:]] + [len(lines)]
    return [
        (version, "\n".join(lines[span[1] : end]).strip())
        for (span, title), end in zip(heads, ends, strict=True)
        if (version := _version(title)) is not None
    ]


def report_error(message: str, keys: list[str]) -> None:
    print(f"::error::{message}", file=sys.stderr)
    print(f"::error::Found headers: {keys}", file=sys.stderr)


def extract(version: str) -> str:
    found = sections(CHANGELOG_PATH.read_text(encoding="utf-8"))
    keys = [key for key, _ in found]
    if repeated := sorted(key for key, count in Counter(keys).items() if count > 1):
        report_error(f"More than one changelog section for {repeated}.", keys)
        return ""
    body = dict(found).get(version)
    if body is None:
        report_error(f"No changelog section for version {version!r}.", keys)
        return ""
    if not body:
        report_error(f"The changelog section for version {version!r} is empty.", keys)
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
        keys = [key for key, _ in sections(CHANGELOG_PATH.read_text(encoding="utf-8"))]
        report_error(f"Tag {args.tag!r} is not a version tag like v0.1.0.", keys)
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
