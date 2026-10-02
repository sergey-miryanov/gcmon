"""Rewrap the prose in a Markdown file, leaving everything else byte for byte.

    poetry run python .github/scripts/wrap_markdown.py --width 78 docs/formats.md

Blocks are read the way CommonMark reads them. Paragraphs are rewrapped,
including those in block quotes and list items; every other line is copied
through: fences, indented code, tables, headings, HTML, link reference
definitions.

Inside a paragraph, a line opening on ``|`` or ``[label]:`` with no prose
before it is kept as written: a table markdown-it rejects, a footnote, a
definition with text after its destination. A hard break stays at the end of
its line. A paragraph markdown-it has stripped a Unicode space from, at either
end, is kept as written.

A link, an inline code span and a short parenthesised list read badly split
over two lines, and stop being greppable, so the spaces inside them are held
together while the wrapping happens, which moves the whole of one down to the
next line rather than break it. A code span or a list too long to fit a line
of its own is left breakable, since holding it would only overflow the line
it landed on. A link is held whichever way, because a broken link costs more
than the overflow it saves. Spaces left on one line keep their count.

A word is held to the one before it when a continuation line opening on it
would start another block: a ``|``, or any word markdown-it reads there as a
quote, a heading, a list item, a fence, a thematic break or HTML, alone on
the line or followed by more.

A definition list is written as a label in bold or italic, a colon, and the
text under it, so a line starting on one of those labels keeps the break
before it. A line that is only the label is left where it stands.

A file carrying CRLF line endings or a NUL is reported and left alone.

The tool compares the block and inline structure, the prose words and the
lines outside paragraphs before and after, and writes nothing when any of them
moved. Pass ``--check`` to report without writing, which is what CI would
call.

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
PARAGRAPH = ["paragraph_open", "inline", "paragraph_close"]
# What a block that can interrupt a paragraph opens on.
OPENERS = frozenset("#>-+*=_`~<0123456789")
LABEL = re.compile(r"(?:\*\*[^*]+\*\*|_[^_]+_):")
# A line opening a paragraph on one of these is kept as written.
KEPT = re.compile(r"\||\[[^\]]+\]:")
# Spaces that must not become a line break.
LINK = re.compile(r"\[[^\]]*\]\([^)]*\)")
# Held only while the whole of it still fits a line: a code span, a list.
FITTED = re.compile(r"`[^`]+`|\((?:[^()\s]+[,;]\s+){1,4}[^()\s]+\)")
# Joined to the word before, so no wrapped line opens like a table.
MARKER = re.compile(r"(?<=\S) +(?=\|)")


def _paragraphs(tokens: list[Token]) -> Iterator[tuple[int, int, str]]:
    """(first line, end line, text without container markers) of each paragraph."""
    for opening, inline in itertools.pairwise(tokens):
        if opening.type == "paragraph_open" and opening.map:
            yield opening.map[0], opening.map[1], inline.content


def _pieces(text: str, room: int) -> list[tuple[str, str]]:
    """*text* split at the spaces a line may break on, as (spaces before, piece)."""
    held: set[int] = set()
    for match in LINK.finditer(text):
        held.update(range(*match.span()))
    for match in FITTED.finditer(text):
        if len(match[0]) <= room:
            held.update(range(*match.span()))
    held.update(match.start() for match in MARKER.finditer(text))
    pieces: list[tuple[str, str]] = []
    start, gap = 0, ""
    for match in re.finditer(r" +", text):
        if match.start() not in held:
            pieces.append((gap, text[start : match.start()]))
            start, gap = match.end(), match[0]
    pieces.append((gap, text[start:]))
    joined = pieces[:1]
    for gap, piece in pieces[1:]:
        if _opens_block(piece):
            before, last = joined[-1]
            joined[-1] = (before, last + gap + piece)
        else:
            joined.append((gap, piece))
    return joined


@functools.cache
def _opens_block(piece: str) -> bool:
    """Whether a continuation line opening on *piece*, alone or with more after it, ends the paragraph."""
    if piece[0] not in OPENERS:
        return False
    return any(
        [token.type for token in MARKDOWN.parse(probe)] != PARAGRAPH for probe in (f"x\n{piece}", f"x\n{piece} x")
    )


def _fill(words: list[str], first: str, rest: str, width: int) -> list[str]:
    (_, opening), *pieces = _pieces(" ".join(words), width - len(rest))
    lines = [first + opening]
    for gap, piece in pieces:
        if len(lines[-1]) + len(gap) + len(piece) <= width:
            lines[-1] += gap + piece
        else:
            lines.append(rest + piece)
    return lines


def _rewrap_paragraph(source: list[str], content: str, width: int) -> list[str]:
    """The paragraph on *source* lines, wrapped; *content* is markdown-it's text of it.

    Its first line carries the list and quote markers. Later lines keep the
    quote bars and turn each list marker into the spaces under it.
    """
    lines = content.split("\n")
    head = source[0].rstrip(" \t")
    first = head[: len(head) - len(lines[0].strip(" \t"))]
    # markdown-it strips Unicode spaces off a paragraph's ends, where CommonMark
    # keeps them as text. A paragraph that lost one is kept as written.
    lined_up = len(lines) == len(source) and all(
        raw.rstrip(" \t").endswith(line.strip(" \t")) for raw, line in zip(source, lines, strict=True)
    )
    if not lined_up or not first.isascii():
        return source
    rest = re.sub(r"[^>\s]", " ", first)
    out: list[str] = []
    group: list[str] = []

    def flush() -> None:
        if group:
            out.extend(_fill(group, rest if out else first, rest, width))
            group.clear()

    for raw, line in zip(source, lines, strict=True):
        text = line.strip(" \t")
        if LABEL.fullmatch(text) or (not group and KEPT.match(text)):
            flush()
            out.append(raw)
            continue
        if LABEL.match(text):
            flush()
        group.append(text)
        if line.endswith(("\\", "  ")):
            flush()
            out[-1] += line[len(line.rstrip(" ")) :]
    flush()
    return out


def rewrap(text: str, width: int) -> str:
    lines = text.split("\n")
    for start, end, content in reversed(list(_paragraphs(MARKDOWN.parse(text)))):
        lines[start:end] = _rewrap_paragraph(lines[start:end], content, width)
    return "\n".join(lines)


def _parts(text: str) -> tuple[list[str], list[str], list[str]]:
    """(block and inline structure, prose words, other lines), what a rewrap must preserve."""
    tokens = MARKDOWN.parse(text)
    structure = [token.type for token in tokens]
    structure += [
        child.type for token in tokens for child in token.children or [] if child.type not in ("text", "softbreak")
    ]
    prose: list[str] = []
    rows: set[int] = set()
    for start, end, content in _paragraphs(tokens):
        prose += content.split()
        rows.update(range(start, end))
    other = [line for number, line in enumerate(text.split("\n")) if number not in rows]
    return structure, prose, other


def process(path: Path, width: int, check: bool) -> bool:
    original = path.read_text(encoding="utf-8", newline="")
    if "\r" in original:
        print(f"{path}: CRLF, skipped")
        return False
    if "\0" in original:
        print(f"{path}: NUL, skipped")
        return False

    result = rewrap(original, width)
    if original == result:
        print(f"{path}: already wrapped at {width}")
        return True
    if _parts(original) != _parts(result):
        print(f"{path}: content moved, not written")
        return False
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
