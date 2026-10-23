"""Records of the plugins / mods MCPanel installed, kept next to the jars.

Every plugin or mod installed through `mcpanel install plugin|mod` (and so
through the desktop app and the WebUI, which call it) gets one small TOML file
in a hidden `.list` folder inside the server's `plugins/` or `mods/` folder:

    <server>/plugins/.list/modrinth-luckperms.toml

    filename = "LuckPerms-Bukkit-5.4.141.jar"
    platform = "modrinth"
    id = "luckperms"
    name = "LuckPerms"
    version = "v5.4.141-bukkit"
    version_id = "OrIs0S6b"
    dependencies = []

`id` is what the platform addresses the project by: the Modrinth slug, the
Hangar slug (plus `owner`), or the SpigotMC resource id. Living inside the
server folder means the records travel with it (backups, profile snapshots,
copying the folder by hand). The file managers hide the folder.

Python 3.8 has no tomllib, so the files are written (and, as a fallback, read)
by the tiny flat-TOML helpers below — only strings, booleans and string arrays
are ever stored.
"""

import datetime
import os
import re

LIST_DIR = ".list"

_KEY_ORDER = ("filename", "platform", "id", "owner", "name", "author", "version",
              "version_id", "project_id", "dependencies", "installed_at", "updated_at")


def list_dir(plugin_dir):
    return os.path.join(plugin_dir, LIST_DIR)


def _record_name(platform, pid):
    safe = re.sub(r"[^a-z0-9._-]+", "_", str(pid).lower()).strip("._") or "plugin"
    return f"{platform}-{safe}.toml"


def record_path(plugin_dir, platform, pid):
    return os.path.join(list_dir(plugin_dir), _record_name(platform, pid))


# ─── flat TOML ────────────────────────────────────────────────────────────────

def _q(s):
    s = str(s).replace("\\", "\\\\").replace('"', '\\"')
    return '"' + s.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t") + '"'


def _dump(rec):
    lines = ["# Managed by MCPanel - where this plugin/mod came from."]
    keys = [k for k in _KEY_ORDER if k in rec] + sorted(k for k in rec if k not in _KEY_ORDER)
    for k in keys:
        v = rec[k]
        if v is None:
            continue
        if isinstance(v, bool):
            lines.append(f"{k} = {'true' if v else 'false'}")
        elif isinstance(v, (list, tuple)):
            lines.append(f"{k} = [{', '.join(_q(x) for x in v)}]")
        else:
            lines.append(f"{k} = {_q(v)}")
    return "\n".join(lines) + "\n"


_STR_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')
_ESC = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\"}


def _unq(s):
    return re.sub(r"\\(.)", lambda m: _ESC.get(m.group(1), m.group(1)), s)


def _parse(text):
    try:
        import tomllib  # 3.11+
        return tomllib.loads(text)
    except ImportError:
        pass
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = (p.strip() for p in line.split("=", 1))
        if val.startswith("["):
            out[key] = [_unq(m) for m in _STR_RE.findall(val)]
        elif val in ("true", "false"):
            out[key] = val == "true"
        else:
            m = _STR_RE.match(val)
            out[key] = _unq(m.group(1)) if m else val
    return out


# ─── records ──────────────────────────────────────────────────────────────────

def _now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def write(plugin_dir, rec):
    """Create or replace the record for rec["platform"] + rec["id"]."""
    os.makedirs(list_dir(plugin_dir), exist_ok=True)
    path = record_path(plugin_dir, rec["platform"], rec["id"])
    rec = dict(rec)
    old = read_file(path)
    if old:
        rec.setdefault("installed_at", old.get("installed_at"))
        rec["updated_at"] = _now()
    else:
        rec.setdefault("installed_at", _now())
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(_dump(rec))
    os.replace(tmp, path)
    return path


def read_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            rec = _parse(f.read())
    except (OSError, ValueError):
        return None
    if not isinstance(rec, dict) or not rec.get("platform") or not rec.get("id"):
        return None
    deps = rec.get("dependencies")
    rec["dependencies"] = [str(d) for d in deps] if isinstance(deps, list) else []
    return rec


def load(plugin_dir):
    """Every record whose jar is still there. Records for jars removed some
    other way (the file manager, by hand) are deleted, so nothing claims a
    plugin that's gone."""
    d = list_dir(plugin_dir)
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return []
    out = []
    for n in names:
        if not n.endswith(".toml"):
            continue
        path = os.path.join(d, n)
        rec = read_file(path)
        if rec is None:
            continue
        if not rec.get("filename") or not os.path.isfile(os.path.join(plugin_dir, rec["filename"])):
            try:
                os.remove(path)
            except OSError:
                pass
            continue
        out.append(rec)
    return out


def find(records, platform, pid):
    """The record for a project, matched case-insensitively on id — or, for
    Modrinth, on the project id too (dependencies are given by project id)."""
    pid = str(pid).lower()
    for r in records:
        if r.get("platform") != platform:
            continue
        if str(r.get("id", "")).lower() == pid or str(r.get("project_id", "")).lower() == pid:
            return r
    return None


def remove(plugin_dir, rec):
    try:
        os.remove(record_path(plugin_dir, rec["platform"], rec["id"]))
    except OSError:
        pass
