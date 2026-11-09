# EMBEDDING MODEL
# Model identity lives in embedding_text.py (dependency-free) so that
# export_embeddings.py / import_embeddings.py can verify it without loading
# sentence-transformers. Only the model OBJECT is created here.
import logging
import threading

from sentence_transformers import SentenceTransformer
from dotenv import load_dotenv
import os 
from embedding_text import (
    EMBEDDING_MODEL_NAME, EMBEDDING_DIMS,
    USE_EMBEDDING_PROMPTS, QUERY_PROMPT, DOCUMENT_PROMPT,
)

load_dotenv()
HF_TOKEN = os.getenv("HF_TOKEN")

logger = logging.getLogger("kgq.config")

embedding_model = SentenceTransformer(EMBEDDING_MODEL_NAME)

# The vector WIDTH is taken from the loaded model, never from the constant:
# Neo4j indexes are created with `vector.dimensions: embedding_dims`, and a
# wrong width does not fail at write time — the index comes up ONLINE but
# indexes nothing, and every hybrid ("nl") search then errors with
#   Vector index 'x' has a configured dimensionality of 512, but the provided
#   vector has dimension 768.
# `get_sentence_embedding_dimension()` reads it off the model config, so no
# inference is needed; the newer spelling is preferred (6.x renamed it) with
# a fallback for older sentence-transformers.
_embed_dim = getattr(embedding_model, "get_embedding_dimension", None) \
    or embedding_model.get_sentence_embedding_dimension
embedding_dims = _embed_dim()

if embedding_dims != EMBEDDING_DIMS:
    # Loud on purpose: EMBEDDING_DIMS is also what export/import stamp into
    # and check against the JSON meta, so a stale constant silently rejects
    # (or worse, accepts) whole vector files. Fix embedding_text.py instead
    # of muting this.
    logger.warning(
        "EMBEDDING_DIMS in embedding_text.py is %d but %s reports %d "
        "dimensions; using %d. Update the constant to match.",
        EMBEDDING_DIMS, EMBEDDING_MODEL_NAME, embedding_dims, embedding_dims)


# Every encode call goes through these two helpers, so queries and documents
# are guaranteed to use the matching side of the asymmetric prompt pair (see
# USE_EMBEDDING_PROMPTS in embedding_text.py).
def encode_query(text):
    if USE_EMBEDDING_PROMPTS:
        return embedding_model.encode(text, prompt=QUERY_PROMPT)
    return embedding_model.encode(text)


def encode_document(text):
    if USE_EMBEDDING_PROMPTS:
        return embedding_model.encode(text, prompt=DOCUMENT_PROMPT)
    return embedding_model.encode(text)


# ---- Relevance thresholds (tiering) -----------------------------------------
# wRRF only sees ranks, so the fused score cannot tell a perfect match from
# noise. These thresholds act on the RAW per-branch scores, before fusion:
#
#   minSim     vector branches, ABSOLUTE floor. Neo4j reports cosine as
#              (1 + cos) / 2, i.e. in [0, 1] (0.5 == orthogonal). Per source,
#              falling back to minSimDefault.
#   highSim    a single vector hit >= highSim is "strong" on its own.
#   bm25Alpha  lexical branches, RELATIVE cut: keep score >= alpha * top score
#              of that branch for this query (BM25 is not comparable across
#              queries).
#   bm25Floor  lexical branches, small absolute floor per source, so one weak
#              hit (which is trivially its own top) does not pass alone.
#
# A result is "strong" when it passes in BOTH a lexical and a semantic branch,
# or has maxVec >= highSim; otherwise it is "far" (shown behind "show more").
# In single-source modes (keywords/title/author) passing is enough.
#
# !!! PLACEHOLDER VALUES — calibrate with scripts/calibrate_thresholds.py.
# They depend on the embedding model AND on USE_EMBEDDING_PROMPTS: recalibrate
# whenever either changes.
LEXICAL_SOURCES = ('fulltext', 'title', 'authors')

THRESHOLDS = {
    'minSimDefault': 0.75,
    'minSim': {
        'doc_description': 0.75,
        'doc_title': 0.75,
        'doc_subtitle': 0.75,
        'recommendation': 0.75,
        'gap': 0.75,
        'project': 0.75,
    },
    'highSim': 0.85,
    'bm25Alpha': 0.4,
    'bm25Floor': {
        'fulltext': 0.5,
        'title': 0.5,
        'authors': 0.5,
    },
}

# Overrides (for thresholds_for / run_search) that let EVERY hit pass:
# calibration dumps, "show me everything". Scalars on purpose: a scalar
# minSim / bm25Floor REPLACES the per-source maps instead of merging into them.
PERMISSIVE_THRESHOLDS = {
    'minSim': 0.0,
    'highSim': 0.0,
    'bm25Alpha': 0.0,
    'bm25Floor': 0.0,
}


def thresholds_for(mode, overrides=None):
    """Build the $thresholds map for the Cypher query.

    `overrides` (all keys optional) may carry:
        minSim     float (applies to every vector source) or {source: float}
        highSim    float
        bm25Alpha  float
        bm25Floor  float (every lexical source) or {source: float}
    Dict-valued overrides are merged per source over the defaults.
    """
    t = {
        'minSimDefault': THRESHOLDS['minSimDefault'],
        'minSim': dict(THRESHOLDS['minSim']),
        'highSim': THRESHOLDS['highSim'],
        'bm25Alpha': THRESHOLDS['bm25Alpha'],
        'bm25Floor': dict(THRESHOLDS['bm25Floor']),
    }
    for key, value in (overrides or {}).items():
        if value is None:
            continue
        if key == 'minSim':
            if isinstance(value, dict):
                t['minSim'].update(value)
            else:
                t['minSimDefault'] = float(value)
                t['minSim'] = {}
        elif key == 'bm25Floor':
            if isinstance(value, dict):
                t['bm25Floor'].update(value)
            else:
                t['bm25Floor'] = {s: float(value) for s in LEXICAL_SOURCES}
        elif key in ('minSimDefault', 'highSim', 'bm25Alpha'):
            t[key] = float(value)
        else:
            raise ValueError(f"unknown threshold {key!r}")
    t['lexicalSources'] = list(LEXICAL_SOURCES)
    t['singleSource'] = mode != "nl"
    return t


# Per-source fusion weights, selected by query length (see chooseSourceWeights).
# Keys MUST match the source labels of _SOURCES_BY_LABEL in query_util.py: the
# Cypher looks each one up via coalesce(sourceWeights[source], 1.0), and they
# are echoed in the response `sources` (the score line on every result card).
sourceWeights_default = {
    'fulltext': 0.4,        
    'doc_description': 1.3,  
    'doc_title': 0.9,  
    'doc_subtitle': 0.9,  
    'recommendation': 1.0,
    'gap': 1.0,
    'project': 1.0,
    'authors': 1.0,
}
sourceWeights_shortText = {
    'fulltext': 1.0,        
    'doc_description': 0.8,  
    'doc_title': 1.0,  
    'doc_subtitle': 1.0,  
    'recommendation': 0.5,
    'gap': 0.5,
    'project': 1.0,
    'authors': 1.0,
}
sourceWeights_longText = {
    'fulltext': 0.8,        
    'doc_description': 1.5,  
    'doc_title': 0.4,  
    'doc_subtitle': 0.6,  
    'recommendation': 1.5,
    'gap': 1.5,
    'project': 1.0,
    'authors': 1.0,
}
def chooseSourceWeights(query: str):
    """
    The idea is to choose the weights based on the query. 
    Later we could introduce some post-processing as well to improve 
    query alignment with the db
    """
    if len(query) < 50:
        return sourceWeights_shortText
    elif len(query) < 140:
        return sourceWeights_default
    else:
        return sourceWeights_longText



# ---- Cross-encoder reranker ---------------------------------------------------
# The fused candidates are re-scored by a cross-encoder that reads query and
# document TOGETHER. Its sigmoid output is roughly a relevance probability,
# comparable across queries and modes, so ONE absolute threshold separates
# "strong" from "far" — replacing the raw-score tiering above whenever the
# reranker runs (the pre-fusion thresholds then only shape the candidate list).
#
# CPU cost grows with RERANK_TOP_N x RERANK_MAX_LENGTH: lower either if the
# search gets too slow.
USE_RERANKER = True
RERANKER_MODEL_NAME = "BAAI/bge-reranker-v2-m3"   # multilingual (IT/EN)
RERANK_TOP_N = 30          # candidates re-scored per query
RERANK_MAX_LENGTH = 512    # tokens of (query + document) seen by the model
# Author mode matches NAMES: a text cross-encoder has nothing to judge there.
RERANK_MODES = ("nl", "keywords", "title")
# !!! PLACEHOLDER — calibrate with scripts/calibrate_thresholds.py (column
# rerank_score). Strong = rerank score >= this.
RERANK_THRESHOLD = 0.5

_reranker = None
_reranker_lock = threading.Lock()


def get_reranker():
    """Lazily load the cross-encoder once per process (thread-safe: FastAPI
    runs sync endpoints in a thread pool)."""
    global _reranker
    if _reranker is None:
        with _reranker_lock:
            if _reranker is None:
                from sentence_transformers import CrossEncoder
                logger.info("loading reranker %s", RERANKER_MODEL_NAME)
                import torch
                # float32 explicitly: the checkpoint loads as bfloat16, which
                # is much slower on most CPUs (sentence-transformers warns).
                dtype = torch.float16 if torch.cuda.is_available() else torch.float32
                _reranker = CrossEncoder(RERANKER_MODEL_NAME,
                                         max_length=RERANK_MAX_LENGTH,
                                         model_kwargs={"dtype": dtype})
    return _reranker
