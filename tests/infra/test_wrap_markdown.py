from __future__ import annotations

import importlib.util
import textwrap
from pathlib import Path
from typing import Any

import pytest

from gcmon.support.vocabulary import ENCODING

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / ".github" / "scripts" / "wrap_markdown.py"


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location("wrap_markdown", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


wrap_markdown = _load_module()


class TestAnOrdinalOpeningALine:
    """A spec citing `0029.` mid-sentence is prose, not a list item."""

    PARAGRAPH = textwrap.dedent("""\
        **4.6: `StdoutExporter` keeps its `_open_writer` override.** Carried from
        0029. Three lines, plus a `close()` that flushes the stream after the base
        drains it, and that is the whole of what differs between a file and an
        already-open stream.
    """)

    def test_the_sentence_stays_one_paragraph(self) -> None:
        """No line is indented under the ordinal, which is what a list item
        would have done to the rest of the sentence.
        """
        result = wrap_markdown.rewrap(self.PARAGRAPH, 78)

        assert not [line for line in result.split("\n") if line.startswith(" ")]

    def test_rewrapping_twice_changes_nothing(self) -> None:
        """The defect reported "already wrapped" on the second run, having
        indented the paragraph on the first.
        """
        once = wrap_markdown.rewrap(self.PARAGRAPH, 78)

        assert wrap_markdown.rewrap(once, 78) == once


class TestAWordThatCanOpenABlock:
    """Wrapped to the start of a line, each of these would turn the rest of
    the paragraph into another block."""

    @pytest.mark.parametrize("word", ["|", ">", "<div>", "#", "-", "+", "*", "1.", "1)", "```", "~~~"])
    def test_stays_on_the_line_before(self, word: str) -> None:
        source = f"Some words here {word} and the rest of the sentence.\n"

        result = wrap_markdown.rewrap(source, 16)

        assert not [line for line in result.split("\n") if line.startswith(word)]
        assert wrap_markdown._parts(result) == wrap_markdown._parts(source)

    @pytest.mark.parametrize("word", ["-", "---", "==", "***"])
    def test_an_underline_ending_the_paragraph_is_never_alone(self, word: str) -> None:
        """Alone on the last line it would turn the paragraph into a heading."""
        source = f"Some words here {word}\n"

        result = wrap_markdown.rewrap(source, 16)

        assert word not in result.split("\n")
        assert wrap_markdown._parts(result) == wrap_markdown._parts(source)

    @pytest.mark.parametrize("word", ["---", "==", "***"])
    def test_an_underline_a_long_word_would_leave_alone_is_never_alone(self, word: str) -> None:
        """The word after it does not fit beside it, so the probe that puts
        more text after it on the line is not enough.
        """
        source = f"Some words here {word} averyveryverylongword more\n"

        result = wrap_markdown.rewrap(source, 16)

        assert word not in result.split("\n")
        assert wrap_markdown._parts(result) == wrap_markdown._parts(source)

    def test_the_word_before_moves_down_with_it(self) -> None:
        """Held to the word before, `1.` takes that word to the next line rather
        than overflow the one it would have ended.
        """
        source = "One two three four 1. five\n"

        assert wrap_markdown.rewrap(source, 20) == "One two three\nfour 1. five\n"

    @pytest.mark.parametrize("word", ["<files>", "2.", "0029."])
    def test_a_word_that_opens_nothing_still_breaks(self, word: str) -> None:
        """None of these can interrupt a paragraph, so holding one would only
        pull the word before it down a line.
        """
        source = f"aaaa bbbb cccc {word} dd\n"

        assert wrap_markdown.rewrap(source, 14) == f"aaaa bbbb cccc\n{word} dd\n"


class TestAListStillWrapsAsAList:
    def test_a_tight_numbered_list_keeps_its_items(self) -> None:
        """Item 2 follows item 1 with no blank line between them, so it is read
        mid-block and must still open an item of its own.
        """
        source = textwrap.dedent("""\
            1. The first item.
            2. The second item.
            3. The third item.
        """)

        assert wrap_markdown.rewrap(source, 78) == source

    def test_a_continuation_line_belongs_to_its_item(self) -> None:
        """The carried-over text stays indented under item 1, and item 2 keeps
        its own marker at the margin.
        """
        source = textwrap.dedent("""\
            1. An item whose text runs on well past one line of its own, far
               enough that the wrapper has to carry some of it over and indent
               what it carried.
            2. The next item.
        """)

        result = wrap_markdown.rewrap(source, 78).split("\n")

        assert result[0].startswith("1. ")
        assert result[1].startswith("   ")
        assert "2. The next item." in result

    def test_a_list_may_start_at_a_number_other_than_one(self) -> None:
        """After a blank line there is no paragraph to interrupt, so the
        CommonMark restriction does not apply.
        """
        source = textwrap.dedent("""\
            Some prose.

            7. An item numbered seven.
            8. And eight.
        """)

        assert wrap_markdown.rewrap(source, 78) == source

    def test_a_bullet_still_interrupts_a_paragraph(self) -> None:
        """CommonMark allows this where it forbids the ordered marker, so the
        fix must not close both.
        """
        source = textwrap.dedent("""\
            Some prose that runs straight into a list.
            - The first bullet.
            - The second bullet.
        """)

        result = wrap_markdown.rewrap(source, 78)

        assert "- The first bullet." in result.split("\n")

    def test_one_may_interrupt_a_paragraph(self) -> None:
        source = "Some prose that runs straight into a list.\n1. The first item.\n"

        assert "1. The first item." in wrap_markdown.rewrap(source, 78).split("\n")


class TestADefinitionListKeepsItsLabels:
    """A label alone on its line stays there, whatever it is called."""

    def test_a_two_word_label_stays_on_its_own_line(self) -> None:
        """The defect asked how many words the line held rather than whether
        the label was the whole of it, so `**Process track**:` was pulled down
        onto the definition under it and `**Track**:` was not.
        """
        source = textwrap.dedent("""\
            **Process track**:
            A process's own row, and what its other rows hang under.
        """)

        assert wrap_markdown.rewrap(source, 78) == source

    def test_a_one_word_label_still_stays_on_its_own_line(self) -> None:
        source = textwrap.dedent("""\
            **Track**:
            One row in a trace.
        """)

        assert wrap_markdown.rewrap(source, 78) == source

    def test_a_label_with_its_text_beside_it_keeps_the_text(self) -> None:
        """Only a line that is nothing but the label is held apart. One
        carrying the definition too wraps as the paragraph it is.
        """
        source = "**Intern id**: The number a packet writes in place of a string.\n"

        assert wrap_markdown.rewrap(source, 78) == source

    def test_a_label_still_breaks_the_paragraph_before_it(self) -> None:
        """The break before a label is the other half of the rule, and it has
        to survive a label that is now flushed by a different branch.
        """
        source = textwrap.dedent("""\
            Some prose ending here.
            **Track**:
            One row in a trace.
        """)

        assert wrap_markdown.rewrap(source, 78) == source

    def test_a_label_ending_its_paragraph_stays_on_its_own_line(self) -> None:
        """A blank line under the label leaves nothing after it to wrap."""
        source = textwrap.dedent("""\
            Some prose ending here.
            **Track**:

            One row in a trace.
        """)

        assert wrap_markdown.rewrap(source, 78) == source


class TestALinkReferenceDefinitionStaysOnItsLine:
    """`[label]: url` is a definition only while nothing follows the URL."""

    def test_a_block_of_definitions_is_left_alone(self) -> None:
        """The defect wrapped them as one paragraph, so a short one and the
        next label landed on a line together and neither resolved.
        """
        source = textwrap.dedent("""\
            Some prose citing [wait(2)][linux-wait] and [pidfd_open(2)][linux-pidfd].

            [linux-wait]: https://man7.org/linux/man-pages/man2/wait.2.html
            [linux-pidfd]: https://man7.org/linux/man-pages/man2/pidfd_open.2.html
        """)

        assert wrap_markdown.rewrap(source, 78) == source

    def test_no_line_carries_two_definitions(self) -> None:
        """What the broken file looked like: one label, its URL, and the next
        label after it, which makes the first invalid too.
        """
        source = textwrap.dedent("""\
            [a]: https://example.com/one
            [b]: https://example.com/two
        """)

        result = wrap_markdown.rewrap(source, 78).split("\n")

        assert not [line for line in result if line.count("]:") > 1]

    def test_a_reference_style_link_in_prose_still_wraps(self) -> None:
        """Only the definition is held. A paragraph using one is prose, and a
        rule that caught both would stop the page wrapping at all.
        """
        source = "Prose citing [wait(2)][linux-wait] over and over, " * 4

        assert max(len(line) for line in wrap_markdown.rewrap(source, 78).split("\n")) <= 78

    def test_a_definition_cannot_interrupt_a_paragraph(self) -> None:
        """CommonMark reads it as part of the paragraph, so the tool does too."""
        source = "Some prose that runs straight on.\n[a]: https://example.com/one\n"

        assert wrap_markdown.rewrap(source, 78) == "Some prose that runs straight on. [a]: https://example.com/one\n"

    def test_a_definition_with_text_after_it_stays_on_its_line(self) -> None:
        """The text makes it a paragraph. Broken after the label or the URL, it
        would become a real definition and a paragraph under it.
        """
        source = f"[a]: https://example.com/{'a' * 60} is here\n"

        assert wrap_markdown.rewrap(source, 78) == source

    def test_footnotes_stay_one_to_a_line(self) -> None:
        """CommonMark has no footnotes, so it reads these as one paragraph,
        and joined, the first would swallow the second on GitHub.
        """
        source = "[^1]: The first footnote.\n[^2]: The second footnote.\n"

        assert wrap_markdown.rewrap(source, 78) == source


LONG = "word " * 30
"""One paragraph line well past any width a test here wraps at."""


class TestWhatIsNotProse:
    def test_a_fenced_block_is_copied_through(self) -> None:
        text = f"```\n{LONG}\n```"

        assert wrap_markdown.rewrap(text, 40) == text

    def test_a_quote_keeps_its_marker_on_every_line(self) -> None:
        wrapped = wrap_markdown.rewrap(f"> {LONG}".rstrip(), 40)

        assert len(wrapped.split("\n")) > 1
        assert all(line.startswith("> ") for line in wrapped.split("\n"))

    def test_a_table_markdown_it_rejects_keeps_its_rows(self) -> None:
        """The delimiter row has a column fewer than the header, so this is a
        paragraph, and joined, the author loses the layout of the typo."""
        source = "| a | b | c |\n|---|---|\n| 1 | 2 | 3 |\n"

        assert wrap_markdown.rewrap(source, 78) == source


class TestALineBreakTheAuthorWrote:
    @pytest.mark.parametrize("end", ["\\", "  "])
    def test_a_hard_break_stays_at_the_end_of_its_line(self, end: str) -> None:
        source = f"{LONG.rstrip()}{end}\nThe next line.\n"

        lines = wrap_markdown.rewrap(source, 40).split("\n")

        assert "The next line." in lines
        assert lines[lines.index("The next line.") - 1].endswith(f"word{end}")

    def test_a_rewrap_that_loses_a_hard_break_is_not_written(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The words and the blocks are unchanged, so only the inline
        structure can tell.
        """
        text = "Some words  \nhere\n"
        path = _page(tmp_path, text)
        monkeypatch.setattr(wrap_markdown, "rewrap", lambda source, width: "Some words here\n")

        assert not wrap_markdown.process(path, 40, check=False)
        assert path.read_bytes() == text.encode(ENCODING)


class TestSpacesTheAuthorWrote:
    def test_two_spaces_between_sentences_survive(self) -> None:
        source = "End of one.  Start of two.\n"

        assert wrap_markdown.rewrap(source, 78) == source

    def test_spaces_inside_a_code_span_too_long_to_hold_survive(self) -> None:
        source = "Run `python  -m gcmon --format json --output somewhere/long/path.json` now\n"

        assert "`python  -m" in wrap_markdown.rewrap(source, 40)


class TestAUnicodeSpaceMarkdownItStrips:
    """markdown-it strips these off a paragraph's ends; CommonMark keeps them."""

    @pytest.mark.parametrize(
        "source",
        [
            "Intro.\n\n\N{NO-BREAK SPACE}\n\nMore.\n",
            f"\N{NO-BREAK SPACE}{LONG.rstrip()}\n",
            f"\N{IDEOGRAPHIC SPACE}{LONG.rstrip()}\n",
            f"\N{NO-BREAK SPACE}\n{LONG.rstrip()}\n",
            f"{LONG.rstrip()}\n{LONG.rstrip()}\N{NO-BREAK SPACE}\n",
        ],
        ids=["only", "leading", "ideographic", "a-line-of-its-own", "trailing"],
    )
    def test_the_paragraph_is_kept_as_written(self, source: str) -> None:
        assert wrap_markdown.rewrap(source, 40) == source

    def test_one_inside_a_paragraph_is_text(self) -> None:
        source = f"{LONG.rstrip()}\N{NO-BREAK SPACE}\n{LONG.rstrip()}\n"

        result = wrap_markdown.rewrap(source, 40)

        assert result != source
        assert "word\N{NO-BREAK SPACE}" in result
        assert wrap_markdown._parts(result) == wrap_markdown._parts(source)


def _page(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "page.md"
    path.write_bytes(text.encode(ENCODING))
    return path


class TestAFileItRewrites:
    def test_a_long_paragraph_is_rewritten(self, tmp_path: Path) -> None:
        path = _page(tmp_path, LONG.rstrip() + "\n")

        done = wrap_markdown.process(path, 40, check=False)

        assert done
        assert max(len(line) for line in path.read_text(encoding=ENCODING).split("\n")) <= 40

    def test_a_fenced_block_survives_the_rewrite(self, tmp_path: Path) -> None:
        fence = f"```\n{LONG}\n```\n"
        path = _page(tmp_path, f"{LONG.rstrip()}\n\n{fence}")

        assert wrap_markdown.process(path, 40, check=False)
        assert path.read_text(encoding=ENCODING).endswith(fence)

    @pytest.mark.parametrize(
        "code", ["    indented = code\n", "- item\n\n      indented = code\n"], ids=["top", "in-item"]
    )
    def test_an_indented_code_block_survives_the_rewrite(self, code: str, tmp_path: Path) -> None:
        path = _page(tmp_path, f"{LONG.rstrip()}\n\n{code}")

        assert wrap_markdown.process(path, 40, check=False)
        assert path.read_text(encoding=ENCODING).endswith(code)


class TestAFileItWillNotTouch:
    """`process` rewrites documentation in place, so each way out that leaves
    the file alone is a guard worth holding."""

    def test_check_reports_the_file_and_leaves_it(self, tmp_path: Path) -> None:
        path = _page(tmp_path, LONG.rstrip() + "\n")

        done = wrap_markdown.process(path, 40, check=True)

        assert not done
        assert path.read_text(encoding=ENCODING) == LONG.rstrip() + "\n"

    def test_a_file_already_wrapped_passes_the_check(self, tmp_path: Path) -> None:
        path = _page(tmp_path, "short\n")

        assert wrap_markdown.process(path, 40, check=True)

    def test_a_crlf_file_is_skipped(self, tmp_path: Path) -> None:
        text = LONG.rstrip() + "\r\n"
        path = _page(tmp_path, text)

        done = wrap_markdown.process(path, 40, check=False)

        assert not done
        assert path.read_bytes() == text.encode(ENCODING)

    def test_a_file_with_a_nul_is_skipped(self, tmp_path: Path) -> None:
        """markdown-it reads a NUL as U+FFFD. The rewrite wrote that back, and
        the content check, parsing both sides the same way, could not see it.
        """
        text = f"short\n{LONG.rstrip()}\0\n"
        path = _page(tmp_path, text)

        done = wrap_markdown.process(path, 40, check=False)

        assert not done
        assert path.read_bytes() == text.encode(ENCODING)

    def test_a_rewrap_that_loses_a_word_is_not_written(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        text = LONG.rstrip() + "\n"
        path = _page(tmp_path, text)
        monkeypatch.setattr(wrap_markdown, "rewrap", lambda source, width: source.replace("word ", "", 1))

        done = wrap_markdown.process(path, 40, check=False)

        assert not done
        assert path.read_bytes() == text.encode(ENCODING)


class TestMain:
    @pytest.mark.parametrize(("texts", "rc"), [(["short\n"], 0), (["short\n", LONG.rstrip() + "\n"], 1)])
    def test_exits_nonzero_when_any_file_fails_the_check(
        self, texts: list[str], rc: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        paths = []
        for number, text in enumerate(texts):
            path = tmp_path / f"page{number}.md"
            path.write_bytes(text.encode(ENCODING))
            paths.append(str(path))
        monkeypatch.setattr("sys.argv", ["wrap_markdown.py", "--check", "--width", "40", *paths])

        assert wrap_markdown.main() == rc


class TestTheToolIsStable:
    def test_a_second_pass_over_the_tree_changes_nothing(self) -> None:
        """The defect was a first pass that indented a paragraph and a second
        that called the result already wrapped, so idempotence over the real
        files is the property worth pinning rather than any one shape.
        """
        paths = sorted(REPO_ROOT.glob("specs/*.md")) + sorted(REPO_ROOT.glob("docs/**/*.md"))
        pages = {
            path.relative_to(REPO_ROOT).as_posix(): path.read_text(encoding=ENCODING, newline="") for path in paths
        }
        # The two kinds of file the tool itself refuses to rewrite.
        wrappable = {name: text for name, text in pages.items() if "\r" not in text and "\0" not in text}
        assert wrappable
        once = {name: wrap_markdown.rewrap(text, 78) for name, text in wrappable.items()}

        twice = {name: wrap_markdown.rewrap(text, 78) for name, text in once.items()}

        assert [name for name in once if twice[name] != once[name]] == []
