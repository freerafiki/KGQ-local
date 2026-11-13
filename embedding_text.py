"""Reproduce the exact text strings that insert_embeddings.py feeds to the
embedding model.

export_embeddings.py and import_embeddings.py both need these builders so the
join key (sha256 of the embedded text) matches between the two. Keep the
concatenation/prefixes in lockstep with insert_embeddings.py.

This module is deliberately dependency-free (no sentence-transformers): it is
imported by export_embeddings.py / import_embeddings.py, which must run WITHOUT
the model being installed or downloaded. It also owns the embedding model
identity so query_config.py (model loader), export and import all agree.
"""

# Identity of the embedding model. query_config.py loads THIS model name and
# re-exports the model's AUTODETECTED dimension as `embedding_dims`
# (cross-checked against EMBEDDING_DIMS below); export_embeddings.py writes it
# into the JSON meta record and import_embeddings.py refuses files whose
# meta does not match (wrong model/dims => incompatible vectors).
EMBEDDING_MODEL_NAME = "BAAI/bge-m3"
EMBEDDING_DIMS = 1024

# EMBEDDING_MODEL_NAME = "google/embeddinggemma-2"
# EMBEDDING_DIMS = 768
# Native output width of the model above (len(model.encode(...)) == 768, and
# model.get_sentence_embedding_dimension() == 768). insert_embeddings.py hands
# it to Neo4j as `vector.dimensions` when creating the vector indexes, so a
# stale value here does NOT fail at write time: the index comes up ONLINE but
# indexes nothing, and every hybrid ("nl") search then dies with
#   Vector index 'x' has a configured dimensionality of 512, but the provided
#   vector has dimension 768.
# query_config.py autodetects the width from the loaded model and warns loudly
# when the two disagree; this constant stays as the model-free fallback for
# export/import, which must run without sentence-transformers installed.


# Task prompts, chosen BY THE MODEL. EmbeddingGemma is trained with asymmetric
# retrieval prompts: queries and documents are encoded with DIFFERENT
# prefixes, and plain `.encode(text)` (no prompt) degrades retrieval quality
# and makes cosine scores hard to threshold. BGE-M3 is trained WITHOUT any
# prompt, so it must get none. The prompt is prepended to the text by
# sentence-transformers (`encode(text, prompt=...)`); it is NOT part of the
# text builders below, so the sha256 join key used by export/import is the
# same with or without prompts — which is exactly why export/import ALSO
# stamp/check this flag: same key, incompatible vectors.
#
# Key = substring of EMBEDDING_MODEL_NAME (case-insensitive), value =
# (query prompt, document prompt). Models not listed get no prompts.
#
# Whenever the RESULTING flag changes (model switch, or editing this table),
# re-run scripts/insert_embeddings.py with RESET_EMBEDDING_STATUS (every
# vector must be re-encoded) AND recalibrate query_config.THRESHOLDS.
MODEL_PROMPTS = {
    "embeddinggemma": ("task: search result | query: ", "title: none | text: "),
}
_prompts = next((p for key, p in MODEL_PROMPTS.items()
                 if key in EMBEDDING_MODEL_NAME.lower()), None)
USE_EMBEDDING_PROMPTS = _prompts is not None
QUERY_PROMPT, DOCUMENT_PROMPT = _prompts or (None, None)


def _s(value):
    """Coerce a property value to a stable string.

    The graph is heterogeneous (still being ingested): a field may be a string
    or a list of values. Always producing the same string keeps the sha256 join
    key consistent between insert/export/import regardless of the stored type.
    """
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(_s(x) for x in value)
    return str(value)


def contribution_description_text(description, findings):
    text = ""
    if _s(description):
        text += "Description: " + _s(description) + ". "
    if _s(findings):
        text += "Findings: " + _s(findings)
    return text


def contribution_title_text(title):
    return _s(title)


def contribution_subtitle_text(subtitle):
    return _s(subtitle)


def recommendation_text(content, motivation):
    text = ""
    if _s(content):
        text += "Contenuto: " + _s(content) + ". "
    if _s(motivation):
        text += "Findings: " + _s(motivation)
    return text


def gap_text(description):
    return _s(description)


def project_text(description):
    """Projects are embedded on their description only (name is indexed
    full-text separately, see insert_embeddings.py)."""
    return _s(description)


# ---- Reranker text -------------------------------------------------------------
# The document side the reranker reads (cross-encoder pair, or ColBERT token
# vectors): title first (most informative per token), then the body fields.
# The ColBERT index is keyed by the sha256 of THIS string, built on one
# machine (scripts/build_colbert_index.py, from raw node properties) and
# looked up on another (query_util.rerank, from search results), so both
# sides MUST go through rerank_text().
#
# label -> body properties, in order. Contribution `description` is what the
# search results call `abstract`.
RERANK_BODY_FIELDS = {
    "Contribution": ("description", "findings"),
    "Recommendation": ("content", "motivation"),
    "Gap": ("description",),
    "Project": ("description",),
}


def rerank_title(official_title, title, name):
    """Same fallback as query_util._serialize's `officialTitle`."""
    return official_title or title or name


def rerank_text(title, *body):
    return "\n".join(_s(p) for p in (title, *body) if _s(p).strip())


def rerank_text_key(text):
    """Join key of the ColBERT index (sha256 of the exact encoded text)."""
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# Which node text each embedding field comes from, for export/import.
# label -> field -> (cypher field aliases needed, text builder)
TEXT_BUILDERS = {
    "Contribution": {
        "descrEmbedding": (["description", "findings"], contribution_description_text),
        "titleEmbedding": (["officialTitle"], contribution_title_text),
        "subtitleEmbedding": (["subtitle"], contribution_subtitle_text),
    },
    "Recommendation": {
        "embedding": (["content", "motivation"], recommendation_text),
    },
    "Gap": {
        "embedding": (["description"], gap_text),
    },
    "Project": {
        "embedding": (["description"], project_text),
    },
}

# per-label: embedding property -> its "done" status flag
FIELD_STATUS = {
    "Contribution": {
        "descrEmbedding": "descrEmbeddingStatus",
        "titleEmbedding": "titleEmbeddingStatus",
        "subtitleEmbedding": "subtitleEmbeddingStatus",
    },
    "Recommendation": {"embedding": "embeddingStatus"},
    "Gap": {"embedding": "embeddingStatus"},
    "Project": {"embedding": "embeddingStatus"},
}