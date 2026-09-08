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


# Hybrid search: BM25 fulltext + 5 vector indexes + authors fulltext, fused with wRRF.
# The fulltext score is unbounded (BM25-like); cosine scores live in ~[0,1].
# wRRF uses ONLY the intra-source rank, so the different scales are fine.
# rawScore is kept for diagnostics / future score-normalization.
#
# The query is composed from fragments, one per source. To FILTER by node type
# we simply leave out the sources belonging to the deselected types and, for
# the fulltext source (which matches all labels), inject a label WHERE clause.
# This is a PRE-filter: only candidates of the requested types enter the wRRF
# fusion, so a small finalK is not starved by "other type" results.
_ALL_LABELS = ("Contribution", "Recommendation", "Gap")

_SOURCES_BY_LABEL = {
    "Contribution": [
        ("description_embeddings", "longQueryVector", "OI_description"),
        ("title_embeddings", "longQueryVector", "OI_title"),
        ("subtitle_embeddings", "longQueryVector", "OI_subtitle"),
    ],
    "Recommendation": [
        ("recommendation_embeddings", "queryVector", "recommendation"),
    ],
    "Gap": [
        ("gap_embeddings", "queryVector", "gap"),
    ],
}

_CYPHER_HEADER = """
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
"""

_CYPHER_FUSION_TAIL = """
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
WITH row, row.result AS n
WITH row, n,
     [(n)-[rm:has_main_function]->(p:Purpose) | p.name] AS mainPurposeNames,
     [(n)-[rs:has_secondary_function]->(p:Purpose) | p.name] AS secPurposeNames,
     [ (parent:Contribution)-[rp:recommends|highlights_gap]->(n) | parent ][0] AS oiParent
WITH row, n, mainPurposeNames, secPurposeNames, oiParent,
     [ (oiParent)-[rpm:has_main_function]->(p:Purpose) | p.name ] AS parentMainPurposeNames,
     [ (oiParent)-[rps:has_secondary_function]->(p:Purpose) | p.name ] AS parentSecPurposeNames,
     [ (ca:ContributionActor)-[rc:contributed_to]->(n) | ca.name ] AS actorNames
RETURN
    n AS n,
    n.title AS title,
    n.description AS abstract,
    n.findings AS findings,
    n.id AS id,
    n.content AS content,
    n.motivation AS motivation,
    n.description AS description,
    row.sources AS sources,
    row.sourceRanks AS sourceRanks,
    row.rawScores AS rawScores,
    elementId(n) AS neo4j_id,
    mainPurposeNames AS mainPurposeNames,
    secPurposeNames AS secPurposeNames,
    oiParent.title AS parentTitle,
    oiParent.id AS parentId,
    labels(oiParent) AS parentLabels,
    elementId(oiParent) AS parentNeo4jId,
    parentMainPurposeNames AS parentMainPurposeNames,
    parentSecPurposeNames AS parentSecPurposeNames,
    actorNames AS actorNames,
    row.wrrf AS wrrf
ORDER BY row.wrrf DESC, n.id ASC;
"""


def _fulltext_fragment(labels):
    """Fulltext source. `labels` empty tuple => all types, no WHERE clause."""
    where = ""
    if labels:
        conds = " OR ".join(f"'{lab}' IN labels(result)" for lab in labels)
        where = f"\nWHERE {conds}"
    return f"""
CALL db.index.fulltext.queryNodes('search_fulltext', query, {{limit: $sourceK}})
YIELD node AS result, score
WITH result, score{where}
ORDER BY score DESC, result.id ASC
WITH collect({{node: result, rawScore: score}}) AS rows
UNWIND CASE WHEN size(rows) = 0 THEN [] ELSE range(0, size(rows) - 1) END AS rankIndex
RETURN
    rows[rankIndex].node AS result,
    'fulltext' AS source,
    rankIndex + 1 AS sourceRank,
    rows[rankIndex].rawScore AS rawScore
"""


def _vector_fragment(label, index, vec, source):
    return f"""
UNION ALL

MATCH (result:{label})
    SEARCH result IN (
        VECTOR INDEX `{index}`
        FOR {vec}
        LIMIT $sourceK
    ) SCORE AS score
WITH result, score
ORDER BY score DESC, result.id ASC
WITH collect({{node: result, rawScore: score}}) AS rows
UNWIND CASE WHEN size(rows) = 0 THEN [] ELSE range(0, size(rows) - 1) END AS rankIndex
RETURN
    rows[rankIndex].node AS result,
    '{source}' AS source,
    rankIndex + 1 AS sourceRank,
    rows[rankIndex].rawScore AS rawScore
"""


def _authors_fragment():
    return """
UNION ALL

CALL db.index.fulltext.queryNodes('authors_fulltext', query, {limit: $sourceK})
YIELD node AS actor, score
WITH actor, score
MATCH (actor)-[ra:contributed_to]->(oi:Contribution)
WITH oi, max(score) AS score
ORDER BY score DESC, oi.id ASC
WITH collect({{node: oi, rawScore: score}}) AS rows
UNWIND CASE WHEN size(rows) = 0 THEN [] ELSE range(0, size(rows) - 1) END AS rankIndex
RETURN
    rows[rankIndex].node AS result,
    'authors' AS source,
    rankIndex + 1 AS sourceRank,
    rows[rankIndex].rawScore AS rawScore
"""


_cypher_cache = {}


def _build_cypher(labels):
    """Compose the hybrid query for the requested node labels.

    `labels` is None / empty => no filter, all sources. Otherwise only the
    sources of the requested labels participate and the fulltext source is
    restricted to those same labels.
    """
    key = tuple(labels) if labels else ()
    cached = _cypher_cache.get(key)
    if cached is not None:
        return cached

    active = set(key)
    if not active:
        active = set(_ALL_LABELS)

    parts = [_CYPHER_HEADER, _fulltext_fragment(key)]
    if "Contribution" in active:
        parts.append(_authors_fragment())
    for label in _ALL_LABELS:
        if label in active:
            for index, vec, source in _SOURCES_BY_LABEL[label]:
                parts.append(_vector_fragment(label, index, vec, source))
    parts.append(_CYPHER_FUSION_TAIL)

    cypher = "".join(parts)
    _cypher_cache[key] = cypher
    return cypher


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


# A4 "main purpose" taxonomy. A Purpose node's name is a comma-joined phrase
# (e.g. "Valutazione, i.e. impatti, rischi, ..."), so we match each known
# keyword and keep the first occurrence per category, in taxonomy order.
PURPOSE_CATEGORIES = [
    ("valutazione", "Valutazione"),
    ("monitoraggio", "Monitoraggio"),
    ("ricostruzione", "Ricostruzione"),
    ("previsione", "Previsione"),
    ("supporto", "Supporto decisioni"),
    ("governance", "Governance"),
    ("gap", "Gap di conoscenza"),
]
_PURPOSE_LABELS = [label for _, label in PURPOSE_CATEGORIES]


def _purpose_labels(names):
    """Map raw Purpose names (main + secondary) to short category labels."""
    labels = []
    for name in names or []:
        low = name.lower()
        for keyword, label in PURPOSE_CATEGORIES:
            if keyword in low and label not in labels:
                labels.append(label)
    return labels


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
        # Own purposes first, then the parent OI's (Rec/Gap inherit their OI's
        # purposes, which is what makes purpose filtering meaningful for them).
        "purposes": _purpose_labels(
            record['mainPurposeNames']
            + record['secPurposeNames']
            + record['parentMainPurposeNames']
            + record['parentSecPurposeNames']
        ),
        "actors": list(dict.fromkeys(record['actorNames'] or [])),
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
    if record['parentNeo4jId'] is not None:
        entry["parent_oi"] = {
            "labels": list(record['parentLabels'] or []),
            "title": record['parentTitle'],
            "id": record['parentId'],
            "neo4j_id": record['parentNeo4jId'],
        }
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


def run_search(query_text, source_k=10, final_k=20, rrf_constant=60,
               source_weights=None, types=None):
    """Run the hybrid search (fulltext + vectors, wRRF fusion).

    Args:
        types: optional list of Neo4j labels to restrict results to
            ("Contribution", "Recommendation", "Gap"). Empty/None => all types.

    Returns:
        {
          "query": str,
          "types": [str] | None,
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
        _build_cypher(types),
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
        "types": tuple(types) if types else None,
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