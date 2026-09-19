"""Server-side lexing: the wire contract both clients render against."""
from review_mate.view.tokens import KINDS, kind_of, lexer_for, tokenize
from pygments.token import Token


def spans_fit(text, per_line):
    """Every span must address real characters on its own line."""
    for line, spans in zip(text.split("\n"), per_line):
        for start, length, kind in spans:
            assert 0 <= start < start + length <= len(line), (line, start, length)
            assert kind in KINDS


def test_one_span_list_per_line_always():
    text = "a = 1\n\nb = 2\n"
    assert len(tokenize(text, "x.py")) == len(text.split("\n"))


def test_spans_address_their_own_line():
    text = "def f(x):\n    return x + 1\n"
    per_line = tokenize(text, "f.py")
    spans_fit(text, per_line)
    assert ["keyword" for _ in range(1)] == [k for _, _, k in per_line[0] if k == "keyword"]


def test_a_construct_spanning_lines_keeps_its_kind_on_each():
    text = 'x = """one\ntwo\nthree"""\n'
    per_line = tokenize(text, "s.py")
    assert all(any(k in ("string", "docstring") for _, _, k in line) for line in per_line[:3])
    spans_fit(text, per_line)


def test_plain_text_is_not_sent():
    # a line of bare identifiers and whitespace carries nothing worth colouring
    per_line = tokenize("foo bar\n", "x.txt")
    assert per_line == [[], []]


def test_an_unknown_file_type_renders_plain_rather_than_guessing():
    text = "some words\nmore words\n"
    per_line = tokenize(text, "notes.unknownext")
    assert per_line == [[], [], []]
    assert len(per_line) == len(text.split("\n"))


def test_an_explicit_language_wins_over_the_filename():
    assert lexer_for("a.txt", "python") is not None
    assert lexer_for("a.txt", "python").name.lower().startswith("python")


def test_kinds_are_a_closed_vocabulary():
    assert kind_of(Token.Comment.Single) == "comment"
    assert kind_of(Token.Keyword) == "keyword"
    assert kind_of(Token.Text) == "text" and "text" not in KINDS
    assert kind_of(Token.Name.Function) == "function"


def test_a_docstring_is_distinguished_from_a_plain_string():
    body = tokenize('def f():\n    """doc"""\n    s = "plain"\n', "d.py")
    assert any(k == "docstring" for _, _, k in body[1])
    assert any(k == "string" for _, _, k in body[2])
