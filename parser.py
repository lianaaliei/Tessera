"""Recursive-descent SQL parser producing plain tuples (expressions) and dataclasses (statements).

Expression nodes (all hashable tuples):
  ('lit', v) ('param', i) ('col', qualifier, name) ('neg', e) ('not', e)
  ('bin', op, l, r) ('and', l, r) ('or', l, r) ('isnull', e, negated)
  ('like', e, pattern, negated) ('in', e, items, negated) ('between', e, lo, hi, negated)
  ('func', name, args) ('agg', name, arg_or_None, distinct) ('case', operand, whens, else)
"""
from dataclasses import dataclass, field

from .lexer import SQLSyntaxError, tokenize

RESERVED = frozenset(
    """select from where group by having order limit offset join inner left outer cross on and or not
    as insert into values update set delete create table index unique drop begin commit rollback null
    is in like between distinct all asc desc primary explain case when then else end default""".split()
)
AGGREGATES = frozenset({"count", "sum", "avg", "min", "max"})
TYPES = {"integer": "INTEGER", "int": "INTEGER", "bigint": "INTEGER", "smallint": "INTEGER",
         "real": "REAL", "float": "REAL", "double": "REAL",
         "text": "TEXT", "varchar": "TEXT", "char": "TEXT", "string": "TEXT"}


@dataclass
class ColumnDef:
    name: str
    type: str
    notnull: bool = False
    unique: bool = False
    pk: bool = False
    default: object = None


@dataclass
class CreateTable:
    name: str
    columns: list
    if_not_exists: bool = False


@dataclass
class CreateIndex:
    name: str
    table: str
    column: str
    unique: bool = False


@dataclass
class DropTable:
    name: str
    if_exists: bool = False


@dataclass
class Insert:
    table: str
    columns: object
    rows: object = None
    select: object = None


@dataclass
class Update:
    table: str
    sets: list
    where: object = None


@dataclass
class Delete:
    table: str
    where: object = None


@dataclass
class Select:
    items: list
    distinct: bool = False
    from_: object = None           # (table, alias)
    joins: list = field(default_factory=list)   # (kind, table, alias, on_expr_or_None)
    where: object = None
    group: tuple = ()
    having: object = None
    order: list = field(default_factory=list)   # (expr, descending)
    limit: object = None
    offset: object = None


@dataclass
class Explain:
    select: Select


@dataclass
class Txn:
    action: str   # begin | commit | rollback


class Parser:
    def __init__(self, sql):
        self.sql = sql
        self.toks = tokenize(sql)
        self.i = 0
        self.nparams = 0

    # ── token helpers ──
    def peek(self, k=0):
        return self.toks[min(self.i + k, len(self.toks) - 1)]

    def next(self):
        t = self.toks[self.i]
        if t.kind != "eof":
            self.i += 1
        return t

    def fail(self, msg=None):
        t = self.peek()
        got = "end of input" if t.kind == "eof" else repr(self.sql[t.pos:t.end])
        raise SQLSyntaxError(msg or f"unexpected {got} at position {t.pos}")

    def is_kw(self, *words, k=0):
        t = self.peek(k)
        return t.kind == "id" and t.value.lower() in words

    def accept_kw(self, *words):
        if self.is_kw(*words):
            return self.next().value.lower()
        return None

    def expect_kw(self, word):
        if not self.accept_kw(word):
            self.fail(f"expected {word.upper()} at position {self.peek().pos}")

    def is_op(self, v, k=0):
        t = self.peek(k)
        return t.kind == "op" and t.value == v

    def accept_op(self, v):
        if self.is_op(v):
            self.next()
            return True
        return False

    def expect_op(self, v):
        if not self.accept_op(v):
            self.fail(f"expected {v!r} at position {self.peek().pos}")

    def ident(self, what="identifier"):
        t = self.peek()
        if t.kind == "qid" or (t.kind == "id" and t.value.lower() not in RESERVED):
            self.next()
            return t.value.lower()
        self.fail(f"expected {what} at position {t.pos}")

    def raw_ident(self):
        """Like ident() but keeps the original capitalisation (for display)."""
        t = self.peek()
        name = self.ident("column name")
        return name, t.value

    # ── statements ──
    def parse_script(self):
        stmts = []
        while True:
            while self.accept_op(";"):
                pass
            if self.peek().kind == "eof":
                return stmts
            start = self.peek().pos
            self.nparams = 0
            stmt = self.statement()
            end = self.toks[self.i - 1].end
            if not (self.accept_op(";") or self.peek().kind == "eof"):
                self.fail()
            stmts.append((stmt, self.nparams, self.sql[start:end]))

    def statement(self):
        if self.is_kw("select"):
            return self.select()
        if self.accept_kw("explain"):
            if not self.is_kw("select"):
                self.fail("EXPLAIN only supports SELECT")
            return Explain(self.select())
        if self.accept_kw("insert"):
            return self.insert()
        if self.accept_kw("update"):
            return self.update()
        if self.accept_kw("delete"):
            self.expect_kw("from")
            table = self.ident("table name")
            return Delete(table, self.where_clause())
        if self.accept_kw("create"):
            return self.create()
        if self.accept_kw("drop"):
            self.expect_kw("table")
            if_exists = False
            if self.accept_kw("if"):
                self.expect_kw("exists")
                if_exists = True
            return DropTable(self.ident("table name"), if_exists)
        if self.accept_kw("begin"):
            self.accept_kw("transaction")
            return Txn("begin")
        if self.accept_kw("commit", "end"):
            self.accept_kw("transaction")
            return Txn("commit")
        if self.accept_kw("rollback"):
            self.accept_kw("transaction")
            return Txn("rollback")
        self.fail()

    def create(self):
        unique = bool(self.accept_kw("unique"))
        if self.accept_kw("index"):
            name = self.ident("index name")
            self.expect_kw("on")
            table = self.ident("table name")
            self.expect_op("(")
            col = self.ident("column name")
            self.expect_op(")")
            return CreateIndex(name, table, col, unique)
        if unique:
            self.fail("expected INDEX after UNIQUE")
        self.expect_kw("table")
        if_not_exists = False
        if self.accept_kw("if"):
            self.expect_kw("not")
            self.expect_kw("exists")
            if_not_exists = True
        name = self.ident("table name")
        self.expect_op("(")
        cols = []
        while True:
            key, shown = self.raw_ident()
            tname = self.ident("column type") if self.peek().kind == "id" else None
            if tname is None or tname not in TYPES:
                self.fail(f"unknown or missing column type for {shown!r} (use INTEGER, REAL or TEXT)")
            if self.accept_op("("):          # VARCHAR(20)
                while not self.accept_op(")"):
                    if self.next().kind == "eof":
                        self.fail()
            col = ColumnDef(shown, TYPES[tname])
            while True:
                if self.accept_kw("primary"):
                    self.expect_kw("key")
                    col.pk = True
                elif self.accept_kw("not"):
                    self.expect_kw("null")
                    col.notnull = True
                elif self.accept_kw("unique"):
                    col.unique = True
                elif self.accept_kw("default"):
                    col.default = self.literal_default()
                else:
                    break
            cols.append(col)
            if not self.accept_op(","):
                break
        self.expect_op(")")
        if sum(c.pk for c in cols) > 1:
            self.fail("only one PRIMARY KEY column is supported")
        if len({c.name.lower() for c in cols}) != len(cols):
            self.fail("duplicate column name")
        return CreateTable(name, cols, if_not_exists)

    def literal_default(self):
        sign = 1
        if self.accept_op("-"):
            sign = -1
        t = self.next()
        if t.kind == "num":
            return sign * t.value
        if t.kind == "str" and sign == 1:
            return t.value
        if t.kind == "id" and t.value.lower() == "null" and sign == 1:
            return None
        self.i -= 1
        self.fail("DEFAULT must be a number, string or NULL")

    def insert(self):
        self.expect_kw("into")
        table = self.ident("table name")
        cols = None
        if self.accept_op("("):
            cols = [self.ident("column name")]
            while self.accept_op(","):
                cols.append(self.ident("column name"))
            self.expect_op(")")
        if self.is_kw("select"):
            return Insert(table, cols, select=self.select())
        self.expect_kw("values")
        rows = []
        while True:
            self.expect_op("(")
            row = [self.value_or_default()]
            while self.accept_op(","):
                row.append(self.value_or_default())
            self.expect_op(")")
            rows.append(tuple(row))
            if not self.accept_op(","):
                break
        return Insert(table, cols, rows=rows)

    def value_or_default(self):
        if self.accept_kw("default"):
            return ("default",)
        return self.expr()

    def update(self):
        table = self.ident("table name")
        self.expect_kw("set")
        sets = []
        while True:
            col = self.ident("column name")
            self.expect_op("=")
            sets.append((col, self.expr()))
            if not self.accept_op(","):
                break
        return Update(table, sets, self.where_clause())

    def where_clause(self):
        return self.expr() if self.accept_kw("where") else None

    def select(self):
        self.expect_kw("select")
        s = Select(items=[])
        if self.accept_kw("distinct"):
            s.distinct = True
        else:
            self.accept_kw("all")
        while True:
            s.items.append(self.select_item())
            if not self.accept_op(","):
                break
        if self.accept_kw("from"):
            s.from_ = (self.ident("table name"), self.alias())
            while True:
                kind = None
                if self.accept_op(","):
                    kind = "cross"
                elif self.accept_kw("cross"):
                    self.expect_kw("join")
                    kind = "cross"
                elif self.accept_kw("inner"):
                    self.expect_kw("join")
                    kind = "inner"
                elif self.accept_kw("left"):
                    self.accept_kw("outer")
                    self.expect_kw("join")
                    kind = "left"
                elif self.accept_kw("join"):
                    kind = "inner"
                if kind is None:
                    break
                table = self.ident("table name")
                alias = self.alias()
                on = None
                if kind != "cross":
                    self.expect_kw("on")
                    on = self.expr()
                s.joins.append((kind, table, alias, on))
        s.where = self.where_clause()
        if self.accept_kw("group"):
            self.expect_kw("by")
            g = [self.expr()]
            while self.accept_op(","):
                g.append(self.expr())
            s.group = tuple(g)
        if self.accept_kw("having"):
            s.having = self.expr()
        if self.accept_kw("order"):
            self.expect_kw("by")
            while True:
                e = self.expr()
                desc = False
                if self.accept_kw("desc"):
                    desc = True
                else:
                    self.accept_kw("asc")
                s.order.append((e, desc))
                if not self.accept_op(","):
                    break
        if self.accept_kw("limit"):
            s.limit = self.expr()
            if self.accept_kw("offset"):
                s.offset = self.expr()
            elif self.accept_op(","):      # LIMIT offset, count
                s.offset, s.limit = s.limit, self.expr()
        return s

    def alias(self):
        if self.accept_kw("as"):
            return self.ident("alias")
        t = self.peek()
        if t.kind == "qid" or (t.kind == "id" and t.value.lower() not in RESERVED):
            return self.ident()
        return None

    def select_item(self):
        if self.accept_op("*"):
            return (("star", None), None, "*")
        if self.peek().kind in ("id", "qid") and self.is_op(".", 1) and self.is_op("*", 2):
            q = self.ident()
            self.next(); self.next()
            return (("star", q), None, q + ".*")
        start = self.peek().pos
        e = self.expr()
        text = self.sql[start:self.toks[self.i - 1].end].strip()
        return (e, self.alias(), text)

    # ── expressions (lowest to highest precedence) ──
    def expr(self):
        return self.or_expr()

    def or_expr(self):
        e = self.and_expr()
        while self.accept_kw("or"):
            e = ("or", e, self.and_expr())
        return e

    def and_expr(self):
        e = self.not_expr()
        while self.accept_kw("and"):
            e = ("and", e, self.not_expr())
        return e

    def not_expr(self):
        if self.accept_kw("not"):
            return ("not", self.not_expr())
        return self.predicate()

    def predicate(self):
        e = self.additive()
        while True:
            t = self.peek()
            if t.kind == "op" and t.value in ("=", "==", "!=", "<>", "<", "<=", ">", ">="):
                self.next()
                op = {"==": "=", "<>": "!="}.get(t.value, t.value)
                e = ("bin", op, e, self.additive())
            elif self.is_kw("is"):
                self.next()
                neg = bool(self.accept_kw("not"))
                self.expect_kw("null")
                e = ("isnull", e, neg)
            else:
                neg = False
                if self.is_kw("not") and self.is_kw("like", "in", "between", k=1):
                    self.next()
                    neg = True
                if self.accept_kw("like"):
                    e = ("like", e, self.additive(), neg)
                elif self.accept_kw("in"):
                    self.expect_op("(")
                    items = [self.expr()]
                    while self.accept_op(","):
                        items.append(self.expr())
                    self.expect_op(")")
                    e = ("in", e, tuple(items), neg)
                elif self.accept_kw("between"):
                    lo = self.additive()
                    self.expect_kw("and")
                    e = ("between", e, lo, self.additive(), neg)
                else:
                    return e

    def additive(self):
        e = self.multiplicative()
        while self.is_op("+") or self.is_op("-"):
            e = ("bin", self.next().value, e, self.multiplicative())
        return e

    def multiplicative(self):
        e = self.concat()
        while self.is_op("*") or self.is_op("/") or self.is_op("%"):
            e = ("bin", self.next().value, e, self.concat())
        return e

    def concat(self):
        e = self.unary()
        while self.accept_op("||"):
            e = ("bin", "||", e, self.unary())
        return e

    def unary(self):
        if self.accept_op("-"):
            e = self.unary()
            return ("lit", -e[1]) if e[0] == "lit" and isinstance(e[1], (int, float)) else ("neg", e)
        if self.accept_op("+"):
            return self.unary()
        return self.primary()

    def primary(self):
        t = self.peek()
        if t.kind == "num" or t.kind == "str":
            self.next()
            return ("lit", t.value)
        if t.kind == "param":
            self.next()
            self.nparams += 1
            return ("param", self.nparams - 1)
        if self.accept_op("("):
            e = self.expr()
            self.expect_op(")")
            return e
        if t.kind == "id":
            low = t.value.lower()
            if low == "null":
                self.next()
                return ("lit", None)
            if low in ("true", "false"):
                self.next()
                return ("lit", 1 if low == "true" else 0)
            if low == "case":
                return self.case_expr()
            if self.is_op("(", 1) and low not in RESERVED:
                return self.call(low)
        if t.kind == "qid" or (t.kind == "id" and t.value.lower() not in RESERVED):
            first = self.ident()
            if self.accept_op("."):
                return ("col", first, self.ident("column name"))
            return ("col", None, first)
        self.fail()

    def call(self, name):
        self.next()
        self.expect_op("(")
        if name in AGGREGATES:
            distinct = bool(self.accept_kw("distinct"))
            if self.accept_op("*"):
                if name != "count" or distinct:
                    self.fail(f"{name.upper()}(*) is not valid")
                self.expect_op(")")
                return ("agg", "count", None, False)
            arg = self.expr()
            self.expect_op(")")
            return ("agg", name, arg, distinct)
        args = []
        if not self.is_op(")"):
            args.append(self.expr())
            while self.accept_op(","):
                args.append(self.expr())
        self.expect_op(")")
        return ("func", name, tuple(args))

    def case_expr(self):
        self.next()
        operand = None if self.is_kw("when") else self.expr()
        whens = []
        while self.accept_kw("when"):
            cond = self.expr()
            self.expect_kw("then")
            whens.append((cond, self.expr()))
        if not whens:
            self.fail("CASE needs at least one WHEN")
        other = self.expr() if self.accept_kw("else") else None
        self.expect_kw("end")
        return ("case", operand, tuple(whens), other)


_CACHE = {}


def parse_script(sql):
    """Parse with a small prepared-statement cache. ASTs only hold parameter *positions*
    (never values) and the executor never mutates them, so sharing them is safe."""
    hit = _CACHE.get(sql)
    if hit is None:
        hit = Parser(sql).parse_script()          # syntax errors raise before anything is cached
        if len(_CACHE) >= 512:
            _CACHE.clear()
        _CACHE[sql] = hit
    return hit
