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



# ---- Rerankers ------------------------------------------------------------------
# The fused candidates are re-scored by a reranker; its score replaces the
# raw-score tiering above whenever it runs (the pre-fusion thresholds then only
# shape the candidate list): strong = score >= the model's threshold.
#
# Which reranker runs:
#   "cross-encoder"  the RERANKER_MODELS entry named by RERANKER: reads
#                    (query, doc) pairs together at query time
#   "colbert"        BGE-M3 late interaction (colbert_m3.py): doc token
#                    vectors precomputed on the build machine
#                    (scripts/build_colbert_index.py), only the query is
#                    encoded here, then MaxSim. Needs the index folder.
# Per request (API `reranker`, run_search(reranker=...), the eval / benchmark
# scripts) a SPEC picks one: "colbert", "cross-encoder" (= RERANKER), or any
# RERANKER_MODELS name, so several cross-encoders can be compared in one
# process. Only registry names are accepted: a request can never make the
# server download an arbitrary model.
USE_RERANKER = True
RERANKER_KIND = "cross-encoder"
RERANKER_KINDS = ("cross-encoder", "colbert")

# The cross-encoder registry (RERANKER_MODELS: model id, score activation,
# threshold per model) and the default RERANKER live in reranker_models.py,
# dependency-free so the eval / calibration scripts can list them without
# loading any model. Edit them THERE.
from reranker_models import RERANKER_MODELS, RERANKER  # noqa: E402

RERANK_TOP_N = 30          # candidates re-scored per query
RERANK_MAX_LENGTH = 512    # tokens of (query + document) seen by the model
# DISCUSSION
# Modes reranked BY DEFAULT. Title mode is a known-item lookup on titles
# (single vague words like "climate" score low on a cross-encoder and would
# all be pushed to "far"); author mode matches NAMES, nothing to judge.
# An explicit rerank=True request still reranks title mode (evaluation).
RERANK_MODES = ("nl", "keywords", "title") # remove "title" ?


def reranker_specs():
    """Every value accepted as a reranker spec."""
    return ("cross-encoder", "colbert", *RERANKER_MODELS)


def resolve_reranker(spec=None):
    """spec -> ("colbert", None) or ("cross-encoder", registry name).
    None = the server default (RERANKER_KIND / RERANKER)."""
    if spec is None:
        spec = RERANKER_KIND
    if spec == "colbert":
        return "colbert", None
    name = RERANKER if spec == "cross-encoder" else spec
    if name not in RERANKER_MODELS:
        raise ValueError(f"unknown reranker {spec!r}; expected one of {reranker_specs()}")
    return "cross-encoder", name


_rerankers = {}
_reranker_lock = threading.Lock()


def get_reranker(name=None):
    """Lazily load a registry cross-encoder once per process (thread-safe:
    FastAPI runs sync endpoints in a thread pool). Loaded models stay cached,
    so a benchmark can compare several in one process."""
    name = name or RERANKER
    if name not in _rerankers:
        with _reranker_lock:
            if name not in _rerankers:
                from sentence_transformers import CrossEncoder
                model_id = RERANKER_MODELS[name]["model"]
                logger.info("loading reranker %s (%s)", name, model_id)
                import torch
                # float32 explicitly on CPU: checkpoints shipped as bfloat16
                # (e.g. Ettin) are much slower there on most CPUs.
                dtype = torch.float16 if torch.cuda.is_available() else torch.float32
                _rerankers[name] = CrossEncoder(model_id,
                                                max_length=RERANK_MAX_LENGTH,
                                                model_kwargs={"dtype": dtype})
    return _rerankers[name]


# ---- ColBERT reranker (RERANKER_KIND == "colbert") ------------------------------
# Folder copied from the build machine (see data_transfer.md).
COLBERT_INDEX_DIR = os.getenv("COLBERT_INDEX_DIR") or None   # None = data/colbert
# Candidates scored by ColBERT: scoring is a few small matmuls, so it can look
# much deeper into the wRRF list than the cross-encoder.
COLBERT_RERANK_TOP_N = 100
# !!! PLACEHOLDER — MaxSim (~[0, 1]) is its own scale, unlike the
# cross-encoder thresholds. Calibrate with scripts/calibrate_thresholds.py
# --reranker colbert.
COLBERT_RERANK_THRESHOLD = 0.5
# A candidate whose text is not in the index (node added or edited after the
# build):
#   "encode"  encode it now (one BGE-M3 pass over the doc: slow on a laptop
#             CPU, but the ranking stays complete)
#   "far"     no forward pass: rerank_score None, tier "far" (after the scored)
COLBERT_ON_MISS = "encode"

_colbert_store = None
_colbert_store_lock = threading.Lock()


def get_colbert_store():
    """Lazily open the ColBERT index (memory-mapped, read-only) once."""
    global _colbert_store
    if _colbert_store is None:
        with _colbert_store_lock:
            if _colbert_store is None:
                import colbert_m3
                _colbert_store = colbert_m3.ColbertStore(
                    COLBERT_INDEX_DIR or colbert_m3.DEFAULT_INDEX_DIR)
    return _colbert_store


def preload_reranker(spec=None):
    """Load everything the default reranker needs (server startup)."""
    kind, name = resolve_reranker(spec)
    if kind == "colbert":
        import colbert_m3
        get_colbert_store()
        colbert_m3.get_colbert_model()
    else:
        get_reranker(name)
