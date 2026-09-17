"""The ADR headers, the packages they name, and the index must agree.

An anchor that names a module rather than a file survives a rename inside it,
but nothing stops the module itself from moving. These assertions are what
notices.
"""

from __future__ import annotations

import re
from pathlib import Path

from gcmon.support.vocabulary import ENCODING, PROGRAM_NAME

REPO_ROOT = Path(__file__).resolve().parents[2]
ADR_DIR = REPO_ROOT / "docs" / "adr"
SRC = REPO_ROOT / "src" / PROGRAM_NAME

TEMPLATE = "0000-template.md"
TESTS = "tests"

RECORDS = sorted(p for p in ADR_DIR.glob("0*.md") if p.name != TEMPLATE)
INDEX_ROW = re.compile(r"^\| \[(\d{4})\]\([^)]*\) \| .* \| .* \| (.*?) \|$", re.M)
FIELD = re.compile(r"^- \*\*Modules:\*\* (.+)$", re.M)
LINK = re.compile(r"^- \*\*(Amended by|Supersedes|Status):\*\*(.*?)(?=^- |\n\n)", re.M | re.S)


def modules_of(path: Path) -> list[str]:
    m = FIELD.search(path.read_text(encoding=ENCODING))
    assert m is not None, f"{path.name} has no Modules field"
    return [part.strip() for part in m.group(1).split(",")]


def header_links(path: Path) -> list[str]:
    """Every record a header field of *path* links to."""
    text = path.read_text(encoding=ENCODING)
    return [
        target for field in LINK.finditer(text) for target in re.findall(r"\]\((\d{4}-[^)]+\.md)\)", field.group(2))
    ]


def index_modules() -> dict[str, str]:
    text = (ADR_DIR / "README.md").read_text(encoding=ENCODING)
    return {m.group(1): m.group(2).strip() for m in INDEX_ROW.finditer(text)}


class TestThereIsSomethingToCheck:
    """Every test below walks the records or the index, and a walk over
    nothing passes."""

    def test_the_glob_finds_the_records(self) -> None:
        assert RECORDS

    def test_the_index_has_rows(self) -> None:
        assert index_modules()

    def test_a_header_links_to_another_record(self) -> None:
        assert [target for path in RECORDS for target in header_links(path)]


class TestEveryRecordNamesItsModules:
    def test_the_field_is_present(self) -> None:
        for path in RECORDS:
            assert modules_of(path), f"{path.name} names no module"

    def test_each_named_module_exists(self) -> None:
        for path in RECORDS:
            for name in modules_of(path):
                if name == TESTS:
                    continue
                package = SRC / name
                assert package.is_dir(), f"{path.name} names {name}, which is gone"
                assert (package / "__init__.py").exists(), f"{path.name} names {name}, which is not a package"

    def test_the_names_are_sorted_and_unique(self) -> None:
        for path in RECORDS:
            names = modules_of(path)
            assert names == sorted(set(names)), f"{path.name}: {names}"


class TestTheIndexAgreesWithTheHeaders:
    """The header is the source of truth; the index table reads from it."""

    def test_every_record_has_a_row(self) -> None:
        assert set(index_modules()) == {p.name[:4] for p in RECORDS}

    def test_each_row_matches_its_record(self) -> None:
        rows = index_modules()
        for path in RECORDS:
            assert rows[path.name[:4]] == ", ".join(modules_of(path)), path.name


class TestEveryRecordLinkResolves:
    def test_header_links_point_at_a_record(self) -> None:
        for path in RECORDS:
            for target in header_links(path):
                assert (ADR_DIR / target).exists(), f"{path.name} -> {target}"
