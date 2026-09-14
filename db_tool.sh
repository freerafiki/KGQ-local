#!/usr/bin/env bash
# Full Neo4j database dump & restore (system + all user databases).
#
# Use cases:
#   - Move a ready-to-ship database from the dev machine to a server:
#       ./db_tool.sh dump   /path/to/outdir
#       scp /path/to/outdir/*.dump server:/path/to/outdir/
#       # on the server (as a user with sudo, neo4j service stopped by script):
#       ./db_tool.sh restore /path/to/outdir
#   - Take a nightly local snapshot:
#       ./db_tool.sh dump /backup/neo4j
#
# Each database becomes its own <DB>.dump file, produced by
# `neo4j-admin database dump`. The files contain the ENTIRE database (nodes,
# relationships, properties, vector embeddings AND indexes/constraints), so
# nothing needs to be recomputed on the target machine. Both the `system`
# database (users/roles) and every user database are dumped by default.
#
# neo4j-admin must run as the owner of the Neo4j data directory (normally
# `neo4j`). The script elevates automatically:
#   - runs directly if the current user IS the owner,
#   - `runuser` if running as root,
#   - `sudo -u <owner>` otherwise (a password prompt may appear).
#
# The <dir> you pass MUST be writable by that owner (e.g. /var/lib/neo4j/backup
# or a directory you chown) and readable on the restore side.
#
# Reads NEO4J_GRAPH from .env only for the default DB; dots must be installed
# as a native (systemd) Neo4j with `neo4j-admin` on PATH.
#
# Usage:
#   ./db_tool.sh dump    <to-dir>   [DB_NAME...] [--keep-running]
#   ./db_tool.sh restore <from-dir> [DB_NAME...] [--no-start]
#
# With no DB_NAME, dump handles every database found in the data directory
# (plus `system`); restore loads every *.dump found in <from-dir>.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---- locate neo4j bits ----------------------------------------------------
find_datadir() {
    local conf=/etc/neo4j/neo4j.conf
    if [[ -f "$conf" ]]; then
        local d
        d="$(grep -E '^server\.directories\.data=' "$conf" | tail -1 | cut -d= -f2-)"
        [[ -n "$d" ]] && echo "$d" && return
    fi
    echo "/var/lib/neo4j/data"
}

DATA_DIR="$(find_datadir)"            # e.g. /var/lib/neo4j/data
DATABASES_DIR="$DATA_DIR/databases"
DATA_OWNER="$(stat -c '%U' "$DATA_DIR" 2>/dev/null || echo neo4j)"

db_exists() { [[ -d "$DATABASES_DIR/$1" ]]; }

# Databases to handle: `system` first, then every user DB present.
# Outputs newline-separated names so mapfile -t collects them correctly.
list_db_names() {
    echo "system"
    for d in "$DATABASES_DIR"/*/; do
        [[ -d "$d" ]] || continue
        local name; name="$(basename "$d")"
        [[ "$name" == system ]] && continue
        echo "$name"
    done
}

run_as_owner() {
    # Execute a command as the Neo4j data directory owner.
    if [[ "$(id -un)" == "$DATA_OWNER" ]]; then
        "$@"
    elif [[ "$(id -u)" -eq 0 ]]; then
        runuser -u "$DATA_OWNER" -- "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo -u "$DATA_OWNER" -- "$@"
    else
        echo "ERROR: must run as '$DATA_OWNER' or root, but no sudo is available" >&2
        return 1
    fi
}

print_access_hint() {
    # `data/db_dumps` can be chowned to neo4j and still be unreachable: the
    # neo4j-admin user must be able to TRAVERSE every directory up to it.
    # Show the permission chain so the blocking mount point is obvious.
    local path="$1"
    echo "  Diagnosis (permissions along the path):"
    if command -v namei >/dev/null 2>&1; then
        namei -l "$path" | sed 's/^/    /' >&2
    else
        ls -ld "$(dirname "$path")" 2>/dev/null | sed 's/^/    dir of target: /' >&2
    fi
    echo >&2
    echo "  Any directory above the dump dir that is NOT traversable by 'others'" >&2
    echo "  (e.g. a home dir with drwxr-x---) blocks the dump even after chown." >&2
    echo "  Fix, e.g.:" >&2
    echo "    sudo chmod o+x /home/palma             # allow traversal into your home" >&2
    echo "    sudo chown $DATA_OWNER:adm \"$path\"    # allow writing the dump dir itself" >&2
    echo "  Or simply use: /var/lib/neo4j/backup  (already owned by neo4j)" >&2
}

dir_writable_by_owner() {
    local path="$1"
    run_as_owner test -w "$path"
}

dir_readable_by_owner() {
    local path="$1"
    run_as_owner test -r "$path"
}

stop_service() {
    if systemctl is-active --quiet neo4j 2>/dev/null; then
        echo "[tool] stopping neo4j service"
        systemctl stop neo4j
    else
        echo "[tool] neo4j service already stopped"
    fi
}

start_service() {
    echo "[tool] starting neo4j service"
    systemctl start neo4j
    for _ in $(seq 1 30); do
        if ss -tln 2>/dev/null | grep -q ':7687 '; then
            echo "[tool] neo4j is up on :7687"
            return
        fi
        sleep 2
    done
    echo "[tool] WARNING: neo4j Bolt port not detected, service may still be booting" >&2
}

verify_admin() {
    if ! command -v neo4j-admin >/dev/null 2>&1; then
        echo "ERROR: neo4j-admin not found on PATH" >&2
        exit 1
    fi
    if [[ "$(id -un)" != "$DATA_OWNER" ]] && [[ "$(id -u)" -ne 0 ]] && ! command -v sudo >/dev/null 2>&1; then
        echo "ERROR: cannot elevate to '$DATA_OWNER' (no root, no sudo)" >&2
        exit 1
    fi
}

cmd_dump() {
    local to_dir="$1"; shift
    local keep_running=""
    local dbs=()
    for a in "$@"; do
        [[ "$a" == "--keep-running" ]] && keep_running=1 && continue
        dbs+=("$a")
    done

    # Canonicalize to an absolute path so palma, sudo and neo4j-admin all
    # resolve it the same way, then make sure it exists (neo4j-admin does NOT
    # create the target directory).
    to_dir="$(realpath -m -- "$to_dir")"
    if [[ ! -d "$to_dir" ]]; then
        echo "[tool] creating dump directory: $to_dir"
        mkdir -p "$to_dir"
    fi

    if [[ ${#dbs[@]} -eq 0 ]]; then
        mapfile -t dbs < <(list_db_names)
    fi
    for db in "${dbs[@]}"; do
        if ! db_exists "$db" && [[ "$db" != system ]]; then
            echo "ERROR: database '$db' not found under $DATABASES_DIR" >&2
            exit 1
        fi
    done
    if ! dir_writable_by_owner "$to_dir"; then
        echo "ERROR: '$to_dir' is not writable by '$DATA_OWNER' (the neo4j-admin user)." >&2
        print_access_hint "$to_dir"
        exit 1
    fi

    verify_admin
    stop_service
    trap start_service EXIT

    for db in "${dbs[@]}"; do
        echo "[tool] dumping '$db' -> $to_dir/$db.dump"
        run_as_owner neo4j-admin database dump "$db" \
            --to-path="$to_dir" --overwrite-destination
    done

    trap - EXIT
    if [[ -z "$keep_running" ]]; then
        start_service
        echo "[tool] dump complete."
    else
        echo "[tool] dump complete (service left stopped)."
    fi
    for db in "${dbs[@]}"; do
        ls -lh "$to_dir/$db.dump"
    done
    echo "[tool] Move $to_dir/*.dump to the target machine."
}

cmd_restore() {
    local from_dir="$1"; shift
    local no_start=""
    local dbs=()
    for a in "$@"; do
        [[ "$a" == "--no-start" ]] && no_start=1 && continue
        dbs+=("$a")
    done
    # Canonicalize to an absolute path; the source dir must already exist.
    from_dir="$(realpath -m -- "$from_dir")"
    [[ -d "$from_dir" ]] || { echo "ERROR: from-dir does not exist: $from_dir" >&2; exit 1; }

    if [[ ${#dbs[@]} -eq 0 ]]; then
        local found=()
        for f in "$from_dir"/*.dump; do
            [[ -f "$f" ]] && found+=("$(basename "$f" .dump)")
        done
        mapfile -t dbs < <(printf '%s\n' "${found[@]}")
    fi
    [[ ${#dbs[@]} -eq 0 ]] && { echo "ERROR: no *.dump files in $from_dir" >&2; exit 1; }
    for db in "${dbs[@]}"; do
        [[ -f "$from_dir/$db.dump" ]] || { echo "ERROR: no dump file: $from_dir/$db.dump" >&2; exit 1; }
    done
    if ! dir_readable_by_owner "$from_dir"; then
        echo "ERROR: '$from_dir' is not readable by '$DATA_OWNER' (the neo4j-admin user)." >&2
        print_access_hint "$from_dir"
        exit 1
    fi

    verify_admin
    stop_service
    [[ -z "$no_start" ]] && trap start_service EXIT

    # system first (users/roles), then the user databases.
    for db in "${dbs[@]}"; do
        echo "[tool] loading '$db' from $from_dir/$db.dump"
        run_as_owner neo4j-admin database load "$db" \
            --from-path="$from_dir" --overwrite-destination
    done

    trap - EXIT
    if [[ -z "$no_start" ]]; then
        start_service
        echo "[tool] restore complete."
    else
        echo "[tool] restore complete (service left stopped)."
    fi
}

# ---- main -----------------------------------------------------------------
cmd="${1:-}"
shift || true
case "$cmd" in
    dump)
        [[ $# -ge 1 ]] || { echo "usage: $0 dump <to-dir> [DB_NAME...] [--keep-running]" >&2; exit 1; }
        cmd_dump "$@"
        ;;
    restore)
        [[ $# -ge 1 ]] || { echo "usage: $0 restore <from-dir> [DB_NAME...] [--no-start]" >&2; exit 1; }
        cmd_restore "$@"
        ;;
    *)
        echo "usage: $0 {dump|restore} <dir> [DB_NAME...] [--keep-running|--no-start]" >&2
        exit 1
        ;;
esac