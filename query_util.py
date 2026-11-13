"""
Shared search core used by the CLI (scripts/query_single.py) and the FastAPI app (app.py).

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

import hashlib
import logging
import math
import os
import re
import time

from dotenv import load_dotenv
from neo4j import GraphDatabase, RoutingControl

import query_config
from embedding_text import _s, rerank_text, rerank_text_key
from query_config import chooseSourceWeights, encode_query, thresholds_for

# Same logger family as app.py ("kgq.api"), so uvicorn's handler shows these
# lines with timestamps in the journal next to the request log.
logger = logging.getLogger("kgq.query")

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

# Hybrid search: BM25 fulltext + 6 vector indexes + authors fulltext, fused with wRRF.
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
        # Third element = the source label: keyed into the sourceWeights dicts
        # (query_config) by the Cypher, and echoed in `sources` on the cards.
        ("description_embeddings", "longQueryVector", "doc_description"),
        ("title_embeddings", "longQueryVector", "doc_title"),
        ("subtitle_embeddings", "longQueryVector", "doc_subtitle"),
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

# Every index the fragments above call BY NAME. A missing one only surfaces
# as a Neo4j ClientError at the first search that needs it ("There is no such
# fulltext schema index: ..."), so app.py checks this list at startup and
# logs which ones the connected database lacks.
EXPECTED_FULLTEXT_INDEXES = (
    "search_fulltext",     # _fulltext_fragment  (nl, keywords)
    "title_fulltext",      # _title_fragment     (title)
    "authors_fulltext",    # _authors_fragment   (nl, author)
)
EXPECTED_VECTOR_INDEXES = tuple(
    index
    for entries in _SOURCES_BY_LABEL.values()
    for index, _vec, _source in entries
)


def check_indexes():
    """List the expected indexes this database is missing (read-only).

    Returns a sorted list of index names the search pipeline needs but the
    connected database does not have, or None when the check could not run
    (database unreachable) — startup must not crash the API for that, the
    driver is lazy on purpose and searches will report their own errors.
    """
    try:
        driver = get_driver()
        # execute_query returns (records, summary, keys); one record per index,
        # each carrying just the `name` field.
        fulltext_rows, _, _ = driver.execute_query(
            "SHOW FULLTEXT INDEXES YIELD name RETURN name")
        vector_rows, _, _ = driver.execute_query(
            "SHOW VECTOR INDEXES YIELD name RETURN name")
        fulltext = {row["name"] for row in fulltext_rows}
        vector = {row["name"] for row in vector_rows}
    except Exception as exc:
        logger.warning("index check failed: %s: %s", type(exc).__name__, exc)
        return None
    missing = [name for name in EXPECTED_FULLTEXT_INDEXES if name not in fulltext]
    missing += [name for name in EXPECTED_VECTOR_INDEXES if name not in vector]
    return sorted(missing)


SEARCH_MODES = ("nl", "keywords", "title", "author")

# Lucene query-parser metacharacters. The fulltext indexes hand the query
# string straight to Lucene, so an unescaped '(' or '"' (e.g. "edilizia
# (residenziale", 'città "venezia') aborts the whole search with a parse
# error, while "PRG: variante" is silently read as a field query against a
# non-existent field and matches nothing.
# Everything is escaped because the UI offers no operator syntax: users only
# ever mean the literal characters. The embedded text for "nl" stays RAW —
# the model must see the query as typed.
_LUCENE_SPECIALS = '+-&|!(){}[]^"~*?:/\\'

# Lucene's clause operators stay operators even as the only word ("AND"),
# where metacharacter escaping does not apply — so their letters get escaped
# too and they become plain terms.
_LUCENE_RESERVED = re.compile(r"\b(AND|OR|NOT|TO)\b")


def escape_lucene(text):
    """Backslash-escape Lucene metacharacters (and clause operators)."""
    escaped = "".join("\\" + c if c in _LUCENE_SPECIALS else c for c in text)
    return _LUCENE_RESERVED.sub(lambda m: "\\" + m.group(1), escaped)


_CYPHER_HEADER = """
CYPHER 25
LET
query = $query,
queryVector = $queryVector,
shortQueryVector = $shortQueryVector,
longQueryVector = $longQueryVector,
finalK = $finalK,
rrfConstant = $rrfConstant,
sourceWeights = $sourceWeights,
thresholds = $thresholds

CALL (query, queryVector, shortQueryVector, longQueryVector) {
"""

_CYPHER_FUSION_TAIL = """
}

LET weight = coalesce(sourceWeights[source], 1.0)
LET contribution = weight / (rrfConstant + sourceRank)

WITH result, finalK, thresholds, source, sourceRank, rawScore, passed, weight, contribution
ORDER BY result.id ASC, source ASC, sourceRank ASC

WITH
result,
finalK,
thresholds,
collect({
    source: source,
    sourceRank: sourceRank,
    weight: weight,
    rawScore: rawScore,
    passed: passed,
    contribution: contribution
}) AS contributions

// Plain wRRF over every hit (kept for display / backwards compatibility) and
// wRRF over the threshold survivors only (the relevance-aware fused score).
LET wrrf = reduce(wrrf = 0.0, contribution IN contributions |
wrrf + contribution.contribution
)
LET strongWrrf = reduce(acc = 0.0, c IN contributions |
acc + CASE WHEN c.passed THEN c.contribution ELSE 0.0 END
)

// Tiering by agreement between independent retrieval FAMILIES (lexical vs
// semantic). The title/subtitle/description vectors of one document are not
// independent votes, so two vector indexes alone do not make a result strong;
// a single very similar vector hit (>= highSim) does.
LET lexicalPass = any(c IN contributions WHERE c.passed AND c.source IN thresholds.lexicalSources)
LET semanticPass = any(c IN contributions WHERE c.passed AND NOT c.source IN thresholds.lexicalSources)
LET maxVec = reduce(m = 0.0, c IN contributions |
CASE WHEN NOT c.source IN thresholds.lexicalSources AND c.rawScore > m THEN c.rawScore ELSE m END
)
LET tier = CASE
    WHEN thresholds.singleSource AND (lexicalPass OR semanticPass) THEN 'strong'
    WHEN thresholds.singleSource THEN 'far'
    WHEN (lexicalPass AND semanticPass) OR maxVec >= thresholds.highSim THEN 'strong'
    ELSE 'far'
END
LET tierRank = CASE tier WHEN 'strong' THEN 0 ELSE 1 END

// Strong results first, so finalK never cuts a strong result in favour of a
// far one; inside each tier the survivors-only wRRF decides.
ORDER BY tierRank ASC, strongWrrf DESC, wrrf DESC, result.id ASC

WITH collect({
result: result,
sources: [contribution IN contributions | contribution.source],
sourceRanks: [contribution IN contributions | contribution.sourceRank],
rawScores: [contribution IN contributions | contribution.rawScore],
passed: [contribution IN contributions | contribution.passed],
wrrf: wrrf,
strongWrrf: strongWrrf,
tier: tier,
tierRank: tierRank
}) AS orderedRows, finalK
LET limitedRows = orderedRows[..finalK]

UNWIND limitedRows AS row
WITH row, row.result AS n
WITH row, n,
     [(n)-[rm:has_main_function]->(p:Purpose) | p.name] AS mainPurposeNames,
     [ (parent:Contribution)-[rp:recommends|highlights_gap]->(n) | parent ][0] AS oiParent
WITH row, n, mainPurposeNames, oiParent,
     [ (oiParent)-[rpm:has_main_function]->(p:Purpose) | p.name ] AS parentMainPurposeNames,
     [ (ca:ContributionActor)-[rc:contributed_to]->(n) | {name: ca.name, type: ca.type} ]
     + [ (ca2:ContributionActor)-[rc2:project_contributor]->(n) | {name: ca2.name, type: ca2.type} ] AS actorEntries
WITH row, n, mainPurposeNames, parentMainPurposeNames, oiParent, actorEntries,
     [ (n)-[:describes]->(p:Phenomenon) | p.name ] AS phenomenonNames,
     [ (n)-[:has_as_main_topic]->(t:Topic) | t.name ] AS topicNames,
     [ (n)-[:refers_to_geographic_area]->(g:GeographicArea) | g.name ] AS geoAreaNames,
     [ (oiParent)-[:describes]->(p:Phenomenon) | p.name ] AS parentPhenomenonNames,
     [ (oiParent)-[:has_as_main_topic]->(t:Topic) | t.name ] AS parentTopicNames,
     [ (oiParent)-[:refers_to_geographic_area]->(g:GeographicArea) | g.name ] AS parentGeoAreaNames
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
    n.releaseYear AS releaseYear,
    oiParent.releaseYear AS parentReleaseYear,
    row.sources AS sources,
    row.sourceRanks AS sourceRanks,
    row.rawScores AS rawScores,
    elementId(n) AS neo4j_id,
    mainPurposeNames AS mainPurposeNames,
    oiParent.title AS parentTitle,
    oiParent.officialTitle AS parentOfficialTitle,
    oiParent.id AS parentId,
    labels(oiParent) AS parentLabels,
    elementId(oiParent) AS parentNeo4jId,
    head([(oiParent)-[:has_formal_type]->(pft:FormalType) | properties(pft)]) AS parentFormalTypeProps,
    parentMainPurposeNames AS parentMainPurposeNames,
    actorEntries AS actorEntries,
    phenomenonNames AS phenomenonNames,
    topicNames AS topicNames,
    geoAreaNames AS geoAreaNames,
    parentPhenomenonNames AS parentPhenomenonNames,
    parentTopicNames AS parentTopicNames,
    parentGeoAreaNames AS parentGeoAreaNames,
    head([(n)-[:has_formal_type]->(ft:FormalType) | properties(ft)]) AS formalTypeProps,
    row.wrrf AS wrrf,
    row.strongWrrf AS strongWrrf,
    row.passed AS passed,
    row.tier AS tier
ORDER BY row.tierRank ASC, row.strongWrrf DESC, row.wrrf DESC, n.id ASC;
"""


def _ranked_tail(node_var, source, lexical):
    """Shared end of every source fragment: rank the (already score-sorted)
    hits and flag which ones pass the relevance threshold of their branch.

    The fragments read the `$thresholds` PARAMETER, not the `thresholds`
    variable of the header: every `WITH` inside a fragment would drop an
    imported variable, while parameters are visible everywhere.

    Both cuts keep a PREFIX of the sorted list (absolute floor on vectors,
    relative-to-top cut on BM25), so a survivor's sourceRank is the same as if
    ranks were computed after filtering: "wRRF over the survivors" is simply
    the sum of the contributions with passed = true (see the fusion tail).
    """
    if lexical:
        # BM25 is not comparable across queries: relative cut against the
        # branch's top score, plus a small absolute floor so a lone weak hit
        # (trivially its own top) does not pass by itself.
        top = ("LET top = CASE WHEN size(rows) = 0 THEN 0.0 "
               "ELSE rows[0].rawScore END\n")
        passed = (f"rawScore >= $thresholds.bm25Alpha * top\n"
                  f"        AND rawScore >= coalesce($thresholds.bm25Floor['{source}'], 0.0)")
    else:
        # Cosine (Neo4j-normalised to [0, 1]) is comparable across queries:
        # absolute floor, per source.
        top = ""
        passed = (f"rawScore >= coalesce($thresholds.minSim['{source}'], "
                  f"$thresholds.minSimDefault)")
    return f"""WITH collect({{node: {node_var}, rawScore: score}}) AS rows
{top}UNWIND CASE WHEN size(rows) = 0 THEN [] ELSE range(0, size(rows) - 1) END AS rankIndex
LET rawScore = rows[rankIndex].rawScore
RETURN
    rows[rankIndex].node AS result,
    '{source}' AS source,
    rankIndex + 1 AS sourceRank,
    rawScore,
    {passed} AS passed
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
""" + _ranked_tail('result', 'fulltext', lexical=True)


def _title_fragment(labels):
    """Title-only fulltext source (`title_fulltext` index).

    Same label filter as _fulltext_fragment, so the Doc Type checkboxes keep
    working: an empty `labels` tuple means no WHERE clause at all.
    """
    where = ""
    if labels:
        conds = " OR ".join(f"'{lab}' IN labels(result)" for lab in labels)
        where = f"\nWHERE {conds}"
    return f"""
CALL db.index.fulltext.queryNodes('title_fulltext', query, {{limit: $sourceK}})
YIELD node AS result, score
WITH result, score{where}
ORDER BY score DESC, result.id ASC
""" + _ranked_tail('result', 'title', lexical=True)


def _vector_fragment(label, index, vec, source):
    # Bare subquery body: _build_cypher() joins the fragments with UNION ALL.
    return f"""
MATCH (result:{label})
    SEARCH result IN (
        VECTOR INDEX `{index}`
        FOR {vec}
        LIMIT $sourceK
    ) SCORE AS score
WITH result, score
ORDER BY score DESC, result.id ASC
""" + _ranked_tail('result', source, lexical=False)


def _authors_fragment(labels):
    """Author source: BM25 over `authors_fulltext`, resolved to the Contribution
    each actor contributed to.

    `labels` applies the same Doc Type filter as the other sources, so in
    `author` mode the result set empties out when Contribution is filtered out.
    """
    where = ""
    if labels:
        conds = " OR ".join(f"'{lab}' IN labels(oi)" for lab in labels)
        where = f"\nWHERE {conds}"
    # Bare subquery body: _build_cypher() joins the fragments with UNION ALL.
    return f"""
CALL db.index.fulltext.queryNodes('authors_fulltext', query, {{limit: $sourceK}})
YIELD node AS actor, score
WITH actor, score
MATCH (actor)-[ra:contributed_to]->(oi:Contribution)
WITH oi, max(score) AS score{where}
ORDER BY score DESC, oi.id ASC
""" + _ranked_tail('oi', 'authors', lexical=True)


_cypher_cache = {}


def _build_cypher(labels, mode="nl"):
    """Compose the search query for the requested labels and search mode.

    `labels` is None / empty => no filter. Otherwise only the sources of the
    requested labels participate and the fulltext source is restricted to
    those same labels.

    `mode` selects WHICH sources participate (see run_search):
        nl        BM25 + every vector index + authors  (hybrid default)
        keywords  BM25 over `search_fulltext` only
        title     BM25 over `title_fulltext` only
        author    BM25 over `authors_fulltext` only (Contribution rows)
    The cache key therefore includes the mode: the fragment set differs.
    """
    if mode not in SEARCH_MODES:
        raise ValueError(f"unknown search mode {mode!r}; expected one of {SEARCH_MODES}")
    key = (mode, tuple(labels) if labels else ())
    cached = _cypher_cache.get(key)
    if cached is not None:
        return cached

    active = set(key[1])
    if not active:
        active = set(_ALL_LABELS)
    type_filter = key[1]

    fragments = []
    if mode in ("nl", "keywords"):
        fragments.append(_fulltext_fragment(type_filter))
    elif mode == "title":
        fragments.append(_title_fragment(type_filter))
    # author mode always runs the authors fragment: the label filter inside it
    # empties the branch when Contribution is filtered out of the results.
    if mode == "author" or (mode == "nl" and "Contribution" in active):
        fragments.append(_authors_fragment(type_filter))
    if mode == "nl":
        for label in _ALL_LABELS:
            if label in active:
                for index, vec, source in _SOURCES_BY_LABEL[label]:
                    fragments.append(_vector_fragment(label, index, vec, source))

    # Every fragment is a bare `CALL/MATCH ... RETURN` body; the UNIONs are the
    # JOINS between them, not part of the fragments. That matters for modes
    # with a single source (title/author): a leading UNION ALL would open the
    # subquery and be a syntax error.
    cypher = _CYPHER_HEADER + "\nUNION ALL\n".join(fragments) + _CYPHER_FUSION_TAIL
    _cypher_cache[key] = cypher
    return cypher


def _record_type(labels):
    # Type comes from the node label itself, so a result found ONLY via the
    # fulltext index still gets a proper Contribution / Recommendation / Gap
    # badge instead of being lumped as "fulltext".
    if "Contribution" in labels:
        return "Oggetto Informativo"
    if "Recommendation" in labels:
        return "Raccomandazione"
    if "Gap" in labels:
        return "Lacuna"
    if "Project" in labels:
        return "Progetto"
    return "Full-text"


# FormalType nodes store their long description ("Documento / report -->
# rapporti, ...") under `name` in some dumps and `C1` in others, so the raw
# properties map is read and whichever key is present is used. None when the
# node has no FormalType. The frontend maps the text to a short badge word
# (Doc / Model / Plan / Norm / Data).
def _formal_type_text(props):
    props = props or {}
    return props.get("name") or props.get("C1")


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


# A5 Phenomenon: the closed list of options (keep in lockstep with the
# Phenomena group of TAXONOMY_GROUPS in static/js/state.js).
PHENOMENA = (
    "Acqua alta e allagamenti",
    "Assetto urbano e territorio",
    "Attività economiche",
    "Biodiversità",
    "Clima e variabili climatiche",
    "Dati, monitoraggio e sistemi informativi",
    "Dinamica mareale e surge",
    "Ecosistemi lagunari",
    "Infrastrutture e opere",
    "Innalzamento del livello del mare",
    "Popolazione e attività umane",
    "Processi decisionali e governance",
    "Qualità delle acque",
    "Salute e benessere",
    "Sedimenti e morfologia lagunare",
    "Subsidenza",
)
_PHENOMENA_SET = frozenset(PHENOMENA)
# Spellings found in the graph -> canonical option. The two halves of
# "Dati, monitoraggio e sistemi informativi" exist as separate nodes because
# an earlier ingestion split that option on its own comma.
_PHENOMENON_ALIASES = {
    "Infrastrutture e opere (es. MOSE)": "Infrastrutture e opere",
    "Dati": "Dati, monitoraggio e sistemi informativi",
    "monitoraggio e sistemi informativi": "Dati, monitoraggio e sistemi informativi",
}


def _split_phenomena(names):
    """Normalise Phenomenon names into the closed A5 option list.

    The latest ingestion stores the whole multiple-choice answer as ONE node
    named "opt1, opt2, ..." instead of one node per option. A plain split on
    ", " is not enough because one option contains a comma itself, so the
    pieces are re-joined pairwise whenever the pair is a known option.
    Unknown pieces are kept as they are (the UI ignores tags it does not know).
    TODO: fix at ingestion (one Phenomenon node per option); this then
    becomes a no-op.
    """
    out = []
    for name in names or []:
        if not name:
            continue
        if name in _PHENOMENA_SET:
            out.append(name)
            continue
        parts = [p.strip() for p in name.split(", ") if p.strip()]
        i = 0
        while i < len(parts):
            pair = parts[i] + ", " + parts[i + 1] if i + 1 < len(parts) else None
            if pair in _PHENOMENA_SET:
                out.append(pair)
                i += 2
                continue
            out.append(_PHENOMENON_ALIASES.get(parts[i], parts[i]))
            i += 1
    return out


def _unique_names(*groups):
    """Order-preserving union of name lists: own values first, then parents'."""
    seen, out = set(), []
    for group in groups:
        for name in group or []:
            if name and name not in seen:
                seen.add(name)
                out.append(name)
    return out


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
        # Relevance tiering (see THRESHOLDS in query_config.py): "strong" or
        # "far"; `passed` is aligned with `sources`; `fused_score` is the wRRF
        # over the threshold survivors only.
        "tier": record['tier'],
        "passed": record['passed'],
        "fused_score": round(record['strongWrrf'], 4),
        "neo4j_id": record['neo4j_id'],
        # Projects have `name` (no `title`); Contributions carry BOTH
        # `officialTitle` (C2, English) and `title` (A1, Italian).
        "title": record['title'] or record['name'],
        "officialTitle": record['officialTitle'] or record['title'] or record['name'],
        # MAIN purposes only: the search page's "Main purpose" filter is the
        # main-purpose filter (stricter — a secondary function no longer
        # matches). Rec/Gap nodes carry no purposes of their own, so they
        # still inherit their source document's MAIN purposes, which is what
        # keeps purpose filtering meaningful for them.
        "purposes": _purpose_labels(
            record['mainPurposeNames']
            + record['parentMainPurposeNames']
        ),
        # A5 Phenomenon, B3 Topic and C8a2 GeographicArea: three closed
        # vocabularies feeding the checkbox filters on the search page. Like
        # purposes, Rec/Gap nodes carry none of their own and inherit the
        # values of the OI they belong to; Projects have none at all.
        "phenomena": _unique_names(_split_phenomena(record['phenomenonNames']),
                                   _split_phenomena(record['parentPhenomenonNames'])),
        "topics": _unique_names(record['topicNames'],
                                record['parentTopicNames']),
        "geographicAreas": _unique_names(record['geoAreaNames'],
                                         record['parentGeoAreaNames']),
        "actors": actors,
        # Contributor filters: people and institutions, kept apart.
        "people": people,
        "institutions": institutions,
        # Release year of the Contribution (the card shows it). Rec/Gap nodes
        # carry no year of their own, so they take the source document's;
        # Projects have neither -> None, and the card simply shows no year.
        "releaseYear": record['releaseYear'] or record['parentReleaseYear'],
    }
    entry["doc_key"] = _doc_key(labels, record)
    if entry["type"] == "Oggetto Informativo":
        entry.update({
            "abstract": record['abstract'],
            "findings": record['findings'],
            "submission_id": record['id'],
            # Raw FormalType text; drives the short Paper/Model/Plan/Norm/
            # Dataset badge on the result card (falls back to "Document"
            # when absent).
            "formalType": _formal_type_text(record['formalTypeProps']),
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
            # Raw FormalType of the parent: the result card labels its parent
            # row by the document type ("Plan: ...") instead of a generic word.
            "formalType": _formal_type_text(record['parentFormalTypeProps']),
        }
    return entry


def _doc_key(labels, record):
    """Identity of a result that SURVIVES a graph rebuild (unlike elementId):
    evaluation judgments are keyed on it. Mirrors KGB's identity properties
    (kg_ids.PREFERRED_KEY): Contribution.id, Project.id_num, and the text for
    Recommendation (content) / Gap (description), which carry no id; the text
    is hashed to keep the key short."""
    def h(text):
        return hashlib.sha1(_s(text).encode("utf-8")).hexdigest()[:16]
    if "Contribution" in labels:
        return f"C:{_s(record['id'])}"
    if "Project" in labels:
        return f"P:{_s(record['id_num'])}"
    if "Recommendation" in labels:
        return f"R:{h(record['content'])}"
    if "Gap" in labels:
        return f"G:{h(record['description'])}"
    return f"X:{record['neo4j_id']}"


def _rerank_text(r):
    """The document side the reranker reads (cross-encoder pair / ColBERT
    doc vectors), built from a search result. The builder itself lives in
    embedding_text.rerank_text, shared with scripts/build_colbert_index.py
    (which builds the same string from raw node properties): the ColBERT
    index is keyed by its hash. Model max length truncates the overflow."""
    title = r.get("officialTitle") or r.get("title")
    t = r["type"]
    if t == "Oggetto Informativo":
        body = (r.get("abstract"), r.get("findings"))    # = description, findings
    elif t == "Raccomandazione":
        body = (r.get("content"), r.get("motivation"))
    else:  # Lacuna, Progetto
        body = (r.get("description"),)
    return rerank_text(title, *body)


def _sigmoid(x):
    return 1.0 / (1.0 + math.exp(-x))


def _score_cross_encoder(query_text, head, timing, name):
    """One pass of the registry cross-encoder `name` over the (query, doc)
    pairs; score = sigmoid(logit) or the raw logit, per the model's
    `activation` in query_config.RERANKER_MODELS."""
    import torch
    model = query_config.get_reranker(name)
    start = time.time()
    # Raw logits, activation applied here: independent of the library's
    # default activation for single-label models.
    logits = model.predict([(query_text, _rerank_text(r)) for r in head],
                           activation_fn=torch.nn.Identity(),
                           convert_to_numpy=True)
    timing["rerank_model_s"] = time.time() - start
    if query_config.RERANKER_MODELS[name]["activation"] == "identity":
        return [float(x) for x in logits]
    return [_sigmoid(float(x)) for x in logits]


def _score_colbert(query_text, head, timing):
    """MaxSim per candidate against the precomputed ColBERT index; only the
    query is encoded (plus index misses when COLBERT_ON_MISS == "encode").
    A candidate left unscored gets None."""
    import colbert_m3
    store = query_config.get_colbert_store()
    start = time.time()
    q = colbert_m3.encode_query(query_text)
    timing["rerank_query_encode_s"] = time.time() - start

    texts = [_rerank_text(r) for r in head]
    docs = [store.get(rerank_text_key(t)) for t in texts]
    missing = [i for i, d in enumerate(docs) if d is None]
    timing["colbert_misses"] = len(missing)
    if missing and query_config.COLBERT_ON_MISS == "encode":
        start = time.time()
        for i, vecs in zip(missing, colbert_m3.encode_documents([texts[i] for i in missing])):
            docs[i] = vecs
        timing["rerank_miss_encode_s"] = time.time() - start
    if missing:
        logger.info("colbert: %d/%d candidates not in the index (%s)", len(missing),
                    len(head), "encoded now" if query_config.COLBERT_ON_MISS == "encode"
                    else "left unscored")

    start = time.time()
    scores = [None if d is None else colbert_m3.maxsim(q, d) for d in docs]
    timing["rerank_maxsim_s"] = time.time() - start
    return scores


def rerank(query_text, results, top_n=None, threshold=None, kind=None):
    """Re-score the first `top_n` results with the reranker and re-tier ALL
    results on its score. Returns (results, seconds, timing).

    `kind` is a reranker spec (query_config.resolve_reranker): "colbert",
    "cross-encoder" (= query_config.RERANKER) or a RERANKER_MODELS name;
    None = the server default.

    - reranked:   tier = strong iff rerank_score >= threshold (the model's
      own; a model with threshold None is uncalibrated: all "far")
    - not reranked (beyond top_n, or a ColBERT index miss left unscored):
      tier = far, kept after the reranked ones in their wRRF order
    The pre-fusion tier is kept as `prefusion_tier` for comparison.
    `timing` breaks the seconds down per step (plus colbert_misses).
    """
    kind, name = query_config.resolve_reranker(kind)
    colbert = kind == "colbert"
    if top_n is None:
        top_n = query_config.COLBERT_RERANK_TOP_N if colbert else query_config.RERANK_TOP_N
    if threshold is None:
        threshold = (query_config.COLBERT_RERANK_THRESHOLD if colbert
                     else query_config.RERANKER_MODELS[name]["threshold"])
    if threshold is None:
        logger.warning("reranker %s has no calibrated threshold: ranking only, "
                       "every result is 'far'", name)
        threshold = math.inf
    head, tail = results[:top_n], results[top_n:]
    timing = {"reranker": name or kind}
    if not head:
        return results, 0.0, timing
    start = time.time()
    if colbert:
        scores = _score_colbert(query_text, head, timing)
    else:
        scores = _score_cross_encoder(query_text, head, timing, name)
    elapsed = time.time() - start
    unscored = []
    for r, score in zip(head, scores):
        r["prefusion_tier"] = r["tier"]
        if score is None:
            r["rerank_score"] = None
            r["tier"] = "far"
            unscored.append(r)
        else:
            r["rerank_score"] = round(score, 4)
            r["tier"] = "strong" if r["rerank_score"] >= threshold else "far"
    for r in tail:
        r["rerank_score"] = None
        r["prefusion_tier"] = r["tier"]
        r["tier"] = "far"
    scored = [r for r in head if r["rerank_score"] is not None]
    scored.sort(key=lambda r: (r["tier"] != "strong", -r["rerank_score"]))
    strong = [r for r in scored if r["tier"] == "strong"]
    far = [r for r in scored if r["tier"] != "strong"]
    timing = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in timing.items()}
    return strong + far + unscored + tail, elapsed, timing


def embed_query(text: str):
    """Encode the query text into the embedding vector.

    TODO(future): query expansion could produce THREE different vectors here:
      - shortQueryVector  (e.g. truncated / keyword-only query)
      - longQueryVector   (e.g. expanded / rewritten query)
      - queryVector       (default, used by recommendation + gap indexes)
    Currently all three are identical.
    """
    return encode_query(text)


def run_search(query_text, source_k=20, final_k=50, rrf_constant=60,
               source_weights=None, types=None, mode="nl", thresholds=None,
               rerank_results=None, rerank_top_n=None, rerank_threshold=None,
               reranker=None):
    """Run the search (hybrid by default; the text-only modes skip embedding).

    Args:
        mode: one of SEARCH_MODES — which retrieval sources participate:
            "nl"        hybrid default: BM25 + every vector index + authors,
                        wRRF-fused (this is what the UI calls
                        "natural language")
            "keywords"  BM25 over `search_fulltext` only
            "title"     BM25 over `title_fulltext` only
            "author"    BM25 over `authors_fulltext` only (Contribution rows)
            Text-only modes do NOT embed the query, so `embedding_time_s` is
            0 and no model call happens.
        types: optional list of Neo4j labels to restrict results to
            ("Contribution", "Recommendation", "Gap", "Project"). Empty/None
            => all types. In "author" mode results are Contributions anyway,
            so filtering to another type yields an empty result set.
        thresholds: optional overrides of query_config.THRESHOLDS (keys
            minSim, highSim, bm25Alpha, bm25Floor; see thresholds_for). Each
            result is tiered "strong" / "far" against them; strong results
            come first.
        rerank_results: re-score the top candidates with the cross-encoder
            (query_config.USE_RERANKER when None; never in "author" mode).
            When it runs, the tier comes from the rerank score.
        rerank_top_n / rerank_threshold: override the reranker's top-N /
            threshold (evaluation, calibration).
        reranker: reranker spec: "colbert", "cross-encoder" (=
            query_config.RERANKER) or a query_config.RERANKER_MODELS name;
            None = server default (RERANKER_KIND / RERANKER).

    Returns:
        {
          "query": str,
          "mode": str,
          "types": [str] | None,
          "embedding_time_s": float,
          "search_time_s": float,
          "results": [ { ... per-result dict, see _serialize ... } ]
        }
    """
    if mode not in SEARCH_MODES:
        raise ValueError(f"unknown search mode {mode!r}; expected one of {SEARCH_MODES}")

    if source_weights is None:
        # Only the hybrid mode fuses several sources, so only it needs the
        # length-based heuristic. Text-only modes run a single source, whose
        # weight is the same for every row (=> ranking unaffected), so they
        # rely on coalesce(sourceWeights[source], 1.0) in the fusion tail.
        source_weights = chooseSourceWeights(query_text) if mode == "nl" else {}

    if mode == "nl":
        start = time.time()
        query_embedding = embed_query(query_text)
        embedding_time = time.time() - start
    else:
        query_embedding = None
        embedding_time = 0.0

    driver = get_driver()
    start_q = time.time()
    records, summary, keys = driver.execute_query(
        _build_cypher(types, mode),
        # Lucene sees the escaped form; the embedding above used the raw text.
        query=escape_lucene(query_text),
        queryVector=query_embedding,
        shortQueryVector=query_embedding,
        longQueryVector=query_embedding,
        sourceK=source_k,
        finalK=final_k,
        rrfConstant=rrf_constant,
        sourceWeights=source_weights,
        thresholds=thresholds_for(mode, thresholds),
        database_=NEO4J_GRAPH,
        routing_=RoutingControl.READ,
    )
    search_time = time.time() - start_q
    results = [_serialize(record) for record in records]

    if rerank_results is None:
        # server default: only the modes configured for it
        reranked = query_config.USE_RERANKER and mode in query_config.RERANK_MODES
    else:
        # explicit request (API flag, evaluation): any mode with text to judge
        reranked = bool(rerank_results) and mode != "author"
    rerank_time = 0.0
    rerank_timing = {}
    if reranked:
        results, rerank_time, rerank_timing = rerank(query_text, results,
                                                     top_n=rerank_top_n,
                                                     threshold=rerank_threshold,
                                                     kind=reranker)

    return {
        "query": query_text,
        "mode": mode,
        "types": tuple(types) if types else None,
        "embedding_time_s": round(embedding_time, 4),
        "search_time_s": round(search_time, 4),
        "reranked": reranked,
        "rerank_time_s": round(rerank_time, 4),
        # per-step breakdown: reranker kind, rerank_model_s (cross-encoder) or
        # rerank_query_encode_s / rerank_maxsim_s / colbert_misses (colbert)
        "rerank_timing": rerank_timing,
        "results": results,
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


# Minimal JSON-able summary of a linked node (child/parent document).
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

# Each entry carries the neighbour's elementId alongside its properties so
# the detail page can link entities (Environment, GeographicArea, ...) to
# their own node page: node.html?eid=...
_RELATED_OUT = (
    "MATCH (n)-[r]->(m) WHERE elementId(n) = $eid "
    "RETURN head(labels(m)) AS lab, type(r) AS rel, "
    "       collect(DISTINCT {eid: elementId(m), props: properties(m)}) AS items"
)
_RELATED_IN = (
    "MATCH (m)-[r]->(n) WHERE elementId(n) = $eid "
    "RETURN head(labels(m)) AS lab, type(r) AS rel, "
    "       collect(DISTINCT {eid: elementId(m), props: properties(m)}) AS items"
)


def _related_item(entry):
    """Trim a neighbour's {eid, props} to {eid, label, url?, props?}.

    Vectors and embedding status flags are dropped (they are bookkeeping,
    like the node's own `vectorProps`).
    """
    props = entry.get("props") or {}
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

    item = {"eid": entry.get("eid"), "label": str(label)}
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
            for entry in row["items"] or []:
                item = _related_item(entry)
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
    """Full details of a single node + its document <-> Rec/Gap neighbourhood.

    Returns None if the node does not exist. Used by GET /node/{eid}.
    Vector properties are summarised as their dimension (the raw float
    arrays — 768 values for the current model — would bloat the payload);
    everything else is passed through.
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
        if not k.endswith("mbeddingStatus"):
            if isinstance(v, (list, tuple)) and len(v) > 30 and all(
                    isinstance(x, (int, float)) for x in v):
                entry["vectorProps"][k] = len(v)
            else:
                entry["properties"][k] = v

    # Same FormalType string the "Formal type" section shows, lifted to the
    # top level so the header badge can map it to Doc/Model/Plan/Norm/Data.
    ft_bucket = entry["related"].get("formalType")
    entry["formalType"] = ft_bucket["items"][0]["label"] if (
        ft_bucket and ft_bucket["items"]) else None

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
            "labels(parent) AS plabels, properties(parent) AS pprops, "
            "head([(parent)-[:has_formal_type]->(ft:FormalType) | properties(ft)]) AS ftprops",
            eid=element_id, database_=NEO4J_GRAPH, routing_=RoutingControl.READ)
        eids = [r["peid"] for r in prows]
        purposes = _fetch_purposes(eids)
        entry["parents"] = [
            {**_make_brief(r["peid"], r["pprops"], r["plabels"], r["rel"]),
             "purposes": purposes.get(r["peid"], []),
             # Parent documents carry their FormalType too -> link-item badge.
             "formalType": _formal_type_text(r["ftprops"])}
            for r in prows
        ]

    return entry