"""Offline evaluation of the search methods on a judged query set.

Workflow (pooled, graded relevance judgments, TREC style):

  1. run    Run every method on every query and save the ranked lists:
                python scripts/eval_search.py run --queries eval/queries.tsv --runs eval/runs

  2. pool   Merge the top-`depth` results of ALL saved runs per query into the
            judgments file. Rows already judged keep their label; only new
            (query, document) pairs are appended, so a new method or a rebuilt
            graph only costs the judging of what is new:
                python scripts/eval_search.py pool --queries eval/queries.tsv \\
                    --runs eval/runs --judgments eval/judgments.csv --depth 20

            Judge every row: label 2 = relevant (answers the query),
            1 = partially relevant / useful context, 0 = not relevant.
            The file deliberately does NOT say which method or rank found a
            row: judging blind avoids favouring one method.

  3. score  Metrics per method (and per query tag) from runs + judgments:
                python scripts/eval_search.py score --queries eval/queries.tsv \\
                    --runs eval/runs --judgments eval/judgments.csv

Files:
  queries.tsv     tab-separated, header `qid  query  tags  narrative`.
                  tags: comma-separated (e.g. short,it,topical / known-item /
                  no-answer); narrative: what counts as relevant (the
                  instruction for whoever judges).
  runs/<m>.jsonl  one line per query: {"qid", "query", "results": [...]}
  judgments.csv   qid, query, doc_key, type, title, text, label, note

Documents are identified by `doc_key` (query_util._doc_key), which survives a
graph rebuild — Neo4j element ids do not.
"""

import argparse
import csv
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# name -> run_search arguments. "+rerank" variants differ ONLY by the
# cross-encoder, so the pair measures what the reranker adds.
METHODS = {
    "nl":              {"mode": "nl",       "rerank_results": False},
    "nl+rerank":       {"mode": "nl",       "rerank_results": True},
    "keywords":        {"mode": "keywords", "rerank_results": False},
    "keywords+rerank": {"mode": "keywords", "rerank_results": True},
    "title":           {"mode": "title",    "rerank_results": False},
    "title+rerank":    {"mode": "title",    "rerank_results": True},
    "author":          {"mode": "author",   "rerank_results": False},
}

JUDGMENT_FIELDS = ["qid", "query", "doc_key", "type", "title", "text", "label", "note"]


# --------------------------------------------------------------- io ---------

def load_queries(path):
    queries = []
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh, delimiter="\t"):
            if not (row.get("qid") or "").strip() or row["qid"].startswith("#"):
                continue
            queries.append({
                "qid": row["qid"].strip(),
                "query": (row.get("query") or "").strip(),
                "tags": [t.strip() for t in (row.get("tags") or "").split(",") if t.strip()],
                "narrative": (row.get("narrative") or "").strip(),
            })
    if not queries:
        raise SystemExit(f"no queries in {path}")
    return queries


def load_runs(runs_dir, methods=None):
    runs = {}
    for f in sorted(Path(runs_dir).glob("*.jsonl")):
        if methods and f.stem not in methods:
            continue
        runs[f.stem] = {}
        for line in f.read_text().splitlines():
            rec = json.loads(line)
            runs[f.stem][rec["qid"]] = rec
    if not runs:
        raise SystemExit(f"no runs in {runs_dir} (run the `run` step first)")
    return runs


def load_judgments(path):
    rows = []
    if Path(path).exists():
        with open(path, newline="") as fh:
            rows = list(csv.DictReader(fh))
    return rows


def labels_by_query(rows):
    """{qid: {doc_key: int label}} — unjudged (blank) rows are left out."""
    out = defaultdict(dict)
    for r in rows:
        lab = (r.get("label") or "").strip()
        if lab in ("0", "1", "2"):
            out[r["qid"]][r["doc_key"]] = int(lab)
    return out


def _text(r, n=700):
    if r["type"] == "Oggetto Informativo":
        parts = [r.get("abstract"), r.get("findings")]
    elif r["type"] == "Raccomandazione":
        parts = [r.get("content"), r.get("motivation")]
    else:
        parts = [r.get("description")]
    text = " ".join(" ".join(str(p).split()) for p in parts if p)
    return text[:n] + ("…" if len(text) > n else "")


# -------------------------------------------------------------- run ---------

def cmd_run(args):
    from query_util import run_search, close_driver
    queries = load_queries(args.queries)
    methods = args.methods or list(METHODS)
    out_dir = Path(args.runs)
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        for m in methods:
            if m not in METHODS:
                raise SystemExit(f"unknown method {m!r}; known: {', '.join(METHODS)}")
            lines, failed = [], 0
            for q in queries:
                try:
                    res = run_search(q["query"], final_k=args.final_k,
                                     source_k=args.source_k, **METHODS[m])
                except Exception as exc:  # e.g. nl before the re-embedding
                    failed += 1
                    print(f"  [{m}] {q['qid']}: FAILED {str(exc)[:160]}")
                    continue
                lines.append(json.dumps({
                    "qid": q["qid"], "query": q["query"], "method": m,
                    "reranked": res.get("reranked", False),
                    "results": [{
                        "doc_key": r["doc_key"], "type": r["type"],
                        "title": r.get("officialTitle") or r.get("title"),
                        "text": _text(r),
                        "tier": r["tier"],
                        "prefusion_tier": r.get("prefusion_tier", r["tier"]),
                        "rerank_score": r.get("rerank_score"),
                        "wrrf": r["wrrf_score"],
                        "sources": r["sources"], "rawScores": r["rawScores"],
                    } for r in res["results"]],
                }, ensure_ascii=False))
            if lines:
                (out_dir / f"{m}.jsonl").write_text("\n".join(lines) + "\n")
            print(f"{m}: {len(lines)} queries saved" + (f", {failed} failed" if failed else ""))
    finally:
        close_driver()


# ------------------------------------------------------------- pool ---------

def cmd_pool(args):
    queries = load_queries(args.queries)
    runs = load_runs(args.runs, args.methods)
    rows = load_judgments(args.judgments)
    have = {(r["qid"], r["doc_key"]) for r in rows}
    added = 0
    for q in queries:
        for m, by_q in runs.items():
            rec = by_q.get(q["qid"])
            if not rec:
                continue
            for r in rec["results"][:args.depth]:
                key = (q["qid"], r["doc_key"])
                if key in have:
                    continue
                have.add(key)
                rows.append({"qid": q["qid"], "query": q["query"],
                             "doc_key": r["doc_key"], "type": r["type"],
                             "title": r["title"] or "", "text": r["text"],
                             "label": "", "note": ""})
                added += 1
    # Grouped by query (file order), documents alphabetical: no rank or
    # method information leaks into the judging order.
    order = {q["qid"]: i for i, q in enumerate(queries)}
    rows.sort(key=lambda r: (order.get(r["qid"], 10**6), r["qid"], r["title"].lower()))
    Path(args.judgments).parent.mkdir(parents=True, exist_ok=True)
    with open(args.judgments, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=JUDGMENT_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    todo = sum(1 for r in rows if (r.get("label") or "").strip() not in ("0", "1", "2"))
    print(f"{added} new pairs added; {len(rows)} rows, {todo} still to judge -> {args.judgments}")


# ------------------------------------------------------------ score ---------

def dcg(gains):
    return sum(g / math.log2(i + 2) for i, g in enumerate(gains))


def query_metrics(ranked, labels, k, rel_min):
    """Metrics for one ranked list. `labels` = {doc_key: 0/1/2} for the query
    (pool-based: unjudged documents count as not relevant)."""
    keys = [r["doc_key"] for r in ranked]
    lab = [labels.get(d) for d in keys]
    rel = [l is not None and l >= rel_min for l in lab]
    n_rel = sum(1 for l in labels.values() if l >= rel_min)

    gains = [(2 ** l - 1) if l else 0 for l in lab[:k]]
    ideal = sorted(((2 ** l - 1) for l in labels.values()), reverse=True)[:k]
    first = next((i for i, x in enumerate(rel) if x), None)

    strong = [x for x, r in zip(rel, ranked) if r["tier"] == "strong"]
    return {
        "P@5": sum(rel[:5]) / 5,
        f"P@{k}": sum(rel[:k]) / k,
        f"nDCG@{k}": dcg(gains) / dcg(ideal) if any(ideal) else None,
        "MRR": 1 / (first + 1) if first is not None else 0.0,
        "R@20": sum(rel[:20]) / n_rel if n_rel else None,
        "strong_n": len(strong),
        "strong_P": sum(strong) / len(strong) if strong else None,
        "strong_R": sum(strong) / n_rel if n_rel else None,
        "unjudged@k": sum(1 for l in lab[:k] if l is None),
        "has_rel": n_rel > 0,
    }


def _mean(vals):
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def _fmt(v, pct=False):
    if v is None:
        return "   -  "
    return f"{v:6.3f}" if not pct else f"{v:6.0%}"


def cmd_score(args):
    queries = load_queries(args.queries)
    runs = load_runs(args.runs, args.methods)
    labels = labels_by_query(load_judgments(args.judgments))
    k = args.k
    cols = ["P@5", f"P@{k}", f"nDCG@{k}", "MRR", "R@20", "strong_P", "strong_R", "strong_n"]
    per_query_rows = []

    def table(title, qs):
        print(f"\n=== {title} ({len(qs)} queries) ===")
        print(f"{'method':18}" + "".join(f"{c:>9}" for c in cols)
              + f"{'no-ans ok':>11}{'unjudged':>10}")
        for m, by_q in runs.items():
            ms, noans, unj = [], [], 0
            for q in qs:
                rec = by_q.get(q["qid"])
                if not rec:
                    continue
                qm = query_metrics(rec["results"], labels.get(q["qid"], {}), k, args.rel_min)
                unj += qm["unjudged@k"]
                if "no-answer" in q["tags"] or not qm["has_rel"]:
                    # Nothing relevant exists: ranking metrics are undefined;
                    # what matters is that NOTHING is presented as strong.
                    noans.append(qm["strong_n"] == 0)
                    continue
                ms.append(qm)
            line = f"{m:18}" + "".join(
                f"{_fmt(_mean([x[c] for x in ms])):>9}" if c != "strong_n"
                else f"{(_mean([x[c] for x in ms]) or 0):9.1f}" for c in cols)
            line += f"{_fmt(_mean(noans), pct=True) if noans else '   -  ':>11}{unj:>10}"
            print(line)

    table("all queries", queries)
    tags = sorted({t for q in queries for t in q["tags"]})
    if args.by_tag:
        for t in tags:
            table(f"tag: {t}", [q for q in queries if t in q["tags"]])

    for m, by_q in runs.items():
        for q in queries:
            rec = by_q.get(q["qid"])
            if rec:
                qm = query_metrics(rec["results"], labels.get(q["qid"], {}), k, args.rel_min)
                per_query_rows.append({"method": m, "qid": q["qid"], "query": q["query"],
                                       "tags": ",".join(q["tags"]), **qm})
    if args.per_query:
        with open(args.per_query, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(per_query_rows[0]))
            w.writeheader()
            w.writerows(per_query_rows)
        print(f"\nper-query metrics -> {args.per_query}")

    print(f"\nrelevant = label >= {args.rel_min}. 'unjudged' = documents in the top {k} "
          f"with no label (counted as NOT relevant): re-run `pool` and judge them.\n"
          f"strong_P / strong_R = precision / recall of the results shown before "
          f"'Show more'; no-ans ok = share of queries without any relevant document "
          f"that show nothing as strong.")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="run the methods, save ranked lists")
    r.add_argument("--queries", required=True)
    r.add_argument("--runs", required=True, help="output directory")
    r.add_argument("--methods", nargs="*", help=f"subset of: {', '.join(METHODS)}")
    r.add_argument("--final-k", type=int, default=50)
    r.add_argument("--source-k", type=int, default=20)
    r.set_defaults(func=cmd_run)

    p = sub.add_parser("pool", help="add new (query, doc) pairs to the judgments")
    p.add_argument("--queries", required=True)
    p.add_argument("--runs", required=True)
    p.add_argument("--judgments", required=True)
    p.add_argument("--depth", type=int, default=20, help="top-N per run to judge")
    p.add_argument("--methods", nargs="*")
    p.set_defaults(func=cmd_pool)

    s = sub.add_parser("score", help="metrics per method")
    s.add_argument("--queries", required=True)
    s.add_argument("--runs", required=True)
    s.add_argument("--judgments", required=True)
    s.add_argument("--methods", nargs="*")
    s.add_argument("--k", type=int, default=10)
    s.add_argument("--rel-min", type=int, default=1, choices=(1, 2),
                   help="lowest label counted as relevant (2 = strict)")
    s.add_argument("--by-tag", action="store_true", help="one table per query tag")
    s.add_argument("--per-query", help="write per-query metrics to this CSV")
    s.set_defaults(func=cmd_score)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
