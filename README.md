# Tessera

A small **SQL database engine written from scratch in pure Python** (standard library only, ~1,800 lines):
tokenizer, recursive-descent parser, cost-aware planner with indexes and hash joins, undo-log transactions,
and a crash-safe write-ahead log.

    python -m tessera                      # interactive shell (in-memory)
    python -m tessera app.tdb              # durable database in a file
    python -m tessera -f examples/demo.sql # run a script
    python -m tessera app.tdb -c "SELECT 1 + 1"

```python
from tessera import Database

db = Database("app.tdb")                   # omit the path for an in-memory database
db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT UNIQUE, age INTEGER)")
db.execute("INSERT INTO users (email, age) VALUES (?, ?)", ("ada@example.com", 36))
for row in db.execute("SELECT email FROM users WHERE age > ?", (30,)):
    print(row)
```

## SQL supported

| Area | Details |
|------|---------|
| DDL | `CREATE TABLE` (INTEGER / REAL / TEXT, `PRIMARY KEY`, `NOT NULL`, `UNIQUE`, `DEFAULT`), `CREATE [UNIQUE] INDEX`, `DROP TABLE`, `IF [NOT] EXISTS` |
| DML | `INSERT` (multi-row, `DEFAULT`, `INSERT ... SELECT`), `UPDATE`, `DELETE`, `?` parameters everywhere |
| Queries | `WHERE`, `DISTINCT`, `GROUP BY` / `HAVING`, `ORDER BY` (expressions, aliases, positions, `DESC`), `LIMIT` / `OFFSET` |
| Joins | `INNER`, `LEFT`, `CROSS` / comma, any number of tables, self-joins with aliases |
| Expressions | `+ - * / % \|\|`, comparisons, `AND OR NOT`, `IS [NOT] NULL`, `[NOT] LIKE / IN / BETWEEN`, `CASE`, full three-valued NULL logic |
| Functions | `COUNT SUM AVG MIN MAX` (`DISTINCT` too), `UPPER LOWER LENGTH ABS ROUND SUBSTR TRIM COALESCE IFNULL NULLIF TYPEOF` |
| Transactions | `BEGIN` / `COMMIT` / `ROLLBACK`, including DDL; every statement is atomic on its own |
| Introspection | `EXPLAIN SELECT ...` shows index use and join strategy |

## How it works

- **Planner** - picks an index for equality or range predicates on the base table (preferring equality and
  unique indexes), uses a **hash join** for equi-joins and a nested loop otherwise, and only trusts an index when
  the literal's type matches the column, so an index can never change an answer.
- **Indexes** - sorted arrays queried with `bisect`: O(log n) lookups and range scans.
- **Atomicity** - every mutation records an inverse operation in an undo log. A failed statement unwinds to its
  start; `ROLLBACK` unwinds the whole transaction, including indexes and DDL.
- **Durability** - committed statements are appended to a JSON-lines log and `fsync`ed. On open the log is
  replayed; a torn final record (power cut mid-write) is discarded and the file repaired, while corruption in the
  middle raises `CorruptError` instead of silently losing data.
- **Prepared-statement cache** - parsed ASTs are cached by SQL text (they hold only parameter *positions*).

## Testing: it is checked against real SQLite

    python -m unittest discover -s tests -t .      # 211 tests, ~4 s

- **Differential tests** run 87 queries (aggregates, GROUP BY/HAVING, every join type, NULL handling, LIKE,
  CASE, ...) on Tessera and on SQLite 3 and require identical results, **with and without indexes**.
- **Expression fuzzer**: 700 randomly generated expressions (each checked as a SELECT value and as a WHERE filter) over NULLable int/real/text columns must agree
  with SQLite, including three-valued logic, integer division and modulo rules.
- **DML fuzzer**: 1,500 random INSERT/UPDATE/DELETE/BEGIN/COMMIT/ROLLBACK statements with constraint violations must
  fail on exactly the same statements as SQLite and leave identical tables, and every index must equal a
  rebuild from the rows after each check.
- **Mutation testing**: 23 deliberately injected bugs (wrong NULL ordering, floor division, broken rollback,
  off-by-one index bounds, ...) were each caught by the suite.

## Performance (pure Python, 50,000 rows, one core)

| Operation | Result |
|-----------|--------|
| Bulk `INSERT` in one transaction | ~73,000 rows/s |
| Lookup by indexed column / primary key | ~13,000 - 35,000 queries/s |
| Same lookup without an index | ~20 queries/s (full scan) |
| Hash join 50k x 2k + GROUP BY | ~0.2 s |

`python examples/bench.py` reproduces these.

## Limits (by design)

No subqueries, `UNION`, window functions, `ALTER TABLE`, views, or triggers. The planner only indexes the base table
of a query and does not use indexes for `ORDER BY`. The write-ahead log is never compacted. Single process, no
locking. Integers are unbounded Python ints (SQLite overflows to float at 2^63). `TEXT` columns convert numbers to
text on insert, and a numeric column rejects non-numeric text, which is stricter than SQLite.
