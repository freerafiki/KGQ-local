# KGQ Search API

Hybrid retrieval over the knowledge graph — BM25 fulltext + vector indexes,
fused with weighted RRF — served by a small FastAPI app together with a
static two-page frontend.

## Production layout

| Piece | How it runs |
|---|---|
| API | `kgq.service` (systemd) starts `uvicorn app:app` on **127.0.0.1:28000**, loopback only |
| Frontend | `static/` served by Apache as `DocumentRoot` |
| Proxy | Apache forwards exactly `/search`, `/node`, `/health` to the API — nothing else |
| Config | `.env` (gitignored, never committed): `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`, `NEO4J_GRAPH`, `HF_TOKEN` |
| Logs | `journalctl -u kgq -f` |

Deployment options, systemd unit and the Apache vhost notes:
[possible_deployments.md](possible_deployments.md).
Moving the graph or embeddings between machines: [data_transfer.md](data_transfer.md).

```bash
systemctl status kgq
curl -s localhost:28000/health     # {"status":"ok"}
journalctl -u kgq -f
```

Endpoints: `POST /search`, `GET /node/{eid}`, `GET /health`, and `GET /metrics`
(Prometheus — scrape it on localhost, do **not** proxy it to the public).
The schema endpoints (`/docs`, `/redoc`, `/openapi.json`) are disabled in
`app.py`, so the API shape is not discoverable from outside.

## Run locally (development)

```bash
uvicorn app:app --host 127.0.0.1 --port 28000   # API
./scripts/run_frontend.sh                       # page on http://localhost:28080
./scripts/stop_servers.sh                       # stop both
```

The page picks the API base automatically: from the dev server (`:28080`) it
calls `hostname:28000` directly — cross-origin, so `app.py` allows exactly
`http://localhost:28080` and `http://127.0.0.1:28080` — and from any other
origin it goes same-origin (`/search`, `/node`, `/health`), which is how it
works behind Apache without any CORS at all.

## Repository layout

- `app.py` — HTTP endpoints only; `query_util.py` — all retrieval logic;
  `query_config.py` / `embedding_text.py` / `graph_helper.py` — shared modules.
- `scripts/` — tools you run, never imported by the API:

  | Script | Purpose |
  |---|---|
  | `query_single.py` | run one query from the CLI |
  | `embedding_status.py` | read-only embedding bookkeeping report |
  | `insert_embeddings.py` | compute missing embeddings + create indexes |
  | `export_embeddings.py` / `import_embeddings.py` | move vectors between machines |
  | `add_node_titles.py` | derive short titles on Rec/Gap nodes |
  | `rewrite_query.py` | compare local LLM query rewrites |
  | `query_benchmark.py` | placeholder (currently empty) |
  | `db_tool.sh` | Neo4j dump/restore wrapper |
  | `security_check.sh` | PASS/FAIL security assertions (CORS, schema endpoints) |
  | `run_frontend.sh` / `stop_servers.sh` | dev helpers |

Run every script from the repository root — `.env` is looked up in the
current directory:

```bash
python3 scripts/embedding_status.py
./scripts/db_tool.sh dump /var/lib/neo4j/backup
./scripts/security_check.sh                          # re-check CORS / schema endpoints
```

## Never commit

These are local-only and gitignored; the repo itself is public:

- `.env` — Neo4j credentials + Hugging Face token
- `data/` — embedding exports and Neo4j dumps (the graph corpus)
- `results/` — saved search results (corpus content)
- `webserver.conf` — the real Apache vhost
