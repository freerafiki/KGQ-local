# EMBEDDING MODEL 
from sentence_transformers import SentenceTransformer

embedding_model = SentenceTransformer("BAAI/bge-m3")
embedding_dims = 1024


sourceWeights_default = {
    'fulltext': 1.0,        
    'OI_description': 1.2,  
    'OI_title': 0.5,  
    'OI_subtitle': 0.8,  
    'recommendation': 1.0,
    'gap': 1.0
}
sourceWeights_shortText = {
    'fulltext': 1.0,        
    'OI_description': 0.8,  
    'OI_title': 1.0,  
    'OI_subtitle': 1.0,  
    'recommendation': 0.5,
    'gap': 0.5
}
sourceWeights_longText = {
    'fulltext': 0.8,        
    'OI_description': 1.5,  
    'OI_title': 0.4,  
    'OI_subtitle': 0.6,  
    'recommendation': 1.5,
    'gap': 1.5
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

