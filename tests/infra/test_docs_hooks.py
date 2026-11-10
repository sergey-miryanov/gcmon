from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest
from mkdocs.config.defaults import MkDocsConfig
from mkdocs.structure.files import File, Files, InclusionLevel
from mkdocs.structure.pages import Page

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / ".github" / "scripts" / "docs_hooks.py"
REPO_URL = "https://github.com/sergey-miryanov/gcmon"


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location("docs_hooks", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


docs_hooks = _load_module()


@pytest.fixture
def context() -> tuple[MkDocsConfig, Page, Files]:
    config = MkDocsConfig()
    assert isinstance(config, MkDocsConfig)
    config.repo_url = REPO_URL
    current = File("index.md", ".", "site", use_directory_urls=True)
    target = File(
        "docs/cli.md",
        ".",
        "site",
        use_directory_urls=True,
        inclusion=InclusionLevel.INCLUDED,
    )
    return config, Page(None, current, config), Files([target])


@pytest.mark.parametrize("html", ['<a id="example">Example</a>', '<a href="">Example</a>'])
def test_unchanged(html: str, context: tuple[MkDocsConfig, Page, Files]) -> None:
    config, page, files = context
    assert docs_hooks.on_page_content(html, page=page, config=config, files=files) == html


def test_link_rewrite(context: tuple[MkDocsConfig, Page, Files]) -> None:
    config, page, files = context
    html = f'<a href="{REPO_URL}/blob/main/docs/cli.md" download title="A &amp; B">CLI</a>'
    result = docs_hooks.on_page_content(html, page=page, config=config, files=files)
    assert result == '<a href="docs/cli/" download title="A &amp; B">CLI</a>'


def test_missing_repo_url(context: tuple[MkDocsConfig, Page, Files]) -> None:
    config, page, files = context
    config.repo_url = None
    html = f'<a href="{REPO_URL}/blob/main/docs/cli.md">CLI</a>'
    assert docs_hooks.on_page_content(html, page=page, config=config, files=files) == html
