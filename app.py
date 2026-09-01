"""
KGQ Search API

Run with:  uvicorn app:app --reload   (or:  python app.py)

Exposes the hybrid retrieval pipeline implemented in query_util.run_search().

Future endpoints to add (see query_util.py for the strategy code):
  - POST /search/semantic       vector-only search (skip the fulltext subquery)
  - POST /search/fulltext       BM25/fulltext-only search
  - POST /search/rerank         hybrid recall + cross-encoder re-scoring
  - POST /search/expand         query expansion/rewrite before embedding
  - POST /search/text2cypher    LLM-generated Cypher over the graph
  - POST /search/neighbours     graph-exploration around a result node
"""

from contextlib import asynccontextmanager
from datetime import datetime
import time

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import query_util


class SearchRequest(BaseModel):
    query: str
    source_k: int = Field(default=10, ge=1, description="Candidates pulled per source")
    final_k: int = Field(default=20, ge=1, description="Final merged results to return")
    rrf_constant: int = Field(default=60, ge=1, description="wRRF denominator offset")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # query_util.get_driver() is lazy; nothing to do here besides a health check.
    for ip in query_util.get_lan_ips():
        print(f"API on http://{ip}:8000  (LAN access)")
    yield
    query_util.close_driver()


app = FastAPI(title="KGQ Search API", lifespan=lifespan)

# The HTML page is served from a separate static server (python -m http.server)
# on another port, so the API must allow cross-origin requests from it.
# Dev-only: open to any origin; tighten before any real deployment.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/search")
def search(req: SearchRequest):
    """Hybrid search: BM25 fulltext + vector indexes, fused via wRRF."""
    query_preview = req.query if len(req.query) <= 100 else req.query[:100] + "\u2026"
    started = time.perf_counter()
    print(f"[{datetime.now().isoformat(timespec='seconds')}] search IN    q={query_preview!r} "
          f"(source_k={req.source_k}, final_k={req.final_k}, rrf={req.rrf_constant})")

    result = query_util.run_search(
        req.query,
        source_k=req.source_k,
        final_k=req.final_k,
        rrf_constant=req.rrf_constant,
    )
    total = time.perf_counter() - started
    print(f"[{datetime.now().isoformat(timespec='seconds')}] search DONE  "
          f"q={query_preview!r} embed={result['embedding_time_s']:.3f}s "
          f"neo4j={result['search_time_s']:.3f}s total={total:.3f}s "
          f"results={len(result['results'])}")
    return result


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="127.0.0.1", port=8000)