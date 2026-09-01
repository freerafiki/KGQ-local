import argparse
import json
import os

import query_util
from graph_helper import GraphHelper


def main(args):
    result = query_util.run_search(
        args.query,
        source_k=args.source_k,
        final_k=args.final_k,
        rrf_constant=args.rrf_constant,
    )
    records = result['results']
    elapsed = result['embedding_time_s']
    elapsed_q = result['search_time_s']

    print(f"Embedding the query text took {elapsed:.3f} seconds")
    print(f"Running the query text took {elapsed_q:.3f} seconds\n")

    print("=" * 75)
    ft_min, ft_max = query_util.fulltext_score_bounds(records)

    helper = GraphHelper(
        query_util.NEO4J_URI, query_util.NEO4J_USER, query_util.NEO4J_PASSWORD
    )

    for j, record in enumerate(records):
        labels_text = ", ".join(record['labels'])
        record_type = record['type']
        print("-" * 60)
        print(f"RANK {j+1} - TYPE: {labels_text} | {record_type} - WRRF: {record['wrrf_score']:.04f}")
        print(
            "\tRaw scores: "
            + query_util.format_scores(
                record['sources'],
                record['sourceRanks'],
                record['rawScores'],
                ft_min,
                ft_max,
            )
        )
        if record_type == "Oggetto Informativo":
            print(f"\t{record['title']}\n\n\tdb_id={record['neo4j_id']}, submission_id={record['submission_id']}\n\tTrovato in {record['sources']}")
        elif record_type == "Raccomandazione":
            print(f"\tContenuto: {record['content']}\n\tMotivazione: {record['motivation']}\n\n\tdb_id={record['neo4j_id']}\n\tTrovato in {record['sources']}")
        elif record_type == "Lacuna":
            print(f"\tDescrizione: {record['description']}\n\n\tdb_id={record['neo4j_id']}\n\tTrovato in {record['sources']}")
        else:
            print(f"\t{record['title']}\n\n\tdb_id={record['neo4j_id']}, submission_id={record['submission_id']}\n\tTrovato in {record['sources']}")

        # ===== USAGE EXAMPLES =====
        # Get all outgoing neighbors
        neighbors_all = helper.neighbours(node_id=record['neo4j_id'])
        neighbors_rec = helper.neighbours(node_id=record['neo4j_id'], label_filter='Recommendation')
        print(f"\tGot {len(neighbors_all)} neighbours, {len(neighbors_rec)} of which are recommendations")

        # Follow a specific relationship
        contrib_content = helper.connected_via(node_id=record['neo4j_id'], rel_type="refers_to_content")

    print("=" * 75)

    helper.close()
    query_util.close_driver()

    # Save results to JSON
    output_data = {
        "user_query": result['query'],
        "embedding_time_seconds": elapsed,
        "search_time_seconds": elapsed_q,
        "results": records,
    }

    sanitized_query = args.query[:10].replace(" ", "_")
    search_result_path = f'search_results_{sanitized_query}.json'
    os.makedirs("results", exist_ok=True)
    with open(os.path.join("results", search_result_path), 'w', encoding='utf-8') as f:
        json.dump(output_data, f, indent=4, ensure_ascii=False)
    print(f"\nSaved {len(records)} records to {search_result_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run a hybrid search against the KG.")
    parser.add_argument("--query", "-Q", type=str, help="query to search")
    parser.add_argument("--env_file", "-env", type=str, default=".env", help="Path to the .env file (default: .env)")
    parser.add_argument("--verbosity", "-V", default=1, type=int, help="verbosity level")
    parser.add_argument("--source_k", type=int, default=10, help="candidates pulled per source")
    parser.add_argument("--final_k", type=int, default=20, help="final merged results to return")
    parser.add_argument("--rrf_constant", type=int, default=60, help="wRRF denominator offset")
    args = parser.parse_args()
    main(args)


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