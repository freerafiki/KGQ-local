# TODO

## ColBERT reranker rollout (BGE-M3 late interaction)

Code is in place (`colbert_m3.py`, `scripts/build_colbert_index.py`,
`RERANKER_KIND` in `query_config.py`). It has not run yet with the real
model. Steps, in order:

### 1. Install FlagEmbedding (every machine that builds or serves ColBERT)

```bash
/usr/bin/python3 -m pip install --user FlagEmbedding==1.4.2
```

Use the interpreter that has the project dependencies. On the dev laptop
that is `/usr/bin/python3`, not the miniconda `python3`. 1.4.2 requires
`transformers<6`, which is compatible with the transformers 5.x pulled in by
sentence-transformers 6.

Smoke test: `python3 -c "import colbert_m3; v = colbert_m3.encode_documents(['ciao']); print(v[0].shape)"`
should print `(n_tokens, 1024)`, and `import query_config` must still load (no
transformers conflict).

### 2. Re-embed the dense vectors without prompts (build machine)

`USE_EMBEDDING_PROMPTS` is now derived from the model (`MODEL_PROMPTS` in
`embedding_text.py`): on for EmbeddingGemma, **off for bge-m3**. The vectors
currently in Neo4j were most likely encoded with prompts. Queries are now
encoded without them, so the two sides no longer match.

- [ ] `scripts/insert_embeddings.py` with `RESET_EMBEDDING_STATUS = True` (full re-encode)
- [ ] `scripts/export_embeddings.py` → copy → `scripts/import_embeddings.py` here
      (old exports are refused: their meta says `prompts: true`)
- [ ] recalibrate `THRESHOLDS` in `query_config.py` (`scripts/calibrate_thresholds.py`)

### 3. Build and ship the ColBERT index (build machine)

- [ ] `python3 scripts/build_colbert_index.py` → `data/colbert/` (~2,000 texts, expect ~1–1.2 GB)
- [ ] `rsync -a data/colbert/` to the laptop / server, restart the API
- details: [data_transfer.md](data_transfer.md#colbert-reranker-index)

### 4. Measure

- [ ] Download the configured cross-encoder for the baseline. On the laptop the cached
      `bge-reranker-v2-m3` / `mmarco-mMiniLMv2` weights are gone, and
      `ms-marco-MiniLM-L6-v2` was never downloaded (the first run fetches it)
- [ ] latency: `python3 scripts/query_benchmark.py --queries eval/queries.tsv --rerankers none cross-encoder colbert`
      (add `--top-n 30` to compare at equal depth)
- [ ] quality: `scripts/eval_search.py run|pool|score` with `nl`, `nl+rerank`, `nl+colbert`
      (nDCG@10 per tag, esp. it vs en)
- [ ] calibrate `COLBERT_RERANK_THRESHOLD` (placeholder 0.5):
      `calibrate_thresholds.py dump --reranker colbert`, then `fit`
- [ ] decide the default `RERANKER_KIND`, `COLBERT_ON_MISS` (`encode` vs `far`) and
      `COLBERT_RERANK_TOP_N` for the laptop and the server

## Ettin rerankers (cross-encoder family, 17m → 1b)

Code is in place: the registry is in `reranker_models.py` (`RERANKER_MODELS`,
default `RERANKER`). Every entry can be selected per request (`"reranker":
"ettin-32m"`), in `eval_search.py` (`nl+ettin-32m`, …), in
`query_benchmark.py` and in `calibrate_thresholds.py dump --reranker`.
**Nothing has been run with a real model yet. Do it on the other machine.**

Two things to keep in mind:
- **English only.** Ettin is trained and evaluated on English. Many of the
  queries here are Italian against English text, so always look at the
  it/en split.
- **Raw logits.** Ettin scores are logits (~2..11), not probabilities. The
  thresholds start as `None`: ranking works, but every result is "far" until
  calibrated (step 4).

### 1. Smoke test (needs `transformers>=4.48` for ModernBERT; downloads ~70 MB)

```bash
python3 -c "
from sentence_transformers import CrossEncoder
m = CrossEncoder('cross-encoder/ettin-reranker-17m-v1')
q = 'flood risk in coastal cities'
docs = ['Sea level rise increases flooding of coastal urban areas.',
        'A recipe for sourdough bread.',
        'Rischio alluvioni nelle città costiere italiane.']
print(m.predict([(q, d) for d in docs]))   # expect logits, relevant ones clearly higher
"
```

### 2. Latency, on EACH serving machine (laptop and server)

```bash
python3 scripts/query_benchmark.py --queries eval/queries.tsv --limit 10 \
    --rerankers none minilm ettin-17m ettin-32m ettin-68m colbert
# same depth for every model:  add --top-n 30
# GPU server: also ettin-150m ettin-400m ettin-1b bge-m3
```

Compare `pairs/s` with the blog's CPU table (i7-13700K: 17m 267, 32m 93,
68m 31, 150m 14). On the laptop expect roughly 10–15x lower.

### 3. Quality

```bash
python3 scripts/eval_search.py run --queries eval/queries.tsv --runs eval/runs \
    --methods nl nl+rerank nl+ettin-17m nl+ettin-32m nl+ettin-68m nl+colbert
python3 scripts/eval_search.py pool --queries eval/queries.tsv --runs eval/runs \
    --judgments eval/judgments.csv          # then judge the new rows
python3 scripts/eval_search.py score --queries eval/queries.tsv --runs eval/runs \
    --judgments eval/judgments.csv --by-tag  # it vs en!
```

### 4. Calibrate the sizes worth keeping

```bash
python3 scripts/calibrate_thresholds.py dump --queries eval/queries.tsv \
    --judgments eval/judgments.csv --reranker ettin-32m --out data/pool_ettin32.csv  # data/ is gitignored
python3 scripts/calibrate_thresholds.py fit --labels data/pool_ettin32.csv
```

Paste the suggested value into `RERANKER_MODELS["ettin-32m"]["threshold"]`
(`reranker_models.py`).

### 5. Choose per machine

- [ ] laptop: `RERANKER = ...` (and `RERANKER_KIND`) in `reranker_models.py` / `query_config.py`
- [ ] server: same, likely a larger size, or bge-m3 if Italian quality wins
