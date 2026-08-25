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

    MATCH (result:Contribution)
        SEARCH result IN (
            VECTOR INDEX `title_embeddings`
            FOR longQueryVector
            LIMIT $sourceK
        ) SCORE AS score
    WITH result, score
    ORDER BY score DESC, result.id ASC
    WITH collect({{node: result, rawScore: score}}) AS rows
    UNWIND CASE WHEN size(rows) = 0 THEN [] ELSE range(0, size(rows) - 1) END AS rankIndex
    RETURN
        rows[rankIndex].node AS result,
        'OI_title' AS source,
        rankIndex + 1 AS sourceRank,
        rows[rankIndex].rawScore AS rawScore

    UNION ALL

    MATCH (result:Contribution)
        SEARCH result IN (
            VECTOR INDEX `subtitle_embeddings`
            FOR longQueryVector
            LIMIT $sourceK
        ) SCORE AS score
    WITH result, score
    ORDER BY score DESC, result.id ASC
    WITH collect({{node: result, rawScore: score}}) AS rows
    UNWIND CASE WHEN size(rows) = 0 THEN [] ELSE range(0, size(rows) - 1) END AS rankIndex
    RETURN
        rows[rankIndex].node AS result,
        'OI_subtitle' AS source,
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
        row.result AS n,
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
        
        # breakpoint()
        labels = list(record['n'].labels)
        labels_text = ", ".join(labels)
        print("-" * 60)
        print(f"RANK {j+1} - TYPE: {labels_text} | {record_type} - SCORE: {record['wrrf']:.04f}")
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
        print(f"\tGot {len(neighbors_all2)} neighbours, {len(neighbors_rec)} of which are recommendations")

        # # Follow a specific relationship
        contrib_content = helper.connected_via(node_id=record['neo4j_id'], rel_type="refers_to_content")
        # print(f"\tRelated contrib: {contrib_content}")
        # contrib_content = helper.follow_relationship(node_id=record['neo4j_id'], rel_type="refers_to_content")
        # print(contrib_content)

        # # Get 2-hop neighbors
        # friends_of_friends = helper.get_n_hop_neighbors(node_id=record['neo4j_id'], max_hops=2)
        # print(friends_of_friends)

        # breakpoint()

    print("=" * 75)


    # Close connection when done
    helper.close()

    # Save results to JSON
    import json
    results_to_save = []
    for record in records:
        labels = list(record['n'].labels)
        if "OI_description" in record['sources']:
            record_type = "Oggetto Informativo"
        elif "recommendation" in record['sources']:
            record_type = "Raccomandazione"
        elif "gap" in record['sources']:
            record_type = "Lacuna"
        else:
            record_type = "Full-text"

        entry = {
            "type": record_type,
            "labels": labels,
            "wrrf_score": round(record['wrrf'], 4),
            "sources": record['sources'],
            "neo4j_id": record['neo4j_id']
        }

        if record_type == "Oggetto Informativo":
            entry.update({"title": record['title'], "submission_id": record['id']})
        elif record_type == "Raccomandazione":
            entry.update({"content": record['content'], "motivation": record['motivation']})
        elif record_type == "Lacuna":
            entry.update({"description": record['description']})
        else:
            entry.update({"title": record['title'], "submission_id": record['id']})

        results_to_save.append(entry)

    output_data = {
        "user_query": args.query,
        "embedding_time_seconds": round(elapsed, 4),
        "search_time_seconds": round(elapsed_q, 4),
        "results": results_to_save
    }

    sanitized_query = args.query[:10].replace(" ", "_")
    search_result_path = f'search_results_{sanitized_query}.json'
    os.makedirs("results", exist_ok=True)
    with open(os.path.join("results", search_result_path), 'w', encoding='utf-8') as f:
        json.dump(output_data, f, indent=4, ensure_ascii=False)
    print(f"\nSaved {len(results_to_save)} records to {search_result_path}")


# ### RERANKING!

# from sentence_transformers import CrossEncoder

# # Load once at startup alongside embedding_model
# reranker = CrossEncoder("BAAI/bge-reranker-v2-m3")  # multilingual, handles Italian

# # After your driver.execute_query call, before printing:
# def rerank(query: str, records: list, text_field_priority=["title", "description", "content"]) -> list:
#     """Rerank Neo4j results using a cross-encoder."""
#     pairs = []
#     for record in records:
#         # Build the document text from whichever field is populated
#         doc_text = next(
#             (record[f] for f in text_field_priority if record.get(f)),
#             ""
#         )
#         pairs.append((query, doc_text))
    
#     scores   = reranker.predict(pairs)
#     reranked = sorted(
#         zip(scores, records),
#         key=lambda x: x[0],
#         reverse=True
#     )
#     return [(score, record) for score, record in reranked]

# # Usage
# reranked_results = rerank(args.query, records)
# for rank, (reranker_score, record) in enumerate(reranked_results):
#     print(f"RANK {rank+1} | wRRF={record['wrrf']:.4f} | reranker={reranker_score:.4f}")


"""
## IDEA FOR including Reranking after extracting from the DB

# Stage 1 — fast recall from Neo4j (bi-encoder, already built)
candidates = hybrid_search.run(
    query_text     = "sea level rise",
    fulltext_index = "contribution_fulltext",
    top_k          = 50,   # retrieve more than you need
)

# Stage 2 — rerank with cross-encoder or ColBERT (in Python, no DB)
from sentence_transformers import CrossEncoder

reranker   = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")
pairs      = [(query, r.properties["name"]) for r in candidates]
scores     = reranker.predict(pairs)

# Sort by reranker score
reranked   = sorted(zip(scores, candidates), key=lambda x: x[0], reverse=True)
top_10     = [r for _, r in reranked[:10]]
""" 

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Process CSV and .env file paths.")
    parser.add_argument("--query", "-Q", type=str, help="query to search")
    parser.add_argument("--env_file", "-env", type=str, default=".env", help="Path to the .env file (default: .env)")
    parser.add_argument("--verbosity", "-V", default=1, type=int, help="verbosity level")
    args = parser.parse_args()
    main(args)


