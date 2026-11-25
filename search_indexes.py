"""Full-text indexes the search API queries BY NAME (see
query_util.EXPECTED_FULLTEXT_INDEXES) — one definition, used by both
scripts/create_search_indexes.py and scripts/insert_embeddings.py.

Why this lives on the KGQ side: the graph snapshot tool (KGB/graph_io.py)
deliberately exports only range/text/point indexes, so after every
`graph_io.py import` these have to be (re)created here. That needs no
embedding model, only the database — run:

    python3 scripts/create_search_indexes.py

`CREATE ... IF NOT EXISTS` alone would keep an index whose DEFINITION is
outdated (e.g. the old `authors_fulltext` on ContributionActor), so an
existing index is compared with the expected labels/properties and rebuilt
when they differ.

No heavy imports here (no sentence-transformers): safe to use anywhere.
"""

# name -> (labels, properties, OPTIONS clause or '')
FULLTEXT_INDEXES = {
    # nl + keywords modes (query_util._fulltext_fragment)
    "search_fulltext": (
        ("Contribution", "Recommendation", "Gap", "Project"),
        ("officialTitle", "subtitle", "description", "findings", "content",
         "motivation", "name"),
        "",
    ),
    # title mode (query_util._title_fragment)
    "title_fulltext": (
        ("Contribution", "Recommendation", "Gap", "Project"),
        ("title", "officialTitle", "name", "subtitle"),
        "",
    ),
    # author mode + the author part of nl (query_util._authors_fragment).
    # People are Author nodes, institutions Institution nodes; the legacy
    # ContributionActor label is kept so older graphs still work.
    "authors_fulltext": (
        ("Author", "Institution", "ContributionActor"),
        ("name",),
        "OPTIONS { indexConfig: { `fulltext.analyzer`: 'standard-no-stop-words', "
        "`fulltext.eventually_consistent`: false } }",
    ),
}


def create_statement(name):
    labels, props, options = FULLTEXT_INDEXES[name]
    return (f"CREATE FULLTEXT INDEX {name} IF NOT EXISTS "
            f"FOR (n:{'|'.join(labels)}) "
            f"ON EACH [{', '.join('n.' + p for p in props)}] {options}").strip()


def ensure_fulltext_indexes(driver, database, dry_run=False, log=print):
    """Create missing full-text indexes and rebuild outdated ones.

    Returns {name: 'ok' | 'created' | 'rebuilt'} (with dry_run, what WOULD
    happen). Population runs in the background inside Neo4j; the indexes
    answer queries once their state is ONLINE (seconds at this graph size).
    """
    rows, _, _ = driver.execute_query(
        "SHOW FULLTEXT INDEXES YIELD name, labelsOrTypes, properties "
        "RETURN name, labelsOrTypes, properties", database_=database)
    existing = {r["name"]: (set(r["labelsOrTypes"]), set(r["properties"]))
                for r in rows}
    result = {}
    for name, (labels, props, _opts) in FULLTEXT_INDEXES.items():
        if name not in existing:
            action = "created"
        elif existing[name] != (set(labels), set(props)):
            action = "rebuilt"
        else:
            result[name] = "ok"
            log(f"  {name}: up to date")
            continue
        log(f"  {name}: {'would be ' if dry_run else ''}{action}"
            + (f" (was on {sorted(existing[name][0])})" if action == "rebuilt" else ""))
        if not dry_run:
            if action == "rebuilt":
                driver.execute_query(f"DROP INDEX {name} IF EXISTS", database_=database)
            driver.execute_query(create_statement(name), database_=database)
        result[name] = action
    return result
