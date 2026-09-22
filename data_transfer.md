# Data transfer: moving embeddings between machines

Two complementary procedures, depending on what you move:

| | `db_tool.sh` (whole DB) | `export/import_embeddings.py` (vectors only) |
|---|---|---|
| Moves | graph + vectors + indexes + status flags (`.dump`) | vectors only (JSON, keyed by text hash) |
| Graph itself | moved wholesale | must be re-ingested/restored separately |
| Recompute needed | none (if dumped in a healthy state) | remaining `todo` nodes only |
| Neo4j downtime | yes (service is stopped) | no |
| Use for | shipping a ready-to-serve DB | resuming interrupted embedding work across machines |

## Whole database via `db_tool.sh`

`neo4j-admin` dump/restore wrapper: every database becomes its own
`<DB>.dump` (system DB included), containing nodes, relationships,
properties, **vector embeddings and indexes/constraints** — nothing is
recomputed on the target.

Requirements: a native (systemd) Neo4j install with `neo4j-admin` on PATH;
root or passwordless `sudo` (the script elevates to the Neo4j data-directory
owner itself); a dump directory writable by that owner
(`/var/lib/neo4j/backup` is the safe default — the script prints a permission
diagnosis via `namei` if traversal is blocked).

On the **source** machine:

```bash
./db_tool.sh dump /var/lib/neo4j/backup          # stops Neo4j, dumps, restarts it
# optional: --keep-running to leave the service stopped
scp /var/lib/neo4j/backup/*.dump server:/path/to/dumps/
```

On the **target** machine (as root/sudo, Neo4j stopped by the script):

```bash
./db_tool.sh restore /path/to/dumps              # loads every *.dump, restarts Neo4j
```

Pass explicit DB names as the second argument to dump/restore only a subset;
`--no-start` leaves the service stopped after a restore.

**Dump only from a healthy state.** A dump captures whatever the graph looks
like at that moment — including missing indexes or `todo` flags left by an
interrupted `insert_embeddings.py`. So either run
`python3 embedding_status.py` first (expect ~100% `done`, and
`SHOW INDEXES` complete), or after restoring run
`python3 insert_embeddings.py` once — it is fast when only gaps/indexes are
missing (it skips `done` nodes and uses `IF NOT EXISTS` for indexes).

## Embeddings only via export / import

Use this when the graph is rebuilt or re-ingested on the target by other
means, and only the computed vectors need to travel. Export the computed
embeddings here, restore them on another machine, and finish the remaining
work there. This doubles as the sanity check that an interrupted embedding job
can be resumed from a different machine before we move to a stable
environment.

### Why the order matters

- `import_embeddings.py` re-attaches vectors and flags those fields `done`,
  so a later `insert_embeddings.py` **skips them** and only computes what is
  still missing.
- `import_embeddings.py` creates **no indexes**. The `insert_embeddings.py`
  run afterwards is what creates the vector and full-text indexes — on a
  graph where everything is already `done` it is fast (only the remaining
  texts are embedded).
- Running `insert_embeddings.py` **before** the import on a fresh graph makes
  every node `todo` and re-embeds everything (~2h wasted; the import would then
  just overwrite those vectors).

### Steps

On the **source** machine (statuses must not be reset first):

```bash
python3 export_embeddings.py export_$(date +%Y%m%d).json
```

Read-only apart from writing the JSON. The file starts with a meta record:

```json
[
  {"meta": {"model": "BAAI/bge-m3", "dims": 1024, "created": "2026-09-22T14:03:12"}},
  {"key": "<sha256 of the embedded text>", "label": "Contribution", "field": "descrEmbedding", "vector": [...]},
  ...
]
```

Transfer the file (plus however you move the graph itself) to the target.

On the **target** machine, in this exact order:

```bash
# 1. restore/re-ingest the graph (whole-DB dump: ./db_tool.sh restore <dir>,
#    otherwise your usual ingestion procedure)
# 2. re-attach the exported embeddings BEFORE doing any embedding work
python3 import_embeddings.py export_YYYYMMDD.json
# 3. compute only what is still missing + create all indexes
python3 insert_embeddings.py
# 4. verify
python3 embedding_status.py
```

`import_embeddings.py` verifies the meta record (`model` + `dims`) against the
target machine's configuration **before opening a database connection**. A file
produced by a different model or dimension count is refused, because its
vectors would be silently incompatible. Old files without a meta record are
accepted with a warning, and each vector's length is still checked.

`insert_embeddings.py` needs the embedding model (and `HF_TOKEN`); the import
alone does not — it never loads sentence-transformers.

### What is and is not in the export

- Included: every node that currently has a vector property, per
  `TEXT_BUILDERS` in `embedding_text.py`
  (Contribution description/title/subtitle, Recommendation, Gap, Project).
- Not included: status flags (they travel with the graph itself), indexes
  (recreated by `insert_embeddings.py`), and nodes with no vector yet —
  those are the ones the target machine will compute.
- Empty-text nodes (no description/content to embed) are never exportable;
  they stay flagged `todo` everywhere by design and show up in
  `embedding_status.py`.

### Verifying

```bash
python3 embedding_status.py
```

Read-only, runs in seconds. Expect ~100% `done` per field except the small
number of empty-text nodes. If anything shows `todo`, re-run
`insert_embeddings.py` (it only processes `todo`/unflagged nodes).

## Troubleshooting

- **Restore fails with a permissions error** — `neo4j-admin` runs as the Neo4j
  data-directory owner, which must be able to *traverse* every directory up to
  the dump/restore dir. Follow the `namei` diagnosis the script prints, or use
  `/var/lib/neo4j/backup`.
- **Import aborts with a model/dims mismatch** — the file was produced with a
  different model. Either export again from a machine using
  `EMBEDDING_MODEL_NAME`/`EMBEDDING_DIMS` (see `embedding_text.py`), or skip
  the import and let `insert_embeddings.py` compute everything locally.
- **A node's text changed between export and import** — the sha256 join key
  won't match, so its vector is skipped. If that node's status flag still says
  `done` (it came across in the graph dump), `insert_embeddings.py` will not
  pick it up either. After changing ingestion data, run once with
  `RESET_EMBEDDING_STATUS = True` in `insert_embeddings.py`.
- **Everything shows `todo` after import** — the import was skipped or failed;
  check its output before re-running `insert_embeddings.py`, otherwise the
  full set gets recomputed.
