"""Build the BGE-M3 ColBERT index used by the "colbert" reranker.

Runs on the BUILD machine (GPU / many cores): every node the search can
return is encoded once into its ColBERT token vectors, from the SAME text the
reranker reads at query time (embedding_text.rerank_text). The result is a
self-contained folder that is copied to the serving machines (laptop,
server); nothing is written to Neo4j. See data_transfer.md.

    python3 scripts/build_colbert_index.py [--out data/colbert] [--update]
                                           [--batch-size 32] [--labels ...]

  --update  keep the existing folder and only encode texts not in it yet
            (new nodes, edited text). Without it the folder is rebuilt.

Entries are keyed by the sha256 of the encoded text, so identical texts are
stored once, and texts that no longer exist in the graph just stay unused
(rebuild without --update now and then to drop them).

Size: ~ n_docs * avg_tokens * 1024 dims * 2 bytes (float16). Lower
colbert_m3.COLBERT_DOC_MAX_LENGTH and rebuild if it is too big.
"""

import argparse
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from neo4j import GraphDatabase, RoutingControl
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import colbert_m3
from embedding_text import (
    RERANK_BODY_FIELDS, rerank_text, rerank_text_key, rerank_title,
)

load_dotenv()


def fetch_texts(driver, db, labels):
    """{key: text} for every node of `labels` with some text."""
    texts = {}
    for label in labels:
        body = RERANK_BODY_FIELDS[label]
        aliases = ", ".join(f"n.`{f}` AS `{f}`" for f in body)
        rows, _, _ = driver.execute_query(
            f"MATCH (n:{label}) RETURN n.officialTitle AS officialTitle, "
            f"n.title AS title, n.name AS name, {aliases}",
            database_=db, routing_=RoutingControl.READ)
        n_before = len(texts)
        empty = 0
        for row in rows:
            text = rerank_text(rerank_title(row["officialTitle"], row["title"], row["name"]),
                               *(row[f] for f in body))
            if not text:
                empty += 1
                continue
            texts[rerank_text_key(text)] = text
        print(f"{label:<15} {len(rows):>6} nodes, {len(texts) - n_before:>6} new texts"
              + (f", {empty} without text" if empty else ""))
    return texts


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(colbert_m3.DEFAULT_INDEX_DIR))
    ap.add_argument("--update", action="store_true",
                    help="only encode texts missing from the existing index")
    ap.add_argument("--batch-size", type=int, default=32)
    ap.add_argument("--chunk", type=int, default=512,
                    help="texts per encode call (progress granularity)")
    ap.add_argument("--labels", nargs="*", default=list(RERANK_BODY_FIELDS),
                    choices=list(RERANK_BODY_FIELDS))
    args = ap.parse_args()

    driver = GraphDatabase.driver(os.getenv("NEO4J_URI"),
                                  auth=(os.getenv("NEO4J_USER"), os.getenv("NEO4J_PASSWORD")))
    try:
        texts = fetch_texts(driver, os.getenv("NEO4J_GRAPH"), args.labels)
    finally:
        driver.close()

    writer = colbert_m3.ColbertStoreWriter(args.out, update=args.update)
    todo = [(k, t) for k, t in texts.items() if k not in writer]
    print(f"\n{len(texts)} distinct texts, {len(texts) - len(todo)} already indexed, "
          f"{len(todo)} to encode")
    # Longest first: batches of similar length waste less padding, and an
    # out-of-memory error shows up at the start, not after an hour.
    todo.sort(key=lambda kt: -len(kt[1]))
    start = time.time()
    try:
        with tqdm(total=len(todo), desc="ColBERT encode", unit="doc") as bar:
            for i in range(0, len(todo), args.chunk):
                chunk = todo[i:i + args.chunk]
                vecs = colbert_m3.encode_documents([t for _, t in chunk],
                                                   batch_size=args.batch_size)
                for (key, _), v in zip(chunk, vecs):
                    writer.add(key, v)
                bar.update(len(chunk))
    finally:
        # also on Ctrl-C: whatever was encoded is kept, and --update resumes
        writer.close()

    size = Path(args.out, colbert_m3.VECS_FILE).stat().st_size
    print(f"\nencoded {len(todo)} texts in {time.time() - start:.0f} s")
    print(f"index {args.out}: {writer.meta['n_docs']} docs, {writer.meta['total_tokens']} "
          f"tokens ({writer.meta['total_tokens'] / max(writer.meta['n_docs'], 1):.0f}/doc), "
          f"{size / 2**20:.0f} MiB")
    print("Copy the whole folder to the serving machines (see data_transfer.md).")


if __name__ == "__main__":
    main()
