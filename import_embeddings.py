"""Re-attach node embeddings from a JSON file produced by export_embeddings.py.

Use after rebuilding / restoring the graph in a local database, so embeddings
are restored without recomputing them.

The file starts with a meta record ({"meta": {"model", "dims", "created"}}).
It is checked against this machine's model identity BEFORE anything is written:
a file exported with a different model or dimension count is refused, since its
vectors would be silently incompatible. Files from older exports without a meta
record are accepted with a warning (each vector's length is still checked).

Matching is done on the sha256 hash of the node's CURRENT text (same builder as
export_embeddings.py / insert_embeddings.py): we recompute the hash for each
node right now, look the embedding up, and write it back by that node's current
elementId. Nodes whose content changed since export won't find a match and are
skipped (insert_embeddings.py will re-embed them). Status flags are set to
'done' for every re-attached field.

Run THIS before insert_embeddings.py on a fresh graph: re-attached fields are
flagged 'done', so the insert run then only computes what is still missing and
creates the indexes.

Usage:
    python3 import_embeddings.py [input.json]
"""

import argparse
import hashlib
import json
import os

from dotenv import load_dotenv
from neo4j import GraphDatabase, RoutingControl

from embedding_text import (
    TEXT_BUILDERS,
    FIELD_STATUS,
    EMBEDDING_MODEL_NAME,
    EMBEDDING_DIMS,
)

load_dotenv()
NEO4J_URI = os.getenv("NEO4J_URI")
NEO4J_USER = os.getenv("NEO4J_USER")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD")
NEO4J_GRAPH = os.getenv("NEO4J_GRAPH")


def import_embeddings(in_path: str) -> None:
    try:
        with open(in_path) as fh:
            records = json.load(fh)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"ABORT: {in_path} is not valid JSON ({exc})")
    if not isinstance(records, list):
        raise SystemExit(f"ABORT: {in_path} is not the JSON array produced by export_embeddings.py")

    # --- Meta validation: BEFORE opening a DB connection or writing anything ---
    meta = None
    if records and isinstance(records[0], dict) and "meta" in records[0]:
        meta = records.pop(0)["meta"]
    if meta is None:
        print(f"WARNING: {in_path} has no meta record (old export format): "
              f"cannot verify compatibility, assuming {EMBEDDING_MODEL_NAME} / {EMBEDDING_DIMS}")
    elif meta.get("model") != EMBEDDING_MODEL_NAME or meta.get("dims") != EMBEDDING_DIMS:
        print("ABORT: export metadata does not match this machine:")
        print(f"  file:  model={meta.get('model')} dims={meta.get('dims')}")
        print(f"  local: model={EMBEDDING_MODEL_NAME} dims={EMBEDDING_DIMS}")
        print("Vectors from a different model/dimension are incompatible. "
              "Re-export with the matching model, or re-run insert_embeddings.py to recompute.")
        return
    else:
        print(f"Metadata OK: {meta['model']} ({meta['dims']} dims)"
              + (f", exported {meta.get('created')}" if meta.get("created") else ""))

    # lookup: (label, field, key) -> vector  (vectors with the wrong length are dropped)
    lookup = {}
    bad_dims = 0
    for r in records:
        vec = r.get("vector")
        if not isinstance(vec, list) or len(vec) != EMBEDDING_DIMS:
            bad_dims += 1
            continue
        lookup[(r["label"], r["field"], r["key"])] = vec
    if bad_dims:
        print(f"WARNING: skipped {bad_dims} records with vector length != {EMBEDDING_DIMS}")
    print(f"Loaded {len(lookup)} embeddings from {in_path}")

    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    try:
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