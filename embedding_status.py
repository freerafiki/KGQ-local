"""Quick health check of the embedding bookkeeping: per label/field, how many
nodes are `done`, `todo` or never flagged (`missing`), plus an ASCII bar chart.

Read-only, runs in seconds.

Usage:
    python3 embedding_status.py
"""

import os

from dotenv import load_dotenv
from neo4j import GraphDatabase, RoutingControl

from embedding_text import FIELD_STATUS

BAR = 40
MARK = {"done": "#", "todo": "!", "missing": "."}

load_dotenv()
driver = GraphDatabase.driver(os.getenv("NEO4J_URI"),
                              auth=(os.getenv("NEO4J_USER"), os.getenv("NEO4J_PASSWORD")))
DB = os.getenv("NEO4J_GRAPH")

try:
    print(f"{'node / embedding field':<45} {'total':>6} {'done':>6} {'todo':>6} {'missing':>8}   chart")
    print("-" * 100)
    grand = {"done": 0, "todo": 0, "missing": 0}
    for label, fields in FIELD_STATUS.items():
        for field, status in fields.items():
            rows, _, _ = driver.execute_query(
                f"MATCH (n:{label}) "
                f"RETURN coalesce(n.`{status}`, 'missing') AS s, count(*) AS c",
                database_=DB, routing_=RoutingControl.READ)
            counts = {"done": 0, "todo": 0, "missing": 0}
            for r in rows:
                counts[r["s"] if r["s"] in counts else "todo"] += r["c"]   # any non-done flag counts as todo
            total = sum(counts.values())
            for k in grand:
                grand[k] += counts[k]

            # proportional segments, done first
            bar = ""
            for k in ("done", "todo", "missing"):
                bar += MARK[k] * round(BAR * counts[k] / total) if total else ""
            bar = bar.ljust(BAR, " ")
            pct = 100 * counts["done"] / total if total else 0
            print(f"{label + ' / ' + field:<45} {total:>6} {counts['done']:>6} "
                  f"{counts['todo']:>6} {counts['missing']:>8}   "
                  f"[{bar}] {pct:5.1f}%")
    print("-" * 100)
    print(f"{'ALL FIELDS':<45} {sum(grand.values()):>6} {grand['done']:>6} "
          f"{grand['todo']:>6} {grand['missing']:>8}")
    print("\nlegend: '#' = done, '!' = todo, '.' = missing (never flagged)")
    if grand["todo"]:
        print("\n-> re-run insert_embeddings.py to process the `todo` nodes")
finally:
    driver.close()
