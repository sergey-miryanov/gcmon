from __future__ import annotations

import importlib.util
import textwrap
from pathlib import Path
from typing import Any

import pytest

from gcmon.support.vocabulary import ENCODING

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / ".github" / "scripts" / "extract_changelog.py"


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location("extract_changelog", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


extract_changelog = _load_module()


@pytest.fixture
def fake_changelog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    p = tmp_path / "CHANGELOG.md"
    p.write_text(
        textwrap.dedent("""\
            # Changelog

            ## WIP
            - upcoming stuff

            ## Version 0.2.0
            - new feature
            - bug fix

            ## Version 0.1.0 (2026-05-22)
            - initial release
        """),
        encoding=ENCODING,
    )
    monkeypatch.setattr(extract_changelog, "CHANGELOG_PATH", p)
    return p


@pytest.fixture
def fake_pyproject(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    p = tmp_path / "pyproject.toml"
    p.write_bytes(b'[tool.poetry]\nversion = "0.2.0"\n')
    monkeypatch.setattr(extract_changelog, "PYPROJECT_PATH", p)
    return p


class TestResolveVersion:
    def test_strips_v_prefix(self) -> None:
        assert extract_changelog.resolve_version("v0.2.0") == "0.2.0"

    def test_keeps_pep440_suffix(self) -> None:
        assert extract_changelog.resolve_version("v0.2.0a1") == "0.2.0a1"

    def test_falls_back_to_pyproject_when_tag_missing(self, fake_pyproject: Path) -> None:
        assert extract_changelog.resolve_version(None) == "0.2.0"

    def test_falls_back_to_pyproject_when_tag_lacks_v_prefix(self, fake_pyproject: Path) -> None:
        assert extract_changelog.resolve_version("refs/heads/main") == "0.2.0"


class TestExtract:
    def test_returns_section_body(self, fake_changelog: Path) -> None:
        assert extract_changelog.extract("0.2.0") == "- new feature\n- bug fix"

    def test_handles_version_with_date_suffix(self, fake_changelog: Path) -> None:
        assert extract_changelog.extract("0.1.0") == "- initial release"

    def test_returns_empty_on_miss(self, fake_changelog: Path, capsys: pytest.CaptureFixture[str]) -> None:
        result = extract_changelog.extract("9.9.9")

        assert result == ""
        captured = capsys.readouterr()
        assert "No changelog section" in captured.err
        assert "0.2.0" in captured.err
        assert "0.1.0" in captured.err

    def test_word_boundary_prevents_false_match(self, fake_changelog: Path) -> None:
        result = extract_changelog.extract("0.2")

        assert result == ""


class TestMain:
    def test_prints_body_and_exits_zero(
        self,
        fake_changelog: Path,
        fake_pyproject: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("sys.argv", ["extract_changelog.py"])
        monkeypatch.delenv("GITHUB_OUTPUT", raising=False)

        rc = extract_changelog.main()

        assert rc == 0
        assert capsys.readouterr().out.strip() == "- new feature\n- bug fix"

    def test_uses_tag_when_provided(
        self,
        fake_changelog: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("sys.argv", ["extract_changelog.py", "v0.1.0"])
        monkeypatch.delenv("GITHUB_OUTPUT", raising=False)

        rc = extract_changelog.main()

        assert rc == 0
        assert capsys.readouterr().out.strip() == "- initial release"

    def test_exits_nonzero_on_missing_version(
        self,
        fake_changelog: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("sys.argv", ["extract_changelog.py", "v9.9.9"])
        monkeypatch.delenv("GITHUB_OUTPUT", raising=False)

        rc = extract_changelog.main()

        assert rc == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "No changelog section" in captured.err


class TestMainWritesToGitHubOutput:
    def test_writes_heredoc_when_github_output_set(
        self,
        fake_changelog: Path,
        fake_pyproject: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        out_file = tmp_path / "gh_output"
        out_file.write_text("", encoding=ENCODING)
        monkeypatch.setenv("GITHUB_OUTPUT", str(out_file))
        monkeypatch.setattr("sys.argv", ["extract_changelog.py"])

        rc = extract_changelog.main()

        assert rc == 0
        content = out_file.read_text(encoding=ENCODING)
        assert content.startswith("body<<EOF\n")
        assert content.endswith("EOF\n")
        assert "- new feature" in content
        assert "- bug fix" in content

    def test_appends_when_output_file_already_has_content(
        self,
        fake_changelog: Path,
        fake_pyproject: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        out_file = tmp_path / "gh_output"
        out_file.write_text("preface=true\n", encoding=ENCODING)
        monkeypatch.setenv("GITHUB_OUTPUT", str(out_file))
        monkeypatch.setattr("sys.argv", ["extract_changelog.py"])

        rc = extract_changelog.main()

        assert rc == 0
        content = out_file.read_text(encoding=ENCODING)
        assert content.startswith("preface=true\n")
        assert "body<<EOF\n" in content

    def test_prints_body_to_stdout_even_when_writing_to_file(
        self,
        fake_changelog: Path,
        fake_pyproject: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        out_file = tmp_path / "gh_output"
        out_file.write_text("", encoding=ENCODING)
        monkeypatch.setenv("GITHUB_OUTPUT", str(out_file))
        monkeypatch.setattr("sys.argv", ["extract_changelog.py"])

        rc = extract_changelog.main()

        assert rc == 0
        captured = capsys.readouterr()
        assert captured.out.strip() == "- new feature\n- bug fix"
        assert "body<<EOF" in out_file.read_text(encoding=ENCODING)

    def test_does_not_write_output_file_on_missing_version(
        self,
        fake_changelog: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        out_file = tmp_path / "gh_output"
        out_file.write_text("", encoding=ENCODING)
        monkeypatch.setenv("GITHUB_OUTPUT", str(out_file))
        monkeypatch.setattr("sys.argv", ["extract_changelog.py", "v9.9.9"])

        extract_changelog.main()

        assert out_file.read_text(encoding=ENCODING) == ""
