#!/usr/bin/env python3
"""(Re)create the full-text search indexes — no embedding model needed.

Run after every graph import (KGB/graph_io.py does not carry full-text
indexes) or whenever the API logs a missing fulltext index at startup:

    python3 scripts/create_search_indexes.py            # apply
    python3 scripts/create_search_indexes.py --dry-run  # only report

Then restart the API (`sudo systemctl restart kgq`) so its startup index
check is clean. Definitions: search_indexes.FULLTEXT_INDEXES.
"""
import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from neo4j import GraphDatabase

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from search_indexes import ensure_fulltext_indexes  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="report only, change nothing")
    args = ap.parse_args()

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    driver = GraphDatabase.driver(os.getenv("NEO4J_URI"),
                                  auth=(os.getenv("NEO4J_USER"), os.getenv("NEO4J_PASSWORD")))
    database = os.getenv("NEO4J_GRAPH")
    print(f"Full-text indexes on database '{database or '(default)'}'"
          + (" (dry run)" if args.dry_run else "") + ":")
    with driver:
        ensure_fulltext_indexes(driver, database, dry_run=args.dry_run)
    if not args.dry_run:
        print("Done. Restart the API so its startup index check is clean.")


if __name__ == "__main__":
    main()
