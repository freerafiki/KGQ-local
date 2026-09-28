"""
KGQ Search API

Run with:  uvicorn app:app --host 0.0.0.0 --port 28000
   (or:    python app.py)

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
import logging
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Literal
from prometheus_fastapi_instrumentator import Instrumentator

import query_util

# stdout logging; uvicorn's root handler gives every line a timestamp, so
# under systemd everything ends up in the journal (journalctl -u kgq.service).
# Level INFO = one line per request + one line per search (incl. timings).
logger = logging.getLogger("kgq.api")


class FilterModel(BaseModel):
    types: list[Literal["Contribution", "Recommendation", "Gap", "Project"]] = Field(
        default=[], description="Node labels to restrict results to; empty = all types"
    )


class SearchRequest(BaseModel):
    query: str
    source_k: int = Field(default=10, ge=1, description="Candidates pulled per source")
    final_k: int = Field(default=20, ge=1, description="Final merged results to return")
    rrf_constant: int = Field(default=60, ge=1, description="wRRF denominator offset")
    filters: FilterModel | None = Field(
        default=None, description="Result filters (e.g. restrict to specific node types)"
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # query_util.get_driver() is lazy; nothing to do here besides a health check.
    for ip in query_util.get_lan_ips():
        logger.info("API on http://%s:28000  (LAN access)", ip)
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


@app.middleware("http")
async def timing_middleware(request: Request, call_next):
    """Standard per-request timing line: method, path, status, duration."""
    started = time.perf_counter()
    response = await call_next(request)
    logger.info("%s %s -> %d in %.3fs",
                request.method, request.url.path,
                response.status_code, time.perf_counter() - started)
    return response


# Prometheus metrics at GET /metrics (request counts + latency histograms
# per route/status). Excluded from the OpenAPI schema. Do NOT proxy it
# through Apache for the public — scrape it from localhost instead.
Instrumentator().instrument(app).expose(app, include_in_schema=False)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/search")
def search(req: SearchRequest):
    """Hybrid search: BM25 fulltext + vector indexes, fused via wRRF."""
    query_preview = req.query if len(req.query) <= 100 else req.query[:100] + "\u2026"
    types = req.filters.types if req.filters else None
    started = time.perf_counter()
    logger.info("search IN   q=%r (source_k=%d, final_k=%d, rrf=%d%s)",
                query_preview, req.source_k, req.final_k, req.rrf_constant,
                ", types=" + ",".join(types) if types else "")

    result = query_util.run_search(
        req.query,
        source_k=req.source_k,
        final_k=req.final_k,
        rrf_constant=req.rrf_constant,
        types=types,
    )
    total = time.perf_counter() - started
    logger.info("search DONE q=%r embed=%.3fs neo4j=%.3fs total=%.3fs n=%d",
                query_preview, result["embedding_time_s"],
                result["search_time_s"], total, len(result["results"]))
    return result


@app.get("/node/{eid}")
def node_detail(eid: str):
    """Full properties of one node + its OI <-> Rec/Gap neighbourhood."""
    detail = query_util.get_node_detail(eid)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"Node not found: {eid}")
    logger.info("node detail eid=%s", eid)
    return detail


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="127.0.0.1", port=28000)