"""Rewrap the prose in a Markdown file, leaving everything else byte for byte.

    python .github/scripts/wrap_markdown.py --width 78 docs/formats.md

Blocks are read the way CommonMark reads them. Paragraphs are rewrapped,
including those in block quotes and list items; every other line is copied
through: fences, tables, headings, HTML, link reference definitions.

A link, an inline code span and a short parenthesised list read badly split
over two lines, and stop being greppable, so the spaces inside them are held
together while the wrapping happens, which moves the whole of one down to the
next line rather than break it. A code span or a list too long to fit a line
of its own is left breakable, since holding it would only overflow the line
it landed on. A link is held whichever way, because a broken link costs more
than the overflow it saves.

A ``|`` and an ordinal are held to the word before them for a different
reason: a continuation line that opens with one reads as a table row or a
list item. Any other word is held there when markdown-it reads a line opening
on it as a block of its own: a quote, a heading, a bullet, a fence, HTML.

A definition list is written as a label in bold or italic, a colon, and the
text under it, so a line starting on one of those labels keeps the break
before it. A line that is only the label is left where it stands.

A file carrying CRLF line endings or a NUL is reported and left alone, since
markdown-it rewrites both and its text of a paragraph would no longer match
the source. So is a file carrying an indented code block; put it in a fence
instead.

The tool compares the block structure, the prose words and the lines outside
paragraphs before and after, and writes nothing when any of them moved. Pass
``--check`` to report without writing, which is what CI would call.

A line still over the width once a file is wrapped is over on one unbreakable
token, a link or a URL, since anything else would have been broken. Those are
counted when the file is written and never fail the run.
"""

from __future__ import annotations

import argparse
import functools
import itertools
import re
import sys
from collections.abc import Iterator
from pathlib import Path

from markdown_it import MarkdownIt
from markdown_it.token import Token

MARKDOWN = MarkdownIt("commonmark").enable("table")
LABEL = re.compile(r"(?:\*\*[^*]+\*\*|_[^_]+_):")
# Spaces that must not become a line break.
LINK = re.compile(r"\[[^\]]*\]\([^)]*\)")
# Held only while the whole of it still fits a line: a code span, a list.
FITTED = re.compile(r"`[^`]+`|\((?:[^()\s]+[,;]\s+){1,4}[^()\s]+\)")
# Joined to the word before, so no wrapped line opens like a table or a list.
MARKER = re.compile(r"(?<=\S) +(?=\||\d+\.(?:\s|$))")


def _paragraphs(tokens: list[Token]) -> Iterator[tuple[int, int, str]]:
    """(first line, end line, text without container markers) of each paragraph."""
    for opening, inline in itertools.pairwise(tokens):
        if opening.type == "paragraph_open" and opening.map:
            yield opening.map[0], opening.map[1], inline.content


def _atoms(text: str, room: int) -> list[str]:
    """*text* split at the spaces a line may break on."""
    held: set[int] = set()
    for match in LINK.finditer(text):
        held.update(range(*match.span()))
    for match in FITTED.finditer(text):
        if len(match[0]) <= room:
            held.update(range(*match.span()))
    held.update(match.start() for match in MARKER.finditer(text))
    atoms: list[str] = []
    start = 0
    for gap in re.finditer(r" +", text):
        if gap.start() not in held:
            atoms.append(text[start : gap.start()])
            start = gap.end()
    atoms.append(text[start:])
    return [atom for atom in atoms if atom]


@functools.cache
def _opens_block(atom: str, last: bool) -> bool:
    """Whether a continuation line opening on *atom* would end the paragraph."""
    probe = f"x\n{atom}" if last else f"x\n{atom} x"
    return [token.type for token in MARKDOWN.parse(probe)] != ["paragraph_open", "inline", "paragraph_close"]


def _fill(words: list[str], first: str, rest: str, width: int) -> list[str]:
    atoms = _atoms(" ".join(words), width - len(rest))
    lines = [first + atoms[0]]
    for number, atom in enumerate(atoms[1:], 2):
        if len(lines[-1]) + 1 + len(atom) <= width or _opens_block(atom, number == len(atoms)):
            lines[-1] += " " + atom
        else:
            lines.append(rest + atom)
    return lines


def _rewrap_paragraph(head: str, content: str, width: int) -> list[str]:
    """The paragraph opening on source line *head*, wrapped.

    Its first line carries the list and quote markers. Later lines keep the
    quote bars and turn each list marker into the spaces under it.
    """
    body = [line.strip() for line in content.split("\n")]
    head = head.rstrip()
    first = head[: len(head) - len(body[0])]
    rest = re.sub(r"[^>\s]", " ", first)
    out: list[str] = []
    group: list[str] = []
    for line in body:
        if LABEL.match(line):
            if group:
                out += _fill(group, rest if out else first, rest, width)
                group = []
            if LABEL.fullmatch(line):
                out.append((rest if out else first) + line)
                continue
        group.append(line)
    if group:
        out += _fill(group, rest if out else first, rest, width)
    return out


def rewrap(text: str, width: int) -> str:
    lines = text.split("\n")
    for start, end, content in reversed(list(_paragraphs(MARKDOWN.parse(text)))):
        lines[start:end] = _rewrap_paragraph(lines[start], content, width)
    return "\n".join(lines)


def _parts(text: str) -> tuple[list[str], list[str], list[str]]:
    """(block structure, prose words, other lines), what a rewrap must preserve."""
    tokens = MARKDOWN.parse(text)
    prose: list[str] = []
    rows: set[int] = set()
    for start, end, content in _paragraphs(tokens):
        prose += content.split()
        rows.update(range(start, end))
    other = [line for number, line in enumerate(text.split("\n")) if number not in rows]
    return [token.type for token in tokens], prose, other


def _indented_code(text: str) -> list[int]:
    """Line numbers of indented code blocks."""
    return [token.map[0] + 1 for token in MARKDOWN.parse(text) if token.type == "code_block" and token.map]


def process(path: Path, width: int, check: bool) -> bool:
    original = path.read_text(encoding="utf-8", newline="")
    if "\r" in original:
        print(f"{path}: CRLF, skipped")
        return False
    if "\0" in original:
        print(f"{path}: NUL, skipped")
        return False
    if lines := _indented_code(original):
        print(f"{path}: indented code at line(s) {lines}, skipped")
        return False

    result = rewrap(original, width)
    if _parts(original) != _parts(result):
        print(f"{path}: content moved, not written")
        return False

    if original == result:
        print(f"{path}: already wrapped at {width}")
        return True
    if check:
        print(f"{path}: would rewrap at {width}")
        return False
    path.write_text(result, encoding="utf-8", newline="")
    rows = {row for start, end, _ in _paragraphs(MARKDOWN.parse(result)) for row in range(start, end)}
    over = [line for number, line in enumerate(result.split("\n")) if number in rows and len(line) > width]
    print(f"{path}: rewrapped at {width}, {len(over)} line(s) still over")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--width", type=int, default=78)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    ok = [process(path, args.width, args.check) for path in args.paths]
    return 0 if all(ok) else 1


if __name__ == "__main__":
    sys.exit(main())
