"""SQLite databases inside server folders — the backend of the built-in
database editor that opens when a .db file is opened from a server's file
browser in MCPanel / MCPanel-WebUI.

Files are opened one at a time by server + path (`-id <server> -file <path in
server>`) and recognised by their file header rather than trusted by
extension. Only files inside a registered server's folder can be opened: the
WebUI passes these paths straight from the browser.

Rows are addressed by a `key` dict the `rows` command hands out with each row:
SQLite's rowid where the table has one, else the primary key.

MySQL / MariaDB (the Databases page) lives in the separate MCPanel-Databases
addon.
"""

import json
import os
import sqlite3
from contextlib import closing
from urllib.parse import quote

from . import paths, runstate
from .config import load_config
from .errors import fail

SQLITE_MAGIC = b"SQLite format 3\x00"
MAX_PAGE = 500
# JavaScript can't hold integers past 2^53 exactly; such values are sent as
# strings (SQLite's integer affinity turns them back into integers on write).
_JS_SAFE_INT = 2 ** 53


# ─── shared helpers ──────────────────────────────────────────────────────────
def _json_arg(raw, what):
    try:
        val = json.loads(raw or "{}")
    except ValueError:
        return None, fail("invalid_arguments", f"{what} must be a JSON object")
    if not isinstance(val, dict):
        return None, fail("invalid_arguments", f"{what} must be a JSON object")
    return val, None


def _page(args):
    try:
        offset = max(0, int(getattr(args, "offset", 0) or 0))
        limit = min(MAX_PAGE, max(1, int(getattr(args, "limit", 100) or 100)))
    except (TypeError, ValueError):
        return None, None, fail("invalid_arguments", "offset/limit must be numbers")
    return offset, limit, None


def _resolve_path(args):
    """Turns -id/-file into args.path. Returns an error dict or None."""
    if getattr(args, "id", None) or getattr(args, "file", None):
        if not (args.id and args.file) or getattr(args, "path", None):
            return fail("invalid_arguments", "-id and -file go together (and not with -path)")
        srv = next((s for s in load_config().get("servers", []) if s.get("id") == args.id), None)
        if not srv:
            return fail("server_not_found")
        args.path = os.path.join(srv["dir"], args.file)
    if not getattr(args, "path", None):
        return fail("invalid_arguments", "Give -id <server> -file <path> or -path <sqlite file>")
    return None


# ─── SQLite ──────────────────────────────────────────────────────────────────
def _is_sqlite_file(path):
    try:
        with open(path, "rb") as f:
            return f.read(16) == SQLITE_MAGIC
    except OSError:
        return False


def _owning_server(path, servers):
    for s in servers:
        if s.get("dir") and paths.is_within(path, s["dir"]):
            return s
    return None


def _sqlite_open(path):
    """Opens `path` only if it is a real SQLite file inside a registered
    server's folder — the WebUI passes this path straight from the browser, so
    it must never reach an arbitrary file on the host."""
    real = os.path.realpath(os.path.expanduser(path or ""))
    if not _owning_server(real, load_config().get("servers", [])):
        return None, fail("invalid_path", "Only databases inside a server's folder can be opened")
    if not os.path.isfile(real) or not _is_sqlite_file(real):
        return None, fail("not_sqlite", f"Not an SQLite database: {os.path.basename(real)}")
    # mode=rw: never create a new file if it vanished in the meantime.
    conn = sqlite3.connect(f"file:{quote(real)}?mode=rw", uri=True, timeout=5)
    return conn, None


def _q(name):
    return '"' + str(name).replace('"', '""') + '"'


def _sqlite_value(v):
    if isinstance(v, bytes):
        return {"$blob": len(v)}
    if isinstance(v, int) and abs(v) > _JS_SAFE_INT:
        return str(v)
    return v


def _sqlite_tables(conn):
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' "
                        "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
    out = []
    for (name,) in rows:
        try:
            count = conn.execute(f"SELECT COUNT(*) FROM {_q(name)}").fetchone()[0]
        except sqlite3.Error:
            count = None
        out.append({"name": name, "rows": count})
    return out


def _sqlite_table_info(conn, table):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
        return None, None, fail("table_not_found", f"Table not found: {table}")
    cols = [{"name": r[1], "type": r[2] or "", "notnull": bool(r[3]),
             "default": r[4], "pk": bool(r[5])}
            for r in conn.execute(f"PRAGMA table_info({_q(table)})")]
    try:
        conn.execute(f"SELECT _rowid_ FROM {_q(table)} LIMIT 0")
        key_mode = "rowid"
    except sqlite3.OperationalError:  # WITHOUT ROWID table: it always has a PK
        key_mode = "pk"
    return cols, key_mode, None


def _sqlite_where(key, cols, key_mode):
    if key_mode == "rowid":
        if "_rowid_" not in key:
            return None, None
        return "_rowid_ IS ?", [key["_rowid_"]]
    names = [c["name"] for c in cols if c["pk"]]
    if not names or any(n not in key for n in names):
        return None, None
    return " AND ".join(f"{_q(n)} IS ?" for n in names), [key[n] for n in names]


def _sqlite_rows(args):
    offset, limit, err = _page(args)
    if err:
        return err
    conn, err = _sqlite_open(args.path)
    if err:
        return err
    with closing(conn):
        cols, key_mode, err = _sqlite_table_info(conn, args.table)
        if err:
            return err
        names = [c["name"] for c in cols]
        sel = ", ".join(_q(n) for n in names)
        if key_mode == "rowid":
            sql = f"SELECT _rowid_, {sel} FROM {_q(args.table)} ORDER BY _rowid_ LIMIT ? OFFSET ?"
        else:
            order = ", ".join(_q(c["name"]) for c in cols if c["pk"])
            sql = f"SELECT {sel} FROM {_q(args.table)} ORDER BY {order} LIMIT ? OFFSET ?"
        rows = []
        for r in conn.execute(sql, (limit, offset)):
            if key_mode == "rowid":
                key, vals = {"_rowid_": _sqlite_value(r[0])}, r[1:]
            else:
                vals = r
                key = {c["name"]: _sqlite_value(v) for c, v in zip(cols, vals) if c["pk"]}
            rows.append({"key": key, "values": [_sqlite_value(v) for v in vals]})
        total = conn.execute(f"SELECT COUNT(*) FROM {_q(args.table)}").fetchone()[0]
    return {"engine": "sqlite", "table": args.table, "columns": cols, "keyMode": key_mode,
            "rows": rows, "total": total, "offset": offset, "limit": limit}


def _sqlite_write(args, op):
    conn, err = _sqlite_open(args.path)
    if err:
        return err
    try:
        with conn:  # commits on success, rolls back on any exception
            cols, key_mode, err = _sqlite_table_info(conn, args.table)
            if err:
                return err
            names = {c["name"] for c in cols}
            if op == "insert":
                values, err = _json_arg(args.values, "-values")
                if err:
                    return err
                unknown = [k for k in values if k not in names]
                if unknown:
                    return fail("invalid_arguments", f"Unknown column(s): {', '.join(unknown)}")
                if values:
                    sql = (f"INSERT INTO {_q(args.table)} ({', '.join(_q(k) for k in values)}) "
                           f"VALUES ({', '.join('?' for _ in values)})")
                    cur = conn.execute(sql, list(values.values()))
                else:
                    cur = conn.execute(f"INSERT INTO {_q(args.table)} DEFAULT VALUES")
                return {"success": True, "changed": cur.rowcount}

            key, err = _json_arg(args.key, "-key")
            if err:
                return err
            where, params = _sqlite_where(key, cols, key_mode)
            if not where:
                return fail("invalid_arguments", "-key doesn't identify a row of this table")
            if op == "update":
                changes, err = _json_arg(args.set, "-set")
                if err:
                    return err
                unknown = [k for k in changes if k not in names]
                if unknown or not changes:
                    return fail("invalid_arguments",
                                f"Unknown column(s): {', '.join(unknown)}" if unknown else "-set is empty")
                sets = ", ".join(f"{_q(k)} = ?" for k in changes)
                cur = conn.execute(f"UPDATE {_q(args.table)} SET {sets} WHERE {where}",
                                   list(changes.values()) + params)
            else:
                cur = conn.execute(f"DELETE FROM {_q(args.table)} WHERE {where}", params)
            if cur.rowcount == 0:
                return fail("row_not_found")
            return {"success": True, "changed": cur.rowcount}
    except sqlite3.Error as e:
        return fail("db_error", f"SQLite: {e}")
    finally:
        conn.close()


# ─── commands ────────────────────────────────────────────────────────────────
def _run(args, fn):
    err = _resolve_path(args)
    if err:
        return err
    try:
        return fn()
    except sqlite3.Error as e:
        return fail("db_error", f"SQLite: {e}")


def db_tables(args, progress=None):
    def run():
        conn, err = _sqlite_open(args.path)
        if err:
            return err
        real = os.path.realpath(args.path)
        srv = _owning_server(real, load_config().get("servers", []))
        with closing(conn):
            return {"engine": "sqlite", "path": real, "tables": _sqlite_tables(conn),
                    "serverId": srv["id"], "serverName": srv.get("name"),
                    "running": runstate.is_running(srv["id"])}
    return _run(args, run)


def db_rows(args, progress=None):
    return _run(args, lambda: _sqlite_rows(args))


def db_update(args, progress=None):
    return _run(args, lambda: _sqlite_write(args, "update"))


def db_insert(args, progress=None):
    return _run(args, lambda: _sqlite_write(args, "insert"))


def db_delete(args, progress=None):
    return _run(args, lambda: _sqlite_write(args, "delete"))


def add_parser(sub, leaf):
    """Registers `db …` on the `api` parser tree (cli.add_commands calls this)."""
    db = sub.add_parser("db", help="(for the built-in SQLite editor) SQLite files in server folders")
    dbsub = db.add_subparsers(dest="noun", metavar="<tables|rows|update|insert|delete>", required=True)

    def target(p):
        p.add_argument("-id", "--id", dest="id", default=None, metavar="<server>",
                       help="with -file: an SQLite file given relative to this server's folder")
        p.add_argument("-file", "--file", dest="file", default=None, metavar="<path>")
        p.add_argument("-path", "--path", dest="path", default=None, metavar="<file>",
                       help="an SQLite file inside a server's folder, by full path")

    def table(p):
        target(p)
        p.add_argument("-table", "--table", dest="table", required=True, metavar="<table>")

    p = leaf(dbsub, "tables", db_tables, "db-tables", help="list a database's tables")
    target(p)
    p = leaf(dbsub, "rows", db_rows, "db-rows", help="one page of a table's rows")
    table(p)
    p.add_argument("-offset", "--offset", dest="offset", type=int, default=0)
    p.add_argument("-limit", "--limit", dest="limit", type=int, default=100, help=f"max {MAX_PAGE}")
    p = leaf(dbsub, "update", db_update, "db-write", help="change cells of one row")
    table(p)
    p.add_argument("-key", "--key", dest="key", required=True, metavar="<json>",
                   help="the row's key object, as returned by `db rows`")
    p.add_argument("-set", "--set", dest="set", required=True, metavar="<json>", help='e.g. {"balance": "10"}')
    p = leaf(dbsub, "insert", db_insert, "db-write", help="add a row (omitted columns get their default)")
    table(p)
    p.add_argument("-values", "--values", dest="values", default="{}", metavar="<json>")
    p = leaf(dbsub, "delete", db_delete, "db-write", help="delete one row")
    table(p)
    p.add_argument("-key", "--key", dest="key", required=True, metavar="<json>")
