"""
Add a short `title` property to Recommendation and Gap nodes.

Recommendation/Gap currently carry only long free-text fields (content /
description), which blow up result cards. Contribution already has `title`.
This derives a compact `title` from the first words of the text field and
stores it once on the node, so both the CLI and the API can show a short name.

Run:  python3 add_node_titles.py
Idempotent: only fills nodes where `title` is missing/empty.
"""

import os
import re

from dotenv import load_dotenv
from neo4j import GraphDatabase, RoutingControl

load_dotenv()
NEO4J_URI = os.getenv('NEO4J_URI')
NEO4J_USER = os.getenv('NEO4J_USER')
NEO4J_PASSWORD = os.getenv('NEO4J_PASSWORD')
NEO4J_GRAPH = os.getenv('NEO4J_GRAPH')

MAX_TITLE = 96


def short_title(text):
    text = re.sub(r'\s+', ' ', text or '').strip()
    if len(text) <= MAX_TITLE:
        return text
    cut = text[:MAX_TITLE]
    if ' ' in cut:
        cut = cut[:cut.rfind(' ')]
    return cut.rstrip('.,;:') + '…'


def fill_titles(driver, label, field):
    records, _, _ = driver.execute_query(f"""
        MATCH (n:{label})
        WHERE n.title IS NULL AND n.{field} IS NOT NULL
        RETURN elementId(n) AS neo4j_id, n.{field} AS text
    """, database_=NEO4J_GRAPH, routing_=RoutingControl.READ)

    rows = []
    skipped = 0
    for record in records:
        title = short_title(record['text'])
        if not title:
            skipped += 1
            continue
        rows.append({'neo4j_id': record['neo4j_id'], 'title': title})

    if not rows:
        print(f"{label}: nothing to do ({skipped} empty texts)")
        return

    _, summary, _ = driver.execute_query(f"""
        UNWIND $rows AS row
        MATCH (n:{label}) WHERE elementId(n) = row.neo4j_id
        SET n.title = row.title
    """, rows=rows, database_=NEO4J_GRAPH, routing_=RoutingControl.WRITE)
    print(f"{label}: set title on {len(rows)} nodes "
          f"({summary.counters.properties_set} properties set, {skipped} skipped)")


def main():
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    try:
        fill_titles(driver, "Recommendation", "content")
        fill_titles(driver, "Gap", "description")
    finally:
        driver.close()


if __name__ == "__main__":
    main()