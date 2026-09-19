"""Server-side lexing: semantic kinds as per-line spans.

A client receives, per line, the spans that are not plain text — `[start, length, kind]` — and maps
each kind to its own palette. The browser has CSS classes and the terminal has colour codes, and
neither is the other's business, so the wire carries the meaning and not the presentation.

The kind vocabulary is deliberately small and closed. A client that meets an unknown kind renders
it as plain text, so adding one never breaks a client that has not caught up.
"""
from __future__ import annotations

from pygments import lex
from pygments.lexers import get_lexer_by_name, get_lexer_for_filename
from pygments.token import Token
from pygments.util import ClassNotFound

# kind -> the pygments token prefixes that map onto it, most specific first
_MAPPING: list[tuple[object, str]] = [
    (Token.Comment, "comment"),
    (Token.String.Doc, "docstring"),
    (Token.String, "string"),
    (Token.Number, "number"),
    (Token.Keyword.Type, "type"),
    (Token.Keyword, "keyword"),
    (Token.Name.Function, "function"),
    (Token.Name.Class, "class"),
    (Token.Name.Decorator, "decorator"),
    (Token.Name.Builtin, "builtin"),
    (Token.Name.Constant, "constant"),
    (Token.Name.Namespace, "namespace"),
    (Token.Name.Tag, "tag"),
    (Token.Name.Attribute, "attribute"),
    (Token.Operator, "operator"),
    (Token.Generic.Deleted, "deleted"),
    (Token.Generic.Inserted, "inserted"),
    (Token.Generic.Heading, "heading"),
]

KINDS = tuple(sorted({kind for _, kind in _MAPPING}))

# spans of these carry no colour anywhere, so emitting them would be pure payload
_SKIP = {"text", "whitespace"}


def kind_of(token_type) -> str:
    """The wire kind for a pygments token type, or "text" when it carries no meaning worth sending."""
    for prefix, kind in _MAPPING:
        if token_type in prefix:
            return kind
    return "text"


def lexer_for(path: str | None, language: str | None = None):
    """A lexer for a file, by explicit language then by filename. None when neither resolves —
    an unknown file type is rendered plain rather than guessed at."""
    for lookup, argument in ((get_lexer_by_name, language), (get_lexer_for_filename, path)):
        if not argument:
            continue
        try:
            return lookup(argument, stripnl=False, ensurenl=False)
        except ClassNotFound:
            continue
    return None


def tokenize(text: str, path: str | None = None, language: str | None = None) -> list[list[list]]:
    """Lex `text` and return one span list per line: `[[start, length, kind], …]`.

    The whole text is lexed in one pass, so a construct spanning several lines — a block comment, a
    triple-quoted string — keeps its kind on every line it covers. Lines with nothing to mark come
    back as empty lists, and the result always has one entry per line of input.
    """
    lines: list[list[list]] = [[] for _ in text.split("\n")]
    lexer = lexer_for(path, language)
    if lexer is None:
        return lines
    row, column = 0, 0
    for token_type, value in lex(text, lexer):
        kind = kind_of(token_type)
        for index, piece in enumerate(value.split("\n")):
            if index:
                row, column = row + 1, 0
            if not piece:
                continue
            if kind not in _SKIP and row < len(lines):
                lines[row].append([column, len(piece), kind])
            column += len(piece)
    return lines
