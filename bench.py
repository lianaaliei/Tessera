"""Quick performance tour:  python examples/bench.py"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from tessera import Database  # noqa: E402

N = 50_000


def timed(label, fn, n=1):
    t0 = time.perf_counter()
    out = fn()
    dt = time.perf_counter() - t0
    print(f"  {label:<46} {dt * 1000:9.1f} ms" + (f"   ({n / dt:,.0f}/s)" if n > 1 else ""))
    return out


db = Database()
db.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER, amount REAL, status TEXT)")
db.execute("CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, region TEXT)")
print(f"Tessera benchmark ({N:,} orders)")

def load():
    db.execute("BEGIN")
    for i in range(2000):
        db.execute("INSERT INTO customers (name, region) VALUES (?, ?)", (f"cust{i}", ("north", "south", "east", "west")[i % 4]))
    for i in range(N):
        db.execute("INSERT INTO orders (customer_id, amount, status) VALUES (?, ?, ?)",
                   (i % 2000 + 1, (i % 997) / 10, ("new", "paid", "shipped")[i % 3]))
    db.execute("COMMIT")

timed("bulk insert (one transaction)", load, N + 2000)
q = "SELECT COUNT(*), SUM(amount) FROM orders WHERE customer_id = ?"
timed("200 lookups by customer_id, no index", lambda: [db.execute(q, (c,)) for c in range(1, 201)], 200)
timed("CREATE INDEX on orders(customer_id)", lambda: db.execute("CREATE INDEX o_cust ON orders (customer_id)"))
timed("200 lookups by customer_id, with index", lambda: [db.execute(q, (c,)) for c in range(1, 201)], 200)
timed("1,000 primary-key lookups", lambda: [db.execute("SELECT * FROM orders WHERE id = ?", (i * 37 % N + 1,)) for i in range(1000)], 1000)
r = timed("hash join + GROUP BY (50k x 2k rows)", lambda: db.execute(
    "SELECT c.region, COUNT(*), ROUND(AVG(o.amount), 2) FROM orders o JOIN customers c ON c.id = o.customer_id "
    "WHERE o.status = 'paid' GROUP BY c.region ORDER BY c.region"))
for row in r.rows:
    print("     ", row)
timed("ORDER BY amount DESC LIMIT 10 (full sort of 50k)", lambda: db.execute("SELECT id, amount FROM orders ORDER BY amount DESC, id LIMIT 10"))
print("\nplan:", db.execute("EXPLAIN " + q.replace("?", "5")).rows)
