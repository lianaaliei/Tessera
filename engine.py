"""Tessera engine: storage, indexes, planner and executor."""
import bisect
import json
import math
import os
import re

from . import parser as P
from .lexer import SQLError


class ConstraintError(SQLError):
    pass


class UnknownColumn(SQLError):
    pass


class AmbiguousColumn(SQLError):
    pass


class CorruptError(SQLError):
    pass


# ───────────────────────── SQL value semantics ─────────────────────────
# Values are Python None / int / float / str. NULL ordering and comparison follow SQLite:
# NULL < numbers < text, and any comparison involving NULL is unknown (None).

def _rank(v):
    return 0 if v is None else 1 if isinstance(v, (int, float)) else 2


def sort_key(v):
    return (0, 0) if v is None else (1, v) if isinstance(v, (int, float)) else (2, v)


def cmp_vals(a, b):
    ra, rb = _rank(a), _rank(b)
    if ra != rb:
        return -1 if ra < rb else 1
    return (a > b) - (a < b)


def num(v):
    """Coerce to a number the way SQLite does for arithmetic (text -> its numeric value, else 0)."""
    if isinstance(v, (int, float)):
        return v
    try:
        return int(v)
    except ValueError:
        try:
            return float(v)
        except ValueError:
            return 0


def truth(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return v != 0
    try:
        return float(v) != 0
    except ValueError:
        return False


def to_text(v):
    return v if isinstance(v, str) else repr(v) if isinstance(v, float) else str(v)


def _arith(fn):
    def op(a, b):
        if a is None or b is None:
            return None
        return fn(num(a), num(b))
    return op


def _div(a, b):
    if b == 0:
        return None
    if isinstance(a, int) and isinstance(b, int):
        q = abs(a) // abs(b)
        return q if (a < 0) == (b < 0) else -q
    return a / b


def _mod(a, b):
    ai, bi = int(a), int(b)
    if bi == 0:
        return None
    r = abs(ai) % abs(bi)
    r = -r if ai < 0 else r
    return r if isinstance(a, int) and isinstance(b, int) else float(r)


def _concat(a, b):
    return None if a is None or b is None else to_text(a) + to_text(b)


def _cmp_op(test):
    def op(a, b):
        if a is None or b is None:
            return None
        return 1 if test(cmp_vals(a, b)) else 0
    return op


BIN_OPS = {
    "+": _arith(lambda a, b: a + b), "-": _arith(lambda a, b: a - b), "*": _arith(lambda a, b: a * b),
    "/": _arith(_div), "%": _arith(_mod), "||": _concat,
    "=": _cmp_op(lambda c: c == 0), "!=": _cmp_op(lambda c: c != 0),
    "<": _cmp_op(lambda c: c < 0), "<=": _cmp_op(lambda c: c <= 0),
    ">": _cmp_op(lambda c: c > 0), ">=": _cmp_op(lambda c: c >= 0),
}


def _round(x, n=0):
    if x is None:
        return None
    x, n = float(num(x)), int(num(n or 0))
    f = 10.0 ** n
    v = math.floor(abs(x) * f + 0.5) / f
    return math.copysign(v, x) if v else 0.0


def _substr(s, start, length=None):
    if s is None or start is None:
        return None
    s, start = to_text(s), int(num(start))
    begin = start - 1 if start > 0 else (len(s) + start if start < 0 else 0)
    if length is None:
        return s[max(begin, 0):]
    length = int(num(length))
    if start == 0:
        length -= 1
    end = begin + length
    return s[max(begin, 0):max(end, 0)] if length >= 0 else s[max(end, 0):max(begin, 0)]


def _coalesce(*a):
    return next((v for v in a if v is not None), None)


def _nullsafe(fn):
    return lambda v, *rest: None if v is None else fn(v, *rest)


FUNCS = {   # name -> (callable, min_args, max_args)
    "upper": (_nullsafe(lambda s: to_text(s).upper()), 1, 1),
    "lower": (_nullsafe(lambda s: to_text(s).lower()), 1, 1),
    "length": (_nullsafe(lambda s: len(to_text(s))), 1, 1),
    "abs": (_nullsafe(lambda x: abs(num(x))), 1, 1),
    "round": (_round, 1, 2),
    "substr": (_substr, 2, 3),
    "trim": (_nullsafe(lambda s: to_text(s).strip(" ")), 1, 1),
    "coalesce": (_coalesce, 2, 99),
    "ifnull": (_coalesce, 2, 2),
    "nullif": (lambda a, b: None if cmp_vals(a, b) == 0 and a is not None else a, 2, 2),
    "typeof": (lambda v: ("null", "integer", "text")[_rank(v)] if not isinstance(v, float) else "real", 1, 1),
}


def like_regex(pattern):
    out = []
    for ch in to_text(pattern):
        out.append(".*" if ch == "%" else "." if ch == "_" else re.escape(ch))
    return re.compile("".join(out), re.S | re.I)


def agg_compute(name, argf, distinct, rows):
    if argf is None:
        return len(rows)
    vals = [v for v in (argf(r, None) for r in rows) if v is not None]
    if distinct:
        vals = list(dict.fromkeys(vals))
    if name == "count":
        return len(vals)
    if not vals:
        return None
    if name == "sum":
        return sum(num(v) for v in vals)
    if name == "avg":
        return sum(num(v) for v in vals) / len(vals)
    return min(vals, key=sort_key) if name == "min" else max(vals, key=sort_key)


def conjuncts(e):
    if e is None:
        return []
    if e[0] == "and":
        return conjuncts(e[1]) + conjuncts(e[2])
    return [e]


def find_aggs(e, out):
    """Collect aggregate nodes (in first-seen order) into the dict `out`."""
    if not isinstance(e, tuple):
        return
    if e and e[0] == "agg":
        if e[2] is not None and contains_agg(e[2]):
            raise SQLError("aggregate functions cannot be nested")
        out.setdefault(e, len(out))
        return
    for part in e:
        if isinstance(part, tuple):
            if part and isinstance(part[0], tuple):      # tuple of nodes (IN list, CASE whens)
                for sub in part:
                    find_aggs(sub, out)
            else:
                find_aggs(part, out)


def contains_agg(e):
    found = {}
    find_aggs(e, found)
    return bool(found)


# ───────────────────────────── Storage ─────────────────────────────

_INF = float("inf")
_NOCONST = object()
_DEFAULT = object()
_FLIP = {"=": "=", "<": ">", "<=": ">=", ">": "<", ">=": "<="}


class Column:
    __slots__ = ("name", "key", "type", "notnull", "unique", "pk", "default")

    def __init__(self, d):
        self.name, self.key, self.type = d.name, d.name.lower(), d.type
        self.notnull, self.unique, self.pk, self.default = d.notnull, d.unique, d.pk, d.default


class Index:
    """A sorted array of (key, rowid): O(log n) lookup and range scans via bisect."""

    def __init__(self, name, col, pos, unique):
        self.name, self.col, self.pos, self.unique = name, col, pos, unique
        self.keys = []

    def add(self, row, rid):
        k = row[self.pos]
        if k is not None:
            bisect.insort(self.keys, (k, rid))

    def remove(self, row, rid):
        k = row[self.pos]
        if k is not None:
            i = bisect.bisect_left(self.keys, (k, rid))
            del self.keys[i]

    def range(self, lo, lo_inc, hi, hi_inc):
        ks = self.keys
        a = 0 if lo is None else bisect.bisect_left(ks, (lo,)) if lo_inc else bisect.bisect_right(ks, (lo, _INF))
        b = len(ks) if hi is None else bisect.bisect_right(ks, (hi, _INF)) if hi_inc else bisect.bisect_left(ks, (hi,))
        return [rid for _, rid in ks[a:b]]


class Table:
    def __init__(self, name, defs):
        self.name = name
        self.columns = [Column(d) for d in defs]
        self.colidx = {c.key: i for i, c in enumerate(self.columns)}
        self.rows = {}
        self.indexes = {}
        self.next_rowid = 1
        self.pk = next((i for i, c in enumerate(self.columns) if c.pk), None)
        for i, c in enumerate(self.columns):
            if c.pk or c.unique:
                self.add_index(Index(f"auto_{name}_{c.key}", c.key, i, True))

    def ddl(self):
        parts = []
        for c in self.columns:
            p = f"{c.name} {c.type}" + (" PRIMARY KEY" if c.pk else "") + (" NOT NULL" if c.notnull and not c.pk else "")
            p += " UNIQUE" if c.unique else ""
            if c.default is not None:
                p += f" DEFAULT {c.default!r}"
            parts.append(p)
        return f"CREATE TABLE {self.name} ({', '.join(parts)})"

    def add_index(self, ix):
        for rid, row in self.rows.items():
            if ix.unique and row[ix.pos] is not None and any(k == row[ix.pos] for k, _ in ix.keys[-1:]):
                raise ConstraintError(f"UNIQUE constraint failed: {self.name}.{ix.col}")
            ix.add(row, rid)
        self.indexes[ix.name] = ix

    def index_for(self, col):
        best = None
        for ix in self.indexes.values():
            if ix.col == col and (best is None or ix.unique):
                best = ix
        return best

    def coerce(self, c, v):
        if v is None:
            return None
        try:
            if c.type == "INTEGER":
                if isinstance(v, int):
                    return v
                f = float(v)
                if f == int(f):
                    return int(f)
                raise ValueError
            if c.type == "REAL":
                return float(v)
        except (ValueError, OverflowError):
            raise ConstraintError(f"datatype mismatch: {v!r} is not a valid {c.type} for {self.name}.{c.name}")
        return v if isinstance(v, str) else to_text(v)

    def prepare(self, values, auto):
        row = []
        for c, v in zip(self.columns, values):
            if auto and v is None and c.pk and c.type == "INTEGER":
                keys = self.index_for(c.key).keys
                v = keys[-1][0] + 1 if keys else 1
            v = self.coerce(c, v)
            if v is None and (c.notnull or c.pk):
                raise ConstraintError(f"NOT NULL constraint failed: {self.name}.{c.name}")
            row.append(v)
        return row

    def check_unique(self, row):
        for ix in self.indexes.values():
            k = row[ix.pos]
            if ix.unique and k is not None:
                i = bisect.bisect_left(ix.keys, (k,))
                if i < len(ix.keys) and ix.keys[i][0] == k:
                    raise ConstraintError(f"UNIQUE constraint failed: {self.name}.{ix.col}")

    def link(self, rid, row):
        self.rows[rid] = row
        for ix in self.indexes.values():
            ix.add(row, rid)

    def insert(self, values):
        row = self.prepare(values, auto=True)
        self.check_unique(row)
        rid = self.next_rowid
        self.next_rowid += 1
        self.link(rid, row)
        return rid

    def delete_row(self, rid):
        row = self.rows.pop(rid)
        for ix in self.indexes.values():
            ix.remove(row, rid)
        return row

    def update(self, rid, values):
        old = self.rows[rid]
        row = self.prepare(values, auto=False)
        for ix in self.indexes.values():
            ix.remove(old, rid)
        try:
            self.check_unique(row)
        except ConstraintError:
            for ix in self.indexes.values():
                ix.add(old, rid)
            raise
        self.link(rid, row)
        return old

    def restore(self, rid, old):
        cur = self.rows.get(rid)
        if cur is not None:
            for ix in self.indexes.values():
                ix.remove(cur, rid)
        self.link(rid, old)


class Scope:
    """Maps (table alias, column) to positions in a flattened joined row."""

    def __init__(self):
        self.cols = []   # (alias, key, display name)

    def add(self, alias, table):
        self.cols.extend((alias, c.key, c.name) for c in table.columns)

    def copy(self):
        s = Scope()
        s.cols = list(self.cols)
        return s

    def resolve(self, qual, name):
        hits = [i for i, (a, k, _) in enumerate(self.cols) if k == name and (qual is None or a == qual)]
        if not hits:
            raise UnknownColumn(f"no such column: {qual + '.' if qual else ''}{name}")
        if len(hits) > 1:
            raise AmbiguousColumn(f"ambiguous column name: {name}")
        return hits[0]


class Result:
    def __init__(self, columns=(), rows=(), rowcount=0, message="", lastrowid=None):
        self.columns, self.rows = list(columns), list(rows)
        self.rowcount, self.message, self.lastrowid = rowcount, message, lastrowid

    def __iter__(self):
        return iter(self.rows)

    def __len__(self):
        return len(self.rows)

    def scalar(self):
        return self.rows[0][0] if self.rows else None


MUTATING = (P.Insert, P.Update, P.Delete, P.CreateTable, P.CreateIndex, P.DropTable)


class Database:
    """An in-memory SQL database; pass a path to make it durable (write-ahead log of committed SQL)."""

    def __init__(self, path=None, sync=True):
        self.tables = {}
        self.undo = []
        self.in_txn = False
        self.txn_log = []
        self.params = ()
        self.path, self.sync, self._log = path, sync, None
        if path:
            if os.path.exists(path):
                self._replay(path)
            self._log = open(path, "a", encoding="utf-8")

    # ── public API ──
    def execute(self, sql, params=()):
        stmts = P.parse_script(sql)
        if len(stmts) > 1 and params:
            raise SQLError("parameters are only supported for a single statement")
        result = Result()
        for stmt, nparams, text in stmts:
            result = self._run(stmt, nparams, text, tuple(params))
        return result

    def run_all(self, sql):
        """Execute a multi-statement script and return one Result per statement."""
        return [self._run(stmt, nparams, text, ()) for stmt, nparams, text in P.parse_script(sql)]

    def schema(self):
        """CREATE statements that rebuild the current structure (without data)."""
        out = []
        for t in self.tables.values():
            out.append(t.ddl())
            out.extend(f"CREATE {'UNIQUE ' if ix.unique else ''}INDEX {ix.name} ON {t.name} ({ix.col})"
                       for ix in t.indexes.values() if not ix.name.startswith("auto_"))
        return out

    def close(self):
        if self.in_txn:
            self._rollback()
        if self._log:
            self._log.close()
            self._log = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ── durability ──
    def _write_log(self, entries):
        if self._log is None or not entries:
            return
        rec = {"tx": [{"sql": s, "params": list(p)} for s, p in entries]}
        self._log.write(json.dumps(rec) + "\n")
        self._log.flush()
        if self.sync:
            os.fsync(self._log.fileno())

    def _replay(self, path):
        with open(path, "rb") as f:
            data = f.read()
        pos = good = 0
        while pos < len(data):
            nl = data.find(b"\n", pos)
            if nl == -1:
                break                                  # torn tail: record never completed
            try:
                rec = json.loads(data[pos:nl])
            except ValueError:
                if nl + 1 >= len(data):
                    break                              # torn final record
                raise CorruptError(f"{path}: corrupt record at byte {pos}")
            for item in rec["tx"]:
                for stmt, nparams, _ in P.parse_script(item["sql"]):
                    self._dispatch(stmt, tuple(item["params"]))
            self.undo.clear()
            pos = good = nl + 1
        if good < len(data):
            with open(path, "r+b") as f:
                f.truncate(good)

    # ── statement runner (atomic per statement; BEGIN/COMMIT group them) ──
    def _undo_to(self, mark):
        while len(self.undo) > mark:
            self.undo.pop()()

    def _rollback(self):
        self._undo_to(0)
        self.in_txn = False
        self.txn_log = []

    def _run(self, stmt, nparams, text, params):
        if len(params) != nparams:
            raise SQLError(f"statement expects {nparams} parameter(s) but {len(params)} were given")
        if isinstance(stmt, P.Txn):
            if stmt.action == "begin":
                if self.in_txn:
                    raise SQLError("cannot start a transaction within a transaction")
                self.in_txn, self.txn_log = True, []
                self.undo.clear()
            elif not self.in_txn:
                raise SQLError(f"cannot {stmt.action}: no transaction is active")
            elif stmt.action == "rollback":
                self._rollback()
            else:
                try:
                    self._write_log(self.txn_log)
                except BaseException:
                    self._rollback()
                    raise
                self.in_txn, self.txn_log = False, []
                self.undo.clear()
            return Result(message=stmt.action)
        mark = len(self.undo)
        try:
            result = self._dispatch(stmt, params)
            if isinstance(stmt, MUTATING):
                if self.in_txn:
                    self.txn_log.append((text, params))
                else:
                    self._write_log([(text, params)])
        except BaseException:
            self._undo_to(mark)
            raise
        if not self.in_txn:
            self.undo.clear()
        return result

    def _dispatch(self, stmt, params):
        self.params = params
        if isinstance(stmt, P.Select):
            return self._select(stmt, [])
        if isinstance(stmt, P.Explain):
            plan = []
            self._select(stmt.select, plan)
            return Result(["plan"], [(line,) for line in plan])
        return {P.Insert: self._insert, P.Update: self._update, P.Delete: self._delete,
                P.CreateTable: self._create_table, P.CreateIndex: self._create_index,
                P.DropTable: self._drop_table}[type(stmt)](stmt)

    # ── DDL ──
    def table(self, name):
        t = self.tables.get(name)
        if t is None:
            raise SQLError(f"no such table: {name}")
        return t

    def _create_table(self, s):
        key = s.name
        if key in self.tables:
            if s.if_not_exists:
                return Result(message="exists")
            raise SQLError(f"table {s.name} already exists")
        self.tables[key] = Table(key, s.columns)
        self.undo.append(lambda: self.tables.pop(key, None))
        return Result(message="table created")

    def _drop_table(self, s):
        t = self.tables.pop(s.name, None)
        if t is None:
            if s.if_exists:
                return Result(message="no such table")
            raise SQLError(f"no such table: {s.name}")
        self.undo.append(lambda: self.tables.__setitem__(s.name, t))
        return Result(message="table dropped")

    def _create_index(self, s):
        t = self.table(s.table)
        if any(s.name in tb.indexes for tb in self.tables.values()):
            raise SQLError(f"index {s.name} already exists")
        if s.column not in t.colidx:
            raise UnknownColumn(f"no such column: {s.column}")
        t.add_index(Index(s.name, s.column, t.colidx[s.column], s.unique))
        self.undo.append(lambda: t.indexes.pop(s.name, None))
        return Result(message="index created")

    # ── DML ──
    def _insert(self, s):
        t = self.table(s.table)
        cols = s.columns or [c.key for c in t.columns]
        for c in cols:
            if c not in t.colidx:
                raise UnknownColumn(f"no such column: {c}")
        if len(set(cols)) != len(cols):
            raise SQLError("duplicate column in INSERT")
        if s.select is not None:
            source = self._select(s.select, []).rows
        else:
            empty = Scope()
            source = [[_DEFAULT if e == ("default",) else self._compile(e, empty)([], None) for e in row]
                      for row in s.rows]
        last = None
        for vals in source:
            if len(vals) != len(cols):
                raise SQLError(f"{len(cols)} column(s) but {len(vals)} value(s) supplied")
            row = [c.default for c in t.columns]
            for c, v in zip(cols, vals):
                if v is not _DEFAULT:
                    row[t.colidx[c]] = v
            last = t.insert(row)
            self.undo.append(lambda t=t, rid=last: t.delete_row(rid))
        return Result(rowcount=len(source), message=f"{len(source)} row(s) inserted", lastrowid=last)

    def _matching(self, t, where, plan=None):
        scope = Scope()
        scope.add(t.name, t)
        pairs = self._candidates(t, t.name, where, set(), plan if plan is not None else [])
        if where is not None:
            f = self._compile(where, scope)
            pairs = [(rid, r) for rid, r in pairs if truth(f(r, None))]
        return scope, pairs

    def _update(self, s):
        t = self.table(s.table)
        scope, pairs = self._matching(t, s.where)
        sets = []
        for col, e in s.sets:
            if col not in t.colidx:
                raise UnknownColumn(f"no such column: {col}")
            sets.append((t.colidx[col], self._compile(e, scope)))
        for rid, row in pairs:
            new = list(row)
            for i, f in sets:
                new[i] = f(row, None)
            old = t.update(rid, new)
            self.undo.append(lambda t=t, rid=rid, old=old: t.restore(rid, old))
        return Result(rowcount=len(pairs), message=f"{len(pairs)} row(s) updated")

    def _delete(self, s):
        t = self.table(s.table)
        _, pairs = self._matching(t, s.where)
        for rid, _ in pairs:
            row = t.delete_row(rid)
            self.undo.append(lambda t=t, rid=rid, row=row: t.link(rid, row))
        return Result(rowcount=len(pairs), message=f"{len(pairs)} row(s) deleted")

    # ── planner ──
    def _pick_index(self, t, alias, where, other_cols):
        if where is None or not t.indexes:
            return None

        def base_col(e):
            if e[0] != "col":
                return None
            q, name = e[1], e[2]
            if name not in t.colidx:
                return None
            return name if (q == alias or (q is None and name not in other_cols)) else None

        def const(e):
            if e[0] == "lit":
                return e[1]
            return self.params[e[1]] if e[0] == "param" else _NOCONST

        by_col = {}

        def note(col, op, val):
            if val is None or val is _NOCONST or isinstance(val, bool):
                return
            ctype = t.columns[t.colidx[col]].type
            if (ctype == "TEXT") != isinstance(val, str):
                return          # mixed-type comparison: leave it to the filter
            d = by_col.setdefault(col, {})
            if op == "=":
                d.setdefault("eq", val)
            elif op in (">", ">="):
                d.setdefault("lo", (val, op == ">="))
            else:
                d.setdefault("hi", (val, op == "<="))

        for c in conjuncts(where):
            if c[0] == "bin" and c[1] in _FLIP:
                col, val = base_col(c[2]), const(c[3])
                op = c[1]
                if col is None or val is _NOCONST:
                    col, val, op = base_col(c[3]), const(c[2]), _FLIP[c[1]]
                if col is not None:
                    note(col, op, val)
            elif c[0] == "between" and not c[4]:
                col = base_col(c[1])
                if col is not None:
                    note(col, ">=", const(c[2]))
                    note(col, "<=", const(c[3]))

        best, best_score = None, 0
        for col, d in by_col.items():
            ix = t.index_for(col)
            if ix is None:
                continue
            if "eq" in d:
                score, plan = 10 + ix.unique, (d["eq"], True, d["eq"], True, f"{col} = ?")
            elif "lo" in d or "hi" in d:
                lo, hi = d.get("lo"), d.get("hi")
                parts = ([f"{col} {'>=' if lo[1] else '>'} ?"] if lo else []) + ([f"{col} {'<=' if hi[1] else '<'} ?"] if hi else [])
                score = 2 if (lo and hi) else 1
                plan = (lo[0] if lo else None, lo[1] if lo else True, hi[0] if hi else None, hi[1] if hi else True, " AND ".join(parts))
            else:
                continue
            if score > best_score:
                best, best_score = (ix, *plan), score
        return best

    def _candidates(self, t, alias, where, other_cols, plan):
        pick = self._pick_index(t, alias, where, other_cols)
        if pick is None:
            plan.append(f"SCAN {t.name}")
            return list(t.rows.items())
        ix, lo, lo_inc, hi, hi_inc, desc = pick
        plan.append(f"SEARCH {t.name} USING INDEX {ix.name} ({desc})")
        return [(rid, t.rows[rid]) for rid in sorted(ix.range(lo, lo_inc, hi, hi_inc))]

    # ── expression compiler: AST -> closure f(row, aggregate_values) ──
    def _compile(self, e, scope, aggs=None):
        k = e[0]
        C = lambda x: self._compile(x, scope, aggs)  # noqa: E731
        if k == "lit":
            v = e[1]
            return lambda r, a: v
        if k == "param":
            v = self.params[e[1]]
            return lambda r, a: v
        if k == "col":
            i = scope.resolve(e[1], e[2])
            return lambda r, a: r[i]
        if k == "neg":
            f = C(e[1])
            return lambda r, a: None if (v := f(r, a)) is None else -num(v)
        if k == "not":
            f = C(e[1])
            return lambda r, a: None if (v := truth(f(r, a))) is None else int(not v)
        if k == "bin":
            fn, l, rr = BIN_OPS[e[1]], C(e[2]), C(e[3])
            return lambda r, a: fn(l(r, a), rr(r, a))
        if k == "and":
            l, rr = C(e[1]), C(e[2])

            def and_(r, a):
                x = truth(l(r, a))
                if x is False:
                    return 0
                y = truth(rr(r, a))
                if y is False:
                    return 0
                return None if x is None or y is None else 1
            return and_
        if k == "or":
            l, rr = C(e[1]), C(e[2])

            def or_(r, a):
                x = truth(l(r, a))
                if x is True:
                    return 1
                y = truth(rr(r, a))
                if y is True:
                    return 1
                return None if x is None or y is None else 0
            return or_
        if k == "isnull":
            f, neg = C(e[1]), e[2]
            return lambda r, a: int((f(r, a) is None) != neg)
        if k == "like":
            f, pf, neg = C(e[1]), C(e[2]), e[3]
            cache = {}

            def like(r, a):
                v, p = f(r, a), pf(r, a)
                if v is None or p is None:
                    return None
                rx = cache.get(p) or cache.setdefault(p, like_regex(p))
                return int(bool(rx.fullmatch(to_text(v))) != neg)
            return like
        if k == "in":
            f, items, neg = C(e[1]), [C(x) for x in e[2]], e[3]

            def in_(r, a):
                v = f(r, a)
                if v is None:
                    return None
                saw_null = False
                for it in items:
                    w = it(r, a)
                    if w is None:
                        saw_null = True
                    elif cmp_vals(v, w) == 0:
                        return int(not neg)
                return None if saw_null else int(neg)
            return in_
        if k == "between":
            neg = e[4]
            ge = C(("bin", ">=", e[1], e[2]))
            le = C(("bin", "<=", e[1], e[3]))

            def between(r, a):
                x, y = truth(ge(r, a)), truth(le(r, a))
                res = 0 if (x is False or y is False) else None if (x is None or y is None) else 1
                return res if (res is None or not neg) else 1 - res
            return between
        if k == "func":
            name = e[1]
            if name not in FUNCS:
                raise SQLError(f"no such function: {name}")
            fn, lo, hi = FUNCS[name]
            if not lo <= len(e[2]) <= hi:
                raise SQLError(f"wrong number of arguments to {name}()")
            args = [C(x) for x in e[2]]
            return lambda r, a: fn(*[g(r, a) for g in args])
        if k == "agg":
            if aggs is None or e not in aggs:
                raise SQLError(f"aggregate {e[1]}() is not allowed here")
            i = aggs[e]
            return lambda r, a: a[i]
        if k == "case":
            operand = C(e[1]) if e[1] is not None else None
            whens = [(C(c), C(v)) for c, v in e[2]]
            other = C(e[3]) if e[3] is not None else None

            def case(r, a):
                base = operand(r, a) if operand else None
                for c, v in whens:
                    cv = c(r, a)
                    hit = (base is not None and cv is not None and cmp_vals(base, cv) == 0) if operand else truth(cv)
                    if hit:
                        return v(r, a)
                return other(r, a) if other else None
            return case
        raise SQLError(f"cannot evaluate expression {e[0]}")

    # ── SELECT ──
    def _select(self, s, plan):
        scope = Scope()
        if s.from_ is None:
            rows, other_cols = [[]], set()
            if s.joins:
                raise SQLError("JOIN requires a FROM table")
        else:
            base = self.table(s.from_[0])
            alias = s.from_[1] or base.name
            joined = [(self.table(j[1]), j[2] or j[1]) for j in s.joins]
            if len({alias} | {a for _, a in joined}) != len(joined) + 1:
                raise SQLError("duplicate table alias in FROM clause (use AS to rename)")
            other_cols = {c.key for tb, _ in joined for c in tb.columns}
            scope.add(alias, base)
            rows = [r for _, r in self._candidates(base, alias, s.where, other_cols, plan)]
            for (kind, _, _, on), (rt, ralias) in zip(s.joins, joined):
                rows, scope = self._join(rows, scope, kind, rt, ralias, on, plan)
        if s.where is not None:
            f = self._compile(s.where, scope)
            rows = [r for r in rows if truth(f(r, None))]
            plan.append("FILTER")

        # expand * and resolve select items
        items = []
        for expr, alias_, text in s.items:
            if expr[0] == "star":
                found = False
                for i, (a, k, disp) in enumerate(scope.cols):
                    if expr[1] is None or a == expr[1]:
                        items.append((("col", a, k), disp, disp, i))
                        found = True
                if not found:
                    raise SQLError("no such table: " + expr[1] if expr[1] else "SELECT * requires a FROM clause")
            else:
                items.append((expr, alias_, text, None))
        names = [(a or text) for _, a, text, _ in items]
        item_exprs = [x[0] for x in items]
        aliases = {(a or "").lower(): i for i, (_, a, _, _) in enumerate(items) if a}

        def resolve_ref(e):
            """GROUP BY / ORDER BY may name an output column by position or alias."""
            if e[0] == "lit" and isinstance(e[1], int) and not isinstance(e[1], bool):
                if not 1 <= e[1] <= len(items):
                    raise SQLError(f"ORDER/GROUP BY position {e[1]} is out of range")
                return items[e[1] - 1][0]
            if e[0] == "col" and e[1] is None and e[2] in aliases:
                try:
                    scope.resolve(None, e[2])
                except UnknownColumn:
                    return item_exprs[aliases[e[2]]]
            return e

        group = tuple(resolve_ref(g) for g in s.group)
        order = [(resolve_ref(e), d) for e, d in s.order]
        agg_nodes = {}
        for e in item_exprs + ([s.having] if s.having is not None else []) + [e for e, _ in order]:
            find_aggs(e, agg_nodes)
        grouped = bool(agg_nodes) or bool(group)

        out_rows, key_rows = [], []
        if grouped:
            plan.append("GROUP BY" if group else "AGGREGATE")
            gfs = [self._compile(g, scope) for g in group]
            arg_fns = {n: (self._compile(n[2], scope) if n[2] is not None else None) for n in agg_nodes}
            buckets = {}
            for r in rows:
                buckets.setdefault(tuple(g(r, None) for g in gfs), []).append(r)
            if not buckets and not group:
                buckets[()] = []
            fs = [self._compile(e, scope, agg_nodes) for e in item_exprs]
            hf = self._compile(s.having, scope, agg_nodes) if s.having is not None else None
            of = [self._compile(e, scope, agg_nodes) for e, _ in order]
            blank = [None] * len(scope.cols)
            for grp in buckets.values():
                aggvals = [None] * len(agg_nodes)
                for n, i in agg_nodes.items():
                    aggvals[i] = agg_compute(n[1], arg_fns[n], n[3], grp)
                first = grp[0] if grp else blank
                if hf is not None and not truth(hf(first, aggvals)):
                    continue
                out_rows.append(tuple(f(first, aggvals) for f in fs))
                key_rows.append([f(first, aggvals) for f in of])
        else:
            if s.having is not None:
                raise SQLError("HAVING requires GROUP BY or an aggregate")
            fs = [(lambda r, a, i=i: r[i]) if i is not None else self._compile(e, scope)
                  for e, _, _, i in items]
            of = [self._compile(e, scope) for e, _ in order]
            for r in rows:
                out_rows.append(tuple(f(r, None) for f in fs))
                key_rows.append([f(r, None) for f in of])

        if s.distinct:
            seen, keep = set(), []
            for i, row in enumerate(out_rows):
                if row not in seen:
                    seen.add(row)
                    keep.append(i)
            out_rows = [out_rows[i] for i in keep]
            key_rows = [key_rows[i] for i in keep]
            plan.append("DISTINCT")
        if order:
            idx = list(range(len(out_rows)))
            for ki in reversed(range(len(order))):
                idx.sort(key=lambda i, ki=ki: sort_key(key_rows[i][ki]), reverse=order[ki][1])
            out_rows = [out_rows[i] for i in idx]
            plan.append("SORT")
        if s.limit is not None or s.offset is not None:
            e0 = Scope()
            lim = self._compile(s.limit, e0)([], None) if s.limit is not None else -1
            off = self._compile(s.offset, e0)([], None) if s.offset is not None else 0
            if not all(isinstance(v, int) for v in (lim, off)):
                raise SQLError("LIMIT and OFFSET must be integers")
            out_rows = out_rows[max(off, 0):]
            out_rows = out_rows if lim < 0 else out_rows[:lim]
            plan.append("LIMIT")
        return Result(names, out_rows, rowcount=len(out_rows))

    def _join(self, left_rows, lscope, kind, rt, ralias, on, plan):
        rscope = Scope()
        rscope.add(ralias, rt)
        combined = lscope.copy()
        combined.cols.extend(rscope.cols)
        width = len(rscope.cols)
        right_rows = list(rt.rows.values())
        if on is not None:
            self._compile(on, combined)        # validates names, raises on ambiguity
        keys, residual = [], []
        for c in conjuncts(on):
            if c[0] == "bin" and c[1] == "=":
                for a, b in ((c[2], c[3]), (c[3], c[2])):
                    try:
                        keys.append((self._compile(a, lscope), self._compile(b, rscope)))
                        break
                    except UnknownColumn:
                        continue
                else:
                    residual.append(c)
            else:
                residual.append(c)
        rest = None
        if residual:
            expr = residual[0]
            for c in residual[1:]:
                expr = ("and", expr, c)
            rest = self._compile(expr, combined)
        out = []
        pad = [None] * width
        if keys:
            plan.append(f"HASH JOIN {rt.name} ({len(keys)} equality key(s))")
            table = {}
            for rr in right_rows:
                k = tuple(rk(rr, None) for _, rk in keys)
                if None not in k:
                    table.setdefault(k, []).append(rr)
            for lr in left_rows:
                k = tuple(lk(lr, None) for lk, _ in keys)
                matches = [] if None in k else table.get(k, [])
                hit = False
                for rr in matches:
                    row = lr + rr
                    if rest is None or truth(rest(row, None)):
                        out.append(row)
                        hit = True
                if not hit and kind == "left":
                    out.append(lr + pad)
        else:
            plan.append(f"NESTED LOOP JOIN {rt.name}")
            f = self._compile(on, combined) if on is not None else None
            for lr in left_rows:
                hit = False
                for rr in right_rows:
                    row = lr + rr
                    if f is None or truth(f(row, None)):
                        out.append(row)
                        hit = True
                if not hit and kind == "left":
                    out.append(lr + pad)
        return out, combined
