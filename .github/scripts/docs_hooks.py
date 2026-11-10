from html import escape
from html.parser import HTMLParser
from typing import override
from urllib.parse import unquote, urlsplit, urlunsplit

from mkdocs.config.defaults import MkDocsConfig
from mkdocs.structure.files import File, Files, InclusionLevel
from mkdocs.structure.pages import Page
from mkdocs.utils import get_relative_url
from pymdownx.slugs import slugify  # type: ignore[import-untyped]


def on_config(config: MkDocsConfig) -> MkDocsConfig:
    # Use GitHub-compatible heading anchors.
    config.mdx_configs.setdefault("toc", {})["slugify"] = slugify(case="lower")
    return config


def on_files(files: Files, *, config: MkDocsConfig) -> Files:
    # add theme assets
    for file in files:
        if file.src_dir in config.theme.dirs:
            file.inclusion = InclusionLevel.INCLUDED
    # LICENSE will be filtered out, so we add it back.
    files.append(File("LICENSE", config.docs_dir, config.site_dir, config.use_directory_urls))
    return files


# Rewrite doc links within this repo to local links.
class Parser(HTMLParser):
    def __init__(self, html: str, repo_url: str, page: Page, files: Files) -> None:
        super().__init__(convert_charrefs=False)
        self.prefix = repo_url.rstrip("/") + "/blob/main/"
        self.page = page
        self.files = files
        self.offsets = [0]
        for line in html.split("\n"):
            self.offsets.append(self.offsets[-1] + len(line) + 1)
        self.replacements: list[tuple[int, int, str]] = []

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        href = dict(attrs).get("href")
        if not href:
            return
        replacement = self.to_local_link(href)
        if replacement == href:
            return
        attributes = []
        for name, value in attrs:
            if value is None:
                attributes.append(f" {name}")
                continue
            if name == "href":
                value = replacement
            escaped_value = escape(value, quote=True)
            attributes.append(f' {name}="{escaped_value}"')
        attributes_str = "".join(attributes)
        line, column = self.getpos()
        start = self.offsets[line - 1] + column
        original_text = self.get_starttag_text()
        assert original_text is not None
        end = start + len(original_text)
        self.replacements.append((start, end, f"<a{attributes_str}>"))

    def to_local_link(self, href: str) -> str:
        if not href.startswith(self.prefix):
            return href
        parts = urlsplit(href[len(self.prefix) :])
        target = self.files.get_file_from_path(unquote(parts.path))
        if target and target.is_documentation_page() and target.inclusion == InclusionLevel.INCLUDED:
            relative = get_relative_url(target.url, self.page.url)
            return urlunsplit(("", "", relative, parts.query, parts.fragment))
        else:
            return href


def on_page_content(html: str, *, page: Page, config: MkDocsConfig, files: Files) -> str:
    if not config.repo_url:
        return html
    parser = Parser(html, config.repo_url, page, files)
    parser.feed(html)
    parser.close()
    for start, end, replacement in reversed(parser.replacements):
        html = html[:start] + replacement + html[end:]
    return html
