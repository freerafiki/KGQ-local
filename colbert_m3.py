"""BGE-M3 ColBERT (late interaction) vectors and their on-disk store.

Thin wrapper around the reference implementation, FlagEmbedding's
BGEM3FlagModel. This is a SEPARATE model instance from the dense
SentenceTransformer in query_config.py: it is loaded only when ColBERT is in
use (building the index, or RERANKER_KIND == "colbert").

Why it is fast: a cross-encoder must read every (query, document) pair at
query time. ColBERT encodes documents ALONE, so their token vectors are
computed once, offline (scripts/build_colbert_index.py, on the build
machine), and copied to the serving machines like the dense vectors. At query
time only the short query is encoded; scoring is MaxSim:

    score(q, d) = mean over query tokens of  max over doc tokens of  q_i . d_j

(token vectors are L2-normalized, so the score is roughly in [0, 1]).

Store layout (a folder, default data/colbert/, copied as a whole):
    vecs.f16    raw float16 matrix [total_tokens, dims], documents back to back
    index.json  {"meta": {...}, "docs": {sha256(text): [row_offset, n_tokens]}}
The key is embedding_text.rerank_text_key(embedding_text.rerank_text(...)):
it survives graph rebuilds and machine moves, and a document whose text
changed simply misses (rebuild with --update).

This module does not import query_config, so the build script does not load
the dense model it does not need.
"""

import json
import logging
import threading
from datetime import datetime
from pathlib import Path

import numpy as np

logger = logging.getLogger("kgq.colbert")

COLBERT_MODEL_NAME = "BAAI/bge-m3"
# Tokens of DOCUMENT text kept (build time). Same budget as RERANK_MAX_LENGTH;
# lower it (and rebuild) if the store gets too big: size is
# ~ n_docs * avg_tokens * dims * 2 bytes.
COLBERT_DOC_MAX_LENGTH = 512
COLBERT_QUERY_MAX_LENGTH = 128
DEFAULT_INDEX_DIR = Path(__file__).resolve().parent / "data" / "colbert"

VECS_FILE = "vecs.f16"
INDEX_FILE = "index.json"

_model = None
_model_lock = threading.Lock()


def get_colbert_model():
    """Lazily load BGEM3FlagModel once per process (thread-safe)."""
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                import torch
                from FlagEmbedding import BGEM3FlagModel
                cuda = torch.cuda.is_available()
                logger.info("loading ColBERT model %s (%s)", COLBERT_MODEL_NAME,
                            "cuda, fp16" if cuda else "cpu, fp32")
                # One explicit device: with several devices FlagEmbedding
                # starts a multi-process pool on every encode call. fp32 on
                # CPU: half precision is much slower there.
                _model = BGEM3FlagModel(COLBERT_MODEL_NAME,
                                        use_fp16=cuda,
                                        devices="cuda:0" if cuda else "cpu",
                                        query_max_length=COLBERT_QUERY_MAX_LENGTH,
                                        passage_max_length=COLBERT_DOC_MAX_LENGTH)
    return _model


def _colbert(texts, max_length, batch_size, query):
    model = get_colbert_model()
    encode = model.encode_queries if query else model.encode_corpus
    out = encode(texts, batch_size=batch_size, max_length=max_length,
                 return_dense=False, return_sparse=False, return_colbert_vecs=True)
    return out["colbert_vecs"]


def encode_query(text):
    """Token vectors of one query, float32 [n_tokens, dims]."""
    vecs = _colbert([text], COLBERT_QUERY_MAX_LENGTH, 1, query=True)[0]
    return np.asarray(vecs, dtype=np.float32)


def encode_documents(texts, batch_size=32):
    """Token vectors of documents, list of float16 [n_tokens, dims]."""
    return [np.asarray(v, dtype=np.float16)
            for v in _colbert(texts, COLBERT_DOC_MAX_LENGTH, batch_size, query=False)]


def maxsim(q, d):
    """BGE-M3 ColBERT score; same math as BGEM3FlagModel.colbert_score
    (einsum, max over doc tokens, mean over query tokens), in numpy so the
    float16 store is used without a torch round-trip."""
    return float((q @ np.asarray(d, dtype=np.float32).T).max(axis=1).mean())


def _flagembedding_version():
    try:
        from importlib.metadata import version
        return version("FlagEmbedding")
    except Exception:
        return None


def store_meta(dims):
    """What a store must agree on to be usable here (checked at load)."""
    return {
        "model": COLBERT_MODEL_NAME,
        "dims": int(dims),
        "doc_max_length": COLBERT_DOC_MAX_LENGTH,
    }


class ColbertStore:
    """Read-only, memory-mapped view of a ColBERT index folder."""

    def __init__(self, index_dir=DEFAULT_INDEX_DIR):
        self.dir = Path(index_dir)
        idx = json.loads((self.dir / INDEX_FILE).read_text())
        self.meta = idx["meta"]
        self.docs = idx["docs"]
        expected = store_meta(self.meta.get("dims", 0))
        wrong = {k: (self.meta.get(k), v) for k, v in expected.items()
                 if self.meta.get(k) != v}
        if wrong:
            raise RuntimeError(
                f"ColBERT index {self.dir} does not match this machine "
                f"(key: (file, local)): {wrong}. Rebuild it with "
                f"scripts/build_colbert_index.py using the same settings.")
        local_fe = _flagembedding_version()
        if self.meta.get("flagembedding") and local_fe and self.meta["flagembedding"] != local_fe:
            logger.warning("ColBERT index built with FlagEmbedding %s, local is %s",
                           self.meta["flagembedding"], local_fe)
        total = self.meta["total_tokens"]
        self.vecs = (np.memmap(self.dir / VECS_FILE, dtype=np.float16, mode="r",
                               shape=(total, self.meta["dims"]))
                     if total else np.zeros((0, self.meta["dims"]), np.float16))
        logger.info("ColBERT index %s: %d docs, %d tokens (built %s)",
                    self.dir, len(self.docs), total, self.meta.get("created"))

    def __len__(self):
        return len(self.docs)

    def __contains__(self, key):
        return key in self.docs

    def get(self, key):
        """float16 [n_tokens, dims] view, or None when the text is not indexed."""
        loc = self.docs.get(key)
        if loc is None:
            return None
        off, n = loc
        return self.vecs[off:off + n]


class ColbertStoreWriter:
    """Builds or extends an index folder; the folder stays valid if the run
    is killed at any point:
    - update: new documents are APPENDED to vecs.f16 (old offsets untouched)
      and index.json is replaced atomically by close();
    - rebuild: vectors go to vecs.f16.tmp, and close() swaps both files in.
    """

    def __init__(self, index_dir=DEFAULT_INDEX_DIR, update=False):
        self.dir = Path(index_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        vecs_path = self.dir / VECS_FILE
        index_path = self.dir / INDEX_FILE
        self._swap = None
        if update and index_path.exists():
            idx = json.loads(index_path.read_text())
            self.meta, self.docs = idx["meta"], idx["docs"]
            expected = store_meta(self.meta["dims"])
            if any(self.meta.get(k) != v for k, v in expected.items()):
                raise SystemExit(f"ABORT: existing index {self.dir} was built with "
                                 f"{ {k: self.meta.get(k) for k in expected} }, "
                                 f"local settings are {expected}: rebuild without --update")
            self.total = self.meta["total_tokens"]
            # drop bytes past the last indexed row (left by an interrupted run)
            with open(vecs_path, "r+b") as fh:
                fh.truncate(self.total * self.meta["dims"] * 2)
            self.fh = open(vecs_path, "ab")
        else:
            self.meta, self.docs, self.total = None, {}, 0
            self._swap = (self.dir / (VECS_FILE + ".tmp"), vecs_path)
            self.fh = open(self._swap[0], "wb")

    def __contains__(self, key):
        return key in self.docs

    def add(self, key, vecs):
        vecs = np.ascontiguousarray(vecs, dtype=np.float16)
        if self.meta is None:
            self.meta = store_meta(vecs.shape[1])
        elif vecs.shape[1] != self.meta["dims"]:
            raise ValueError(f"dims {vecs.shape[1]} != index dims {self.meta['dims']}")
        self.fh.write(vecs.tobytes())
        self.docs[key] = [self.total, int(vecs.shape[0])]
        self.total += int(vecs.shape[0])

    def close(self):
        self.fh.close()
        if self.meta is None:      # nothing ever added to a fresh index
            self.meta = store_meta(0)
        self.meta.update({
            "flagembedding": _flagembedding_version(),
            "created": datetime.now().isoformat(timespec="seconds"),
            "n_docs": len(self.docs),
            "total_tokens": self.total,
        })
        tmp = self.dir / (INDEX_FILE + ".tmp")
        tmp.write_text(json.dumps({"meta": self.meta, "docs": self.docs}))
        if self._swap:
            # Old index first: between the two renames the folder has new
            # vectors and no index (a clean "not built" error), never an old
            # index pointing into new vectors.
            (self.dir / INDEX_FILE).unlink(missing_ok=True)
            self._swap[0].replace(self._swap[1])
        tmp.replace(self.dir / INDEX_FILE)
