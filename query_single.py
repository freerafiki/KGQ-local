import argparse
import json
import os
import time

import numpy as np
from dotenv import load_dotenv
from neo4j import GraphDatabase
from sentence_transformers import SentenceTransformer

load_dotenv()
# Neo4j connection
NEO4J_URI=os.getenv('NEO4J_URI')
NEO4J_USER=os.getenv('NEO4J_USER')
NEO4J_PASSWORD=os.getenv('NEO4J_PASSWORD')
NEO4J_GRAPH=os.getenv('NEO4J_GRAPH')
driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
embedding_model = SentenceTransformer("BAAI/bge-m3")
embedding_dims = 768



# # Models to test (different dimensions!)
# test_models = {
#     "embeddinggemma-300m": {"model": "google/embeddinggemma-300m", "dim": 768},
#     "qwen3-0.6b": {"model": "Qwen/Qwen3-Embedding-0.6B", "dim": 1024},
#     "bge-m3": {"model": "BAAI/bge-m3", "dim": 1024},
#     "all-minilm-l6": {"model": "sentence-transformers/all-MiniLM-L6-v2", "dim": 384},
#     "modernbert": {"model": "nomic-ai/modernbert-embed-base", "dim": 768},
# }

def main(args):

    driver = GraphDatabase.driver("bolt://localhost:7687", auth=("neo4j", "password"))

    print(f"loaded model {embedding_model}")

    # Time embedding generation
    start = time.time()
    query_embedding = embedding_model.encode(args.query)
    elapsed = time.time() - start
    print(f"Embedding the query text took {elapsed}:.1f seconds")

    start_q = time.time()

    # need to finish reading neo4j article
    # https://neo4j.com/developer/genai-ecosystem/hybrid-search/

    records, summary, keys = driver.execute_query(f"""
    CYPHER 25
    LET
    query = $query,
    queryVector = $queryVector,
    structuralQueryVector = $structuralQueryVector,
    finalK = $finalK,
    rrfConstant = $rrfConstant,
    sourceWeights = $sourceWeights

    CALL (query, queryVector, structuralQueryVector) {
    CALL db.index.fulltext.queryNodes('search-fulltext', query, {limit: $sourceK})
    YIELD node AS result, score
    WITH result, score
    ORDER BY score DESC, result.id ASC
    WITH collect({node: result, rawScore: score}) AS rows
    UNWIND CASE WHEN size(rows) = 0 THEN [] ELSE range(0, size(rows) - 1) END AS rankIndex
    RETURN
        rows[rankIndex].node AS result,
        'fulltext' AS source,
        rankIndex + 1 AS sourceRank,
        rows[rankIndex].rawScore AS rawScore

    UNION ALL

    MATCH (result:result)
        SEARCH result IN (
        VECTOR INDEX `description-embeddings`
        FOR queryVector
        LIMIT $sourceK
        ) SCORE AS score
    WITH result, score
    ORDER BY score DESC, result.id ASC
    WITH collect({node: result, rawScore: score}) AS rows
    UNWIND CASE WHEN size(rows) = 0 THEN [] ELSE range(0, size(rows) - 1) END AS rankIndex
    RETURN
        rows[rankIndex].node AS result,
        'semantic' AS source,
        rankIndex + 1 AS sourceRank,
        rows[rankIndex].rawScore AS rawScore

    UNION ALL

    MATCH (result:result)
        SEARCH result IN (
        VECTOR INDEX `recommendation-embeddings`
        FOR structuralQueryVector
        LIMIT $sourceK
        ) SCORE AS score
    WITH result, score
    ORDER BY score DESC, result.id ASC
    WITH collect({node: result, rawScore: score}) AS rows
    UNWIND CASE WHEN size(rows) = 0 THEN [] ELSE range(0, size(rows) - 1) END AS rankIndex
    RETURN
        rows[rankIndex].node AS result,
        'structural' AS source,
        rankIndex + 1 AS sourceRank,
        rows[rankIndex].rawScore AS rawScore
    }

    LET weight = coalesce(sourceWeights[source], 1.0)
    LET contribution = weight / (rrfConstant + sourceRank)

    WITH result, finalK, source, sourceRank, rawScore, weight, contribution
    ORDER BY result.id ASC, source ASC, sourceRank ASC

    WITH
    result,
    finalK,
    collect({
        source: source,
        sourceRank: sourceRank,
        weight: weight,
        rawScore: rawScore,
        contribution: contribution
    }) AS contributions

    LET wrrf = reduce(wrrf = 0.0, contribution IN contributions |
    wrrf + contribution.contribution
    )

    ORDER BY wrrf DESC, result.id ASC

    WITH collect({
    result: result,
    sources: [contribution IN contributions | contribution.source],
    wrrf: wrrf
    }) AS orderedRows, finalK
    LET limitedRows = orderedRows[..finalK]

    UNWIND limitedRows AS row
    RETURN
    row.result.title AS title,
    row.sources AS sources,
    row.wrrf AS wrrf
    ORDER BY row.wrrf DESC, row.result.id ASC;
""", query=args.query, queryVector=query_embedding, \
     sourceK=10, finalK=20, rrfConstant=60, sourceWeights=[1, 1, 0])

    print("RECORDS")
    print(records)
    print("SUMMARY")
    print(summary)
    print("KEYS")
    print(keys)

#     cypher_query = f"""
# LET
#   query = $query,
#   queryVector = $queryVector,
#   structuralQueryVector = $structuralQueryVector,
#   finalK = $finalK,
#   rrfConstant = $rrfConstant,
#   sourceWeights = $sourceWeights

# CALL (query, queryVector, structuralQueryVector) {
#   CALL db.index.fulltext.queryNodes('result-text', query, {limit: $sourceK})
#   YIELD node AS result, score
#   WITH result, score
#   ORDER BY score DESC, result.id ASC
#   WITH collect({node: result, rawScore: score}) AS rows
#   UNWIND CASE WHEN size(rows) = 0 THEN [] ELSE range(0, size(rows) - 1) END AS rankIndex
#   RETURN
#     rows[rankIndex].node AS result,
#     'fulltext' AS source,
#     rankIndex + 1 AS sourceRank,
#     rows[rankIndex].rawScore AS rawScore

#   UNION ALL
# """


    # with driver.session() as session:
    #     res = session.run(f"""
    #         CALL {{ CALL db.index.vector.queryNodes('title_embeddings', 20, $q) YIELD node, score RETURN node, score AS titleScore, 0 AS bodyScore
    #         UNION
    #         CALL db.index.vector.queryNodes('body_embeddings', 20, $q) YIELD node, score RETURN node, 0 AS titleScore, score AS bodyScore }}
    #         WITH node, max(titleScore) AS t, max(bodyScore) AS b
    #         RETURN node, (t + b) AS combined ORDER BY combined DESC LIMIT $k
            
            
    #         CALL db.index.vector.queryNodes('test_{name.replace('-','_')}', 5, $emb)
    #         YIELD node, score
    #         RETURN node.text, score
    #     """, emb=query_embedding.tolist())
        
    #     hits = res.data()
    #     elapsed_q = time.time() - start_q
    #     print(f"Query search took {elapsed_q}:.1f seconds")

    # print("Results:")
    # for j, h in enumerate(hits):
    #     print("result {j}") 
    #     for hk in h:
    #         print(f"{hk}: {h[hk]}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Process CSV and .env file paths.")
    parser.add_argument("--query", "-Q", type=str, help="query to search")
    # parser.add_argument("--model", "-M", type=str, choices=test_models.keys(), help="model to use for embedding")
    parser.add_argument("--env_file", "-env", type=str, default=".env", help="Path to the .env file (default: .env)")
    # parser.add_argument("--reset", "-R", default=False, action='store_true', help="reset the database (clear)")
    parser.add_argument("--verbosity", "-V", default=1, type=int, help="verbosity level")
    # parser.add_argument("--index", "-I", default=False, action='store_true', help="create indices")
    args = parser.parse_args()
    print(f"CSV file: {args.csv_file}")
    print(f".env file: {args.env_file}")
    main(args)
