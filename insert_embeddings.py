import os

from dotenv import load_dotenv
from neo4j import GraphDatabase, RoutingControl
from sentence_transformers import SentenceTransformer

"""

This code inserts the embedding into the desired node. 
In our case, we want to have embeddings for:
 - Contribution
 - recommendations
 - Gap
 - Projects

"""

load_dotenv()
# Neo4j connection
NEO4J_URI=os.getenv('NEO4J_URI')
NEO4J_USER=os.getenv('NEO4J_USER')
NEO4J_PASSWORD=os.getenv('NEO4J_PASSWORD')
NEO4J_GRAPH=os.getenv('NEO4J_GRAPH')
driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
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
print("=" * 70)    
print("Overview of contributions")
records, summary, keys = driver.execute_query("""
    MATCH (n:Contribution)
    RETURN n
""", database_=NEO4J_GRAPH, routing_=RoutingControl.READ)
print(f"We have {len(records)} Contributions")

print("Fetching contributions and setting `todo` status")
records, summary, keys = driver.execute_query("""
    MATCH (n:Contribution)
    WHERE n.descrEmbeddingStatus IS NULL
    SET n.descrEmbeddingStatus = 'todo'
""", database_=NEO4J_GRAPH, routing_=RoutingControl.WRITE)
print(f"1. set descrEmbeddingStatus to `todo` on {summary.counters.properties_set} nodes")   # should be ~2 * len(title_rows) (embedding + status)

records, summary, keys = driver.execute_query("""
    MATCH (n:Contribution)
    WHERE n.titleEmbeddingStatus IS NULL
    SET n.titleEmbeddingStatus = 'todo', n.subtitleEmbeddingStatus = 'todo'
""", database_=NEO4J_GRAPH, routing_=RoutingControl.WRITE)
print(f"2. set titleEmbeddingStatus to `todo` on {summary.counters.properties_set} nodes")   # should be ~2 * len(title_rows) (embedding + status)

records, summary, keys = driver.execute_query("""
    MATCH (n:Contribution)
    WHERE n.subtitleEmbeddingStatus IS NULL
    SET n.subtitleEmbeddingStatus = 'todo'
""", database_=NEO4J_GRAPH, routing_=RoutingControl.WRITE)
print(f"3. set subtitleEmbeddingStatus to `todo` on {summary.counters.properties_set} nodes")   # should be ~2 * len(title_rows) (embedding + status)


print("done - now we fetch all the textual information, combine it and embed it")

###############################3
# DESCRIPTION
demb_records, summary, keys = driver.execute_query("""
    MATCH (n:Contribution)
    WHERE n.descrEmbeddingStatus = 'todo'
    RETURN n.id AS id, elementId(n) AS n4j_id, n.description AS desc, n.findings AS findings
""", database_=NEO4J_GRAPH, routing_=RoutingControl.READ)
contributions_descr_embeddings = []
for j, row in enumerate(records):
    description_text = ""
    if row['desc']:
    	description_text += "Description: " + row["desc"] + ". "																									
    if row['findings']:
    	description_text += "Findings: " + row['findings']
    if row['desc'] or row['findings']:
        contributions_descr_embeddings.append({
    	'id':row['id'],
        'neo4j_id': row['n4j_id'],
    	'descr_embedding':embedding_model.encode(description_text),
    })
print(f"Prepared {len(demb_records)} description embeddings for contributions")   # should be ~2 * len(title_rows) (embedding + status)
done_descr_records, summary, keys = driver.execute_query("""
    UNWIND $rows AS row
	MATCH (n:Contribution) WHERE elementId(n) = row.neo4j_id
    CALL db.create.setNodeVectorProperty(n, 'descrEmbedding', row.descr_embedding)
    SET n.descrEmbeddingStatus = 'done'
""", rows=contributions_descr_embeddings, database_=NEO4J_GRAPH, routing_=RoutingControl.WRITE)
print(f"set status `done` for descrEmbeddingStatus on {summary.counters.properties_set} nodes")   # should be ~2 * len(title_rows) (embedding + status)

###############################3
# TITLE
temb_records, summary, keys = driver.execute_query("""
    MATCH (n:Contribution)
    WHERE n.titleEmbeddingStatus = 'todo'
    RETURN n.id AS id, elementId(n) AS n4j_id, n.officialTitle AS title
""", database_=NEO4J_GRAPH, routing_=RoutingControl.READ)
contributions_title_embeddings = []
for j, row in enumerate(records):
    if row['title']:
        contributions_title_embeddings.append({
    	'id':row['id'],
        'neo4j_id': row['n4j_id'],
    	'title_embedding':embedding_model.encode(row['title']),
    })
print(f"Prepared {len(temb_records)} title embeddings for contributions")   # should be ~2 * len(title_rows) (embedding + status)
records, summary, keys = driver.execute_query("""
    UNWIND $rows AS row
	MATCH (n:Contribution) WHERE elementId(n) = row.neo4j_id
    CALL db.create.setNodeVectorProperty(n, 'titleEmbedding', row.title_embedding)
    SET n.titleEmbeddingStatus = 'done'
""", rows=contributions_title_embeddings, database_=NEO4J_GRAPH, routing_=RoutingControl.WRITE)
print(f"set status `done` for titleEmbeddingStatus on {summary.counters.properties_set} nodes")   # should be ~2 * len(title_rows) (embedding + status)

###############################3
# SUBTITLE
semb_records, summary, keys = driver.execute_query("""
    MATCH (n:Contribution)
    WHERE n.subtitleEmbeddingStatus = 'todo'
    RETURN n.id AS id, elementId(n) AS n4j_id, n.subtitle AS subtitle
""", database_=NEO4J_GRAPH, routing_=RoutingControl.READ)
contributions_subtitle_embeddings = []
for j, row in enumerate(records):
    if row['subtitle']:
        contributions_subtitle_embeddings.append({
    	'id':row['id'],
        'neo4j_id': row['n4j_id'],
    	'subtitle_embedding':embedding_model.encode(row['subtitle']),
    })
print(f"Prepared {len(temb_records)} subtitle embeddings for contributions")   # should be ~2 * len(title_rows) (embedding + status)
records, summary, keys = driver.execute_query("""
    UNWIND $rows AS row
	MATCH (n:Contribution) WHERE elementId(n) = row.neo4j_id
    CALL db.create.setNodeVectorProperty(n, 'subtitleEmbedding', row.subtitle_embedding)
    SET n.subtitleEmbeddingStatus = 'done'
""", rows=contributions_subtitle_embeddings, database_=NEO4J_GRAPH, routing_=RoutingControl.WRITE)
print(f"set status `done` for subtitleEmbeddingStatus on {summary.counters.properties_set} nodes")   # should be ~2 * len(title_rows) (embedding + status)

print("Creating index for contributions")
###### VECTOR INDEX 
driver.execute_query("""
    CREATE VECTOR INDEX description-embeddings IF NOT EXISTS
    FOR (n:Contribution) ON (n.descrEmbedding)
    OPTIONS { indexConfig: { `vector.dimensions`: $vec_dim, `vector.similarity_function`: $sim } }
""", vec_dim=embedding_dims, sim='cosine', database_=NEO4J_GRAPH, routing_=RoutingControl.WRITE)
driver.execute_query("""
    CREATE VECTOR INDEX title-embeddings IF NOT EXISTS
    FOR (n:Contribution) ON (n.titleEmbedding)
    OPTIONS { indexConfig: { `vector.dimensions`: $vec_dim, `vector.similarity_function`: $sim } }
""", vec_dim=embedding_dims, sim='cosine', database_=NEO4J_GRAPH, routing_=RoutingControl.WRITE)
driver.execute_query("""
    CREATE VECTOR INDEX subtitle-embeddings IF NOT EXISTS
    FOR (n:Contribution) ON (n.subtitleEmbedding)
    OPTIONS { indexConfig: { `vector.dimensions`: $vec_dim, `vector.similarity_function`: $sim } }
""", vec_dim=embedding_dims, sim='cosine', database_=NEO4J_GRAPH, routing_=RoutingControl.WRITE)

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
print("=" * 70)                                                                         
print("Overview of Recommendations")
records, summary, keys = driver.execute_query("""
    MATCH (n:Recommendation)
    RETURN n
""", database_=NEO4J_GRAPH, routing_=RoutingControl.READ)
print(f"We have {len(records)} Recommendations")

print("Fetching recommendations")
records, summary, keys = driver.execute_query("""
    MATCH (n:Recommendation)
    WHERE n.embeddingStatus IS NULL
    SET n.embeddingStatus = 'todo'
""", database_=NEO4J_GRAPH, routing_=RoutingControl.WRITE)
print(f"set status on {summary.counters.properties_set} nodes")   # should be ~2 * len(title_rows) (embedding + status)

records, summary, keys = driver.execute_query("""
    MATCH (n:Recommendation)
    WHERE n.embeddingStatus = 'todo'
    RETURN n.id AS id, elementId(n) AS n4j_id, n.content AS content, n.motivation as motivation
""", database_=NEO4J_GRAPH, routing_=RoutingControl.READ)
print(f"Found {len(records)} Recommendations with `todo`")   # should be ~2 * len(title_rows) (embedding + status)

print(f"Embedding recommendations (we have {len(records)})")
recommendations_embeddings = []
for row in records:
    r_id = row['id']
    neo4j_id = row['n4j_id']
    text_to_embed = ""
    if row['content']:
        text_to_embed += "Contenuto: " + row["content"] + ". "																									
    if row['motivation']:
        text_to_embed += "Findings: " + row['motivation']
    if (not row['content']) and (not row['motivation']):
        print(f"\tWARNING: We discard the recommendation:\n\t\t{row}\n\tBecause it does not have content")
    if (row['content'] or row['motivation']):
        recommendations_embeddings.append({
            'id':r_id,
            'neo4j_id': neo4j_id,
            'embedding':embedding_model.encode(text_to_embed)
        })

print(f"Setting recommendations node properties ({len(recommendations_embeddings)} recommendations embedded)")
records, summary, keys = driver.execute_query("""
    UNWIND $rows AS row
	MATCH (n:Recommendation) WHERE elementId(n) = row.neo4j_id
    CALL db.create.setNodeVectorProperty(n, 'embedding', row.embedding)
    SET n.embeddingStatus = 'done'
""", rows=recommendations_embeddings, database_=NEO4J_GRAPH, routing_=RoutingControl.WRITE)
print(f"set status `done` on {summary.counters.properties_set} nodes")   # should be ~2 * len(title_rows) (embedding + status)

print("Creating index for recommendations")
driver.execute_query("""
    CREATE VECTOR INDEX recommendation_embeddings IF NOT EXISTS
    FOR (n:Recommendation) ON (n.embedding)
    OPTIONS { indexConfig: { `vector.dimensions`: $vec_dim, `vector.similarity_function`: $sim } }
""", vec_dim=embedding_dims, sim='cosine', database_=NEO4J_GRAPH, routing_=RoutingControl.WRITE)

#                     
#    __ _  __ _ _ __  
#   / _` |/ _` | '_ \ 
#  | (_| | (_| | |_) |
#   \__, |\__,_| .__/ 
#   |___/      |_|    
print("=" * 70)    
print("Overview of Gaps")
records, summary, keys = driver.execute_query("""
    MATCH (n:Gap)
    RETURN n
""", database_=NEO4J_GRAPH, routing_=RoutingControl.READ)
print(f"We have {len(records)} Gaps")

# print("Fetching recommendations")
records, summary, keys = driver.execute_query("""
    MATCH (n:Gap)
    WHERE n.embeddingStatus IS NULL
    SET n.embeddingStatus = 'todo'
""", database_=NEO4J_GRAPH, routing_=RoutingControl.WRITE)
print(f"set status on {summary.counters.properties_set} nodes")   # should be ~2 * len(title_rows) (embedding + status)

records, summary, keys = driver.execute_query("""
    MATCH (n:Gap)
    WHERE n.embeddingStatus = 'todo'
    RETURN n.id AS id, elementId(n) AS n4j_id, n.description AS description
""", database_=NEO4J_GRAPH, routing_=RoutingControl.READ)
print(f"Found {len(records)} Gaps with `todo`")   # should be ~2 * len(title_rows) (embedding + status)

print(f"Embedding {len(records)} gaps")
gap_embeddings = []
for row in records:
    r_id = row['id']
    neo4j_id = row['n4j_id']
    text_to_embed = row["description"]
    if text_to_embed:
        gap_embeddings.append({
			'id':r_id,
            'neo4j_id': neo4j_id,
			'embedding':embedding_model.encode(text_to_embed)
		})

print(f"Actually managed to embed {len(gap_embeddings)} gaps (others were empty)")
print("Setting gaps node properties")
# Update Neo4j in transaction
records, summary, keys = driver.execute_query("""
    UNWIND $rows AS row
	MATCH (n:Gap) WHERE elementId(n) = row.neo4j_id
    CALL db.create.setNodeVectorProperty(n, 'embedding', row.embedding)
	SET n.embeddingStatus = 'done'
""", rows=gap_embeddings, database_=NEO4J_GRAPH, routing_=RoutingControl.WRITE)
print(f"set status `done` on {summary.counters.properties_set} nodes")   # should be ~2 * len(title_rows) (embedding + status)

print("Creating index for gaps")
driver.execute_query("""
    CREATE VECTOR INDEX gap_embeddings IF NOT EXISTS
    FOR (n:Contribution) ON (n.embedding)
    OPTIONS { indexConfig: { `vector.dimensions`: $vec_dim, `vector.similarity_function`: $sim } }
""", vec_dim=embedding_dims, sim='cosine', database_=NEO4J_GRAPH, routing_=RoutingControl.WRITE)




#    __       _ _       _            _   
#   / _|_   _| | |     | |_ _____  _| |_ 
#  | |_| | | | | |_____| __/ _ \ \/ / __|
#  |  _| |_| | | |_____| ||  __/>  <| |_ 
#  |_|  \__,_|_|_|      \__\___/_/\_\\__|
#   
# ONE FULL-TEXT for all nodes together     
print("=" * 70)    
print("Creating index for full text")                                
records, summary, keys = driver.execute_query("""
    CREATE FULLTEXT INDEX search-fulltext IF NOT EXISTS
    FOR (n:Contribution|Recommendation|Gap)
    ON EACH [n.officialTitle, n.subtitle, n.description, n.findings, n.content, n.motivation]
""", database_=NEO4J_GRAPH, routing_=RoutingControl.WRITE)

print(f'Created index for {len(records)} ({summary.counters.properties_set} properties set)')

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