"""Latency benchmark of the rerankers on a query set.

Runs every query once per reranker (after one untimed warm-up query that
loads the models) and reports per-step timings, so the cost of the
cross-encoder and of ColBERT MaxSim can be compared on THIS machine:

    python3 scripts/query_benchmark.py --queries eval/queries.tsv \\
        --rerankers none minilm ettin-17m ettin-32m colbert [--mode nl] [--top-n 30]

--rerankers: "none", "colbert", "cross-encoder" (the default model) or any
name in reranker_models.RERANKER_MODELS. Each cross-encoder also reports
pairs/s, comparable with the Ettin blog's throughput table.

queries: the eval set's queries.tsv (column `query`) or one query per line.
--top-n defaults to each reranker's own config (RERANK_TOP_N /
COLBERT_RERANK_TOP_N); pass it to compare at the same depth.
"""

import argparse
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

STEPS = ("embedding_time_s", "search_time_s", "rerank_time_s")
RERANK_STEPS = ("rerank_model_s", "rerank_query_encode_s", "rerank_maxsim_s",
                "rerank_miss_encode_s")


def load_queries(path):
    lines = Path(path).read_text().splitlines()
    if lines and lines[0].split("\t")[:2] == ["qid", "query"]:
        return [l.split("\t")[1].strip() for l in lines[1:]
                if l.strip() and not l.startswith("#") and "\t" in l]
    return [q.strip() for q in lines if q.strip() and not q.strip().startswith("#")]


def summary(values):
    values = sorted(values)
    if not values:
        return "-"
    p90 = values[min(len(values) - 1, int(0.9 * len(values)))]
    return (f"median {statistics.median(values):7.3f}  mean {statistics.mean(values):7.3f}  "
            f"p90 {p90:7.3f}  max {values[-1]:7.3f}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--queries", required=True)
    from reranker_models import RERANKER_MODELS   # dependency-free
    ap.add_argument("--rerankers", nargs="+", default=["none", "cross-encoder", "colbert"],
                    choices=("none", "cross-encoder", "colbert", *RERANKER_MODELS))
    ap.add_argument("--mode", default="nl", choices=("nl", "keywords", "title"))
    ap.add_argument("--top-n", type=int, default=None)
    ap.add_argument("--limit", type=int, default=None, help="first N queries only")
    args = ap.parse_args()

    from query_util import run_search, close_driver
    queries = load_queries(args.queries)[:args.limit]
    if not queries:
        raise SystemExit(f"no queries in {args.queries}")
    print(f"{len(queries)} queries, mode={args.mode}\n")
    try:
        for kind in args.rerankers:
            kw = {"mode": args.mode, "rerank_results": kind != "none",
                  "reranker": None if kind == "none" else kind, "rerank_top_n": args.top_n}
            run_search(queries[0], **kw)   # warm-up: model loads, caches
            times = {k: [] for k in STEPS + RERANK_STEPS}
            n_scored, misses = [], 0
            for q in queries:
                res = run_search(q, **kw)
                for k in STEPS:
                    times[k].append(res[k])
                for k in RERANK_STEPS:
                    if k in res["rerank_timing"]:
                        times[k].append(res["rerank_timing"][k])
                misses += res["rerank_timing"].get("colbert_misses", 0)
                n_scored.append(sum(r.get("rerank_score") is not None for r in res["results"]))
            print(f"=== {kind} ===" + (f"  (candidates scored per query: "
                                        f"{statistics.mean(n_scored):.0f})" if kind != "none" else ""))
            for k in STEPS + RERANK_STEPS:
                if times[k] and (k in STEPS or kind != "none"):
                    print(f"  {k:<24} {summary(times[k])}")
            if kind == "colbert":
                print(f"  colbert index misses     {misses} in total")
            elif kind != "none" and sum(times["rerank_model_s"]):
                print(f"  {'pairs/s':<24} {sum(n_scored) / sum(times['rerank_model_s']):7.1f}")
            print()
    finally:
        close_driver()


if __name__ == "__main__":
    main()
