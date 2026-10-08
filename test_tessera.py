"""Behavioural tests: constraints, atomicity, transactions, planner, persistence, shell."""
import io
import json
import os
import random
import sqlite3
import tempfile
import time
import unittest

from tessera import ConstraintError, CorruptError, Database, SQLError
from tessera.engine import UnknownColumn
from tessera.repl import format_table, repl


def index_consistent(db):
    """White-box invariant: every index must be exactly derivable from the rows."""
    for t in db.tables.values():
        for ix in t.indexes.values():
            want = sorted((row[ix.pos], rid) for rid, row in t.rows.items() if row[ix.pos] is not None)
            if ix.keys != want:
                return False
    return True


class Constraints(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.db.execute("CREATE TABLE u (id INTEGER PRIMARY KEY, email TEXT UNIQUE, name TEXT NOT NULL, age INTEGER DEFAULT 18)")

    def test_primary_key_autoincrement_and_defaults(self):
        self.db.execute("INSERT INTO u (email, name) VALUES ('a@x', 'A'), ('b@x', 'B')")
        self.assertEqual(self.db.execute("SELECT id, age FROM u ORDER BY id").rows, [(1, 18), (2, 18)])
        self.db.execute("INSERT INTO u (id, name) VALUES (10, 'C')")
        self.assertEqual(self.db.execute("INSERT INTO u (name) VALUES ('D')").lastrowid is not None, True)
        self.assertEqual(self.db.execute("SELECT MAX(id) FROM u").scalar(), 11)

    def test_not_null_unique_and_type_errors(self):
        self.db.execute("INSERT INTO u (email, name) VALUES ('a@x', 'A')")
        with self.assertRaisesRegex(ConstraintError, "NOT NULL"):
            self.db.execute("INSERT INTO u (email) VALUES ('z@x')")
        with self.assertRaisesRegex(ConstraintError, "UNIQUE"):
            self.db.execute("INSERT INTO u (email, name) VALUES ('a@x', 'dup')")
        with self.assertRaisesRegex(ConstraintError, "UNIQUE"):
            self.db.execute("INSERT INTO u (id, name) VALUES (1, 'dup pk')")
        with self.assertRaisesRegex(ConstraintError, "datatype"):
            self.db.execute("INSERT INTO u (name, age) VALUES ('x', 'not a number')")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM u").scalar(), 1)

    def test_nulls_do_not_collide_in_unique_columns(self):
        self.db.execute("INSERT INTO u (name) VALUES ('a'), ('b'), ('c')")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM u WHERE email IS NULL").scalar(), 3)

    def test_type_coercion(self):
        self.db.execute("INSERT INTO u (name, age) VALUES (42, '30')")
        self.assertEqual(self.db.execute("SELECT name, age FROM u").rows, [("42", 30)])

    def test_multi_row_insert_is_atomic(self):
        self.db.execute("INSERT INTO u (email, name) VALUES ('a@x', 'A')")
        with self.assertRaises(ConstraintError):
            self.db.execute("INSERT INTO u (email, name) VALUES ('b@x', 'B'), ('c@x', 'C'), ('a@x', 'dup')")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM u").scalar(), 1)
        self.assertTrue(index_consistent(self.db))

    def test_failed_update_restores_everything(self):
        self.db.execute("INSERT INTO u (email, name) VALUES ('a@x','A'), ('b@x','B'), ('c@x','C')")
        before = self.db.execute("SELECT * FROM u ORDER BY id").rows
        with self.assertRaises(ConstraintError):
            self.db.execute("UPDATE u SET name = 'changed', email = 'same@x'")   # 2nd row collides
        self.assertEqual(self.db.execute("SELECT * FROM u ORDER BY id").rows, before)
        self.assertTrue(index_consistent(self.db))

    def test_unique_index_creation_fails_on_existing_duplicates(self):
        self.db.execute("INSERT INTO u (name, age) VALUES ('a', 5), ('b', 5)")
        with self.assertRaises(ConstraintError):
            self.db.execute("CREATE UNIQUE INDEX u_age ON u (age)")
        self.db.execute("CREATE UNIQUE INDEX u_name ON u (name)")
        with self.assertRaises(ConstraintError):
            self.db.execute("INSERT INTO u (name) VALUES ('a')")

    def test_ddl_errors(self):
        for sql in ("CREATE TABLE u (x INTEGER)", "CREATE TABLE q (a INTEGER, a TEXT)",
                    "CREATE TABLE q (a BLOB)", "CREATE TABLE q (a INTEGER PRIMARY KEY, b INTEGER PRIMARY KEY)",
                    "DROP TABLE nope", "CREATE INDEX i ON nope (x)", "CREATE INDEX i ON u (nope)"):
            with self.assertRaises(SQLError, msg=sql):
                self.db.execute(sql)
        self.db.execute("CREATE TABLE IF NOT EXISTS u (x INTEGER)")
        self.db.execute("DROP TABLE IF EXISTS nope")


class Transactions(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.db.execute("CREATE TABLE a (id INTEGER PRIMARY KEY, v INTEGER)")
        self.db.execute("INSERT INTO a (v) VALUES (10), (20), (30)")
        self.db.execute("CREATE INDEX a_v ON a (v)")

    def count(self):
        return self.db.execute("SELECT COUNT(*) FROM a").scalar()

    def test_commit_and_rollback(self):
        self.db.execute("BEGIN")
        self.db.execute("INSERT INTO a (v) VALUES (40)")
        self.assertEqual(self.count(), 4)
        self.db.execute("ROLLBACK")
        self.assertEqual(self.count(), 3)
        self.db.execute("BEGIN")
        self.db.execute("DELETE FROM a WHERE v > 10")
        self.db.execute("COMMIT")
        self.assertEqual(self.count(), 1)

    def test_rollback_restores_data_and_indexes(self):
        before = self.db.execute("SELECT * FROM a ORDER BY id").rows
        self.db.execute("BEGIN")
        self.db.execute("UPDATE a SET v = v + 1")
        self.db.execute("DELETE FROM a WHERE id = 2")
        self.db.execute("INSERT INTO a (v) VALUES (99)")
        self.db.execute("ROLLBACK")
        self.assertEqual(self.db.execute("SELECT * FROM a ORDER BY id").rows, before)
        self.assertEqual(self.db.execute("SELECT id FROM a WHERE v = 20").rows, [(2,)])   # via restored index
        self.assertTrue(index_consistent(self.db))

    def test_ddl_rolls_back(self):
        self.db.execute("BEGIN")
        self.db.execute("CREATE TABLE tmp (x INTEGER)")
        self.db.execute("DROP TABLE a")
        self.db.execute("ROLLBACK")
        self.assertIn("a", self.db.tables)
        self.assertNotIn("tmp", self.db.tables)
        self.assertEqual(self.count(), 3)

    def test_failed_statement_inside_txn_keeps_earlier_work(self):
        self.db.execute("BEGIN")
        self.db.execute("INSERT INTO a (v) VALUES (40)")
        with self.assertRaises(SQLError):
            self.db.execute("INSERT INTO a (id, v) VALUES (1, 0)")    # PK clash: statement undone only
        self.assertEqual(self.count(), 4)
        self.db.execute("COMMIT")
        self.assertEqual(self.count(), 4)

    def test_misuse(self):
        for sql in ("COMMIT", "ROLLBACK"):
            with self.assertRaisesRegex(SQLError, "no transaction"):
                self.db.execute(sql)
        self.db.execute("BEGIN")
        with self.assertRaisesRegex(SQLError, "within a transaction"):
            self.db.execute("BEGIN")


class Planner(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.db.execute("CREATE TABLE p (id INTEGER PRIMARY KEY, a INTEGER, s TEXT)")
        self.db.execute("CREATE TABLE q (id INTEGER PRIMARY KEY, p_id INTEGER)")
        self.db.execute("CREATE INDEX p_a ON p (a)")

    def plan(self, sql, params=()):
        return [r[0] for r in self.db.execute("EXPLAIN " + sql, params).rows]

    def test_index_choices(self):
        self.assertIn("SEARCH p USING INDEX auto_p_id (id = ?)", self.plan("SELECT * FROM p WHERE id = 5"))
        self.assertIn("SEARCH p USING INDEX p_a (a > ? AND a <= ?)", self.plan("SELECT * FROM p WHERE a > 1 AND a <= 9"))
        self.assertIn("SEARCH p USING INDEX p_a (a >= ? AND a <= ?)", self.plan("SELECT * FROM p WHERE a BETWEEN 1 AND 9"))
        self.assertEqual(self.plan("SELECT * FROM p WHERE 5 = id")[0].split("(")[0], "SEARCH p USING INDEX auto_p_id ")
        self.assertTrue(self.plan("SELECT * FROM p WHERE a = ?", (3,))[0].startswith("SEARCH"))

    def test_prefers_equality_and_unique(self):
        plan = self.plan("SELECT * FROM p WHERE a > 1 AND id = 7")
        self.assertIn("auto_p_id", plan[0])

    def test_falls_back_to_scan(self):
        for sql in ("SELECT * FROM p WHERE a = 1 OR a = 2", "SELECT * FROM p WHERE s = 'x'",
                    "SELECT * FROM p WHERE a + 1 = 3", "SELECT * FROM p WHERE a = 'text'",
                    "SELECT * FROM p WHERE a = NULL", "SELECT * FROM p WHERE NOT a = 1"):
            self.assertEqual(self.plan(sql)[0], "SCAN p", sql)

    def test_join_strategies(self):
        self.assertTrue(any("HASH JOIN" in l for l in self.plan("SELECT * FROM p JOIN q ON q.p_id = p.id")))
        self.assertTrue(any("NESTED LOOP" in l for l in self.plan("SELECT * FROM p JOIN q ON q.p_id > p.id")))

    def test_ambiguous_columns_do_not_use_index(self):
        # `id` exists in both tables: the unqualified reference is ambiguous, never silently indexed.
        with self.assertRaisesRegex(SQLError, "ambiguous"):
            self.db.execute("SELECT * FROM p JOIN q ON q.p_id = p.id WHERE id = 1")

    def test_index_speeds_up_lookups(self):
        self.db.execute("BEGIN")
        for i in range(30000):
            self.db.execute("INSERT INTO p (a, s) VALUES (?, 'x')", (i,))
        self.db.execute("COMMIT")
        t0 = time.perf_counter()
        for i in range(0, 30000, 300):
            self.db.execute("SELECT * FROM p WHERE a = ?", (i,))
        fast = time.perf_counter() - t0
        t0 = time.perf_counter()
        for i in range(0, 30000, 3000):                       # 10x fewer queries on the scan path
            self.db.execute("SELECT * FROM p WHERE a + 0 = ?", (i,))
        slow = (time.perf_counter() - t0) * 10
        self.assertGreater(slow / fast, 5, f"index {fast:.4f}s vs scan {slow:.4f}s")


class ErrorsAndParams(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, a INTEGER, s TEXT)")
        self.db.execute("INSERT INTO t (a, s) VALUES (1,'x'), (2,'y'), (3,'z')")

    def test_parameters(self):
        self.assertEqual(self.db.execute("SELECT s FROM t WHERE a IN (?, ?) ORDER BY a", (1, 3)).rows, [("x",), ("z",)])
        self.assertEqual(self.db.execute("SELECT a FROM t ORDER BY a LIMIT ? OFFSET ?", (1, 1)).rows, [(2,)])
        with self.assertRaisesRegex(SQLError, "parameter"):
            self.db.execute("SELECT * FROM t WHERE a = ?")
        with self.assertRaisesRegex(SQLError, "parameter"):
            self.db.execute("SELECT * FROM t", (1,))

    def test_cached_statements_rebind_parameters(self):
        q = "SELECT s FROM t WHERE a = ?"
        self.assertEqual([self.db.execute(q, (v,)).rows for v in (1, 3, 2, 1, 99)],
                         [[("x",)], [("z",)], [("y",)], [("x",)], []])
        ins = "INSERT INTO t (a, s) VALUES (?, ?)"
        for i in range(5):
            self.db.execute(ins, (100 + i, f"s{i}"))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM t WHERE a >= 100").scalar(), 5)
        with self.assertRaises(SQLError):          # a failing statement text must not poison the cache
            self.db.execute("SELECT FROM")
        with self.assertRaises(SQLError):
            self.db.execute("SELECT FROM")

    def test_sql_injection_is_inert_with_parameters(self):
        evil = "x'; DROP TABLE t; --"
        self.db.execute("INSERT INTO t (a, s) VALUES (?, ?)", (9, evil))
        self.assertEqual(self.db.execute("SELECT s FROM t WHERE a = 9").scalar(), evil)
        self.assertIn("t", self.db.tables)

    def test_error_messages(self):
        cases = {
            "SELECT * FROM nope": "no such table",
            "SELECT nope FROM t": "no such column",
            "SELECT t.nope FROM t": "no such column",
            "SELECT x.a FROM t": "no such column",
            "SELECT a FROM t WHERE COUNT(*) > 1": "not allowed here",
            "SELECT SUM(COUNT(a)) FROM t": "nested",
            "SELECT a FROM t HAVING a > 1": "HAVING",
            "SELECT FROM t": "unexpected",
            "SELECT a FROM": "expected",
            "SELECT 'unterminated": "unexpected character",
            "SELECT a FROM t WHERE": "unexpected",
            "SELEC 1": "unexpected",
            "SELECT bogus(a) FROM t": "no such function",
            "SELECT ABS(1, 2)": "wrong number",
            "SELECT a FROM t ORDER BY 9": "out of range",
            "SELECT a FROM t LIMIT 'x'": "integers",
            "INSERT INTO t (a, s) VALUES (1)": "value",
            "INSERT INTO t (nope) VALUES (1)": "no such column",
            "UPDATE t SET nope = 1": "no such column",
            "SELECT * FROM t a JOIN t a ON a.id = a.id": "alias",
            "SELECT 1 FROM t JOIN t ON 1": "alias",
        }
        for sql, msg in cases.items():
            with self.assertRaisesRegex(SQLError, msg, msg=sql):
                self.db.execute(sql)

    def test_state_intact_after_errors(self):
        for _ in range(3):
            with self.assertRaises(SQLError):
                self.db.execute("INSERT INTO t (id, a) VALUES (1, 0)")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM t").scalar(), 3)

    def test_misc_semantics(self):
        q = lambda s: self.db.execute(s).rows  # noqa: E731
        self.assertEqual(q("SELECT 1 FROM t WHERE 0"), [])
        self.assertEqual(q("SELECT COUNT(*) FROM t WHERE a > 100"), [(0,)])
        self.assertEqual(q("SELECT 'a' || 1 || 2.5"), [("a12.5",)])
        self.assertEqual(q("SELECT TYPEOF(1), TYPEOF(1.5), TYPEOF('x'), TYPEOF(NULL)"), [("integer", "real", "text", "null")])
        self.assertEqual(q("SELECT a FROM t WHERE s LIKE 'X'"), [(1,)])           # case-insensitive
        self.assertEqual(q("SELECT s AS \"Weird Name\" FROM t LIMIT 1"), [("x",)])
        self.assertEqual(q("select A, S from T order by A desc limit 1"), [(3, "z")])   # keywords/identifiers: any case
        with self.assertRaises(UnknownColumn):
            q("SELECT zzz FROM t")


class Persistence(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "db.log")

    def tearDown(self):
        self.dir.cleanup()

    def reopen(self):
        return Database(self.path, sync=False)

    def test_survives_reopen(self):
        with self.reopen() as db:
            db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, s TEXT UNIQUE, n REAL)")
            db.execute("CREATE INDEX t_n ON t (n)")
            db.execute("INSERT INTO t (s, n) VALUES (?, ?)", ("it's \"quoted\"\nnewline ✓ Ω", 1.5))
            db.execute("INSERT INTO t (s, n) VALUES (?, ?)", (None, None))
            db.execute("UPDATE t SET n = n * 2 WHERE n IS NOT NULL")
            db.execute("DELETE FROM t WHERE s IS NULL")
        with self.reopen() as db:
            self.assertEqual(db.execute("SELECT s, n FROM t").rows, [("it's \"quoted\"\nnewline ✓ Ω", 3.0)])
            self.assertTrue(index_consistent(db))
            with self.assertRaises(ConstraintError):
                db.execute("INSERT INTO t (s) VALUES (?)", ("it's \"quoted\"\nnewline ✓ Ω",))
            self.assertIn("t_n", db.tables["t"].indexes)

    def test_only_committed_work_is_durable(self):
        with self.reopen() as db:
            db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v INTEGER)")
            db.execute("INSERT INTO t (v) VALUES (1)")
            db.execute("BEGIN")
            db.execute("INSERT INTO t (v) VALUES (2)")
            db.execute("ROLLBACK")
            db.execute("BEGIN")
            db.execute("INSERT INTO t (v) VALUES (3)")
            db.execute("COMMIT")
            db.execute("BEGIN")
            db.execute("INSERT INTO t (v) VALUES (4)")          # never committed: process "crashes"
            with self.assertRaises(SQLError):
                db.execute("INSERT INTO t (id, v) VALUES (1, 0)")
            db._log.close()                                       # simulate the crash
        with self.reopen() as db:
            self.assertEqual(db.execute("SELECT v FROM t ORDER BY v").rows, [(1,), (3,)])

    def test_failed_statements_are_not_logged(self):
        with self.reopen() as db:
            db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
            for _ in range(3):
                with self.assertRaises(SQLError):
                    db.execute("INSERT INTO t (id) VALUES (NULL), (1), (1)")
        with open(self.path) as f:
            self.assertEqual(len(f.readlines()), 1)

    def test_torn_tail_is_discarded(self):
        with self.reopen() as db:
            db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v INTEGER)")
            db.execute("INSERT INTO t (v) VALUES (1)")
        with open(self.path, "ab") as f:
            f.write(b'{"tx": [{"sql": "INSERT INTO t (v) VALUES (2)", "par')    # power cut mid-write
        with self.reopen() as db:
            self.assertEqual(db.execute("SELECT v FROM t").rows, [(1,)])
            db.execute("INSERT INTO t (v) VALUES (3)")
        with self.reopen() as db:                                               # file was repaired, still valid
            self.assertEqual(db.execute("SELECT v FROM t ORDER BY v").rows, [(1,), (3,)])

    def test_corruption_in_the_middle_is_an_error(self):
        with self.reopen() as db:
            db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
            db.execute("INSERT INTO t (id) VALUES (1)")
            db.execute("INSERT INTO t (id) VALUES (2)")
        lines = open(self.path).read().split("\n")
        lines[1] = lines[1][:20] + "###"
        open(self.path, "w").write("\n".join(lines))
        with self.assertRaises(CorruptError):
            self.reopen()

    def test_log_format_is_plain_json(self):
        with self.reopen() as db:
            db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        rec = json.loads(open(self.path).readline())
        self.assertEqual(rec["tx"][0]["sql"], "CREATE TABLE t (id INTEGER PRIMARY KEY)")


class Shell(unittest.TestCase):
    def run_shell(self, script):
        out = io.StringIO()
        repl(Database(), io.StringIO(script), out)
        return out.getvalue()

    def test_table_formatting(self):
        text = format_table(["id", "name"], [(1, "ada"), (22, None)])
        self.assertEqual(text.splitlines()[3], "|  1 | ada  |")
        self.assertIn("NULL", text)

    def test_multiline_statements_dot_commands_and_errors(self):
        out = self.run_shell("CREATE TABLE t (id INTEGER PRIMARY KEY,\n  v TEXT);\nINSERT INTO t (v) VALUES ('a;b');\n"
                             "SELECT * FROM t;\n.tables\n.schema\nSELECT nope FROM t;\nSELECT 1 +;\n.bogus\n")
        for expected in ("| a;b |", "(1 row)", "t\n", "CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT);",
                         "Error: no such column: nope", "Error: unexpected", "unknown command .bogus"):
            self.assertIn(expected, out)


class DmlDifferential(unittest.TestCase):
    """Random INSERT/UPDATE/DELETE/transaction streams must leave Tessera and SQLite in the same state,
    and must agree on which statements fail."""

    def test_random_dml_stream(self):
        rnd = random.Random(31337)
        ours = Database()
        lite = sqlite3.connect(":memory:", isolation_level=None)
        ddl = "CREATE TABLE d (id INTEGER PRIMARY KEY, code TEXT UNIQUE, n INTEGER NOT NULL, note TEXT)"
        for db in (ours, lite):
            db.execute(ddl)
            db.execute("CREATE INDEX d_n ON d (n)")
        codes = [f"c{i}" for i in range(25)]
        failures = 0
        for step in range(1500):
            k = rnd.random()
            if k < 0.35:
                sql, p = "INSERT INTO d (code, n, note) VALUES (?, ?, ?)", (rnd.choice(codes + [None]), rnd.choice([rnd.randint(0, 20), None]), rnd.choice(["x", None]))
            elif k < 0.42:
                sql, p = "INSERT INTO d (id, code, n) VALUES (?, ?, ?)", (rnd.randint(1, 60), rnd.choice(codes), rnd.randint(0, 20))
            elif k < 0.55:
                sql, p = "UPDATE d SET n = n + ?, note = ? WHERE n > ?", (rnd.randint(-3, 3), rnd.choice(["u", None]), rnd.randint(0, 20))
            elif k < 0.62:
                sql, p = "UPDATE d SET code = ? WHERE id = ?", (rnd.choice(codes + [None]), rnd.randint(1, 60))
            elif k < 0.67:
                sql, p = "UPDATE d SET id = ? WHERE id = ?", (rnd.randint(1, 80), rnd.randint(1, 60))
            elif k < 0.80:
                sql, p = "DELETE FROM d WHERE n BETWEEN ? AND ?", (lo := rnd.randint(0, 20), lo + rnd.randint(0, 2))
            elif k < 0.85:
                sql, p = "DELETE FROM d WHERE id = ?", (rnd.randint(1, 60),)
            elif k < 0.90:
                sql, p = "BEGIN", ()
            elif k < 0.95:
                sql, p = "COMMIT", ()
            else:
                sql, p = "ROLLBACK", ()
            e1 = e2 = None
            try:
                ours.execute(sql, p)
            except SQLError as e:
                e1 = e
            try:
                lite.execute(sql, p)
            except sqlite3.Error as e:
                e2 = e
            self.assertEqual(e1 is None, e2 is None, f"step {step}: {sql} {p}\n  tessera: {e1!r}\n  sqlite : {e2!r}")
            failures += e1 is not None
            if step % 25 == 0:
                self.assertEqual(ours.execute("SELECT * FROM d ORDER BY id").rows,
                                 lite.execute("SELECT * FROM d ORDER BY id").fetchall(), f"state diverged at step {step}")
                self.assertTrue(index_consistent(ours), f"index corrupted at step {step}")
        self.assertEqual(ours.execute("SELECT * FROM d ORDER BY id").rows, lite.execute("SELECT * FROM d ORDER BY id").fetchall())
        self.assertGreater(failures, 100)      # the stream really exercises constraint failures


if __name__ == "__main__":
    unittest.main(verbosity=1)
