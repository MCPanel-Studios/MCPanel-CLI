"""Server controllers — 1:1 ports of the server-related IPC handlers in
main.js. Each returns a plain dict in the same shape the handler returned, so
the `api` command family can emit it verbatim as JSON.

Signatures take the parsed argparse namespace plus an optional `progress`
callback `progress(percent, status)` used only for human-mode feedback.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time

from . import paths, runstate, buildtools
from .config import load_config, locked_config, find_server, write_server_manifest
from .versions import resolve_download_url, SOFTWARE
from .http import download_file
from .ping import ping_server
from . import util
from .errors import fail

# Parent directory of the mcpanel package — used to set PYTHONPATH when
# spawning the supervisor subprocess so it can import mcpanel regardless of cwd.
_PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _now_ms():
    return int(time.time() * 1000)


def _unique_id(prefix, base_dir):
    sid = f"{prefix}_{_now_ms()}"
    while os.path.exists(os.path.join(base_dir, sid)):
        time.sleep(0.002)
        sid = f"{prefix}_{_now_ms()}"
    return sid


def _write_port(props_file, port):
    props = ""
    if os.path.exists(props_file):
        with open(props_file, "r", encoding="utf-8") as f:
            props = f.read()
    line = f"server-port={port}"
    if re.search(r"^server-port=.*$", props, re.M):
        props = re.sub(r"^server-port=.*$", lambda _m: line, props, count=1, flags=re.M)
    else:
        props = (props.rstrip() + "\n" + line + "\n") if props else line + "\n"
    # Query shares the game port by default (create_server writes it that
    # way); leaving it behind would make the new port clash with the old one.
    props = re.sub(r"^query\.port=.*$", lambda _m: f"query.port={port}", props, count=1, flags=re.M)
    util.atomic_write_text(props_file, props)


def _write_velocity_port(toml_file, port):
    """Update bind = "host:port" in velocity.toml for the given port."""
    content = ""
    if os.path.exists(toml_file):
        with open(toml_file, "r", encoding="utf-8") as f:
            content = f.read()
    new_bind = f'bind = "0.0.0.0:{port}"'
    if re.search(r'^bind\s*=\s*"[^"]*"', content, re.M):
        content = re.sub(r'^bind\s*=\s*"[^"]*"', lambda _m: new_bind, content, count=1, flags=re.M)
    else:
        content = new_bind + "\n" + content if content else new_bind + "\n"
    util.atomic_write_text(toml_file, content)


def _invalid_id(value):
    return None if paths.is_valid_id(value) else fail("invalid_id", f"Invalid id: {value!r}")


def _validate_settings(port=None, ram=None, software=None, heap=None, ram_for_heap=None):
    """Shared by create/update/import. Returns an error dict or None — catching
    these here beats writing them into server.properties / the java command
    line and finding out when the server refuses to boot.

    `heap` "" means "clear it" (automatic heap); `ram_for_heap` is the RAM
    limit to check it against when `ram` itself isn't being changed."""
    if port is not None and not util.is_valid_port(port):
        return {"error": f"Invalid port {port!r} - must be 1-65535", "code": "invalid_port"}
    if ram is not None and not util.is_valid_ram(util.normalize_ram(ram)):
        return {"error": f"Invalid RAM {ram!r} - use megabytes (2048) or e.g. 4G", "code": "invalid_ram"}
    if heap:
        norm = util.normalize_ram(heap)
        if not util.is_valid_ram(norm):
            return {"error": f"Invalid heap limit {heap!r} - use megabytes (2048) or e.g. 4G", "code": "invalid_ram"}
        limit = util.ram_mb(util.normalize_ram(ram if ram is not None else ram_for_heap))
        if limit is not None and util.ram_mb(norm) > limit:
            return {"error": f"The heap limit ({norm}) can't be larger than the RAM limit - the heap is part of it",
                    "code": "invalid_ram"}
    if software is not None and software not in SOFTWARE:
        return {"error": f"Unknown software '{software}'. One of: {', '.join(SOFTWARE)}",
                "code": "invalid_software"}
    return None


def _remove_server_dir(server_dir):
    """rmtree, but only ever inside SERVERS_DIR. config.json is hand-editable,
    and a "dir" pointing at $HOME must never be deleted with a server."""
    if (os.path.isdir(server_dir) and paths.is_within(server_dir, paths.SERVERS_DIR)
            and os.path.realpath(server_dir) != os.path.realpath(paths.SERVERS_DIR)):
        shutil.rmtree(server_dir, ignore_errors=True)
        return True
    return False


# Characters no filesystem MCPanel runs on accepts in a folder name.
_BAD_FOLDER_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _folder_name(name, fallback):
    """A server's display name as a folder name — it's what the user will be
    looking for when browsing that drive later, unlike an srv_<ms> id."""
    clean = _BAD_FOLDER_CHARS.sub("_", name or "").strip().strip(".")
    return clean[:100] or fallback


def _new_server_dir(dest, name, sid):
    """Pick (but don't create) the folder for a new server.

    No `dest` — or MCPanel's own servers dir — keeps the default servers/<id>.
    Anything else is a PARENT folder (another drive, a mount, …) and the server
    gets its own subfolder in it, named after the server.

    Returns (server_dir, external, error). An external folder is registered
    as `linked`: it lives outside servers/, which is what delete/remove and
    discovery key on."""
    if not (dest or "").strip():
        return os.path.join(paths.SERVERS_DIR, sid), False, None
    parent = os.path.abspath(os.path.expanduser(dest.strip()))
    if os.path.realpath(parent) == os.path.realpath(paths.SERVERS_DIR):
        return os.path.join(paths.SERVERS_DIR, sid), False, None
    if not os.path.isdir(parent):
        return None, False, fail("invalid_path", f"Destination is not a folder: {dest}")
    if not os.access(parent, os.W_OK | os.X_OK):
        return None, False, fail("invalid_path", f"MCPanel can't write to {parent}")
    if paths.is_within(parent, paths.USER_DATA):
        return None, False, fail("invalid_path",
                                 "Pick a destination outside MCPanel's data directory "
                                 "(or leave it empty for the default location)")
    for s in load_config().get("servers", []):
        if s.get("dir") and paths.is_within(parent, s["dir"]):
            return None, False, fail("invalid_path",
                                     f"Destination is inside the folder of server \"{s.get('name')}\" ({s['id']})")
    base = _folder_name(name, sid)
    server_dir = os.path.join(parent, base)
    n = 2
    while os.path.lexists(server_dir):
        server_dir = os.path.join(parent, f"{base}-{n}")
        n += 1
    return server_dir, True, None


def _discard_new_dir(server_dir, external):
    """Undo a create/duplicate that failed half-way. An external folder was
    created by this very call (_new_server_dir never reuses an existing one),
    so it is safe to remove even though it's outside servers/."""
    if not server_dir:
        return
    if external:
        shutil.rmtree(server_dir, ignore_errors=True)
    else:
        _remove_server_dir(server_dir)


def _install_jar(software, version, server_dir, progress=None, java_path="java", unstable=False):
    """Download (or, for Spigot, build) `software` `version` into
    <server_dir>/server.jar. Returns an error dict or None."""
    if software == "spigot":
        return buildtools.build_spigot(version, server_dir, progress, java_path=java_path)
    if progress:
        progress(0, "Resolving download URL...")
    url = resolve_download_url(software, version, unstable)
    if progress:
        progress(0, "Downloading server jar...")
    download_file(url, os.path.join(server_dir, "server.jar"),
                  (lambda p: progress(p, f"Downloading... {p}%")) if progress else None)
    return None


# ─── listing / fetching ─────────────────────────────────────────────────────
def get_config(args, progress=None):
    return load_config()


def list_servers(args, progress=None):
    cfg = load_config()
    servers = []
    for s in cfg.get("servers", []):
        item = dict(s)
        item["running"] = runstate.is_running(s["id"])
        servers.append(item)
    return {"servers": servers}


def fetch_server(args, progress=None):
    cfg = load_config()
    srv = find_server(cfg, args.id)
    if not srv:
        return fail("server_not_found")
    out = dict(srv)
    out["running"] = runstate.is_running(srv["id"])
    return out


# ─── create / delete / update ───────────────────────────────────────────────
def create_server(args, progress=None):
    server_dir = None
    external = False
    try:
        software = args.software
        err = _validate_settings(port=args.port, ram=args.ram, software=software, heap=getattr(args, "heap", None))
        if err:
            return err
        if not args.version:
            return fail("version_required", "Version is required (-v)")
        if not (args.name or "").strip():
            return fail("invalid_name", "Name is required (-t)")
        if args.profile:
            err = _invalid_id(args.profile)
            if err:
                return err
            if not os.path.isdir(os.path.join(paths.PROFILES_DIR, args.profile)):
                return fail("profile_not_found", f"Profile not found: {args.profile}")

        paths.ensure_dirs()
        sid = _unique_id("srv", paths.SERVERS_DIR)
        target, external, err = _new_server_dir(getattr(args, "dest", None), args.name, sid)
        if err:
            return err
        os.makedirs(target)
        server_dir = target

        server = {
            "id": sid,
            "name": args.name,
            "port": int(args.port),
            "ram": util.normalize_ram(args.ram),
            "heap": util.normalize_ram(args.heap) if getattr(args, "heap", None) else None,
            "storageLimit": args.storage or None,
            "software": software,
            "version": args.version,
            "profileId": args.profile or None,
            "javaPath": args.java or "java",
            "javaArgs": args.jargs or util.default_java_args(),
            "created": _now_ms(),
            "dir": server_dir,
        }
        if external:
            server["linked"] = True

        if args.profile:
            profile_dir = os.path.join(paths.PROFILES_DIR, args.profile)
            if os.path.exists(profile_dir):
                util.copy_dir(profile_dir, server_dir)

        if software == "velocity":
            _write_velocity_port(os.path.join(server_dir, "velocity.toml"), server["port"])
        else:
            props_file = os.path.join(server_dir, "server.properties")
            if not os.path.exists(props_file):
                with open(props_file, "w", encoding="utf-8") as f:
                    f.write(f"server-port={server['port']}\nquery.port={server['port']}\n")
            else:
                _write_port(props_file, server["port"])

        if getattr(args, "accept_eula", False):
            with open(os.path.join(server_dir, "eula.txt"), "w", encoding="utf-8") as f:
                f.write("eula=true\n")

        err = _install_jar(software, args.version, server_dir, progress,
                           java_path=server["javaPath"], unstable=getattr(args, "unstable", False))
        if err:
            _discard_new_dir(server_dir, external)
            return err

        # Re-read under the lock right before saving: the download above can
        # take minutes, and another command may have changed config.json since.
        with locked_config() as cfg:
            cfg.setdefault("servers", []).append(server)
        write_server_manifest(server)
        if progress:
            progress(100, "Done!")
        return {"success": True, "server": server}
    except BaseException as e:
        # Never leave a half-created, unregistered server folder behind
        # (failed download, Ctrl-C, …).
        _discard_new_dir(server_dir, external)
        if not isinstance(e, Exception):
            raise
        return fail("operation_failed", str(e))


def _safe_external_dir(path):
    """Whether a linked server's folder (outside MCPanel's own directory) may
    be deleted. Refuses anything that could take more than one server with
    it: a symlink, a filesystem root or near-root path, the home directory,
    MCPanel's data directory, or any parent of those."""
    if not path or os.path.islink(path) or not os.path.isdir(path):
        return False
    real = os.path.realpath(path)
    home = os.path.realpath(os.path.expanduser("~"))
    protected = [home, os.path.realpath(paths.USER_DATA)]
    # The standard data dir too, in case MCPANEL_HOME points somewhere else.
    saved = os.environ.pop("MCPANEL_HOME", None)
    try:
        protected.append(os.path.realpath(paths._default_home()))
    finally:
        if saved is not None:
            os.environ["MCPANEL_HOME"] = saved
    for p in protected:
        # The folder is (or contains) a protected directory.
        if paths.is_within(p, real):
            return False
    parts = [x for x in os.path.normpath(real).split(os.sep) if x]
    if len(parts) < 2:  # never "/" or "/srv"-style top-level folders
        return False
    # Last line of defence against a config entry that was edited to point
    # somewhere else: only delete what still looks like a server folder.
    return bool(_scan_folder(real).get("isServer"))


def delete_server(args, progress=None):
    """`delete server` removes the server from MCPanel AND deletes its files —
    linked servers included. `--keep-files` (the panel's "Remove" button)
    only takes it off the list and leaves the folder where it is."""
    keep_files = bool(getattr(args, "keep_files", False))
    try:
        srv = find_server(load_config(), args.id)
        if not srv:
            return fail("server_not_found")
        # Touching the folder (or dropping the entry) under a live JVM corrupts
        # the world or leaves a process the panel can no longer stop.
        if runstate.is_running(srv["id"]):
            verb = "removing" if keep_files else "deleting"
            return fail("server_running", f"Server is running - stop it before {verb} it")
        server_dir = srv["dir"]
        if not keep_files and srv.get("linked") and not _safe_external_dir(server_dir):
            return fail("invalid_path",
                        f"Refusing to delete {server_dir} - it is (or contains) a protected folder. "
                        "Remove the server instead to keep its files.")
        with locked_config() as cfg:
            cfg["servers"] = [s for s in cfg.get("servers", []) if s["id"] != args.id]
            # Drop links other servers hold to this one (when it was a proxy).
            for s in cfg["servers"]:
                link = s.get("velocityLink")
                if isinstance(link, dict) and link.get("velocityId") == args.id:
                    s.pop("velocityLink", None)
        # Unregister first, delete second: if the delete is interrupted the
        # leftovers are just an orphan folder, not a config entry pointing at
        # half a server.
        runstate.purge(args.id)

        if keep_files:
            # Without its manifest the folder is just files again: discovery
            # won't re-register it (it would, inside MCPanel's servers dir).
            try:
                os.remove(os.path.join(server_dir, "mcpanel.json"))
            except OSError:
                pass
            return {"success": True, "removed": True, "kept": server_dir,
                    "message": f"Removed from MCPanel - the files were kept: {server_dir}"}

        if srv.get("linked"):
            failed = []
            note = lambda fn, p, exc: failed.append(p)  # noqa: E731
            if sys.version_info >= (3, 12):
                shutil.rmtree(server_dir, onexc=note)
            else:
                shutil.rmtree(server_dir, onerror=note)
            out = {"success": True, "deleted": server_dir}
            if failed or os.path.exists(server_dir):
                out["warning"] = f"Some files could not be deleted from {server_dir}"
            return out
        removed = _remove_server_dir(server_dir)
        out = {"success": True}
        if not removed and os.path.exists(server_dir):
            out["warning"] = f"Server folder was left in place (outside {paths.SERVERS_DIR}): {server_dir}"
        return out
    except Exception as e:
        return fail("operation_failed", str(e))


def update_server(args, progress=None):
    try:
        current = find_server(load_config(), args.id)
        if not current:
            return {"error": "Server not found", "code": "server_not_found"}
        heap = getattr(args, "heap", None)
        # A lower RAM limit must still fit the heap the server keeps.
        err = _validate_settings(port=args.port, ram=args.ram, software=args.software,
                                 heap=heap if heap is not None else (current.get("heap") if args.ram else None),
                                 ram_for_heap=current.get("ram"))
        if err:
            return err
        if args.name is not None and not args.name.strip():
            return fail("invalid_name", "Name cannot be empty")

        updates = {}
        if args.name is not None:
            updates["name"] = args.name
        if args.port is not None:
            updates["port"] = int(args.port)
        if args.ram is not None:
            updates["ram"] = util.normalize_ram(args.ram)
        if heap is not None:
            updates["heap"] = util.normalize_ram(heap) if heap.strip() else None
        if args.software is not None:
            updates["software"] = args.software
        if args.version is not None:
            updates["version"] = args.version
        if args.java is not None:
            updates["javaPath"] = args.java
        if args.jargs is not None:
            updates["javaArgs"] = args.jargs
        if args.storage is not None:
            updates["storageLimit"] = args.storage or None

        with locked_config() as cfg:
            idx = next((i for i, s in enumerate(cfg.get("servers", [])) if s["id"] == args.id), -1)
            if idx == -1:
                return {"error": "Server not found", "code": "server_not_found"}
            cfg["servers"][idx] = {**cfg["servers"][idx], **updates}
            server = cfg["servers"][idx]

            # Written inside the lock: if this fails the exception skips the
            # config save, so config.json never claims a port the files don't have.
            if "port" in updates:
                if server.get("software", "") == "velocity":
                    _write_velocity_port(os.path.join(server["dir"], "velocity.toml"), updates["port"])
                else:
                    _write_port(os.path.join(server["dir"], "server.properties"), updates["port"])

        write_server_manifest(server)
        return {"success": True, "server": server}
    except Exception as e:
        return fail("operation_failed", str(e))


def duplicate_server(args, progress=None):
    """Clone a server: copy its folder under a new name, optionally to another
    location (-dest) and/or switched to another version of the same software
    (-v — the copy gets a freshly downloaded jar; worlds and configs are
    copied as-is, so going DOWN a version is at the user's own risk)."""
    new_dir = None
    external = False
    try:
        if not (args.name or "").strip():
            return fail("invalid_name", "Name is required (-t)")
        src = find_server(load_config(), args.id)
        if not src:
            return {"error": "Server not found", "code": "server_not_found"}
        if not os.path.isdir(src["dir"]):
            return fail("server_dir_missing", f"Server folder is missing: {src['dir']}")
        version = (getattr(args, "version", None) or "").strip() or None
        if version == src.get("version"):
            version = None
        if version and src.get("software") not in SOFTWARE:
            return fail("invalid_software",
                        f"Can't switch versions: unknown software '{src.get('software')}'")
        paths.ensure_dirs()
        new_id = _unique_id("srv", paths.SERVERS_DIR)
        target, external, err = _new_server_dir(getattr(args, "dest", None), args.name, new_id)
        if err:
            return err
        os.makedirs(target)
        new_dir = target
        if progress:
            progress(0, "Copying server files…")
        # copy_dir skips profile.json and the manifest, so the copy gets its own
        util.copy_dir(src["dir"], new_dir)

        new_server = {**src, "id": new_id, "name": args.name, "dir": new_dir, "created": _now_ms()}
        # The copy is a different server: it isn't registered in the proxy
        # (velocity.toml still points at the original) and has never booted.
        new_server.pop("velocityLink", None)
        new_server.pop("lastBoot", None)
        # Ownership follows where the copy lives, not where the source did.
        new_server.pop("linked", None)
        if external:
            new_server["linked"] = True

        if version:
            # resolve_jar may have picked a custom-named jar; the new one is
            # always server.jar, which resolve_jar prefers, so drop the old one.
            old_jar, _ = util.resolve_jar(new_server)
            err = _install_jar(src["software"], version, new_dir, progress,
                               java_path=src.get("javaPath") or "java",
                               unstable=getattr(args, "unstable", False))
            if err:
                _discard_new_dir(new_dir, external)
                return err
            if old_jar and os.path.basename(old_jar) != "server.jar" and os.path.exists(old_jar):
                os.remove(old_jar)
            new_server["version"] = version

        with locked_config() as cfg:
            cfg.setdefault("servers", []).append(new_server)
        write_server_manifest(new_server)
        if progress:
            progress(100, "Done!")
        return {"success": True, "server": new_server}
    except BaseException as e:
        _discard_new_dir(new_dir, external)
        if not isinstance(e, Exception):
            raise
        return fail("operation_failed", str(e))


def import_server(args, progress=None):
    """Register an existing Minecraft server folder — one MCPanel never saw,
    no mcpanel.json needed. Anything not given on the command line is filled
    in from scan_server_folder() (software, version, port, RAM).

    Default: the folder is COPIED into MCPanel's servers directory.
    --link:  the folder is used IN PLACE — nothing is copied and MCPanel works
             on the original files. `delete server --keep-files` takes it off
             the list again; a plain delete deletes that folder."""
    server_dir = None
    link = bool(getattr(args, "link", False))
    try:
        src = os.path.abspath(os.path.expanduser(args.path or ""))
        if not os.path.isdir(src):
            return fail("invalid_path", f"Not a folder: {args.path}")
        # Importing a folder that contains the servers dir would copy the
        # copy into itself until the disk is full — and linking it would hand
        # MCPanel's own data to a server process.
        if paths.is_within(paths.SERVERS_DIR, src) or paths.is_within(paths.USER_DATA, src):
            return fail("invalid_path", "Cannot import a folder that contains MCPanel's own data directory")
        if link:
            real = os.path.realpath(src)
            for s in load_config().get("servers", []):
                if os.path.realpath(s.get("dir", "")) == real:
                    return fail("server_already_registered",
                                f"That folder is already registered as \"{s.get('name')}\" ({s['id']})")
            if not os.access(src, os.W_OK):
                return fail("invalid_path", f"MCPanel can't write to {src} - it needs to, to run the server")
        err = _validate_settings(port=args.port, ram=args.ram, software=args.software)
        if err:
            return err
        detected = _scan_folder(src)
        paths.ensure_dirs()
        sid = _unique_id("srv", paths.SERVERS_DIR)
        if link:
            server_dir = None  # never ours to clean up
            target = src
        else:
            server_dir = os.path.join(paths.SERVERS_DIR, sid)
            if progress:
                progress(0, "Copying server files...")
            os.makedirs(server_dir)
            util.copy_dir(src, server_dir)
            target = server_dir
        server = {
            "id": sid,
            "name": args.name or os.path.basename(src.rstrip("/\\")) or sid,
            "port": int(args.port) if args.port else detected["port"],
            "ram": util.normalize_ram(args.ram) if args.ram else (detected.get("ram") or "2G"),
            "storageLimit": None,
            "software": args.software or detected.get("software") or "paper",
            "version": args.version or detected.get("version") or "Unknown",
            "profileId": None,
            "javaPath": args.java or "java",
            "javaArgs": args.jargs or util.default_java_args(),
            "created": _now_ms(),
            "dir": target,
        }
        if link:
            server["linked"] = True
        with locked_config() as cfg:
            cfg.setdefault("servers", []).append(server)
        write_server_manifest(server)
        if progress:
            progress(100, "Done!")
        return {"success": True, "server": server, "detected": detected}
    except BaseException as e:
        if server_dir:
            _remove_server_dir(server_dir)
        if not isinstance(e, Exception):
            raise
        return fail("operation_failed", str(e))


# ─── EULA ────────────────────────────────────────────────────────────────────
def accept_eula(args, progress=None):
    try:
        cfg = load_config()
        srv = find_server(cfg, args.id)
        if not srv:
            return fail("server_not_found")
        with open(os.path.join(srv["dir"], "eula.txt"), "w", encoding="utf-8") as f:
            f.write("eula=true\n")
        return {"success": True}
    except Exception as e:
        return fail("operation_failed", str(e))


# ─── lifecycle ────────────────────────────────────────────────────────────────
def _eula_accepted(srv):
    p = os.path.join(srv["dir"], "eula.txt")
    try:
        with open(p, "r", encoding="utf-8") as f:
            return "eula=true" in f.read()
    except OSError:
        return False


def _spawn_supervisor(server_id, timeout=8.0):
    """Launch the detached supervisor and wait for it to report running."""
    # A previous supervisor may still be tearing down (java already exited,
    # e.g. right after a stop/kill). Starting now would make the new one
    # refuse with "already running" — or, before the lock existed, have the
    # old one delete the new one's state files on its way out.
    if not runstate.wait_supervisor_exit(server_id, timeout=10):
        return {"error": "Previous instance is still shutting down - try again in a moment",
                "code": "still_stopping"}
    try:
        os.remove(runstate.boot_err_path(server_id))
    except OSError:
        pass
    env = os.environ.copy()
    pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = _PKG_PARENT + (os.pathsep + pp if pp else "")
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(
        [sys.executable, "-m", "mcpanel.supervisor", server_id],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=env,
        **kwargs,
    )
    deadline = time.time() + timeout
    while time.time() < deadline:
        if runstate.is_running(server_id):
            return {"success": True}
        if os.path.exists(runstate.boot_err_path(server_id)):
            try:
                with open(runstate.boot_err_path(server_id), "r", encoding="utf-8") as f:
                    return fail("start_failed", f.read().strip() or None)
            except OSError:
                return fail("start_failed")
        time.sleep(0.15)
    # Last chance — boot error may have appeared right at the deadline
    try:
        with open(runstate.boot_err_path(server_id), "r", encoding="utf-8") as f:
            return fail("start_failed", f.read().strip() or None)
    except OSError:
        pass
    return fail("start_timeout")


def start_server(args, progress=None):
    try:
        cfg = load_config()
        srv = find_server(cfg, args.id)
        if not srv:
            return fail("server_not_found")
        if runstate.is_running(srv["id"]):
            return fail("server_already_running")

        if not _eula_accepted(srv):
            if getattr(args, "accept_eula", False):
                with open(os.path.join(srv["dir"], "eula.txt"), "w", encoding="utf-8") as f:
                    f.write("eula=true\n")
            else:
                return {"needsEula": True}

        if srv.get("storageLimit"):
            limit = util.parse_storage_limit(srv["storageLimit"])
            if limit is not None:
                current = util.get_dir_size(srv["dir"])
                if current > limit:
                    used_mb = round(current / 1048576)
                    return fail("storage_limit_exceeded", f"Storage limit exceeded: {used_mb} MB used, "
                                                          f"limit is {srv['storageLimit']}")

        _, err = util.resolve_jar(srv)
        if err:
            return fail("jar_not_found", err)

        result = _spawn_supervisor(srv["id"])
        if result.get("success"):
            with locked_config() as fresh:
                entry = find_server(fresh, srv["id"])
                if entry is not None:
                    entry["lastBoot"] = _now_ms()
            if entry is not None:
                write_server_manifest(entry)
        return result
    except Exception as e:
        return fail("operation_failed", str(e))


def _stop_command(server_id):
    # Velocity has no `stop` command — its shutdown command is `end`.
    srv = find_server(load_config(), server_id)
    return "end" if srv and srv.get("software") == "velocity" else "stop"


def stop_server(args, progress=None):
    if not runstate.is_running(args.id):
        return fail("server_not_running")
    r = runstate.send_request(args.id, {"op": "cmd", "text": _stop_command(args.id)})
    if r.get("ok"):
        return {"success": True}
    # The supervisor is gone (crashed / kill -9) but java lives on; there is no
    # console to send `stop` to any more, so the only way out is to kill it.
    if runstate.kill_orphan(args.id):
        return {"success": True, "warning": "Supervisor was gone; the server process was killed"}
    return fail("stop_failed", r.get("error"))


def kill_server(args, progress=None):
    if not runstate.is_running(args.id):
        return fail("server_not_running")
    r = runstate.send_request(args.id, {"op": "kill"})
    if r.get("ok"):
        return {"success": True}
    if runstate.kill_orphan(args.id):
        return {"success": True}
    return fail("stop_failed", r.get("error"))


def restart_server(args, progress=None):
    if runstate.is_running(args.id):
        stopped = stop_server(args)
        deadline = time.time() + 30
        while time.time() < deadline and runstate.is_running(args.id):
            time.sleep(0.25)
        if runstate.is_running(args.id):
            kill_server(args)
            deadline = time.time() + 5
            while time.time() < deadline and runstate.is_running(args.id):
                time.sleep(0.1)
        if runstate.is_running(args.id):
            return fail("stop_failed", "Server did not stop; not restarting",
                        detail=stopped.get("error"))
    return start_server(args, progress)


def send_command(args, progress=None):
    if not runstate.is_running(args.id):
        return fail("server_not_running")
    r = runstate.send_request(args.id, {"op": "cmd", "text": args.command})
    return {"success": True} if r.get("ok") else fail("command_failed", r.get("error"))


def get_server_log(args, progress=None):
    if getattr(args, "session", None) is not None:
        return read_session_log(args, progress)
    return runstate.read_log(args.id)


def list_session_logs(args, progress=None):
    log_paths = runstate.session_log_paths(args.id)
    sessions = []
    for i, p in enumerate(log_paths):
        fname = os.path.basename(p)
        # filename: <id>.log.<ts>.jsonl
        try:
            ts = int(fname[len(args.id) + 5:-6])  # strip "<id>.log." and ".jsonl"
        except Exception:
            ts = 0
        sessions.append({"n": i + 1, "timestamp": ts, "path": p})
    return {"sessions": sessions}


def read_session_log(args, progress=None):
    n = getattr(args, "session", 1) or 1
    log_paths = runstate.session_log_paths(args.id)
    if not log_paths:
        return [{"time": int(time.time() * 1000),
                 "text": "No archived sessions found.", "type": "err"}]
    if n < 1 or n > len(log_paths):
        count = len(log_paths)
        return [{"time": int(time.time() * 1000),
                 "text": f"Session {n} not found ({count} archived session{'s' if count != 1 else ''} available).",
                 "type": "err"}]
    return runstate.read_log_file(log_paths[n - 1])


def is_server_running(args, progress=None):
    return runstate.is_running(args.id)


# ─── ping / stats / files ─────────────────────────────────────────────────────
def ping(args, progress=None):
    host = getattr(args, "host", None)
    port = getattr(args, "port", None)
    if getattr(args, "id", None):
        cfg = load_config()
        srv = find_server(cfg, args.id)
        if not srv:
            return fail("server_not_found")
        host = host or "127.0.0.1"
        port = port or srv.get("port", 25565)
    if not host:
        host = "127.0.0.1"
    if not port:
        port = 25565
    if not util.is_valid_port(port):
        return fail("invalid_port", f"Invalid port {port!r}")
    return ping_server(host, int(port))


def _cpu_sample_path(server_id):
    return os.path.join(paths.RUN_DIR, server_id + ".cpu")


def _read_proc_cpu_seconds(pid):
    """Total CPU time (user+kernel) consumed by `pid`, in seconds.

    Returns None if the process can't be read. Cross-platform: /proc on POSIX,
    GetProcessTimes on Windows.
    """
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not handle:
                return None
            creation = wintypes.FILETIME()
            exit_t = wintypes.FILETIME()
            kernel = wintypes.FILETIME()
            user = wintypes.FILETIME()
            ok = ctypes.windll.kernel32.GetProcessTimes(
                handle, ctypes.byref(creation), ctypes.byref(exit_t),
                ctypes.byref(kernel), ctypes.byref(user))
            ctypes.windll.kernel32.CloseHandle(handle)
            if not ok:
                return None
            def _ticks(ft):
                return (ft.dwHighDateTime << 32) | ft.dwLowDateTime
            # FILETIME is in 100-nanosecond units.
            return (_ticks(kernel) + _ticks(user)) / 1e7
        except Exception:
            return None
    try:
        with open(f"/proc/{pid}/stat", "r") as f:
            data = f.read()
        # comm (field 2) may contain spaces/parens, so split on the last ')'.
        after = data.rpartition(")")[2].split()
        # After the ')', field indices shift by 3: utime is field 14, stime 15.
        utime = int(after[11])
        stime = int(after[12])
        clk = os.sysconf("SC_CLK_TCK") or 100
        return (utime + stime) / clk
    except (OSError, ValueError, IndexError):
        return None


def _server_cpu_pct(server_id, pid):
    """Percent of total machine CPU used by `pid` since the previous call.

    A CLI run is one-shot, so the prior sample (pid, cpu-seconds, wall-clock)
    is cached in run/<id>.cpu and the delta is computed against it. Normalised
    by logical CPU count so it stays within 0..100, matching the system gauge.
    Returns 0.0 on the first sample (no baseline yet).
    """
    proc_seconds = _read_proc_cpu_seconds(pid)
    if proc_seconds is None:
        return None
    now = time.time()
    pct = 0.0
    sample_path = _cpu_sample_path(server_id)
    try:
        with open(sample_path, "r") as f:
            prev_pid, prev_secs, prev_wall = f.read().split()
        if int(prev_pid) == int(pid):
            dt = now - float(prev_wall)
            if dt > 0:
                ncpu = os.cpu_count() or 1
                pct = (proc_seconds - float(prev_secs)) / dt * 100.0 / ncpu
                pct = max(0.0, min(100.0, pct))
    except (OSError, ValueError):
        pass
    try:
        with open(sample_path, "w") as f:
            f.write(f"{pid} {proc_seconds} {now}")
    except OSError:
        pass
    return round(pct, 1)


def _max_players(srv):
    """The player cap the server advertises: max-players in server.properties,
    or show-max-players in velocity.toml - or, before the first start has
    written that file, the default the server will use (20 / Velocity's 500)."""
    if srv.get("software") == "velocity":
        path, pattern, default = (os.path.join(srv["dir"], "velocity.toml"),
                                  r"^\s*show-max-players\s*=\s*(\d+)", 500)
    else:
        path, pattern, default = (os.path.join(srv["dir"], "server.properties"),
                                  r"^max-players\s*=\s*(\d+)", 20)
    m = re.search(pattern, _read_text(path) or "", re.M)
    return int(m.group(1)) if m else default


def get_server_dir_stats(args, progress=None):
    cfg = load_config()
    srv = find_server(cfg, args.id)
    if not srv or not os.path.exists(srv["dir"]):
        return {"size": 0}
    # --no-size: the server cards refresh RAM every few seconds, and walking a
    # world folder each time would cost far more than the numbers are worth.
    result = {} if getattr(args, "no_size", False) else {"size": util.get_dir_size(srv["dir"])}
    result["maxPlayers"] = _max_players(srv)
    st = runstate.read_state(srv["id"])
    if st:
        pid = st.get("javaPid")
        if pid:
            cpu = _server_cpu_pct(srv["id"], pid)
            if cpu is not None:
                result["cpuPct"] = cpu
            if sys.platform == "win32":
                try:
                    import ctypes
                    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
                    handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
                    if handle:
                        class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                            _fields_ = [
                                ("cb", ctypes.c_ulong),
                                ("PageFaultCount", ctypes.c_ulong),
                                ("PeakWorkingSetSize", ctypes.c_size_t),
                                ("WorkingSetSize", ctypes.c_size_t),
                                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                                ("PagefileUsage", ctypes.c_size_t),
                                ("PeakPagefileUsage", ctypes.c_size_t),
                            ]
                        pmc = _PROCESS_MEMORY_COUNTERS()
                        pmc.cb = ctypes.sizeof(pmc)
                        ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb)
                        ctypes.windll.kernel32.CloseHandle(handle)
                        result["ramBytes"] = pmc.WorkingSetSize
                except Exception:
                    pass
            else:
                try:
                    with open(f"/proc/{pid}/status", "r") as f:
                        for line in f:
                            if line.startswith("VmRSS:"):
                                result["ramBytes"] = int(line.split()[1]) * 1024
                                break
                except (OSError, ValueError):
                    pass
    return result


def get_server_file_tree(args, progress=None):
    cfg = load_config()
    srv = find_server(cfg, args.id)
    if not srv:
        return fail("server_not_found")
    return {"tree": util.build_file_tree(srv["dir"], srv["dir"])}


_VERSION_RE = re.compile(r"(?<![\d.])(1\.\d{1,2}(?:\.\d{1,2})?|2\d\.\d{1,2}(?:\.\d{1,2})?)(?![\d])")


def _jar_version(jar_path):
    """Minecraft version baked into a server jar, or None. Reads the jar's
    own metadata: vanilla/Paper/Purpur/Leaf ship version.json; Paperclip
    launchers list the bundled server in META-INF/versions.list."""
    import zipfile
    try:
        with zipfile.ZipFile(jar_path) as zf:
            names = set(zf.namelist())
            if "version.json" in names:
                data = json.loads(zf.read("version.json").decode("utf-8", "replace"))
                v = data.get("id") or data.get("name")
                if isinstance(v, str) and v:
                    return v
            if "META-INF/versions.list" in names:
                text = zf.read("META-INF/versions.list").decode("utf-8", "replace")
                m = re.search(r"versions/([^/\s]+)/", text)
                if m:
                    return m.group(1)
    except (OSError, ValueError, zipfile.BadZipFile, KeyError):
        pass
    m = _VERSION_RE.search(os.path.basename(jar_path))
    return m.group(1) if m else None


def _velocity_version(jar_path):
    """Velocity's version from its jar manifest (Implementation-Version,
    e.g. "4.1.0-SNAPSHOT (git-4772ca30)" -> "4.1.0-SNAPSHOT"), falling back
    to a version in the file name (velocity-3.4.0-SNAPSHOT-526.jar)."""
    import zipfile
    try:
        with zipfile.ZipFile(jar_path) as zf:
            manifest = zf.read("META-INF/MANIFEST.MF").decode("utf-8", "replace")
        m = re.search(r"^Implementation-Version:\s*([^\s(]+)", manifest, re.M)
        if m:
            return m.group(1)
    except (OSError, KeyError, zipfile.BadZipFile):
        pass
    m = re.search(r"(\d+\.\d+\.\d+(?:-SNAPSHOT)?)", os.path.basename(jar_path))
    return m.group(1) if m else None


def _scan_folder(folder):
    """Best-effort description of an existing server folder — works without
    any MCPanel config: port, software, version, RAM, and whether it looks
    like a Minecraft server at all."""
    result = {"port": 25565, "software": None, "version": None, "ram": None,
              "isServer": False, "hasMcpanelConfig": False, "jar": None}
    if not os.path.isdir(folder):
        return result
    try:
        entries = set(os.listdir(folder))
    except OSError:
        return result
    has = lambda *p: os.path.exists(os.path.join(folder, *p))  # noqa: E731
    result["hasMcpanelConfig"] = "mcpanel.json" in entries

    props = os.path.join(folder, "server.properties")
    if os.path.isfile(props):
        try:
            with open(props, "r", encoding="utf-8", errors="replace") as f:
                m = re.search(r"^server-port=(\d+)", f.read(), re.M)
            if m and util.is_valid_port(m.group(1)):
                result["port"] = int(m.group(1))
        except OSError:
            pass
    if has("velocity.toml"):
        try:
            with open(os.path.join(folder, "velocity.toml"), "r", encoding="utf-8", errors="replace") as f:
                m = re.search(r'^bind\s*=\s*"[^"]*:(\d+)"', f.read(), re.M)
            if m and util.is_valid_port(m.group(1)):
                result["port"] = int(m.group(1))
        except OSError:
            pass

    jars = sorted(e for e in entries if e.lower().endswith(".jar"))
    jar = "server.jar" if "server.jar" in entries else (jars[0] if len(jars) == 1 else None)
    if jar is None and jars:
        # Several jars: prefer one whose name says what it is.
        jar = next((j for j in jars if re.search(r"paper|purpur|folia|leaf|velocity|fabric|spigot|server",
                                                  j, re.I)), jars[0])
    result["jar"] = jar

    # Software: config files are the most reliable tell, then the jar name.
    if has("velocity.toml"):
        sw = "velocity"
    elif has("config", "leaf-global.yml") or has("leaf.yml"):
        sw = "leaf"
    elif has("purpur.yml"):
        sw = "purpur"
    elif has("config", "folia-global.yml"):
        sw = "folia"
    elif has("config", "paper-global.yml") or has("paper.yml"):
        sw = "paper"
    elif has(".fabric") or "fabric-server-launch.jar" in entries or has("fabric-server-launcher.properties"):
        sw = "fabric"
    elif has("spigot.yml"):
        sw = "spigot"
    else:
        sw = None
    name_lc = (jar or "").lower()
    for k in ("purpur", "folia", "leaf", "velocity", "fabric", "paper", "spigot"):
        if k in name_lc:
            # A jar name is more specific than e.g. paper-global.yml, which
            # every Paper fork also has.
            if sw in (None, "paper", "spigot") or k == sw:
                sw = k
            break
    if sw is None and jar and (has("server.properties") or has("eula.txt")):
        sw = "vanilla"
    result["software"] = sw

    if jar:
        if result["software"] == "velocity":
            # Velocity has its own versioning (3.4.0-SNAPSHOT, 4.2.0) — never
            # guess a Minecraft-looking number from the file name.
            result["version"] = _velocity_version(os.path.join(folder, jar))
        else:
            result["version"] = _jar_version(os.path.join(folder, jar))

    # RAM from a start script, if there is one.
    for script in ("start.sh", "run.sh", "start.bat", "run.bat", "start.command", "user_jvm_args.txt"):
        p = os.path.join(folder, script)
        if os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    m = re.search(r"-Xmx(\d+[MmGg])", f.read())
                if m:
                    result["ram"] = m.group(1).upper()
                    break
            except OSError:
                pass

    result["isServer"] = bool(jar) and bool(
        {"server.properties", "eula.txt", "velocity.toml", "world"} & entries or result["software"])
    return result


def scan_server_folder(args, progress=None):
    try:
        return _scan_folder(os.path.abspath(os.path.expanduser(args.path or "")))
    except Exception:
        return {"port": 25565}


# Software that reads paper-global.yml's Velocity modern-forwarding block.
# Spigot is deliberately absent: it ignores that file, so "linking" it only
# flipped online-mode=false with no forwarding behind it — an offline-mode
# server anyone could join under any username.
PAPER_SOFTWARES = {"paper", "purpur", "folia", "leaf"}


def _extract_velocity_secret_from_dir(velocity_dir):
    toml_file = os.path.join(velocity_dir, "velocity.toml")
    if not os.path.exists(toml_file):
        return None
    with open(toml_file, "r", encoding="utf-8") as f:
        content = f.read()
    for line in content.splitlines():
        t = line.strip()
        if t.startswith("forwarding-secret-file") and "=" in t:
            m = re.search(r'"([^"]+)"', t)
            if m:
                fpath = os.path.join(velocity_dir, m.group(1))
                if os.path.exists(fpath):
                    with open(fpath, "r", encoding="utf-8") as f:
                        s = f.read().strip()
                    if s:
                        return s
    for line in content.splitlines():
        t = line.strip()
        if (t.startswith("forwarding-secret")
                and not t.startswith("forwarding-secret-file")
                and "=" in t):
            m = re.search(r'"([^"]+)"', t)
            if m and m.group(1):
                return m.group(1)
    fpath = os.path.join(velocity_dir, "forwarding.secret")
    if os.path.exists(fpath):
        with open(fpath, "r", encoding="utf-8") as f:
            s = f.read().strip()
        if s:
            return s
    return None


def _parse_velocity_try_list(content):
    m = re.search(r'^\s*try\s*=\s*\[([^\]]*)\]', content, re.M)
    if not m:
        return []
    return re.findall(r'"([^"]+)"', m.group(1))


# Velocity server names become bare TOML keys, so they're limited to the
# characters a bare key allows. Addresses go inside a TOML string.
_VELOCITY_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_ADDRESS_RE = re.compile(r"^(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]+):(\d{1,5})$")
_TOML_HEADER_RE = re.compile(r"^\s*\[([^\[\]]+)\]\s*(#.*)?$")


def _toml_section(lines, name):
    """(header_index, end_index_exclusive) of `[name]`, or None."""
    start = None
    for i, line in enumerate(lines):
        m = _TOML_HEADER_RE.match(line)
        if not m:
            continue
        if start is not None:
            return start, i
        if m.group(1).strip() == name:
            start = i
    return (start, len(lines)) if start is not None else None


def _add_server_to_velocity_toml(content, server_name, server_address, priority):
    """Register `server_name` in velocity.toml's [servers] table and its try
    list. Every edit stays inside [servers]: the old whole-file regexes would
    delete a same-named top-level key (a server called "motd" or "bind" took
    Velocity's own setting with it), and a missing [servers] table used to be
    *prepended*, which pulled every top-level setting into it.

    A missing [forced-hosts] table is added empty: Velocity otherwise falls
    back to its built-in forced hosts (lobby/factions/minigames.example.com),
    which point at servers our [servers] table no longer has, and refuses to
    start."""
    lines = content.splitlines()
    if _toml_section(lines, "forced-hosts") is None:
        while lines and not lines[-1].strip():
            lines.pop()
        if lines:
            lines.append("")
        lines.append("[forced-hosts]")
    entry = f'{server_name} = "{server_address}"'
    span = _toml_section(lines, "servers")
    if span is None:
        while lines and not lines[-1].strip():
            lines.pop()
        if lines:
            lines.append("")
        lines += ["[servers]", entry, f'try = ["{server_name}"]']
        return "\n".join(lines) + "\n"

    start, end = span
    body = lines[start + 1:end]
    key_re = re.compile(r'^\s*(["\']?)' + re.escape(server_name) + r'\1\s*=')
    body = [ln for ln in body if not key_re.match(ln)]

    # The try list may span several lines ("try = [\n  \"lobby\"\n]").
    try_start = next((i for i, ln in enumerate(body) if re.match(r"^\s*try\s*=", ln)), None)
    items = []
    if try_start is not None:
        try_end = try_start
        while try_end < len(body) and "]" not in body[try_end]:
            try_end += 1
        if try_end >= len(body):
            raise ValueError("velocity.toml: unterminated 'try = [' list in [servers]")
        joined = " ".join(body[try_start:try_end + 1])
        items = [i for i in re.findall(r'"([^"]+)"', joined) if i != server_name]
        del body[try_start:try_end + 1]
    priority = max(0, min(priority, len(items)))
    items.insert(priority, server_name)
    try_line = "try = [" + ", ".join(f'"{i}"' for i in items) + "]"

    # New entry goes after the last existing server entry, else right below
    # the header; the try list goes back where it was (or after the entries).
    entry_re = re.compile(r'^\s*["\']?[A-Za-z0-9_-]+["\']?\s*=')
    entry_idx = [i for i, ln in enumerate(body) if entry_re.match(ln)]
    insert_at = (entry_idx[-1] + 1) if entry_idx else 0
    body.insert(insert_at, entry)
    if try_start is None:
        try_start = insert_at + 1
    elif try_start >= insert_at:
        try_start += 1
    body.insert(min(try_start, len(body)), try_line)

    lines[start + 1:end] = body
    return "\n".join(lines) + "\n"


def _remove_server_from_velocity_toml(content, server_name):
    """Undo `_add_server_to_velocity_toml`: drop `server_name` from [servers]
    and its try list, and from every [forced-hosts] list. A forced host still
    naming the server would stop Velocity from starting, so one left with an
    empty list is removed entirely."""
    lines = content.splitlines()
    name_re = re.compile(r'"([^"]+)"')

    def strip_lists(start, end, keep_key):
        """Rewrite every `key = [...]` in lines[start:end] without server_name
        (lists may span several lines). Returns the new body."""
        body, out, i = lines[start:end], [], 0
        while i < len(body):
            m = re.match(r'^(\s*)([^=\s][^=]*?)\s*=\s*\[', body[i])
            if not m:
                out.append(body[i])
                i += 1
                continue
            j = i
            while j < len(body) and "]" not in body[j]:
                j += 1
            if j >= len(body):
                raise ValueError(f"velocity.toml: unterminated list '{m.group(2)}'")
            items = name_re.findall(" ".join(body[i:j + 1]).split("=", 1)[1])
            if server_name in items:
                items = [x for x in items if x != server_name]
                if items or keep_key(m.group(2).strip()):
                    out.append(f"{m.group(1)}{m.group(2)} = [" + ", ".join(f'"{x}"' for x in items) + "]")
            else:
                out.extend(body[i:j + 1])
            i = j + 1
        return out

    span = _toml_section(lines, "servers")
    if span:
        start, end = span
        key_re = re.compile(r'^\s*(["\']?)' + re.escape(server_name) + r'\1\s*=')
        body = strip_lists(start + 1, end, lambda key: key == "try")
        lines[start + 1:end] = [ln for ln in body if not key_re.match(ln)]
    span = _toml_section(lines, "forced-hosts")
    if span:
        start, end = span
        lines[start + 1:end] = strip_lists(start + 1, end, lambda key: False)
    return "\n".join(lines) + "\n"


def _set_velocity_server_port(content, server_name, port):
    """Point `server_name`'s [servers] entry at `port`, keeping its host.
    Returns (new_content, old_address, new_address)."""
    lines = content.splitlines()
    span = _toml_section(lines, "servers")
    if span is None:
        raise ValueError("velocity.toml has no [servers] table")
    start, end = span
    key_re = re.compile(r'^(\s*(["\']?)' + re.escape(server_name) + r'\2\s*=\s*)"([^"]*)"(.*)$')
    for i in range(start + 1, end):
        m = key_re.match(lines[i])
        if not m:
            continue
        old = m.group(3)
        am = _ADDRESS_RE.match(old)
        host = am.group(1) if am else (old.rsplit(":", 1)[0] if ":" in old else old)
        new = f"{host}:{port}"
        lines[i] = f'{m.group(1)}"{new}"{m.group(4)}'
        return "\n".join(lines) + "\n", old, new
    raise ValueError(f"'{server_name}' is not registered in the proxy's [servers] table")


def _disable_paper_velocity_yml(content):
    """Switch `proxies.velocity.enabled` off in paper-global.yml and blank the
    secret (Paper's default is ''), so the proxy's secret doesn't linger in a
    server that no longer uses it. No velocity block means forwarding was
    never on, so the file is returned unchanged."""
    lines = content.splitlines()
    proxies = _yaml_find_key(lines, 0, len(lines), "proxies", 0)
    if proxies is None:
        return content
    b_start, b_end = _yaml_child_block(lines, proxies)
    child_indent = next((_indent_of(lines[i]) for i in range(b_start, b_end)
                         if _yaml_is_content(lines[i])), 2)
    vel = _yaml_find_key(lines, b_start, b_end, "velocity", child_indent)
    if vel is None:
        return content
    v_start, v_end = _yaml_child_block(lines, vel)
    key_indent = next((_indent_of(lines[i]) for i in range(v_start, v_end)
                       if _yaml_is_content(lines[i])), child_indent + 2)
    changed = False
    for key, value in (("enabled", "false"), ("secret", "''")):
        idx = _yaml_find_key(lines, v_start, v_end, key, key_indent)
        if idx is not None:
            lines[idx] = " " * key_indent + f"{key}: {value}"
            changed = True
    return "\n".join(lines) + "\n" if changed else content


def _ensure_modern_forwarding(content):
    """Set the top-level `player-info-forwarding-mode = "modern"` that the
    paper-global.yml secret pairs with. Without it Velocity keeps its
    legacy/none mode and every linked backend rejects the forwarded login."""
    lines = content.splitlines()
    first_section = next((i for i, ln in enumerate(lines) if _TOML_HEADER_RE.match(ln)), len(lines))
    key_re = re.compile(r"^\s*player-info-forwarding-mode\s*=")
    new_line = 'player-info-forwarding-mode = "modern"'
    for i in range(first_section):
        if key_re.match(lines[i]):
            lines[i] = new_line
            break
    else:
        lines.insert(first_section, new_line)
    return "\n".join(lines) + "\n"


def _set_server_property_text(content, key, value):
    pattern = rf'^{re.escape(key)}\s*=.*$'
    line = f"{key}={value}"
    if re.search(pattern, content, re.M):
        return re.sub(pattern, lambda _m: line, content, flags=re.M)
    return (content.rstrip("\n") + "\n" + line + "\n") if content.strip() else line + "\n"


def _set_server_property(props_file, key, value):
    content = ""
    if os.path.exists(props_file):
        with open(props_file, "r", encoding="utf-8") as f:
            content = f.read()
    util.atomic_write_text(props_file, _set_server_property_text(content, key, value))


def _yaml_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def _indent_of(line):
    return len(line) - len(line.lstrip(" "))


def _yaml_is_content(line):
    t = line.strip()
    return bool(t) and not t.startswith("#")


def _yaml_child_block(lines, parent_idx):
    """(start, end) line range of the block nested under lines[parent_idx]."""
    parent_indent = _indent_of(lines[parent_idx])
    end = parent_idx + 1
    while end < len(lines):
        if _yaml_is_content(lines[end]) and _indent_of(lines[end]) <= parent_indent:
            break
        end += 1
    return parent_idx + 1, end


def _yaml_find_key(lines, start, end, key, indent):
    pat = re.compile(r"^" + " " * indent + re.escape(key) + r":(\s.*)?$")
    for i in range(start, end):
        if pat.match(lines[i]):
            return i
    return None


def _velocity_paper_yml(content, secret):
    """Enable Velocity modern forwarding in paper-global.yml, editing only the
    keys under `proxies.velocity`. The previous regexes changed the *first*
    `enabled:` / `online-mode:` in the whole file — on current Paper that is
    `anticheat.obfuscation.items.enabled` and the bungee-cord section — so
    Velocity forwarding was never switched on at all."""
    lines = content.splitlines()
    values = {"enabled": "true", "online-mode": "true", "secret": _yaml_quote(secret)}

    proxies = _yaml_find_key(lines, 0, len(lines), "proxies", 0)
    if proxies is None:
        while lines and not lines[-1].strip():
            lines.pop()
        lines += ["proxies:", "  velocity:"] + [f"    {k}: {v}" for k, v in values.items()]
        return "\n".join(lines) + "\n"
    if lines[proxies].split(":", 1)[1].split("#", 1)[0].strip():
        raise ValueError("paper-global.yml: 'proxies' is written inline; edit it by hand")

    b_start, b_end = _yaml_child_block(lines, proxies)
    child_indent = next((_indent_of(lines[i]) for i in range(b_start, b_end)
                         if _yaml_is_content(lines[i])), 2)
    vel = _yaml_find_key(lines, b_start, b_end, "velocity", child_indent)
    if vel is None:
        block = [" " * child_indent + "velocity:"] + \
                [" " * (child_indent + 2) + f"{k}: {v}" for k, v in values.items()]
        lines[b_end:b_end] = block
        return "\n".join(lines) + "\n"
    if lines[vel].split(":", 1)[1].split("#", 1)[0].strip():
        raise ValueError("paper-global.yml: 'proxies.velocity' is written inline; edit it by hand")

    v_start, v_end = _yaml_child_block(lines, vel)
    key_indent = next((_indent_of(lines[i]) for i in range(v_start, v_end)
                       if _yaml_is_content(lines[i])), child_indent + 2)
    missing = []
    for key, value in values.items():
        idx = _yaml_find_key(lines, v_start, v_end, key, key_indent)
        if idx is None:
            missing.append(" " * key_indent + f"{key}: {value}")
        else:
            lines[idx] = " " * key_indent + f"{key}: {value}"
    lines[vel + 1:vel + 1] = missing
    return "\n".join(lines) + "\n"


def _configure_paper_velocity(paper_dir, secret):
    yml_path = os.path.join(paper_dir, "config", "paper-global.yml")
    content = ""
    if os.path.exists(yml_path):
        with open(yml_path, "r", encoding="utf-8") as f:
            content = f.read()
    os.makedirs(os.path.dirname(yml_path), exist_ok=True)
    util.atomic_write_text(yml_path, _velocity_paper_yml(content, secret))


class _FileTransaction:
    """Snapshot-before-write for a group of files that must change together.
    `rollback()` restores every touched file to its exact original bytes (or
    removes it if it didn't exist) and drops directories it created."""

    def __init__(self):
        self._orig = {}
        self._made_dirs = []

    def _snapshot(self, path):
        if path in self._orig:
            return
        try:
            with open(path, "rb") as f:
                self._orig[path] = f.read()
        except FileNotFoundError:
            self._orig[path] = None

    def write(self, path, text):
        self._snapshot(path)
        parent = os.path.dirname(path)
        missing = []
        while parent and not os.path.isdir(parent):
            missing.append(parent)
            parent = os.path.dirname(parent)
        for d in reversed(missing):
            os.mkdir(d)
            self._made_dirs.append(d)
        util.atomic_write_text(path, text)

    def rollback(self):
        errors = []
        for path, data in reversed(list(self._orig.items())):
            try:
                if data is None:
                    if os.path.exists(path):
                        os.remove(path)
                else:
                    tmp = f"{path}.{os.getpid()}.rollback"
                    with open(tmp, "wb") as f:
                        f.write(data)
                    os.replace(tmp, path)
            except OSError as e:
                errors.append(f"{path}: {e}")
        for d in reversed(self._made_dirs):
            try:
                os.rmdir(d)
            except OSError:
                pass
        return errors


def proxy_info(args, progress=None):
    try:
        cfg = load_config()
        vel = find_server(cfg, args.velocity_id)
        if not vel:
            return fail("velocity_not_found")
        if vel.get("software") != "velocity":
            return fail("not_velocity")
        toml_file = os.path.join(vel["dir"], "velocity.toml")
        if not os.path.exists(toml_file):
            return {"servers": {}, "tryList": []}
        with open(toml_file, "r", encoding="utf-8") as f:
            content = f.read()
        servers_map = {}
        in_servers = False
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("["):
                in_servers = (stripped == "[servers]")
                continue
            if in_servers:
                m = re.match(r'^([\w-]+)\s*=\s*"([^"]+)"', stripped)
                if m:
                    servers_map[m.group(1)] = m.group(2)
        return {"servers": servers_map, "tryList": _parse_velocity_try_list(content)}
    except Exception as e:
        return fail("operation_failed", str(e))


def _read_text(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def _link_error(message, code, **extra):
    return fail(code, message, **extra)


def link_to_proxy(args, progress=None):
    """Link a Paper-based server into a Velocity proxy.

    This touches three files in two server folders plus config.json, and a
    half-applied link is worse than none (the backend expects forwarded
    connections the proxy doesn't send, or vice versa). So it is all or
    nothing: every new file body is computed in memory first — any parse
    problem fails before a single byte is written — then the writes run as a
    transaction. If any write fails, everything already written is restored
    to its original bytes and the API reports:

        {"error": "...", "code": "proxy_link_failed", "failedStep": "<step>",
         "rolledBack": true|false, "rollbackErrors": [...]}

    `rolledBack: false` means the restore itself failed for the files listed
    in `rollbackErrors`, which then need checking by hand.
    """
    try:
        cfg = load_config()
        paper_srv = find_server(cfg, args.id)
        if not paper_srv:
            return _link_error("Server not found", "server_not_found")
        if paper_srv.get("software") not in PAPER_SOFTWARES:
            hint = (" - Spigot has no Velocity modern forwarding; use Paper (or a fork) instead"
                    if paper_srv.get("software") == "spigot" else "")
            return _link_error(f"'{paper_srv.get('software')}' is not a Paper-based server{hint}",
                               "not_paper")
        vel_srv = find_server(cfg, args.velocity_id)
        if not vel_srv:
            return _link_error("Velocity server not found", "velocity_not_found")
        if vel_srv.get("software") != "velocity":
            return _link_error("Target is not a Velocity server", "not_velocity")
        if vel_srv["id"] == paper_srv["id"]:
            return _link_error("A server cannot be linked to itself", "invalid_target")
        toml_file = os.path.join(vel_srv["dir"], "velocity.toml")
        if not os.path.exists(toml_file):
            return _link_error("velocity.toml not found - start Velocity once to generate it",
                               "velocity_toml_missing")
        secret = _extract_velocity_secret_from_dir(vel_srv["dir"])
        if not secret:
            return _link_error("Could not read forwarding secret from velocity.toml", "secret_missing")

        server_name = args.server_name
        if not server_name:
            server_name = (
                re.sub(r'[^a-z0-9_-]', '-', paper_srv["name"].lower()).strip('-') or "server"
            )[:64]
        if not _VELOCITY_NAME_RE.match(server_name):
            return _link_error(f"Invalid server name {server_name!r} - use letters, digits, '-' or '_'",
                               "invalid_server_name")
        custom = (args.custom_ip or "").strip()
        # A bare host (what the desktop/web link dialog asks for) gets the
        # backend's own port appended; "host:port" is taken as-is.
        if custom and not _ADDRESS_RE.match(custom):
            custom = f"{custom}:{paper_srv['port']}"
        server_address = custom or f"127.0.0.1:{paper_srv['port']}"
        m = _ADDRESS_RE.match(server_address)
        if not m or not util.is_valid_port(m.group(2)):
            return _link_error(f"Invalid address {server_address!r} - expected host:port",
                               "invalid_address")
        priority = int(args.priority) if args.priority is not None else 999

        props_file = os.path.join(paper_srv["dir"], "server.properties")
        yml_file = os.path.join(paper_srv["dir"], "config", "paper-global.yml")
        try:
            new_toml = _ensure_modern_forwarding(_add_server_to_velocity_toml(
                _read_text(toml_file), server_name, server_address, priority))
            new_props = _set_server_property_text(_read_text(props_file), "online-mode", "false")
            new_yml = _velocity_paper_yml(_read_text(yml_file), secret)
        except (OSError, ValueError) as e:
            return _link_error(f"Nothing was changed: {e}", "proxy_config_invalid")
    except Exception as e:
        return fail("operation_failed", str(e))

    txn = _FileTransaction()
    step = None
    try:
        step = "velocity.toml"
        txn.write(toml_file, new_toml)
        step = "server.properties"
        txn.write(props_file, new_props)
        step = "paper-global.yml"
        txn.write(yml_file, new_yml)
        step = "config.json"
        with locked_config() as cfg:
            entry = find_server(cfg, args.id)
            if entry is None:
                raise RuntimeError("server was deleted while linking")
            entry["velocityLink"] = {
                "velocityId": args.velocity_id,
                "serverName": server_name,
                "customIp": args.custom_ip or None,
            }
        write_server_manifest(entry)
    except BaseException as e:
        rollback_errors = txn.rollback()
        try:
            from . import applog
            applog.error(f"proxy link {args.id} -> {args.velocity_id} failed at {step}: {e}; "
                         f"rollback {'failed: ' + '; '.join(rollback_errors) if rollback_errors else 'ok'}")
        except Exception:
            pass
        if not isinstance(e, Exception):
            raise
        return _link_error(
            f"Proxy link failed while writing {step}: {e}. "
            + ("All changes were rolled back." if not rollback_errors
               else "Rollback was incomplete - check the files listed in rollbackErrors."),
            "proxy_link_failed",
            failedStep=step,
            rolledBack=not rollback_errors,
            rollbackErrors=rollback_errors,
        )
    return {"success": True, "serverName": server_name, "address": server_address}


def sync_proxy_port(args, progress=None):
    """After a linked server's port changed, point its velocity.toml entry at
    the new port (the host — 127.0.0.1 or a custom IP — is kept)."""
    try:
        cfg = load_config()
        srv = find_server(cfg, args.id)
        if not srv:
            return fail("server_not_found")
        link = srv.get("velocityLink")
        if not isinstance(link, dict) or not link.get("serverName"):
            return _link_error("This server is not linked to a Velocity proxy", "not_linked")
        vel_srv = find_server(cfg, link.get("velocityId"))
        if not vel_srv:
            return _link_error("The linked Velocity proxy no longer exists", "velocity_not_found")
        port = args.port if args.port is not None else srv.get("port")
        if not util.is_valid_port(port):
            return _link_error(f"Invalid port {port!r}", "invalid_port")
        toml_file = os.path.join(vel_srv["dir"], "velocity.toml")
        if not os.path.exists(toml_file):
            return _link_error("velocity.toml not found - start Velocity once to generate it",
                               "velocity_toml_missing")
        try:
            new_toml, old, new = _set_velocity_server_port(_read_text(toml_file), link["serverName"], port)
        except ValueError as e:
            return _link_error(f"Nothing was changed: {e}", "proxy_config_invalid")
        if old != new:
            util.atomic_write_text(toml_file, new_toml)
        return {"success": True, "serverName": link["serverName"], "velocityId": vel_srv["id"],
                "oldAddress": old, "address": new, "changed": old != new}
    except Exception as e:
        return fail("operation_failed", str(e))


def unlink_from_proxy(args, progress=None):
    """Undo `link_to_proxy`: remove the server from the proxy's velocity.toml,
    turn Velocity forwarding off in paper-global.yml, set online-mode back to
    true and drop `velocityLink`. Same all-or-nothing transaction as the link.
    A proxy that no longer exists (or has no velocity.toml) is skipped, so a
    stale link can always be cleared."""
    try:
        cfg = load_config()
        paper_srv = find_server(cfg, args.id)
        if not paper_srv:
            return _link_error("Server not found", "server_not_found")
        link = paper_srv.get("velocityLink")
        if not isinstance(link, dict):
            return _link_error("This server is not linked to a Velocity proxy", "not_linked")
        server_name = link.get("serverName") or ""
        vel_srv = find_server(cfg, link.get("velocityId"))
        toml_file = os.path.join(vel_srv["dir"], "velocity.toml") if vel_srv else None
        props_file = os.path.join(paper_srv["dir"], "server.properties")
        yml_file = os.path.join(paper_srv["dir"], "config", "paper-global.yml")
        try:
            new_toml = None
            if toml_file and server_name and os.path.exists(toml_file):
                new_toml = _remove_server_from_velocity_toml(_read_text(toml_file), server_name)
            new_props = _set_server_property_text(_read_text(props_file), "online-mode", "true")
            new_yml = None
            if os.path.exists(yml_file):
                new_yml = _disable_paper_velocity_yml(_read_text(yml_file))
        except (OSError, ValueError) as e:
            return _link_error(f"Nothing was changed: {e}", "proxy_config_invalid")
    except Exception as e:
        return fail("operation_failed", str(e))

    txn = _FileTransaction()
    step = None
    try:
        if new_toml is not None:
            step = "velocity.toml"
            txn.write(toml_file, new_toml)
        step = "server.properties"
        txn.write(props_file, new_props)
        if new_yml is not None:
            step = "paper-global.yml"
            txn.write(yml_file, new_yml)
        step = "config.json"
        with locked_config() as cfg:
            entry = find_server(cfg, args.id)
            if entry is None:
                raise RuntimeError("server was deleted while unlinking")
            entry.pop("velocityLink", None)
        write_server_manifest(entry)
    except BaseException as e:
        rollback_errors = txn.rollback()
        try:
            from . import applog
            applog.error(f"proxy unlink {args.id} failed at {step}: {e}; "
                         f"rollback {'failed: ' + '; '.join(rollback_errors) if rollback_errors else 'ok'}")
        except Exception:
            pass
        if not isinstance(e, Exception):
            raise
        return _link_error(
            f"Proxy unlink failed while writing {step}: {e}. "
            + ("All changes were rolled back." if not rollback_errors
               else "Rollback was incomplete - check the files listed in rollbackErrors."),
            "proxy_unlink_failed",
            failedStep=step,
            rolledBack=not rollback_errors,
            rollbackErrors=rollback_errors,
        )
    return {"success": True, "serverName": server_name,
            "velocityId": link.get("velocityId"), "proxyFound": vel_srv is not None}


def open_server_folder(args, progress=None):
    cfg = load_config()
    srv = find_server(cfg, args.id)
    if not srv:
        return fail("server_not_found")
    try:
        if sys.platform == "win32":
            subprocess.Popen(["explorer", srv["dir"]])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", srv["dir"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(["xdg-open", srv["dir"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    return {"success": True, "dir": srv["dir"]}
