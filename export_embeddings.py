"""Export all stored node embeddings to a JSON file.

For local development: lets you wipe/recreate the graph and re-attach the
embeddings later (see import_embeddings.py) without re-running the model.

Each record is keyed by the sha256 hash of the EXACT text that was embedded at
creation time (see embedding_text.py). This is stable across graph rebuilds,
unlike `elementId`, and works even for Recommendation/Gap nodes (which have no
`id` property). If the content of a node changes between export and import, the
hash won't match and the embedding is simply not re-attached (re-embed it).

Shared text builders live in embedding_text.py and MUST stay in lockstep with
insert_embeddings.py.

Usage:
    python3 export_embeddings.py [output.json]
"""

import argparse
import hashlib
import json
import os

from dotenv import load_dotenv
from neo4j import GraphDatabase, RoutingControl

from embedding_text import TEXT_BUILDERS

load_dotenv()
NEO4J_URI = os.getenv("NEO4J_URI")
NEO4J_USER = os.getenv("NEO4J_USER")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD")
NEO4J_GRAPH = os.getenv("NEO4J_GRAPH")


def export(out_path: str) -> None:
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    exported = skipped = 0
    try:
        with open(out_path, "w") as fh:
            fh.write("[\n")
            first = True
            for label, fields in TEXT_BUILDERS.items():
                for field, (raw_fields, builder) in fields.items():
                    aliases = ", ".join(f"n.`{f}` AS `{f}`" for f in raw_fields)
                    rows, _, _ = driver.execute_query(
                        f"MATCH (n:{label}) "
                        f"RETURN {aliases}, n.`{field}` AS vector",
                        database_=NEO4J_GRAPH,
                        routing_=RoutingControl.READ,
                    )
                    for row in rows:
                        vec = row["vector"]
                        if vec is None:
                            continue
                        text = builder(*(row[f] for f in raw_fields))
                        if not text:
                            skip_note = f"[{label}/{field}] node with empty text "
                            print(f"\tWARNING: {skip_note}-> not re-joinable, skipped")
                            skipped += 1
                            continue
                        key = hashlib.sha256(text.encode("utf-8")).hexdigest()
                        if not first:
                            fh.write(",\n")
                        first = False
                        json.dump({"key": key, "label": label, "field": field,
                                   "vector": vec}, fh)
                        exported += 1
            fh.write("\n]\n")
        print(f"Exported {exported} embeddings ({skipped} skipped as empty text) "
              f"-> {out_path}")
    finally:
        driver.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", nargs="?", default="embeddings_export.json",
                        help="JSON file to write (default: embeddings_export.json)")
    args = parser.parse_args()
    export(args.output)