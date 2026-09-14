"""Reproduce the exact text strings that insert_embeddings.py feeds to the
embedding model.

export_embeddings.py and import_embeddings.py both need these builders so the
join key (sha256 of the embedded text) matches between the two. Keep the
concatenation/prefixes in lockstep with insert_embeddings.py.
"""


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
}