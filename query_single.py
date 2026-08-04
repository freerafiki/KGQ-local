import argparse
import os
import time

from dotenv import load_dotenv
from neo4j import GraphDatabase, RoutingControl
from sentence_transformers import SentenceTransformer
from graph_helper import GraphHelper
from query_config import chooseSourceWeights

load_dotenv()
# Neo4j connection
NEO4J_URI=os.getenv('NEO4J_URI')
NEO4J_USER=os.getenv('NEO4J_USER')
NEO4J_PASSWORD=os.getenv('NEO4J_PASSWORD')
NEO4J_GRAPH=os.getenv('NEO4J_GRAPH')
driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
embedding_model = SentenceTransformer("LiquidAI/LFM2.5-Embedding-350M")
embedding_dims = 1024


# # Models to test (different dimensions!)  
# test_models = {
#     "embeddinggemma-300m": {"model": "google/embeddinggemma-300m", "dim": 768},
#     "qwen3-0.6b": {"model": "Qwen/Qwen3-Embedding-0.6B", "dim": 1024},
#     "bge-m3": {"model": "BAAI/bge-m3", "dim": 1024},
#     "all-minilm-l6": {"model": "sentence-transformers/all-MiniLM-L6-v2", "dim": 384},
#     "modernbert": {"model": "nomic-ai/modernbert-embed-base", "dim": 768},
# }


def main(args):

    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))

    print(f"loaded model {embedding_model}")

    # Connect
    helper = GraphHelper(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)

    # Time embedding generation
    start = time.time()
    query_embedding = embedding_model.encode(args.query)
    elapsed = time.time() - start
    print(f"Embedding the query text took {elapsed:.3f} seconds")

    sourceWeights_query = chooseSourceWeights(args.query)
    start_q = time.time()

    records, summary, keys = driver.execute_query(f"""
    CYPHER 25
    LET
    query = $query,
    queryVector = $queryVector,
    shortQueryVector = $shortQueryVector,
    longQueryVector = $longQueryVector,
    finalK = $finalK,
    rrfConstant = $rrfConstant,
    sourceWeights = $sourceWeights

    CALL (query, queryVector, shortQueryVector, longQueryVector) {{
    CALL db.index.fulltext.queryNodes('search_fulltext', query, {{limit: $sourceK}})
    YIELD node AS result, score
    WITH result, score
    ORDER BY score DESC, result.id ASC
    WITH collect({{node: result, rawScore: score}}) AS rows
    UNWIND CASE WHEN size(rows) = 0 THEN [] ELSE range(0, size(rows) - 1) END AS rankIndex
    RETURN
        rows[rankIndex].node AS result,
        'fulltext' AS source,
        rankIndex + 1 AS sourceRank,
        rows[rankIndex].rawScore AS rawScore

    UNION ALL

    MATCH (result:Contribution)
        SEARCH result IN (
        VECTOR INDEX `description_embeddings`
        FOR longQueryVector
        LIMIT $sourceK
        ) SCORE AS score
    WITH result, score
    ORDER BY score DESC, result.id ASC
    WITH collect({{node: result, rawScore: score}}) AS rows
    UNWIND CASE WHEN size(rows) = 0 THEN [] ELSE range(0, size(rows) - 1) END AS rankIndex
    RETURN
        rows[rankIndex].node AS result,
        'OI_description' AS source,
        rankIndex + 1 AS sourceRank,
        rows[rankIndex].rawScore AS rawScore

    UNION ALL

    MATCH (result:Recommendation)
        SEARCH result IN (
        VECTOR INDEX `recommendation_embeddings`
        FOR queryVector
        LIMIT $sourceK
        ) SCORE AS score
    WITH result, score
    ORDER BY score DESC, result.id ASC
    WITH collect({{node: result, rawScore: score}}) AS rows
    UNWIND CASE WHEN size(rows) = 0 THEN [] ELSE range(0, size(rows) - 1) END AS rankIndex
    RETURN
        rows[rankIndex].node AS result,
        'recommendation' AS source,
        rankIndex + 1 AS sourceRank,
        rows[rankIndex].rawScore AS rawScore

    UNION ALL

    MATCH (result:Gap)
        SEARCH result IN (
        VECTOR INDEX `gap_embeddings`
        FOR queryVector
        LIMIT $sourceK
        ) SCORE AS score
    WITH result, score
    ORDER BY score DESC, result.id ASC
    WITH collect({{node: result, rawScore: score}}) AS rows
    UNWIND CASE WHEN size(rows) = 0 THEN [] ELSE range(0, size(rows) - 1) END AS rankIndex
    RETURN
        rows[rankIndex].node AS result,
        'gap' AS source,
        rankIndex + 1 AS sourceRank,
        rows[rankIndex].rawScore AS rawScore
    }}

    LET weight = coalesce(sourceWeights[source], 1.0)
    LET contribution = weight / (rrfConstant + sourceRank)

    WITH result, finalK, source, sourceRank, rawScore, weight, contribution
    ORDER BY result.id ASC, source ASC, sourceRank ASC

    WITH
    result,
    finalK,
    collect({{
        source: source,
        sourceRank: sourceRank,
        weight: weight,
        rawScore: rawScore,
        contribution: contribution
    }}) AS contributions

    LET wrrf = reduce(wrrf = 0.0, contribution IN contributions |
    wrrf + contribution.contribution
    )

    ORDER BY wrrf DESC, result.id ASC

    WITH collect({{
    result: result,
    sources: [contribution IN contributions | contribution.source],
    wrrf: wrrf
    }}) AS orderedRows, finalK
    LET limitedRows = orderedRows[..finalK]

    UNWIND limitedRows AS row
    RETURN
    row.result.title AS title,
    row.result.id AS id,
    row.result.content AS content,
    row.result.motivation AS motivation,
    row.result.description AS description,
    row.sources AS sources,
    elementId(row.result) AS neo4j_id,
    row.wrrf AS wrrf
    ORDER BY row.wrrf DESC, row.result.id ASC;
""", query=args.query, queryVector=query_embedding, shortQueryVector=query_embedding, longQueryVector=query_embedding, \
     sourceK=10, finalK=20, rrfConstant=60, sourceWeights=sourceWeights_query, \
     database_=NEO4J_GRAPH, routing_=RoutingControl.READ)

    elapsed_q = time.time() - start_q
    print(f"Running the query text took {elapsed_q:.3f} seconds\n")

    print("=" * 75)
    for j, record in enumerate(records):
        if "OI_description" in record['sources']:
            record_type = "Oggetto Informativo"
        elif "recommendation" in record['sources']:
            record_type = "Raccomandazione"
        elif "gap" in record['sources']:
            record_type = "Lacuna"
        else: # full-text
            record_type = "OI | Rac | Lac (full-text)"
        
        print("-" * 60)
        print(f"RANK {j+1} - TYPE: {record.label} | {record_type} - SCORE: {record['wrrf']:.04f}")
        if record_type == "Oggetto Informativo":
            print(f"\t{record['title']}\n\n\tdb_id={record['neo4j_id']}, submission_id={record['id']}\n\tTrovato in {record['sources']}")
        elif record_type == "Raccomandazione":
            print(f"\tContenuto: {record['content']}\n\tMotivazione: {record['motivation']}\n\n\tdb_id={record['neo4j_id']}\n\tTrovato in {record['sources']}")
        elif record_type == "Lacuna":
            print(f"\tDescrizione: {record['description']}\n\n\tdb_id={record['neo4j_id']}\n\tTrovato in {record['sources']}")
        else:
            print(f"\t{record['title']}\n\n\tdb_id={record['neo4j_id']}, submission_id={record['id']}\n\tTrovato in {record['sources']}")


        
        # ===== USAGE EXAMPLES =====
        # Get all outgoing neighbors
        neighbors_all2 = helper.neighbours(node_id=record['neo4j_id'])
        neighbors_rec = helper.neighbours(node_id=record['neo4j_id'], label_filter='Recommendation')
        print(f"Got {len(neighbors_all2)} neighbours, {len(neighbors_rec)} of which are recommendations")

        # # Follow a specific relationship
        contrib_content = helper.connected_via(node_id=record['neo4j_id'], rel_type="refers_to_content")
        print(contrib_content)
        # contrib_content = helper.follow_relationship(node_id=record['neo4j_id'], rel_type="refers_to_content")
        # print(contrib_content)

        # # Get 2-hop neighbors
        # friends_of_friends = helper.get_n_hop_neighbors(node_id=record['neo4j_id'], max_hops=2)
        # print(friends_of_friends)

        breakpoint()

    print("=" * 75)


    # Close connection when done
    helper.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Process CSV and .env file paths.")
    parser.add_argument("--query", "-Q", type=str, help="query to search")
    parser.add_argument("--env_file", "-env", type=str, default=".env", help="Path to the .env file (default: .env)")
    parser.add_argument("--verbosity", "-V", default=1, type=int, help="verbosity level")
    args = parser.parse_args()
    main(args)
