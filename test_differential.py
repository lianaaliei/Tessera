"""Differential tests: every query is run on Tessera and on the real SQLite, results must agree."""
import math
import random
import sqlite3
import unittest

from tessera import Database
from tessera.engine import sort_key

SCHEMA = [
    "CREATE TABLE t (id INTEGER PRIMARY KEY, a INTEGER, b INTEGER, r REAL, s TEXT, g TEXT)",
    "CREATE TABLE u (id INTEGER PRIMARY KEY, t_id INTEGER, v INTEGER)",
    "CREATE TABLE tg (t_id INTEGER, tag TEXT)",
]
INDEXES = [
    "CREATE INDEX t_a ON t (a)", "CREATE INDEX t_s ON t (s)", "CREATE INDEX t_r ON t (r)",
    "CREATE INDEX u_t ON u (t_id)", "CREATE INDEX u_v ON u (v)",
]
WORDS = ["apple", "banana", "cherry", "date", "Elder", "fig", "grape", "kiwi", "Lime", "", "ab", "ba"]


def make_data(seed=7):
    rnd = random.Random(seed)
    nul = lambda v, p=0.1: None if rnd.random() < p else v  # noqa: E731
    t = [(i, nul(rnd.randint(-5, 5)), nul(rnd.randint(0, 9)), nul(rnd.randint(-6, 6) / 2),
          nul(rnd.choice(WORDS)), rnd.choice("xyz")) for i in range(1, 301)]
    u = [(i, rnd.randint(1, 320), nul(rnd.randint(0, 100))) for i in range(1, 401)]
    tg = [(rnd.randint(1, 300), rnd.choice(["red", "green", "blue"])) for _ in range(150)]
    return t, u, tg


def close(a, b):
    if a is None or b is None:
        return a is b
    if isinstance(a, float) or isinstance(b, float):
        return isinstance(a, float) and isinstance(b, float) and math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)
    return type(a) is type(b) and a == b


class Differential(unittest.TestCase):
    with_indexes = False

    @classmethod
    def setUpClass(cls):
        cls.ours, cls.lite = Database(), sqlite3.connect(":memory:")
        t, u, tg = make_data()
        stmts = SCHEMA + (INDEXES if cls.with_indexes else [])
        for s in stmts:
            cls.ours.execute(s)
            cls.lite.execute(s)
        for table, rows in (("t", t), ("u", u), ("tg", tg)):
            ph = ",".join("?" * len(rows[0]))
            for row in rows:
                cls.ours.execute(f"INSERT INTO {table} VALUES ({ph})", row)
            cls.lite.executemany(f"INSERT INTO {table} VALUES ({ph})", rows)

    def check(self, sql, params=(), ordered=None):
        if ordered is None:
            ordered = "ORDER BY" in sql.upper()
        ours = [tuple(r) for r in self.ours.execute(sql, params).rows]
        theirs = [tuple(r) for r in self.lite.execute(sql, params).fetchall()]
        if not ordered:
            key = lambda row: tuple(sort_key(v) for v in row)  # noqa: E731
            ours, theirs = sorted(ours, key=key), sorted(theirs, key=key)
        self.assertEqual(len(ours), len(theirs), f"row count differs for: {sql}")
        for ro, rt in zip(ours, theirs):
            self.assertEqual(len(ro), len(rt), sql)
            for x, y in zip(ro, rt):
                self.assertTrue(close(x, y), f"\n{sql}\n  tessera: {ro}\n  sqlite : {rt}")
        return len(ours)


QUERIES = [
    # projection & arithmetic (NULL propagation, integer division, modulo)
    "SELECT * FROM t ORDER BY id",
    "SELECT id, a + b, a - b, a * b FROM t ORDER BY id",
    "SELECT id, a / b, a % b, b / 2, r * 2, r / 2 FROM t ORDER BY id",
    "SELECT id, -a, -r, a + 1.5, ABS(a), ABS(r), ROUND(r), ROUND(a / 3.0) FROM t ORDER BY id",
    "SELECT id, s || '-' || g, UPPER(s), LOWER(s), LENGTH(s) FROM t ORDER BY id",
    "SELECT id, COALESCE(a, b, -1), IFNULL(s, 'none'), NULLIF(g, 'x') FROM t ORDER BY id",
    "SELECT id, CASE WHEN a > 0 THEN 'pos' WHEN a < 0 THEN 'neg' ELSE 'zero' END FROM t ORDER BY id",
    "SELECT id, CASE g WHEN 'x' THEN 1 WHEN 'y' THEN 2 END FROM t ORDER BY id",
    "SELECT id, SUBSTR(s, 2), SUBSTR(s, 1, 3), SUBSTR(s, -2) FROM t WHERE s IS NOT NULL ORDER BY id",
    "SELECT 1 + 2 * 3, 7 / 2, -7 / 2, 7 % 3, -7 % 3, 7.0 / 2, 1 / 0, 5 % 0, 'a' || 'b', NULL + 1",
    # filtering & three-valued logic
    "SELECT id FROM t WHERE a > 0 ORDER BY id",
    "SELECT id FROM t WHERE a = b ORDER BY id",
    "SELECT id FROM t WHERE a != b ORDER BY id",
    "SELECT id FROM t WHERE NOT (a > 0) ORDER BY id",
    "SELECT id FROM t WHERE a > 0 AND b < 5 ORDER BY id",
    "SELECT id FROM t WHERE a > 0 OR b < 5 ORDER BY id",
    "SELECT id FROM t WHERE (a > 0 AND b < 5) OR s = 'fig' ORDER BY id",
    "SELECT id FROM t WHERE a BETWEEN -2 AND 2 ORDER BY id",
    "SELECT id FROM t WHERE a NOT BETWEEN -2 AND 2 ORDER BY id",
    "SELECT id FROM t WHERE a IN (1, 2, 3) ORDER BY id",
    "SELECT id FROM t WHERE a NOT IN (1, 2, 3) ORDER BY id",
    "SELECT id FROM t WHERE a IN (1, NULL) ORDER BY id",
    "SELECT id FROM t WHERE a NOT IN (1, NULL) ORDER BY id",
    "SELECT id FROM t WHERE a IS NULL ORDER BY id",
    "SELECT id FROM t WHERE a IS NOT NULL AND s IS NULL ORDER BY id",
    "SELECT id FROM t WHERE s LIKE 'a%' ORDER BY id",
    "SELECT id FROM t WHERE s LIKE '%E%' ORDER BY id",
    "SELECT id FROM t WHERE s LIKE '_a%' ORDER BY id",
    "SELECT id FROM t WHERE s NOT LIKE '%e%' ORDER BY id",
    "SELECT id FROM t WHERE s = 'fig' ORDER BY id",
    "SELECT id FROM t WHERE s > 'cherry' ORDER BY id",
    "SELECT id FROM t WHERE s >= 'Lime' AND s < 'kiwi' ORDER BY id",
    "SELECT id FROM t WHERE r > 0.5 ORDER BY id",
    "SELECT id FROM t WHERE r >= -1 AND r <= 1.5 ORDER BY id",
    "SELECT id FROM t WHERE a > 1.5 ORDER BY id",
    "SELECT id FROM t WHERE 2 < a ORDER BY id",
    "SELECT id FROM t WHERE id = 42",
    "SELECT id FROM t WHERE id > 290 ORDER BY id",
    "SELECT id FROM t WHERE id BETWEEN 10 AND 14 ORDER BY id",
    "SELECT id FROM t WHERE id < 5 OR id > 297 ORDER BY id",
    "SELECT id FROM t WHERE a = 3 AND s LIKE '%a%' ORDER BY id",
    # distinct / order / limit
    "SELECT DISTINCT g FROM t ORDER BY g",
    "SELECT DISTINCT a, b FROM t ORDER BY a, b",
    "SELECT DISTINCT s FROM t ORDER BY s",
    "SELECT id, a, b FROM t ORDER BY a, b DESC, id",
    "SELECT id, a FROM t ORDER BY a DESC, id DESC",
    "SELECT id, s FROM t ORDER BY s, id",
    "SELECT id, r FROM t ORDER BY r DESC, id",
    "SELECT id, a + b AS total FROM t ORDER BY total DESC, id",
    "SELECT id, a FROM t ORDER BY 2, 1",
    "SELECT id FROM t ORDER BY id LIMIT 10",
    "SELECT id FROM t ORDER BY id LIMIT 10 OFFSET 295",
    "SELECT id FROM t ORDER BY id LIMIT 3, 4",
    "SELECT id FROM t ORDER BY id DESC LIMIT 5",
    "SELECT id FROM t ORDER BY id LIMIT 0",
    # aggregates
    "SELECT COUNT(*), COUNT(a), COUNT(s), COUNT(DISTINCT a), COUNT(DISTINCT s) FROM t",
    "SELECT SUM(a), SUM(b), SUM(r), AVG(a), AVG(r), MIN(a), MAX(a), MIN(s), MAX(s), MIN(r), MAX(r) FROM t",
    "SELECT COUNT(*), SUM(a), AVG(a), MIN(a), MAX(s) FROM t WHERE id < 0",
    "SELECT SUM(a) + 1, MAX(a) - MIN(a), COUNT(*) * 2 FROM t",
    "SELECT g, COUNT(*), SUM(a), AVG(b), MIN(s), MAX(r) FROM t GROUP BY g ORDER BY g",
    "SELECT g, a, COUNT(*) FROM t GROUP BY g, a ORDER BY g, a",
    "SELECT a % 3, COUNT(*) FROM t GROUP BY a % 3 ORDER BY 1",
    "SELECT s, COUNT(*) AS c FROM t GROUP BY s ORDER BY c DESC, s",
    "SELECT g, COUNT(*) FROM t GROUP BY g HAVING COUNT(*) > 95 ORDER BY g",
    "SELECT g, SUM(a) FROM t GROUP BY g HAVING SUM(a) > 0 AND MAX(b) >= 8 ORDER BY g",
    "SELECT g, COUNT(DISTINCT s) FROM t GROUP BY g ORDER BY g",
    "SELECT g, COUNT(*) FROM t WHERE a > 0 GROUP BY 1 ORDER BY g",
    "SELECT a, COUNT(*) AS n FROM t GROUP BY a ORDER BY n DESC, a LIMIT 3",
    "SELECT g, MAX(a) - MIN(a) AS spread FROM t GROUP BY g ORDER BY spread, g",
    "SELECT COUNT(*) FROM t GROUP BY g ORDER BY 1",
    # joins
    "SELECT t.id, u.id, u.v FROM t JOIN u ON u.t_id = t.id ORDER BY t.id, u.id",
    "SELECT t.id, u.id FROM t INNER JOIN u ON t.id = u.t_id WHERE u.v > 90 ORDER BY t.id, u.id",
    "SELECT t.id, u.id FROM t LEFT JOIN u ON u.t_id = t.id ORDER BY t.id, u.id",
    "SELECT t.id FROM t LEFT JOIN u ON u.t_id = t.id WHERE u.id IS NULL ORDER BY t.id",
    "SELECT t.id, u.id FROM t LEFT JOIN u ON u.t_id = t.id AND u.v > 50 WHERE t.id < 30 ORDER BY t.id, u.id",
    "SELECT t.id, u.id FROM t JOIN u ON u.t_id = t.id AND u.v > t.b * 10 ORDER BY t.id, u.id",
    "SELECT t.id, u.id FROM t JOIN u ON u.v > t.id AND t.id < 5 ORDER BY t.id, u.id",
    "SELECT t.id, u.id FROM t JOIN u ON t.a = u.v ORDER BY t.id, u.id",
    "SELECT x.id, y.id FROM t x JOIN t y ON x.a = y.b AND x.id < y.id WHERE x.id < 40 ORDER BY x.id, y.id",
    "SELECT t.id, u.id, tg.tag FROM t JOIN u ON u.t_id = t.id JOIN tg ON tg.t_id = t.id ORDER BY t.id, u.id, tg.tag",
    "SELECT t.id, u.id, tg.tag FROM t LEFT JOIN u ON u.t_id = t.id LEFT JOIN tg ON tg.t_id = t.id WHERE t.id < 25 ORDER BY t.id, u.id, tg.tag",
    "SELECT t.g, COUNT(*), SUM(u.v) FROM t JOIN u ON u.t_id = t.id GROUP BY t.g ORDER BY t.g",
    "SELECT t.id, COUNT(u.id) AS n FROM t LEFT JOIN u ON u.t_id = t.id GROUP BY t.id ORDER BY n DESC, t.id LIMIT 10",
    "SELECT t.id, COUNT(u.id) FROM t LEFT JOIN u ON u.t_id = t.id GROUP BY t.id HAVING COUNT(u.id) = 0 ORDER BY t.id",
    "SELECT tg.tag, COUNT(DISTINCT t.s), AVG(t.a) FROM tg JOIN t ON t.id = tg.t_id GROUP BY tg.tag ORDER BY tg.tag",
    "SELECT t.id, u.v FROM t, u WHERE u.t_id = t.id AND u.v > 95 ORDER BY t.id, u.id",
    "SELECT * FROM tg JOIN t ON t.id = tg.t_id WHERE t.id < 10 ORDER BY t.id, tg.tag",
]


def add_tests(cls):
    for i, q in enumerate(QUERIES):
        setattr(cls, f"test_query_{i:03d}", lambda self, q=q: self.check(q))
    return cls


@add_tests
class DifferentialNoIndex(Differential):
    with_indexes = False


@add_tests
class DifferentialWithIndex(Differential):
    with_indexes = True

    def test_parameters_and_indexes(self):
        for params, q in [((42,), "SELECT id, s FROM t WHERE id = ?"),
                          ((3, 9), "SELECT id FROM t WHERE a >= ? AND a < ? ORDER BY id"),
                          (("fig",), "SELECT id FROM t WHERE s = ? ORDER BY id"),
                          ((2.5,), "SELECT id FROM t WHERE r > ? ORDER BY id"),
                          ((7,), "SELECT t.id, u.id FROM t JOIN u ON u.t_id = t.id WHERE t.id = ? ORDER BY u.id")]:
            self.check(q, params)


# ───────────────────────────── expression fuzzer ─────────────────────────────

class Gen:
    INTS, NUMS, TEXTS = ["a", "b"], ["a", "b", "r"], ["s", "g"]

    def __init__(self, seed):
        self.r = random.Random(seed)

    def num(self, d):
        r = self.r
        if d <= 0 or r.random() < 0.25:
            return r.choice([r.choice(self.NUMS), str(r.randint(-3, 4)), "NULL", f"{r.randint(-4, 4) / 2}"])
        k = r.randrange(9)
        if k <= 3:
            return f"({self.num(d-1)} {r.choice('+-*')} {self.num(d-1)})"
        if k == 4:
            return f"({self.num(d-1)} / {self.num(d-1)})"
        if k == 5:
            return f"({self.intx(d-1)} % {self.intx(d-1)})"
        if k == 6:
            return f"{r.choice(['-', 'ABS', 'ROUND'])}({self.num(d-1)})".replace("-(", "-(")
        if k == 7:
            return f"COALESCE({self.num(d-1)}, {self.num(d-1)})"
        return f"CASE WHEN {self.boolx(d-1)} THEN {self.num(d-1)} ELSE {self.num(d-1)} END"

    def intx(self, d):
        r = self.r
        if d <= 0 or r.random() < 0.5:
            return r.choice([r.choice(self.INTS), str(r.randint(-3, 4))])
        return f"({self.intx(d-1)} {r.choice('+-*')} {self.intx(d-1)})"

    def text(self, d):
        r = self.r
        if d <= 0 or r.random() < 0.5:
            return r.choice([r.choice(self.TEXTS), "'fig'", "'ab'", "''", "NULL"])
        k = r.randrange(3)
        if k == 0:
            return f"({self.text(d-1)} || {self.text(d-1)})"
        return f"{r.choice(['UPPER', 'LOWER'])}({self.text(d-1)})"

    def boolx(self, d):
        r = self.r
        k = r.randrange(11) if d > 0 else r.randrange(5)
        if k <= 1:
            return f"({self.num(d-1)} {r.choice(['=', '!=', '<', '<=', '>', '>='])} {self.num(d-1)})"
        if k == 2:
            return f"({self.num(d-1)} IS {r.choice(['', 'NOT '])}NULL)"
        if k == 3:
            return f"({self.text(d-1)} {r.choice(['=', '<', '>=', '!='])} {self.text(d-1)})"
        if k == 4:
            pat = r.choice(["'a%'", "'%e'", "'_a%'", "'ab'", "'%'", "'%i%'", "''"])
            return f"({self.text(d-1)} {r.choice(['', 'NOT '])}LIKE {pat})"
        if k == 5:
            return f"({self.boolx(d-1)} AND {self.boolx(d-1)})"
        if k == 6:
            return f"({self.boolx(d-1)} OR {self.boolx(d-1)})"
        if k == 7:
            return f"(NOT {self.boolx(d-1)})"
        if k == 8:
            return f"({self.num(d-1)} {r.choice(['', 'NOT '])}BETWEEN {self.num(d-1)} AND {self.num(d-1)})"
        if k == 9:
            items = ", ".join(self.num(0) for _ in range(r.randint(1, 4)))
            return f"({self.num(d-1)} {r.choice(['', 'NOT '])}IN ({items}))"
        return f"({self.text(d-1)} {r.choice(['', 'NOT '])}IN ('fig', 'ab', NULL, {self.text(0)}))"


class Fuzz(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = DifferentialNoIndex
        cls.d.setUpClass()

    def test_random_expressions_agree_with_sqlite(self):
        gen = Gen(2024)
        helper = self.d("check")
        n = 0
        for i in range(700):
            e = gen.boolx(3) if i % 2 else gen.num(3)
            for q in (f"SELECT id, {e} FROM t ORDER BY id", f"SELECT id FROM t WHERE {e} ORDER BY id"):
                helper.check(q)
                n += 1
        self.assertEqual(n, 1400)

    def test_random_expressions_use_indexes_consistently(self):
        """The same fuzzed WHERE clauses must give identical answers with indexes present."""
        idx = DifferentialWithIndex
        idx.setUpClass()
        helper = idx("check")
        gen = Gen(99)
        for _ in range(250):
            col = gen.r.choice(["a", "r", "s", "id"])
            val = {"a": str(gen.r.randint(-5, 5)), "r": str(gen.r.randint(-6, 6) / 2),
                   "s": repr(gen.r.choice(WORDS)), "id": str(gen.r.randint(1, 300))}[col]
            op = gen.r.choice(["=", "<", "<=", ">", ">="])
            extra = gen.boolx(2)
            helper.check(f"SELECT id FROM t WHERE {col} {op} {val} AND {extra} ORDER BY id")
            helper.check(f"SELECT id FROM t WHERE {col} {op} {val} ORDER BY id")


if __name__ == "__main__":
    unittest.main(verbosity=1)
