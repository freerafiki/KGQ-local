"""Re-attach node embeddings from a JSON file produced by export_embeddings.py.

Use after rebuilding / restoring the graph in a local database, so embeddings
are restored without recomputing them.

Matching is done on the sha256 hash of the node's CURRENT text (same builder as
export_embeddings.py / insert_embeddings.py): we recompute the hash for each
node right now, look the embedding up, and write it back by that node's current
elementId. Nodes whose content changed since export won't find a match and are
skipped (insert_embeddings.py will re-embed them). Status flags are set to
'done' for every re-attached field.

Usage:
    python3 import_embeddings.py [input.json]
"""

import argparse
import hashlib
import json
import os

from dotenv import load_dotenv
from neo4j import GraphDatabase, RoutingControl

from embedding_text import TEXT_BUILDERS, FIELD_STATUS

load_dotenv()
NEO4J_URI = os.getenv("NEO4J_URI")
NEO4J_USER = os.getenv("NEO4J_USER")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD")
NEO4J_GRAPH = os.getenv("NEO4J_GRAPH")


def import_embeddings(in_path: str) -> None:
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    try:
        with open(in_path) as fh:
            records = json.load(fh)

        # lookup: (label, field, key) -> vector
        lookup = {
            (r["label"], r["field"], r["key"]): r["vector"]
            for r in records
        }
        print(f"Loaded {len(records)} embeddings from {in_path}")

        matched = 0
        unmatched = 0
        for label, fields in TEXT_BUILDERS.items():
            status_map = FIELD_STATUS[label]
            for field, (raw_fields, builder) in fields.items():
                aliases = ", ".join(f"n.`{f}` AS `{f}`" for f in raw_fields)
                rows, _, _ = driver.execute_query(
                    f"MATCH (n:{label}) RETURN elementId(n) AS eid, {aliases}",
                    database_=NEO4J_GRAPH,
                    routing_=RoutingControl.READ,
                )
                writes = []
                for row in rows:
                    text = builder(*(row[f] for f in raw_fields))
                    if not text:
                        continue
                    key = hashlib.sha256(text.encode("utf-8")).hexdigest()
                    vec = lookup.get((label, field, key))
                    if vec is None:
                        unmatched += 1
                        continue
                    writes.append({"eid": row["eid"], "vector": vec})

                if writes:
                    _, summary, _ = driver.execute_query(
                        f"""
                        UNWIND $rows AS row
                        MATCH (n:{label}) WHERE elementId(n) = row.eid
                        CALL db.create.setNodeVectorProperty(n, '{field}', row.vector)
                        SET n.`{status_map[field]}` = 'done'
                        """,
                        rows=writes,
                        database_=NEO4J_GRAPH,
                        routing_=RoutingControl.WRITE,
                    )
                    matched += len(writes)
                    print(f"[{label}/{field}] restored {len(writes)} embeddings "
                          f"({summary.counters.properties_set} props set)")

        print(f"Import done: {matched} embeddings re-attached, "
              f"{unmatched} nodes with changed/absent text left to re-embed.")
    finally:
        driver.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", nargs="?", default="embeddings_export.json",
                        help="JSON file produced by export_embeddings.py "
                             "(default: embeddings_export.json)")
    args = parser.parse_args()
    import_embeddings(args.input)