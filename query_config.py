# EMBEDDING MODEL
# Model identity lives in embedding_text.py (dependency-free) so that
# export_embeddings.py / import_embeddings.py can verify it without loading
# sentence-transformers. Only the model OBJECT is created here.
import logging

from sentence_transformers import SentenceTransformer
from dotenv import load_dotenv
import os 
from embedding_text import EMBEDDING_MODEL_NAME, EMBEDDING_DIMS

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

