"""Cross-encoder reranker registry.

Dependency-free on purpose (like embedding_text.py): scripts/eval_search.py
and scripts/calibrate_thresholds.py build their method lists from it without
loading sentence-transformers or any model. query_config.py re-exports both
names and does the loading (get_reranker); see its "Rerankers" section for how
a reranker is chosen per request.
"""

# Cross-encoder registry. Per model:
#   model       Hugging Face id
#   activation  "sigmoid": the model emits a logit meant as a probability
#                  (score = sigmoid(logit), in [0, 1], rounded to 4 decimals)
#               "identity": raw logit kept as the score (Ettin: trained with an
#                  identity activation, logits ~2..11; a sigmoid would squash
#                  the top candidates into ties at 0.9999)
#   threshold   strong tier cut ON THAT SCALE. None = not calibrated yet:
#               ranking still works, but every result is "far" (warned).
#               Calibrate with scripts/calibrate_thresholds.py
#               dump --reranker <name>, then fit.
#
# CPU cost grows with model size x RERANK_TOP_N x RERANK_MAX_LENGTH.
# Measured on the dev laptop (i5-4278U, 2 cores), 20 candidates:
#   cross-encoder/mmarco-mMiniLMv2-L12-H384-v1 (118M, 384 tok)   ~4 s
#   BAAI/bge-reranker-v2-m3                    (568M, 512 tok)  ~95 s
# bge-reranker-v2-m3 is clearly better (esp. Italian query <-> English text).
# Ettin (https://huggingface.co/blog/ettin-reranker, ModernBERT, 8k ctx):
# ENGLISH-ONLY training/evaluation -> check the eval per tag (it vs en)
# before trusting it here. Published CPU throughput (i7-13700K, pairs/s):
# 17m 267, 32m 93, 68m 31, 150m 14, 400m 5.2, 1b 2.1; the dev laptop is
# roughly 10-15x slower. 400m / 1b: GPU only in practice.
# Also tried: "Qwen/Qwen3-Reranker-0.6B" -> too slow.
_ETTIN = {"activation": "identity", "threshold": None}
RERANKER_MODELS = {
    "minilm":     {"model": "cross-encoder/ms-marco-MiniLM-L6-v2", "activation": "sigmoid",
                   # !!! PLACEHOLDER — calibrate
                   "threshold": 0.5},
    "bge-m3":     {"model": "BAAI/bge-reranker-v2-m3", "activation": "sigmoid",   # multilingual
                   "threshold": 0.5},   # !!! PLACEHOLDER — calibrate
    "ettin-17m":  {"model": "cross-encoder/ettin-reranker-17m-v1", **_ETTIN},
    "ettin-32m":  {"model": "cross-encoder/ettin-reranker-32m-v1", **_ETTIN},
    "ettin-68m":  {"model": "cross-encoder/ettin-reranker-68m-v1", **_ETTIN},
    "ettin-150m": {"model": "cross-encoder/ettin-reranker-150m-v1", **_ETTIN},
    "ettin-400m": {"model": "cross-encoder/ettin-reranker-400m-v1", **_ETTIN},
    "ettin-1b":   {"model": "cross-encoder/ettin-reranker-1b-v1", **_ETTIN},
}
RERANKER = "minilm"        # the default cross-encoder (spec "cross-encoder")
