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
_ALL_LABELS = ("Contribution", "Recommendation", "Gap", "Project")

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
    "Project": [
        ("project_embeddings", "queryVector", "project"),
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
     [ (ca:ContributionActor)-[rc:contributed_to]->(n) | {name: ca.name, type: ca.type} ]
     + [ (ca2:ContributionActor)-[rc2:project_contributor]->(n) | {name: ca2.name, type: ca2.type} ] AS actorEntries
RETURN
    n AS n,
    n.title AS title,
    n.officialTitle AS officialTitle,
    n.name AS name,
    n.id_num AS id_num,
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
    oiParent.officialTitle AS parentOfficialTitle,
    oiParent.id AS parentId,
    labels(oiParent) AS parentLabels,
    elementId(oiParent) AS parentNeo4jId,
    parentMainPurposeNames AS parentMainPurposeNames,
    parentSecPurposeNames AS parentSecPurposeNames,
    actorEntries AS actorEntries,
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
WITH collect({node: oi, rawScore: score}) AS rows
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
    if "Project" in labels:
        return "Progetto"
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


def _split_actors(entries):
    """Unique actor names, plus the same names split by ContributionActor.type.

    Returns `(actors, people, institutions)`:
      - `actors`      -> every name once, for the result cards;
      - `people`      -> names whose actor is a person (or has no type);
      - `institutions`-> names whose actor is an institution.

    The index page needs the split to offer two separate contributor filters;
    the detail page does the same split from `ContributionActor.type`.
    """
    seen, people_seen, inst_seen = set(), set(), set()
    actors, people, institutions = [], [], []
    for entry in entries or []:
        name = entry.get("name")
        if not name:
            continue
        kind = (entry.get("type") or "").lower()
        if (name, kind) in seen:
            continue
        seen.add((name, kind))
        if name not in actors:
            actors.append(name)
        if kind == "institution":
            if name not in inst_seen:
                inst_seen.add(name)
                institutions.append(name)
        elif name not in people_seen:
            people_seen.add(name)
            people.append(name)
    return actors, people, institutions


def _serialize(record):
    """Turn a raw Neo4j record (w/ Node) into a plain JSON-able dict."""
    sources = record['sources']
    labels = list(record['n'].labels)
    actors, people, institutions = _split_actors(record['actorEntries'])
    entry = {
        "type": _record_type(labels),
        "labels": labels,
        "wrrf_score": round(record['wrrf'], 4),
        "sources": sources,
        "sourceRanks": record['sourceRanks'],
        "rawScores": [round(s, 6) for s in record['rawScores']],
        "neo4j_id": record['neo4j_id'],
        # Projects have `name` (no `title`); Contributions carry BOTH
        # `officialTitle` (C2, English) and `title` (A1, Italian).
        "title": record['title'] or record['name'],
        "officialTitle": record['officialTitle'] or record['title'] or record['name'],
        # Own purposes first, then the parent OI's (Rec/Gap inherit their OI's
        # purposes, which is what makes purpose filtering meaningful for them).
        "purposes": _purpose_labels(
            record['mainPurposeNames']
            + record['secPurposeNames']
            + record['parentMainPurposeNames']
            + record['parentSecPurposeNames']
        ),
        "actors": actors,
        # Contributor filters: people and institutions, kept apart.
        "people": people,
        "institutions": institutions,
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
    elif entry["type"] == "Progetto":
        # Projects carry `id_num` (no `id`) and their body text is `description`.
        entry.update({"description": record['description'],
                      "submission_id": record['id_num']})
    else:
        entry.update({"submission_id": record['id']})
    if record['parentNeo4jId'] is not None:
        entry["parent_oi"] = {
            "labels": list(record['parentLabels'] or []),
            # A1 (IT) in `title`, C2 (EN) in `officialTitle`.
            "title": record['parentTitle'],
            "officialTitle": record['parentOfficialTitle'] or record['parentTitle'],
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
            ("Contribution", "Recommendation", "Gap", "Project"). Empty/None
            => all types.

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


# Minimal JSON-able summary of a linked node (child/parent OI).
def _make_brief(eid, props, labels, rel=None):
    props = props or {}
    return {
        "eid": eid,
        "labels": sorted(labels or []),
        "type": _record_type(labels or []),
        "rel": rel,
        "title": props.get("officialTitle") or props.get("title") or props.get("name"),
        # A1 (Italian) kept alongside the C2 English title.
        "title_it": props.get("title"),
        "officialTitle": props.get("officialTitle") or props.get("title"),
        "content": props.get("content"),
        "description": props.get("description"),
        "motivation": props.get("motivation"),
    }


def _fetch_purposes(eids):
    """Batch-read Purpose names for a set of element IDs. eids => dict{eid: [labels]}."""
    eids = [e for e in eids if e]
    if not eids:
        return {}
    driver = get_driver()
    rows, _, _ = driver.execute_query(
        "MATCH (x)-[:has_main_function|has_secondary_function]->(p:Purpose) "
        "WHERE elementId(x) IN $eids "
        "RETURN elementId(x) AS eid, collect(DISTINCT p.name) AS names",
        eids=eids, database_=NEO4J_GRAPH, routing_=RoutingControl.READ)
    return {r["eid"]: _purpose_labels(r["names"]) for r in rows}


def _fetch_purpose_split(element_id):
    """(main, secondary) purpose labels for ONE node.

    The UI shows A4 main purpose at the top of the detail page and the
    secondary functions later, so they must arrive separately.
    """
    driver = get_driver()
    rows, _, _ = driver.execute_query(
        "MATCH (n) WHERE elementId(n) = $eid "
        "OPTIONAL MATCH (n)-[:has_main_function]->(mp:Purpose) "
        "OPTIONAL MATCH (n)-[:has_secondary_function]->(sp:Purpose) "
        "RETURN collect(DISTINCT mp.name) AS main, collect(DISTINCT sp.name) AS sec",
        eid=element_id, database_=NEO4J_GRAPH, routing_=RoutingControl.READ)
    if not rows:
        return [], []
    main = _purpose_labels([n for n in rows[0]["main"] if n])
    sec = _purpose_labels([n for n in rows[0]["sec"] if n])
    return main, sec


# One-hop neighbourhood, grouped for the detail page.
# (target label, payload key, section title) - `related` keeps this order.
# Labels absent from this map are handled elsewhere in the payload:
#   Purpose -> purposes, ContributionActor -> actors,
#   Contribution/Recommendation/Gap/Project -> parents/children.
_RELATED_GROUPS = (
    ("Reference",           "references",        "URL / DOI"),
    ("Environment",         "environment",       "Environment"),
    ("GeographicArea",      "geographicArea",    "Geographic area"),
    ("FormalType",          "formalType",        "Formal type"),
    ("Phenomenon",          "phenomenon",        "Phenomena"),
    ("Output",              "output",            "Outputs"),
    ("Topic",               "topic",             "Main topic"),
    ("Content",             "content",           "Content"),
    ("License",             "license",           "License"),
    ("Accessibility",       "accessibility",     "Accessibility"),
    ("InstitutionalLevel",  "institutionalLevel", "Institutional level"),
    ("DataMaintainer",      "dataMaintainer",    "Data maintainer"),
    ("Stakeholder",         "stakeholder",       "Stakeholders"),
    ("Institution",         "institution",       "Institutions"),
    ("User",                "user",              "Assisted by"),
    ("DecisionMaker",       "decisionMaker",     "Decision makers"),
    ("PolicyMaker",         "policyMaker",       "Policy makers"),
)
_RELATED_ORDER = {key: i for i, (_, key, _) in enumerate(_RELATED_GROUPS)}
_RELATED_BY_LABEL = {lab: (key, title) for lab, key, title in _RELATED_GROUPS}

# Best-effort display name for a neighbour: node `name` first, then the usual
# title variants, then the person/institution identifiers.
_RELATED_LABEL_KEYS = ("name", "title", "officialTitle", "referencePerson",
                       "acronym", "email")
_RELATED_URL_KEYS = ("URL", "url", "uri")

_RELATED_OUT = (
    "MATCH (n)-[r]->(m) WHERE elementId(n) = $eid "
    "RETURN head(labels(m)) AS lab, type(r) AS rel, "
    "       collect(DISTINCT properties(m)) AS items"
)
_RELATED_IN = (
    "MATCH (m)-[r]->(n) WHERE elementId(n) = $eid "
    "RETURN head(labels(m)) AS lab, type(r) AS rel, "
    "       collect(DISTINCT properties(m)) AS items"
)


def _related_item(props):
    """Trim a neighbour's properties to {label, url?, props?}.

    Vectors and embedding status flags are dropped (they are bookkeeping,
    like the node's own `vectorProps`).
    """
    clean = {}
    for k, v in (props or {}).items():
        if k.endswith("Embedding") or k.endswith("EmbeddingStatus"):
            continue
        if isinstance(v, (list, tuple)) and len(v) > 30:
            continue
        if v is None or (isinstance(v, str) and not v.strip()):
            continue
        clean[k] = v.strip() if isinstance(v, str) else v
    if not clean:
        return None

    label_key, label = None, None
    for k in _RELATED_LABEL_KEYS:
        if isinstance(clean.get(k), str):
            label_key, label = k, clean[k]
            break
    if label is None:
        for k, v in clean.items():
            if isinstance(v, str):
                label_key, label = k, v
                break
    if label is None:
        label = ", ".join(f"{k}: {v}" for k, v in list(clean.items())[:3])
        label_key = None

    item = {"label": str(label)}
    used = {label_key} if label_key else set()

    # Users are split over name/surname -> show them as one line.
    if "surname" in clean and "name" in used and isinstance(clean["surname"], str):
        item["label"] = f"{label} {clean['surname']}".strip()
        used.add("surname")

    for k in _RELATED_URL_KEYS:
        if isinstance(clean.get(k), str):
            item["url"] = clean[k]
            used.add(k)
            break
    extras = {k: v for k, v in clean.items() if k not in used}
    if extras:
        item["props"] = extras
    return item


def _fetch_related(element_id):
    """One-hop neighbours grouped by target label, in display order.

    Returns an ordered dict: key -> {"label": section title, "items": [...]},
    empty groups omitted. Both edge directions are read (Stakeholders point
    AT a Contribution); the grouping key is the neighbour's label either way.
    """
    driver = get_driver()
    grouped = {}
    for cypher in (_RELATED_OUT, _RELATED_IN):
        rows, _, _ = driver.execute_query(
            cypher, eid=element_id, database_=NEO4J_GRAPH,
            routing_=RoutingControl.READ)
        for row in rows:
            key_label = _RELATED_BY_LABEL.get(row["lab"])
            if key_label is None:
                continue
            key, title = key_label
            bucket = grouped.setdefault(key, {"label": title, "items": []})
            for props in row["items"] or []:
                item = _related_item(props)
                if item is not None and item not in bucket["items"]:
                    bucket["items"].append(item)

    ordered = {}
    for key in sorted(grouped, key=lambda k: _RELATED_ORDER[k]):
        bucket = grouped[key]
        bucket["items"].sort(key=lambda it: it["label"].lower())
        if bucket["items"]:
            ordered[key] = bucket
    return ordered


def _unique_actors(rows):
    """Drop duplicated ContributionActor rows.

    The source data stores the same person/institution as several distinct
    nodes (one project has 15 rows all named "Luca Zaggia"), so the detail
    page must collapse them by (name, type). The search payload already
    dedupes names on its side.
    """
    seen, out = set(), []
    for actor in rows:
        key = (actor.get("name"), actor.get("type"))
        if key in seen:
            continue
        seen.add(key)
        out.append(actor)
    return out


def get_node_detail(element_id):
    """Full details of a single node + its OI <-> Rec/Gap neighbourhood.

    Returns None if the node does not exist. Used by GET /node/{eid}.
    Vector properties are summarised as their dimension (the raw 1024-float
    arrays would bloat the payload); everything else is passed through.
    """
    driver = get_driver()

    rows, _, _ = driver.execute_query(
        "MATCH (n) WHERE elementId(n) = $eid "
        "RETURN properties(n) AS props, labels(n) AS labels",
        eid=element_id, database_=NEO4J_GRAPH, routing_=RoutingControl.READ)
    if not rows:
        return None
    props = dict(rows[0]["props"] or {})
    labels = sorted(rows[0]["labels"] or [])

    main_purposes, sec_purposes = _fetch_purpose_split(element_id)
    combined_purposes = list(main_purposes) + [p for p in sec_purposes
                                               if p not in main_purposes]

    entry = {
        "eid": element_id,
        "labels": labels,
        "type": _record_type(labels),
        "properties": {},
        "vectorProps": {},
        "purposes": combined_purposes,
        "mainPurposes": main_purposes,
        "secPurposes": sec_purposes,
        # Relationship-linked fields (URL/DOI, Environment, GeographicArea,
        # FormalType, Output, Phenomenon, ...) grouped and display-ordered.
        "related": _fetch_related(element_id),
        "actors": [],
        "parents": [],
        "children": [],
    }

    for k, v in props.items():
        if isinstance(v, (list, tuple)) and len(v) > 30 and all(
                isinstance(x, (int, float)) for x in v):
            entry["vectorProps"][k] = len(v)
        else:
            entry["properties"][k] = v

    if "Contribution" in labels:
        arows, _, _ = driver.execute_query(
            "MATCH (ca:ContributionActor)-[:contributed_to]->(n) "
            "WHERE elementId(n) = $eid RETURN ca.name AS name, ca.type AS atype",
            eid=element_id, database_=NEO4J_GRAPH, routing_=RoutingControl.READ)
        entry["actors"] = _unique_actors(
            [{"name": r["name"], "type": r["atype"]} for r in arows])

        crows, _, _ = driver.execute_query(
            "MATCH (n)-[r:recommends|highlights_gap]->(child) "
            "WHERE elementId(n) = $eid "
            "RETURN type(r) AS rel, elementId(child) AS ceid, "
            "labels(child) AS clabels, properties(child) AS cprops",
            eid=element_id, database_=NEO4J_GRAPH, routing_=RoutingControl.READ)
        eids = [r["ceid"] for r in crows]
        purposes = _fetch_purposes(eids)
        entry["children"] = [
            {**_make_brief(r["ceid"], r["cprops"], r["clabels"], r["rel"]),
             "purposes": purposes.get(r["ceid"], [])}
            for r in crows
        ]
    elif "Project" in labels:
        # Projects link to actors via project_contributor (not contributed_to)
        # and have no Contribution parent / child Rec/Gap edges.
        arows, _, _ = driver.execute_query(
            "MATCH (ca:ContributionActor)-[:project_contributor]->(n) "
            "WHERE elementId(n) = $eid RETURN ca.name AS name, ca.type AS atype",
            eid=element_id, database_=NEO4J_GRAPH, routing_=RoutingControl.READ)
        entry["actors"] = _unique_actors(
            [{"name": r["name"], "type": r["atype"]} for r in arows])
    else:
        prows, _, _ = driver.execute_query(
            "MATCH (parent:Contribution)-[r:recommends|highlights_gap]->(n) "
            "WHERE elementId(n) = $eid "
            "RETURN type(r) AS rel, elementId(parent) AS peid, "
            "labels(parent) AS plabels, properties(parent) AS pprops",
            eid=element_id, database_=NEO4J_GRAPH, routing_=RoutingControl.READ)
        eids = [r["peid"] for r in prows]
        purposes = _fetch_purposes(eids)
        entry["parents"] = [
            {**_make_brief(r["peid"], r["pprops"], r["plabels"], r["rel"]),
             "purposes": purposes.get(r["peid"], [])}
            for r in prows
        ]

    return entry