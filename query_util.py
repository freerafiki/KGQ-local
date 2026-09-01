"""
Shared search core used by the CLI (query_single.py) and the FastAPI app (app.py).

All retrieval logic lives here so that any improvement to the query mechanism
(new indexes, query expansion, reranking, score normalization, ...) benefits
every entry point at once.

Future search strategies to implement here (and expose via new endpoints):
  - semantic-only retrieval  (skip the fulltext subquery)
  - fulltext-only search     (skip the vector subqueries)
  - hybrid + reranking       (cross-encoder re-scoring of the wRRF candidates)
  - query expansion          (rewrite/expand the query before embedding)
  - text2cypher              (LLM-generated Cypher over the graph)
"""

import os
import subprocess
import time

from dotenv import load_dotenv
from neo4j import GraphDatabase, RoutingControl

from query_config import chooseSourceWeights, embedding_model

load_dotenv()
NEO4J_URI = os.getenv("NEO4J_URI")
NEO4J_USER = os.getenv("NEO4J_USER")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD")
NEO4J_GRAPH = os.getenv("NEO4J_GRAPH")

_driver = None


def get_driver():
    """Return a lazily-initialised shared Neo4j driver (one per process)."""
    global _driver
    if _driver is None:
        _driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    return _driver


def close_driver():
    global _driver
    if _driver is not None:
        _driver.close()
        _driver = None


def get_lan_ips():
    """Non-loopback IPv4 addresses of this machine (for LAN access notices)."""
    try:
        out = subprocess.check_output(["hostname", "-I"], text=True).split()
    except Exception:
        return ["127.0.0.1"]
    ips = [ip for ip in out if not ip.startswith("127.")]
    return ips or ["127.0.0.1"]


# Single hybrid search: BM25 fulltext + 5 vector indexes, fused with wRRF.
# The fulltext score is unbounded (BM25-like); cosine scores live in ~[0,1].
# wRRF uses ONLY the intra-source rank, so the different scales are fine.
# rawScore is kept for diagnostics / future score-normalization.
HYBRID_SEARCH_CYPHER = """
CYPHER 25
LET
query = $query,
queryVector = $queryVector,
shortQueryVector = $shortQueryVector,
longQueryVector = $longQueryVector,
finalK = $finalK,
rrfConstant = $rrfConstant,
sourceWeights = $sourceWeights

CALL (query, queryVector, shortQueryVector, longQueryVector) {
CALL db.index.fulltext.queryNodes('search_fulltext', query, {limit: $sourceK})
YIELD node AS result, score
WITH result, score
ORDER BY score DESC, result.id ASC
WITH collect({node: result, rawScore: score}) AS rows
UNWIND CASE WHEN size(rows) = 0 THEN [] ELSE range(0, size(rows) - 1) END AS rankIndex
RETURN
    rows[rankIndex].node AS result,
    'fulltext' AS source,
    rankIndex + 1 AS sourceRank,
    rows[rankIndex].rawScore AS rawScore

UNION ALL

MATCH (result:Contribution)
    SEARCH result IN (
        VECTOR INDEX `description_embeddings`
        FOR longQueryVector
        LIMIT $sourceK
    ) SCORE AS score
WITH result, score
ORDER BY score DESC, result.id ASC
WITH collect({node: result, rawScore: score}) AS rows
UNWIND CASE WHEN size(rows) = 0 THEN [] ELSE range(0, size(rows) - 1) END AS rankIndex
RETURN
    rows[rankIndex].node AS result,
    'OI_description' AS source,
    rankIndex + 1 AS sourceRank,
    rows[rankIndex].rawScore AS rawScore

UNION ALL

MATCH (result:Contribution)
    SEARCH result IN (
        VECTOR INDEX `title_embeddings`
        FOR longQueryVector
        LIMIT $sourceK
    ) SCORE AS score
WITH result, score
ORDER BY score DESC, result.id ASC
WITH collect({node: result, rawScore: score}) AS rows
UNWIND CASE WHEN size(rows) = 0 THEN [] ELSE range(0, size(rows) - 1) END AS rankIndex
RETURN
    rows[rankIndex].node AS result,
    'OI_title' AS source,
    rankIndex + 1 AS sourceRank,
    rows[rankIndex].rawScore AS rawScore

UNION ALL

MATCH (result:Contribution)
    SEARCH result IN (
        VECTOR INDEX `subtitle_embeddings`
        FOR longQueryVector
        LIMIT $sourceK
    ) SCORE AS score
WITH result, score
ORDER BY score DESC, result.id ASC
WITH collect({node: result, rawScore: score}) AS rows
UNWIND CASE WHEN size(rows) = 0 THEN [] ELSE range(0, size(rows) - 1) END AS rankIndex
RETURN
    rows[rankIndex].node AS result,
    'OI_subtitle' AS source,
    rankIndex + 1 AS sourceRank,
    rows[rankIndex].rawScore AS rawScore

UNION ALL

MATCH (result:Recommendation)
    SEARCH result IN (
        VECTOR INDEX `recommendation_embeddings`
        FOR queryVector
        LIMIT $sourceK
    ) SCORE AS score
WITH result, score
ORDER BY score DESC, result.id ASC
WITH collect({node: result, rawScore: score}) AS rows
UNWIND CASE WHEN size(rows) = 0 THEN [] ELSE range(0, size(rows) - 1) END AS rankIndex
RETURN
    rows[rankIndex].node AS result,
    'recommendation' AS source,
    rankIndex + 1 AS sourceRank,
    rows[rankIndex].rawScore AS rawScore

UNION ALL

MATCH (result:Gap)
    SEARCH result IN (
        VECTOR INDEX `gap_embeddings`
        FOR queryVector
        LIMIT $sourceK
    ) SCORE AS score
WITH result, score
ORDER BY score DESC, result.id ASC
WITH collect({node: result, rawScore: score}) AS rows
UNWIND CASE WHEN size(rows) = 0 THEN [] ELSE range(0, size(rows) - 1) END AS rankIndex
RETURN
    rows[rankIndex].node AS result,
    'gap' AS source,
    rankIndex + 1 AS sourceRank,
    rows[rankIndex].rawScore AS rawScore
}

LET weight = coalesce(sourceWeights[source], 1.0)
LET contribution = weight / (rrfConstant + sourceRank)

WITH result, finalK, source, sourceRank, rawScore, weight, contribution
ORDER BY result.id ASC, source ASC, sourceRank ASC

WITH
result,
finalK,
collect({
    source: source,
    sourceRank: sourceRank,
    weight: weight,
    rawScore: rawScore,
    contribution: contribution
}) AS contributions

LET wrrf = reduce(wrrf = 0.0, contribution IN contributions |
wrrf + contribution.contribution
)

ORDER BY wrrf DESC, result.id ASC

WITH collect({
result: result,
sources: [contribution IN contributions | contribution.source],
sourceRanks: [contribution IN contributions | contribution.sourceRank],
rawScores: [contribution IN contributions | contribution.rawScore],
wrrf: wrrf
}) AS orderedRows, finalK
LET limitedRows = orderedRows[..finalK]

UNWIND limitedRows AS row
RETURN
    row.result AS n,
    row.result.title AS title,
    row.result.description AS abstract,
    row.result.findings AS findings,
    row.result.id AS id,
    row.result.content AS content,
    row.result.motivation AS motivation,
    row.result.description AS description,
    row.sources AS sources,
    row.sourceRanks AS sourceRanks,
    row.rawScores AS rawScores,
    elementId(row.result) AS neo4j_id,
    row.wrrf AS wrrf
ORDER BY row.wrrf DESC, row.result.id ASC;
"""


def _record_type(labels):
    # Type comes from the node label itself, so a result found ONLY via the
    # fulltext index still gets a proper OI / Recommendation / Gap badge
    # instead of being lumped as "fulltext".
    if "Contribution" in labels:
        return "Oggetto Informativo"
    if "Recommendation" in labels:
        return "Raccomandazione"
    if "Gap" in labels:
        return "Lacuna"
    return "Full-text"


def _serialize(record):
    """Turn a raw Neo4j record (w/ Node) into a plain JSON-able dict."""
    sources = record['sources']
    labels = list(record['n'].labels)
    entry = {
        "type": _record_type(labels),
        "labels": labels,
        "wrrf_score": round(record['wrrf'], 4),
        "sources": sources,
        "sourceRanks": record['sourceRanks'],
        "rawScores": [round(s, 6) for s in record['rawScores']],
        "neo4j_id": record['neo4j_id'],
        "title": record['title'],
    }
    if entry["type"] == "Oggetto Informativo":
        entry.update({
            "abstract": record['abstract'],
            "findings": record['findings'],
            "submission_id": record['id'],
        })
    elif entry["type"] == "Raccomandazione":
        entry.update({"content": record['content'], "motivation": record['motivation']})
    elif entry["type"] == "Lacuna":
        entry.update({"description": record['description']})
    else:
        entry.update({"submission_id": record['id']})
    return entry


def embed_query(text: str):
    """Encode the query text into the embedding vector.

    TODO(future): query expansion could produce THREE different vectors here:
      - shortQueryVector  (e.g. truncated / keyword-only query)
      - longQueryVector   (e.g. expanded / rewritten query)
      - queryVector       (default, used by recommendation + gap indexes)
    Currently all three are identical.
    """
    return embedding_model.encode(text)


def run_search(query_text, source_k=10, final_k=20, rrf_constant=60, source_weights=None):
    """Run the hybrid search (fulltext + vectors, wRRF fusion) and return a dict.

    Returns:
        {
          "query": str,
          "embedding_time_s": float,
          "search_time_s": float,
          "results": [ { ... per-result dict, see _serialize ... } ]
        }
    """
    if source_weights is None:
        source_weights = chooseSourceWeights(query_text)

    start = time.time()
    query_embedding = embed_query(query_text)
    embedding_time = time.time() - start

    driver = get_driver()
    start_q = time.time()
    records, summary, keys = driver.execute_query(
        HYBRID_SEARCH_CYPHER,
        query=query_text,
        queryVector=query_embedding,
        shortQueryVector=query_embedding,
        longQueryVector=query_embedding,
        sourceK=source_k,
        finalK=final_k,
        rrfConstant=rrf_constant,
        sourceWeights=source_weights,
        database_=NEO4J_GRAPH,
        routing_=RoutingControl.READ,
    )
    search_time = time.time() - start_q

    return {
        "query": query_text,
        "embedding_time_s": round(embedding_time, 4),
        "search_time_s": round(search_time, 4),
        "results": [_serialize(record) for record in records],
    }


def format_scores(sources, source_ranks, raw_scores, ft_min, ft_max):
    """Human-readable per-source scores.

    Vector sources show the raw cosine score; fulltext shows a min-max
    normalized score (0-1) with the raw BM25-like score in parentheses.
    """
    parts = []
    for src, rank, score in zip(sources, source_ranks, raw_scores):
        if src == "fulltext" and ft_max > ft_min:
            norm = (score - ft_min) / (ft_max - ft_min)
            parts.append(f"{src}#{rank}: {norm:.04f} ({score:.04f})")
        else:
            parts.append(f"{src}#{rank}: {score:.04f}")
    return ", ".join(parts)


def fulltext_score_bounds(results):
    """Return (min, max) of fulltext raw scores across the result batch."""
    ft = [
        score
        for r in results
        for src, score in zip(r["sources"], r["rawScores"])
        if src == "fulltext"
    ]
    return (min(ft), max(ft)) if ft else (0.0, 1.0)