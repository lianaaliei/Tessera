"""SQL tokenizer."""
import re
from collections import namedtuple


class SQLError(Exception):
    """Base class for every error Tessera raises."""


class SQLSyntaxError(SQLError):
    pass


Token = namedtuple("Token", "kind value pos end")

_TOKEN = re.compile(
    r"""
    (?P<ws>\s+|--[^\n]*)
  | (?P<num>\d+\.\d*|\.\d+|\d+)
  | (?P<str>'(?:[^']|'')*')
  | (?P<qid>"(?:[^"]|"")*")
  | (?P<id>[A-Za-z_][A-Za-z_0-9]*)
  | (?P<op><=|>=|<>|!=|==|\|\||[-+*/%(),.;=<>])
  | (?P<param>\?)
    """,
    re.X,
)


def tokenize(sql):
    out, i, n = [], 0, len(sql)
    while i < n:
        m = _TOKEN.match(sql, i)
        if not m:
            raise SQLSyntaxError(f"unexpected character {sql[i]!r} at position {i}")
        i = m.end()
        kind, text = m.lastgroup, m.group()
        if kind == "ws":
            continue
        if kind == "num":
            value = float(text) if "." in text else int(text)
        elif kind == "str":
            value = text[1:-1].replace("''", "'")
        elif kind == "qid":
            value = text[1:-1].replace('""', '"')
        else:
            value = text
        out.append(Token(kind, value, m.start(), m.end()))
    out.append(Token("eof", None, n, n))
    return out
