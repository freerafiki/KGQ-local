# Possible deployments for the FastAPI backend

Two standard options for keeping `app.py` running unattended (days/week+),
plus the repo-specific constraints that shape both. Nothing here is committed
to yet — pick when/where the deployment happens.

FastAPI's official docs intentionally do **not** prescribe a single tool; they
define the concerns you must cover and list example tools for each
([Deployments Concepts](https://fastapi.tiangolo.com/deployment/concepts/)),
with concrete recipes in
[Uvicorn with Workers](https://fastapi.tiangolo.com/server-workers/) and
[FastAPI in Containers - Docker](https://fastapi.tiangolo.com/docker/).
Overview of all options:
[Deployment index](https://fastapi.tiangolo.com/deployment/).

The concerns, mapped:

| Concern (per official docs) | Option A: systemd | Option B: Docker |
|---|---|---|
| Run on startup | `systemd` unit, `enable` | `restart: unless-stopped` + compose `restart` policies |
| Restart after crash | `Restart=on-failure` | Docker restart policy |
| HTTPS | Caddy / **Apache** / nginx+certbot in front ([About HTTPS](https://fastapi.tiangolo.com/deployment/https/)) | same proxy, or Traefik alongside containers |
| Workers / memory | `uvicorn --workers N` ([Server Workers](https://fastapi.tiangolo.com/server-workers/)) | one container process; scale with replicas |
| Run before start | `ExecStartPre=` or wrapper script | init/entrypoint step |

## Constraints from THIS repo (apply to both options)

- **`--workers 1`.** `query_config.py` loads the bge-m3 model (~2 GB) at
  import time, and the docs warn that memory is *per process*
  ([Memory per Process](https://fastapi.tiangolo.com/deployment/concepts/#memory-per-process)):
  4 workers ≈ 8 GB RAM. Endpoints are sync `def`, so FastAPI runs them in a
  threadpool — a single process already serves concurrent requests. Raise the
  worker count only if profiling shows CPU contention *and* there is RAM to
  spare.
- **Ports:** API `28000` (`app.py` binds `127.0.0.1:28000` in `__main__`;
  `static/index.html` targets `hostname:28000`), static frontend `28080`.
  The README's 8000/8080 are stale — `stop_servers.sh` still cleans up both
  pairs.
- **Frontend:** it is fully static (two HTML files, no build step), so it needs
  no service of its own. Today `run_frontend.sh` uses `python3 -m http.server`,
  a dev server; in production serve `static/` from the reverse proxy or mount
  it with FastAPI's
  [StaticFiles](https://fastapi.tiangolo.com/tutorial/static-files/).
- **API base URL:** `static/index.html` and `static/node.html` pick it
  automatically — served from ports `28080`/`8080` (the dev static server)
  they call `hostname:28000` directly; from any other origin they use
  **same-origin**, i.e. the reverse proxy must forward `/search`, `/node` and
  `/health` to the backend (see Option A′).
- **Bind:** keep the API on `127.0.0.1` and expose it only through the proxy,
  instead of `uvicorn --host 0.0.0.0` as in the README.
- **CORS:** `allow_origins=["*"]` in `app.py` is marked dev-only. It becomes
  irrelevant (and can be tightened) once proxy and API share an origin.
- **Neo4j dependency:** the service must start after Neo4j is up (systemd:
  `After=neo4j.service`; Docker: depends_on/healthcheck, or tolerate a slow
  first request). The driver is created lazily, so a restart order glitch
  shows up as failed requests, not a crash.
- **Health check:** `GET /health` already exists — use it for the systemd
  watchdog, Docker `HEALTHCHECK`, or the proxy's upstream check.
- **Model availability:** the model loads from the local Hugging Face cache at
  startup (the app never calls `login()`; only `insert_embeddings.py` needs
  `HF_TOKEN`, and bge-m3 is public). Pre-populate the cache on the server or
  mount it as a volume so restarts don't depend on the network.
- **Config:** everything comes from `.env` (`python-dotenv`); systemd uses
  `EnvironmentFile=`, Docker uses `env_file=`.

## Option A — systemd + uvicorn (+ reverse proxy)

The classic "bare Linux VM" stack. Nothing runs in a terminal; survives
reboot and crash; logs go to `journalctl` for free. Matches the docs'
[Run a Server Manually](https://fastapi.tiangolo.com/deployment/manually/)
setup, promoted from manual to supervised.

```ini
# /etc/systemd/system/kgq.service
[Unit]
Description=KGQ search API
After=network-online.target neo4j.service
Wants=network-online.target

[Service]
WorkingDirectory=/home/palma/code/CSRCC/KGQ-local
EnvironmentFile=/home/palma/code/CSRCC/KGQ-local/.env
ExecStart=/path/to/venv/bin/uvicorn app:app --host 127.0.0.1 --port 28000 --workers 1
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now kgq
journalctl -u kgq -f          # logs
curl -s localhost:28000/health # quick check
```

Reverse proxy (choose one of the tools listed under
[HTTPS](https://fastapi.tiangolo.com/deployment/https/) — see Option A′ if the
server already runs Apache):

- **Caddy** (simplest: automatic HTTPS):

  ```
  search.example.org {
      reverse_proxy 127.0.0.1:28000
      root * /home/palma/code/CSRCC/KGQ-local/static
      file_server
  }
  ```

- **nginx + Certbot** — the traditional combination, more configuration.

**Pros:** no new tooling to learn, native integration with the existing
Neo4j systemd install (same patterns as `db_tool.sh`), low overhead.
**Cons:** machine-specific setup, not portable to another host without
repeating it.

## Option A′ — Apache + systemd (this server's setup)

Same backend as Option A, but the **already-installed Apache** plays the proxy
role — no Caddy/nginx to add. Apache does both jobs:

- serves the static frontend (`DocumentRoot` → `static/`, **no extra service**),
- reverse-proxies `/search`, `/node`, `/health` to the private uvicorn on
  `127.0.0.1:28000`, so the backend is never exposed publicly and the browser
  stays same-origin (which also makes the dev-only `allow_origins=["*"]`
  CORS in `app.py` unnecessary in production).

The backend runs as the `kgq.service` unit from Option A. The frontend JS
already handles the split: on ports `28080`/`8080` (local dev) it calls
`:28000` directly, everywhere else it uses same-origin.

Enable the proxy/SSL modules and the vhost:

```bash
sudo a2enmod proxy proxy_http ssl
sudo systemctl reload apache2
sudo certbot --apache -d search.example.org    # HTTPS + automatic renewal
```

```apache
# /etc/apache2/sites-available/kgq.conf
<VirtualHost *:443>
    ServerName search.example.org

    DocumentRoot /home/palma/code/CSRCC/KGQ-local/static
    <Directory /home/palma/code/CSRCC/KGQ-local/static>
        Require all granted
    </Directory>

    ProxyPreserveHost On
    ProxyPass        /search http://127.0.0.1:28000/search
    ProxyPassReverse /search http://127.0.0.1:28000/search
    ProxyPass        /node   http://127.0.0.1:28000/node
    ProxyPassReverse /node   http://127.0.0.1:28000/node
    ProxyPass        /health http://127.0.0.1:28000/health
    ProxyPassReverse /health http://127.0.0.1:28000/health
    # optional: ProxyPass /docs, /redoc, /openapi.json to expose the API UI
</VirtualHost>
```

**Pros:** uses what is already installed and supervised on the machine; one
origin end-to-end; backend loopback-only; certbot handles HTTPS/renewal.
**Cons:** the vhost lives outside the repo (keep it documented here); Apache
must be able to *traverse* to the repo's `static/` dir — home directories are
often `750`, so check with `namei -l /home/palma/code/CSRCC/KGQ-local/static`
and `chmod o+x` the path components if `www-data` cannot read it (same
trouble `db_tool.sh` diagnoses for Neo4j).

## Option B — Docker (+ compose)

The docs'
[FastAPI in Containers - Docker](https://fastapi.tiangolo.com/docker/)
recipe. Portability fits the "move to a stable environment" plan: same image
on dev box and server.

```dockerfile
FROM python:3.10-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 28000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:28000/health')"
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "28000", "--workers", "1"]
```

`start-period=60s` covers the model load at import time.

```yaml
# docker-compose.yml
services:
  api:
    build: .
    restart: unless-stopped
    env_file: .env            # set NEO4J_URI to the host's Neo4j, e.g. host.docker.internal:7687
    ports:
      - "127.0.0.1:28000:28000"
    volumes:
      - hf-cache:/root/.cache/huggingface   # keep the model across restarts
volumes:
  hf-cache:
```

Neo4j can stay a host/native install (point `NEO4J_URI` at it) or become its
own container — note the `db_tool.sh` dump/restore tooling assumes a **native**
systemd Neo4j, so keep that trade-off in mind.

**Pros:** identical everywhere, easy hand-off, restart policies built in.
**Cons:** image + model-cache size, another layer to debug, native-tool
assumptions (like `db_tool.sh`) don't transfer into the container.

## Decision helpers

- Deployment target is **your own Linux VM with Apache already running** →
  **Option A + A′** (least friction, no new components).
- Want the same artifact on several machines, or the target environment is
  container-first → **Option B**.
- Either way: proxy in front for HTTPS, `--workers 1`, `Enable`/`restart`
  policy on, verify `/health`, and check `embedding_status.py` on the graph
  before pointing traffic at it.

Official docs, for later:

- [Deployment index](https://fastapi.tiangolo.com/deployment/)
- [Deployments Concepts](https://fastapi.tiangolo.com/deployment/concepts/)
- [Run a Server Manually](https://fastapi.tiangolo.com/deployment/manually/)
- [Uvicorn with Workers](https://fastapi.tiangolo.com/server-workers/)
- [FastAPI in Containers - Docker](https://fastapi.tiangolo.com/docker/)
- [About HTTPS](https://fastapi.tiangolo.com/deployment/https/)
- [Deploy on Cloud Providers](https://fastapi.tiangolo.com/deployment/cloud/)
