from __future__ import annotations

import importlib.util
import textwrap
from pathlib import Path
from typing import Any

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

    def test_the_ordinal_is_pulled_back_onto_the_line_before(self) -> None:
        """`MARKER` holds it to the word before, so a later pass cannot read it
        as a marker either.
        """
        result = wrap_markdown.rewrap(self.PARAGRAPH, 78)

        assert not [line for line in result.split("\n") if line.startswith("0029.")]

    def test_rewrapping_twice_changes_nothing(self) -> None:
        """The defect reported "already wrapped" on the second run, having
        indented the paragraph on the first.
        """
        once = wrap_markdown.rewrap(self.PARAGRAPH, 78)

        assert wrap_markdown.rewrap(once, 78) == once


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
        """CommonMark reads it as part of the paragraph, so the tool does too:
        the `not para` guard in front of the verbatim branch is deliberate.
        """
        source = "Some prose that runs straight on.\n[a]: https://example.com/one\n"

        assert wrap_markdown.rewrap(source, 78) == "Some prose that runs straight on. [a]: https://example.com/one\n"


class TestTheToolIsStable:
    def test_a_second_pass_over_the_tree_changes_nothing(self) -> None:
        """The defect was a first pass that indented a paragraph and a second
        that called the result already wrapped, so idempotence over the real
        files is the property worth pinning rather than any one shape.
        """
        unstable = []
        for path in sorted(REPO_ROOT.glob("specs/*.md")) + sorted(REPO_ROOT.glob("docs/**/*.md")):
            original = path.read_text(encoding="utf-8", newline="")
            if "\r" in original or wrap_markdown._indented_code(original):
                continue
            once = wrap_markdown.rewrap(original, 78)
            if wrap_markdown.rewrap(once, 78) != once:
                unstable.append(path.relative_to(REPO_ROOT).as_posix())

        assert not unstable
