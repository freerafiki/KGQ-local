# EMBEDDING MODEL
# Model identity lives in embedding_text.py (dependency-free) so that
# export_embeddings.py / import_embeddings.py can verify it without loading
# sentence-transformers. Only the model OBJECT is created here.
from sentence_transformers import SentenceTransformer
from dotenv import load_dotenv
import os 
from embedding_text import EMBEDDING_MODEL_NAME, EMBEDDING_DIMS

load_dotenv()
HF_TOKEN = os.getenv("HF_TOKEN")

embedding_model = SentenceTransformer(EMBEDDING_MODEL_NAME)
embedding_dims = EMBEDDING_DIMS


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

