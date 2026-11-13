"""Calibrate the relevance thresholds of the tiered wRRF search
(query_config.THRESHOLDS).

Two steps:

1) dump: run real queries with PERMISSIVE thresholds (every hit passes) and
   write the pooled top results, with the raw score of every branch, to a CSV
   for manual labelling:

       python scripts/calibrate_thresholds.py dump \
           --queries queries.txt --out pool.csv [--pool 20] [--source-k 50]

   queries.txt: one query per line (blank lines and lines starting with '#'
   are ignored), OR the eval set's queries.tsv (column `query`). Open
   pool.csv and fill the `relevant` column with 2 / 1 / 0 (leave it empty to
   skip a row). With --judgments eval/judgments.csv, rows already judged
   for scripts/eval_search.py are pre-filled, so nothing is labelled twice.
   The reranker scores the pooled rows too (column rerank_score);
   --reranker picks which one: "colbert", or a cross-encoder name from
   reranker_models.RERANKER_MODELS (e.g. ettin-32m). The column is on THAT
   model's scale, so `fit` it into that model's threshold:
   RERANKER_MODELS[name]["threshold"] (reranker_models.py), or
   COLBERT_RERANK_THRESHOLD (query_config.py) for colbert.
   Needs the DB, the embedding model and the reranker.

2) fit: read the labelled CSV, sweep each threshold, and suggest values:

       python scripts/calibrate_thresholds.py fit \
           --labels pool.csv [--target-precision 0.8] [--plot]

   Per vector source: precision / recall / F1 of `score >= minSim` among the
   labelled rows that branch retrieved. Lexical sources: the same for
   `score >= alpha * top` (plus the floor). Then the full tier rule is
   replayed on the labelled set and a ready-to-paste THRESHOLDS block is
   printed. Needs neither the DB nor the model (stdlib only; --plot needs
   matplotlib).

Remember: the vector thresholds depend on the embedding model AND on
USE_EMBEDDING_PROMPTS (embedding_text.py); recalibrate when either changes.
"""

import argparse
import csv
import sys
from pathlib import Path

# Shared modules live in the repo root, one level up.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

LEXICAL = ("fulltext", "title", "authors")
VECTOR = ("doc_description", "doc_title", "doc_subtitle",
          "recommendation", "gap", "project")
SOURCES = LEXICAL + VECTOR


# ---------------------------------------------------------------- dump ------

def _snippet(r, n=200):
    text = (r.get("abstract") or r.get("content") or r.get("description") or "")
    text = " ".join(str(text).split())
    return text[:n] + ("…" if len(text) > n else "")


def dump(args):
    # Imported here: `fit` must run without the model / DB.
    from query_config import PERMISSIVE_THRESHOLDS
    from query_util import run_search, close_driver

    lines = Path(args.queries).read_text().splitlines()
    if lines and lines[0].split("\t")[:2] == ["qid", "query"]:
        # the eval set's queries.tsv
        queries = [l.split("\t")[1].strip() for l in lines[1:]
                   if l.strip() and not l.startswith("#") and "\t" in l]
    else:
        queries = [q.strip() for q in lines
                   if q.strip() and not q.strip().startswith("#")]
    prefill = {}
    if args.judgments and Path(args.judgments).exists():
        with open(args.judgments, newline="") as fh:
            for row in csv.DictReader(fh):
                if (row.get("label") or "").strip() in ("0", "1", "2"):
                    prefill[(row["query"], row["doc_key"])] = row["label"].strip()
    if not queries:
        raise SystemExit(f"no queries in {args.queries}")

    header = (["query", "rank", "doc_key", "type", "title", "snippet", "wrrf", "rerank_score"]
              + [f"{s}_score" for s in SOURCES]
              + [f"{s}_rel" for s in LEXICAL]
              + ["relevant"])
    rows_out = 0
    try:
        with open(args.out, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(header)
            for q in queries:
                # Large final_k: EVERY branch hit comes back, so each lexical
                # branch's top score (its rank-1 hit) is known even when that
                # hit is not in the pooled top-N written below.
                # Permissive thresholds: every hit "passes", so the Cypher
                # order is plain wRRF and the reranker scores exactly the
                # pooled top-N written below.
                res = run_search(q, source_k=args.source_k, final_k=100000,
                                 mode="nl", thresholds=PERMISSIVE_THRESHOLDS,
                                 rerank_results=not args.no_rerank,
                                 rerank_top_n=args.pool,
                                 reranker=args.reranker)
                results = sorted(res["results"], key=lambda r: -r["wrrf_score"])
                top = {}
                for r in results:
                    for s, raw in zip(r["sources"], r["rawScores"]):
                        top[s] = max(top.get(s, 0.0), raw)
                for rank, r in enumerate(results[:args.pool], 1):
                    raw = dict(zip(r["sources"], r["rawScores"]))
                    w.writerow(
                        [q, rank, r["doc_key"], r["type"], r["title"] or "",
                         _snippet(r), r["wrrf_score"],
                         "" if r.get("rerank_score") is None else r["rerank_score"]]
                        + [raw.get(s, "") for s in SOURCES]
                        + [round(raw[s] / top[s], 4) if s in raw and top.get(s) else ""
                           for s in LEXICAL]
                        + [prefill.get((q, r["doc_key"]), "")])
                    rows_out += 1
                print(f"{q!r}: {len(results)} hits, wrote top {min(args.pool, len(results))}")
    finally:
        close_driver()
    print(f"\n{rows_out} rows -> {args.out}. Fill the `relevant` column (2/1/0), then run `fit`.")


# ----------------------------------------------------------------- fit ------

def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def load_labels(path, rel_min=1):
    """Graded labels 2 / 1 / 0 (yes/no also accepted); relevant = label >= rel_min."""
    rows = []
    with open(path, newline="") as fh:
        for rec in csv.DictReader(fh):
            lab = (rec.get("relevant") or "").strip().lower()
            if lab in ("y", "yes", "true", "r"):
                lab = "2"
            elif lab in ("n", "no", "false", "nr"):
                lab = "0"
            if lab not in ("0", "1", "2"):
                continue
            rel = int(lab) >= rel_min
            rows.append({
                "query": rec["query"],
                "relevant": rel,
                "score": {s: _f(rec.get(f"{s}_score")) for s in SOURCES},
                "rel": {s: _f(rec.get(f"{s}_rel")) for s in LEXICAL},
                "rerank": _f(rec.get("rerank_score")),
            })
    return rows


def prf(pairs, keep):
    """pairs: [(value, relevant)], keep(value) -> bool. Returns P, R, F1, n_kept."""
    tp = sum(1 for v, rel in pairs if rel and keep(v))
    fp = sum(1 for v, rel in pairs if not rel and keep(v))
    fn = sum(1 for v, rel in pairs if rel and not keep(v))
    p = tp / (tp + fp) if tp + fp else 1.0
    r = tp / (tp + fn) if tp + fn else 0.0
    f = 2 * p * r / (p + r) if p + r else 0.0
    return p, r, f, tp + fp


def pick(pairs, target):
    """Sweep every distinct observed value as a threshold.

    Returns (best_f1_threshold, lowest threshold reaching `target` precision
    or None, table)."""
    cands = sorted({v for v, _ in pairs})
    table = [(t, *prf(pairs, lambda v, t=t: v >= t)) for t in cands]
    best = max(table, key=lambda row: (row[3], -row[0]))[0] if table else None
    hit = next((row[0] for row in table if row[1] >= target and row[4] > 0), None)
    return best, hit, table


def _print_table(table, label, every):
    print(f"  {label:>10}   prec   rec    F1   kept")
    for i, (t, p, r, f, n) in enumerate(table):
        if i % every == 0 or i == len(table) - 1:
            print(f"  {t:10.4f}  {p:5.2f} {r:5.2f} {f:5.2f} {n:6d}")


def tier_of(row, th):
    lexical = semantic = False
    max_vec = 0.0
    for s in LEXICAL:
        v, rel = row["score"][s], row["rel"][s]
        if v is not None and rel is not None:
            if rel >= th["bm25Alpha"] and v >= th["bm25Floor"].get(s, 0.0):
                lexical = True
    for s in VECTOR:
        v = row["score"][s]
        if v is not None:
            max_vec = max(max_vec, v)
            if v >= th["minSim"].get(s, th["minSimDefault"]):
                semantic = True
    return (lexical and semantic) or max_vec >= th["highSim"]


def fit(args):
    rows = load_labels(args.labels, args.rel_min)
    if not rows:
        raise SystemExit(f"no labelled rows in {args.labels} (fill the `relevant` column)")
    n_rel = sum(r["relevant"] for r in rows)
    print(f"{len(rows)} labelled rows over {len({r['query'] for r in rows})} queries, "
          f"{n_rel} relevant ({n_rel / len(rows):.0%})\n")
    pick_key = "precision" if args.target_precision else "F1"
    every = max(1, args.every)

    suggested = {"minSim": {}, "bm25Floor": {}}
    all_vec = []

    print("=== Vector sources: absolute threshold on the Neo4j cosine score ===")
    for s in VECTOR:
        pairs = [(r["score"][s], r["relevant"]) for r in rows if r["score"][s] is not None]
        if len(pairs) < 5:
            print(f"\n[{s}] only {len(pairs)} labelled hits, skipped")
            continue
        all_vec += pairs
        best, hit, table = pick(pairs, args.target_precision or 1.01)
        choice = hit if (args.target_precision and hit is not None) else best
        suggested["minSim"][s] = round(choice, 4)
        print(f"\n[{s}] {len(pairs)} hits; best F1 at {best:.4f}"
              + (f"; precision >= {args.target_precision} from {hit:.4f}" if hit is not None else ""))
        _print_table(table, "minSim", every)

    print("\n=== Lexical sources: relative cut (score / top score of the branch) ===")
    alphas = []
    for s in LEXICAL:
        pairs = [(r["rel"][s], r["relevant"]) for r in rows if r["rel"][s] is not None]
        if len(pairs) < 5:
            print(f"\n[{s}] only {len(pairs)} labelled hits, skipped")
            continue
        best, hit, table = pick(pairs, args.target_precision or 1.01)
        choice = hit if (args.target_precision and hit is not None) else best
        alphas.append(choice)
        print(f"\n[{s}] {len(pairs)} hits; best F1 at alpha={best:.4f}"
              + (f"; precision >= {args.target_precision} from {hit:.4f}" if hit is not None else ""))
        _print_table(table, "alpha", every)
        # Floor: the lowest raw score among the relevant hits, so the floor
        # only removes hits that never turned out relevant in the sample.
        raws = [r["score"][s] for r in rows if r["score"][s] is not None and r["relevant"]]
        if raws:
            suggested["bm25Floor"][s] = round(min(raws) * 0.9, 4)

    # highSim: a single vector hit strong on its own needs HIGH precision.
    # Never below the vector floors, or the lexical+semantic agreement rule
    # would become redundant.
    hs_target = max(args.target_precision or 0.0, 0.9)
    vec_floor = max(suggested["minSim"].values(), default=0.0)
    _, high, _ = pick([(v, rel) for v, rel in all_vec if v > vec_floor], hs_target)
    sugg = {
        "minSimDefault": round(min(suggested["minSim"].values()), 4) if suggested["minSim"] else 0.75,
        "minSim": suggested["minSim"],
        "highSim": round(high, 4) if high is not None else 0.85,
        "bm25Alpha": round(sum(alphas) / len(alphas), 4) if alphas else 0.4,
        "bm25Floor": suggested["bm25Floor"],
    }
    print(f"\nhighSim: lowest vector score with precision >= {hs_target}: "
          + (f"{high:.4f}" if high is not None else "not reached (kept 0.85)"))

    # The reranker: ONE threshold, the tier decision whenever it runs.
    rr_pairs = [(r["rerank"], r["relevant"]) for r in rows if r["rerank"] is not None]
    rr_choice = None
    if len(rr_pairs) >= 5:
        print("\n=== Reranker: absolute threshold on the rerank_score column ===")
        best, hit, table = pick(rr_pairs, args.target_precision or 1.01)
        rr_choice = hit if (args.target_precision and hit is not None) else best
        print(f"[rerank] {len(rr_pairs)} rows; best F1 at {best:.4f}"
              + (f"; precision >= {args.target_precision} from {hit:.4f}" if hit is not None else ""))
        _print_table(table, "threshold", every)
        p, rc, f, n = prf(rr_pairs, lambda v: v >= rr_choice)
        print(f"reranker strong tier at {rr_choice:.4f}: {n} rows, precision {p:.2f}, "
              f"recall {rc:.2f}, F1 {f:.2f}")

    # Replay the full tier rule on the labelled rows.
    print(f"\n=== Tier rule replay (selection by {pick_key}) ===")
    p, rc, f, n = prf([(tier_of(r, sugg), r["relevant"]) for r in rows], lambda v: v)
    print(f"strong tier: {n}/{len(rows)} rows, precision {p:.2f}, recall {rc:.2f}, F1 {f:.2f}")
    try:
        from query_config import THRESHOLDS  # noqa: F401  (needs the model)
        p0, r0, f0, n0 = prf([(tier_of(r, THRESHOLDS), r["relevant"]) for r in rows], lambda v: v)
        print(f"current THRESHOLDS: {n0}/{len(rows)} rows, precision {p0:.2f}, recall {r0:.2f}, F1 {f0:.2f}")
    except Exception:
        pass

    print("\n# ---- paste into query_config.py ----")
    print("THRESHOLDS = {")
    print(f"    'minSimDefault': {sugg['minSimDefault']},")
    print("    'minSim': {")
    for s, v in sugg["minSim"].items():
        print(f"        '{s}': {v},")
    print("    },")
    print(f"    'highSim': {sugg['highSim']},")
    print(f"    'bm25Alpha': {sugg['bm25Alpha']},")
    print("    'bm25Floor': {")
    for s, v in sugg["bm25Floor"].items():
        print(f"        '{s}': {v},")
    print("    },")
    print("}")
    if rr_choice is not None:
        print(f"# reranker threshold (the model the pool was dumped with):\n"
              f"#   cross-encoder: RERANKER_MODELS[<name>]['threshold'] = {round(rr_choice, 4)}"
              f"   (reranker_models.py)\n"
              f"#   colbert:       COLBERT_RERANK_THRESHOLD = {round(rr_choice, 4)}"
              f"   (query_config.py)")

    if args.plot:
        plot(rows, args.plot_out)


def plot(rows, out):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not installed: --plot skipped")
        return
    panels = [(s, "score") for s in VECTOR] + [(s, "rel") for s in LEXICAL]
    panels = [(s, k) for s, k in panels
              if any(r[k][s] is not None for r in rows)]
    if not panels:
        return
    cols = 3
    nrows = (len(panels) + cols - 1) // cols
    fig, axes = plt.subplots(nrows, cols, figsize=(5 * cols, 3.2 * nrows), squeeze=False)
    for ax, (s, k) in zip(axes.flat, panels):
        rel = [r[k][s] for r in rows if r[k][s] is not None and r["relevant"]]
        non = [r[k][s] for r in rows if r[k][s] is not None and not r["relevant"]]
        ax.hist([rel, non], bins=20, label=["relevant", "not relevant"], alpha=0.7)
        ax.set_title(f"{s} ({'cosine' if k == 'score' else 'score / top'})")
        ax.legend(fontsize=8)
    for ax in list(axes.flat)[len(panels):]:
        ax.axis("off")
    fig.tight_layout()
    fig.savefig(out, dpi=120)
    print(f"plot -> {out}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("dump", help="run queries, write the pooled results to label")
    d.add_argument("--queries", required=True, help="text file, one query per line")
    d.add_argument("--out", default="pool.csv")
    d.add_argument("--pool", type=int, default=20, help="top-N per query to label")
    d.add_argument("--source-k", type=int, default=50, help="candidates per branch")
    d.add_argument("--judgments", help="eval judgments.csv: pre-fill known labels")
    d.add_argument("--no-rerank", action="store_true", help="skip the reranker column")
    from reranker_models import RERANKER_MODELS   # dependency-free
    d.add_argument("--reranker", default=None,
                   choices=("cross-encoder", "colbert", *RERANKER_MODELS),
                   help="reranker for the rerank_score column (default: server config)")
    d.set_defaults(func=dump)

    f = sub.add_parser("fit", help="suggest thresholds from a labelled CSV")
    f.add_argument("--labels", required=True)
    f.add_argument("--target-precision", type=float, default=None,
                   help="pick the lowest threshold reaching this precision "
                        "instead of the F1-maximising one")
    f.add_argument("--every", type=int, default=5, help="print every Nth sweep row")
    f.add_argument("--rel-min", type=int, default=1, choices=(1, 2),
                   help="lowest label counted as relevant (2 = strict)")
    f.add_argument("--plot", action="store_true", help="score histograms (matplotlib)")
    f.add_argument("--plot-out", default="calibration.png")
    f.set_defaults(func=fit)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
