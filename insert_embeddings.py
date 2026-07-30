"""

This code inserts the embedding into the desired node. 
In our case, we want to have embeddings for:
 - Contribution
 - recommendations
 - Gap
 - Projects

"""
# embed only what needs to be embedded 
from neo4j import GraphDatabase
from sentence_transformers import SentenceTransformer

driver = GraphDatabase.driver("bolt://localhost:7687", auth=("neo4j", "password"))
embedding_model = SentenceTransformer("BAAI/bge-m3")
embedding_dims = 768


# 1. FETCH NODES 
#
# Get only nodes that SHOULD have embeddings
#   we might need more information and join them together to get a full string for embedding 


#                  _        _ _           _   _             
#   ___ ___  _ __ | |_ _ __(_) |__  _   _| |_(_) ___  _ __  
#  / __/ _ \| '_ \| __| '__| | '_ \| | | | __| |/ _ \| '_ \ 
# | (_| (_) | | | | |_| |  | | |_) | |_| | |_| | (_) | | | |
#  \___\___/|_| |_|\__|_|  |_|_.__/ \__,_|\__|_|\___/|_| |_|
#

with driver.session() as session:
    result = session.run("""
        MATCH (n:Contribution)
        WHERE n.embedding IS NULL  -- Skip already-embedded nodes
        RETURN n.id AS id, n.description AS desc, n.findings AS findings, n.officialTitle AS title, n.subtitle AS subtitle
    """)
    
contributions_embeddings = []
for row in result:

    r_id = row['id']
    description_text = "Description: " + row["desc"] + ", Findings: " + row['findings']
    title = row['title']
    subtitle = row['subtitle']
    # contributions_to_embed.append((r_id, text_to_embed))
    contributions_embeddings.append({
        'id':r_id,
        'descr_embedding':embedding_model.encode(description_text),
        'title_embedding':embedding_model.encode(title),
        'subtitle_embedding':embedding_model.encode(subtitle)
    })
    
# Update Neo4j in transaction
with driver.session() as session:
    session.run("""
        UNWIND $rows AS row
        MATCH (n) WHERE elementId(n) = row.id
        CALL db.create.setNodeVectorProperty(n, 'descrEmbedding', row.descr_embedding)
        CALL db.create.setNodeVectorProperty(n, 'titleEmbedding', row.title_embedding)
        CALL db.create.setNodeVectorProperty(n, 'subtitleEmbedding', row.subtitle_embedding)
    """, rows=contributions_embeddings)

###### VECTOR INDEX 
with driver.session() as session:
    session.run("""
        CREATE VECTOR INDEX description_embeddings IF NOT EXISTS
        FOR (n:Contribution) ON (n.descrEmbedding)
        OPTIONS {{ indexConfig: {{ `vector.dimensions`: $vec_dim, `vector.similarity_function`: $sim }} }}
    """, vec_dim=embedding_dims, sim='cosine')

with driver.session() as session:
    session.run("""
        CREATE VECTOR INDEX title_embeddings IF NOT EXISTS
        FOR (n:Contribution) ON (n.titleEmbedding)
        OPTIONS {{ indexConfig: {{ `vector.dimensions`: $vec_dim, `vector.similarity_function`: $sim }} }}
    """, vec_dim=embedding_dims, sim='cosine')

with driver.session() as session:
    session.run("""
        CREATE VECTOR INDEX subtitle_embeddings IF NOT EXISTS
        FOR (n:Contribution) ON (n.subtitleEmbedding)
        OPTIONS {{ indexConfig: {{ `vector.dimensions`: $vec_dim, `vector.similarity_function`: $sim }} }}
    """, vec_dim=embedding_dims, sim='cosine')

# ------------------------------------------------------------------
# this would be a full-text index only for Contributions 
# not sure if we actually need / want this
# -------------
# with driver.session() as session:
#     session.run("""
#         CREATE FULLTEXT INDEX contribution_fulltext IF NOT EXISTS
#         FOR (n:Contribution)
#         ON EACH [n.officialTitle, n.subtitle, n.description, n.findings]
#     """)
# ------------------------------------------------------------------

#                                                         _       _   _             
#   _ __ ___  ___ ___  _ __ ___  _ __ ___   ___ _ __   __| | __ _| |_(_) ___  _ __  
#  | '__/ _ \/ __/ _ \| '_ ` _ \| '_ ` _ \ / _ \ '_ \ / _` |/ _` | __| |/ _ \| '_ \ 
#  | | |  __/ (_| (_) | | | | | | | | | | |  __/ | | | (_| | (_| | |_| | (_) | | | |
#  |_|  \___|\___\___/|_| |_| |_|_| |_| |_|\___|_| |_|\__,_|\__,_|\__|_|\___/|_| |_|
#                                                                                   

with driver.session() as session:
    result = session.run("""
        MATCH (n:Recomendation)
        WHERE n.embedding IS NULL  -- Skip already-embedded nodes
        RETURN n.id AS id, n.content AS content, n.motivation as motivation
    """)
    
recommendations_embeddings = []
for row in result:

    r_id = row['id']
    text_to_embed = "Contenuto: " + row["content"] + ", Motivazione: " + row['motivation']
    # contributions_to_embed.append((r_id, text_to_embed))
    recommendations_embeddings.append({
        'id':r_id,
        'embedding':embedding_model.encode(text_to_embed)
    })
    
# Update Neo4j in transaction
with driver.session() as session:
    session.run("""
        UNWIND $rows AS row
        MATCH (n) WHERE elementId(n) = row.id
        CALL db.create.setNodeVectorProperty(n, 'embedding', row.embedding)
    """, rows=recommendations_embeddings)

with driver.session() as session:
    session.run("""
        CREATE VECTOR INDEX recomendation_embeddings IF NOT EXISTS
        FOR (n:Contribution) ON (n.embedding)
        OPTIONS {{ indexConfig: {{ `vector.dimensions`: $vec_dim, `vector.similarity_function`: $sim }} }}
    """, vec_dim=embedding_dims, sim='cosine')

#                     
#    __ _  __ _ _ __  
#   / _` |/ _` | '_ \ 
#  | (_| | (_| | |_) |
#   \__, |\__,_| .__/ 
#   |___/      |_|    

with driver.session() as session:
    result = session.run("""
        MATCH (n:Gap)
        WHERE n.embedding IS NULL  -- Skip already-embedded nodes
        RETURN n.id AS id, n.text AS text, n.#### as ####
    """)
    
gap_embeddings = []
for row in result:

    r_id = row['id']
    text_to_embed = row["description"]
    # contributions_to_embed.append((r_id, text_to_embed))
    gap_embeddings.append({
        'id':r_id,
        'embedding':embedding_model.encode(text_to_embed)
    })
    
# Update Neo4j in transaction
with driver.session() as session:
    session.run("""
        UNWIND $rows AS row
        MATCH (n) WHERE elementId(n) = row.id
        CALL db.create.setNodeVectorProperty(n, 'embedding', row.embedding)
    """, rows=gap_embeddings)

with driver.session() as session:
    session.run("""
        CREATE VECTOR INDEX gap_embeddings IF NOT EXISTS
        FOR (n:Contribution) ON (n.embedding)
        OPTIONS {{ indexConfig: {{ `vector.dimensions`: $vec_dim, `vector.similarity_function`: $sim }} }}
    """, vec_dim=embedding_dims, sim='cosine')



#    __       _ _       _            _   
#   / _|_   _| | |     | |_ _____  _| |_ 
#  | |_| | | | | |_____| __/ _ \ \/ / __|
#  |  _| |_| | | |_____| ||  __/>  <| |_ 
#  |_|  \__,_|_|_|      \__\___/_/\_\\__|
#   
# ONE FULL-TEXT for all nodes together                                     
with driver.session() as session:
    session.run("""
        CREATE FULLTEXT INDEX contribution_fulltext IF NOT EXISTS
        FOR (n:Contribution|Recommendation|Gap)
        ON EACH [n.officialTitle, n.subtitle, n.description, n.findings, n.content, n.motivation, n.description]
    """)


#############################################################
# CHUNK VERSION ?
#
# chunks are indexed but maintain "part of" relation 
# to the parent document
#
# CALL db.index.vector.queryNodes('chunk_embeddings', 20, $q) YIELD node AS chunk, score
# MATCH (chunk)-[:PART_OF]->(doc)
# RETURN doc, max(score) AS bestChunkScore
# ORDER BY bestChunkScore DESC
#############################################################