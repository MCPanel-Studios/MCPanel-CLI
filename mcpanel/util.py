"""Pure helpers ported from main.js (copyDirSync, parseStorageLimit,
getDirSize, parsJavaArgs, xmsFromRam, compareVersions, buildFileTree) plus a
couple of CLI conveniences (RAM normalisation, human sizes)."""

import os
import re
import shutil
import sys

from .config import MANIFEST_FILENAME

_DEFAULT_JAVA_ARGS = "-XX:+UseG1GC -XX:+ParallelRefProcEnabled -XX:MaxGCPauseMillis=200"


def default_java_args():
    return _DEFAULT_JAVA_ARGS


def copy_dir(src, dest):
    """Recursive copy that skips profile.json and mcpanel.json — matches
    copyDirSync. The manifest is skipped so a duplicated/imported server gets
    its own fresh one instead of inheriting the source's id."""
    if not os.path.exists(src):
        return
    os.makedirs(dest, exist_ok=True)
    for entry in os.scandir(src):
        if entry.name in ("profile.json", MANIFEST_FILENAME):
            continue
        sp = os.path.join(src, entry.name)
        dp = os.path.join(dest, entry.name)
        # Symlinks are recreated, never followed: following one that points
        # back up the tree recursed until the disk filled.
        if entry.is_symlink():
            try:
                if os.path.lexists(dp):
                    os.remove(dp)
                os.symlink(os.readlink(sp), dp)
            except OSError:
                pass
        elif entry.is_dir(follow_symlinks=False):
            copy_dir(sp, dp)
        else:
            shutil.copy2(sp, dp)


def atomic_write_text(path, text):
    """Write `text` to `path` via a temp file + rename, so a crash or full
    disk never leaves a truncated server.properties / velocity.toml behind."""
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        try:
            shutil.copymode(path, tmp)
        except OSError:
            pass
        os.replace(tmp, path)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def parse_storage_limit(value):
    """'20G' / '500MB' / '1024' → bytes, or None if unparseable."""
    if value is None:
        return None
    m = re.match(r"^(\d+(?:\.\d+)?)\s*(B|KB|MB|GB|TB|K|M|G|T)?$", str(value).strip(), re.I)
    if not m:
        return None
    num = float(m.group(1))
    unit = (m.group(2) or "B").upper().rstrip("B")
    mult = {"": 1, "K": 1024, "M": 1048576, "G": 1073741824, "T": 1099511627776}
    return num * mult.get(unit, 1)


def get_dir_size(path):
    size = 0
    try:
        for entry in os.scandir(path):
            full = os.path.join(path, entry.name)
            if entry.is_dir(follow_symlinks=False):
                size += get_dir_size(full)
            else:
                try:
                    size += entry.stat(follow_symlinks=False).st_size
                except OSError:
                    pass
    except OSError:
        pass
    return size


def parse_java_args(args_str):
    return [a for a in (args_str or "").split() if a]


def normalize_ram(value):
    """Accept 20480 (MB), '20480', '20480M', '20G' → canonical '<n>M'/'<n>G'.

    The MCPanel UI/examples pass plain megabytes (e.g. -ram 20480), which we
    store as an Xmx-ready string."""
    if value is None:
        return None
    s = str(value).strip()
    m = re.match(r"^(\d+)\s*(M|MB|G|GB)?$", s, re.I)
    if not m:
        return s
    num = m.group(1)
    unit = (m.group(2) or "M").upper()[0]  # M or G
    return f"{num}{unit}"


def is_valid_ram(ram):
    """Whether `ram` (already normalised) is something -Xmx accepts."""
    return bool(re.match(r"^[1-9]\d*[MG]$", str(ram or "")))


def is_valid_port(port):
    try:
        return 1 <= int(port) <= 65535
    except (TypeError, ValueError):
        return False


def ram_mb(ram):
    """'6G' / '2048M' → megabytes, or None if it isn't a RAM value."""
    m = re.match(r"^(\d+)(M|G)$", str(ram or ""), re.I)
    if not m:
        return None
    return int(m.group(1)) * 1024 if m.group(2).upper() == "G" else int(m.group(1))


def auto_heap(ram):
    """The -Xmx for a server whose `ram` is its TOTAL memory budget and that
    has no explicit heap: the JVM needs memory beyond the heap (classes, JIT
    code, thread stacks, GC, network buffers), so leave 20% of the budget for
    that - at least 384 MB, at most 1.5 GB - and never go below 256 MB."""
    total = ram_mb(ram)
    if total is None:
        return ram
    overhead = min(max(total // 5, 384), 1536)
    return f"{max(total - overhead, 256)}M"


def java_heap(srv):
    """The heap a server actually runs with: its own `heap`, else auto_heap."""
    return srv.get("heap") or auto_heap(srv.get("ram"))


def xms_from_ram(ram):
    m = re.match(r"^(\d+)(M|G)$", str(ram or ""), re.I)
    if not m:
        return "512M"
    mb = int(m.group(1)) * 1024 if m.group(2).upper() == "G" else int(m.group(1))
    return f"{min(512, mb)}M"


def compare_versions(a, b):
    pa = [int(x or 0) for x in re.sub(r"^v", "", str(a)).split(".") if x.isdigit() or x == ""]
    pb = [int(x or 0) for x in re.sub(r"^v", "", str(b)).split(".") if x.isdigit() or x == ""]
    for i in range(max(len(pa), len(pb))):
        diff = (pa[i] if i < len(pa) else 0) - (pb[i] if i < len(pb) else 0)
        if diff != 0:
            return diff
    return 0


def build_file_tree(dir_path, root_path, depth=0):
    if depth > 10:
        return []
    try:
        entries = list(os.scandir(dir_path))
    except OSError:
        return []
    items = []
    # plugins/.list and mods/.list hold MCPanel's install records — hidden
    # from the file manager (see pluginlist.py).
    hide_list = os.path.basename(os.path.normpath(dir_path)) in ("plugins", "mods")
    for entry in entries:
        full = os.path.join(dir_path, entry.name)
        rel = os.path.relpath(full, root_path)
        if hide_list and entry.name == ".list":
            continue
        if entry.is_dir(follow_symlinks=False):
            items.append({
                "name": entry.name, "type": "dir", "path": rel,
                "children": build_file_tree(full, root_path, depth + 1),
            })
        else:
            try:
                size = entry.stat().st_size
            except OSError:
                size = 0
            items.append({"name": entry.name, "type": "file", "path": rel, "size": size})
    items.sort(key=lambda a: (0 if a["type"] == "dir" else 1, a["name"].lower()))
    return items


def resolve_jar(srv):
    """Pick the server jar — server.jar, else the only .jar present.
    Returns (jar_path, error_message)."""
    d = srv["dir"]
    jar = os.path.join(d, "server.jar")
    if os.path.exists(jar):
        return jar, None
    try:
        jars = [f for f in os.listdir(d) if f.endswith(".jar")]
    except OSError:
        jars = []
    if len(jars) == 1:
        return os.path.join(d, jars[0]), None
    if len(jars) == 0:
        return None, "No .jar found in server directory"
    return None, "Multiple .jars found. Please rename one to server.jar"


# ─── custom software ────────────────────────────────────────────────────────
# "custom"      runs a start script / executable from the server folder
#               (Forge/NeoForge's run.sh, a hand-written start.bat, a native
#               server binary, …).
# "custom-java" runs any .jar with MCPanel's own java command line.
# Both keep the chosen file in the server's "runFile" (relative to its folder).
CUSTOM_SOFTWARE = ("custom", "custom-java")

# Pumpkin (pumpkinmc.org) is a native Rust server: no Java, no jar, configured
# through pumpkin.toml instead of server.properties.
PUMPKIN_BINARY = "pumpkin.exe" if sys.platform == "win32" else "pumpkin"
NATIVE_SOFTWARE = ("custom", "pumpkin")

# Names a start script usually has, best first; anything else sorts after.
_RUN_NAME_RANK = ("run", "start", "launch", "server", "startserver", "start-server", "start_server")


def run_file_extensions(software, platform=None):
    """File types a custom server's run file may have on this OS."""
    platform = platform or sys.platform
    if software == "custom-java":
        return (".jar",)
    if platform == "win32":
        return (".bat", ".cmd", ".ps1", ".exe")
    if platform == "darwin":
        return (".sh", ".command")
    return (".sh",)


def find_run_files(folder, software):
    """Candidate run files in `folder` (top level only), best guess first."""
    exts = run_file_extensions(software)
    try:
        names = [e.name for e in os.scandir(folder)
                 if e.is_file() and e.name.lower().endswith(exts)]
    except OSError:
        return []

    def rank(name):
        stem = os.path.splitext(name)[0].lower()
        if software == "custom-java":
            # server.jar first, then anything that names itself a server jar;
            # installers are almost never what should be run.
            return (stem != "server", "installer" in stem, "server" not in stem, name.lower())
        pos = _RUN_NAME_RANK.index(stem) if stem in _RUN_NAME_RANK else len(_RUN_NAME_RANK)
        return (pos, "install" in stem, name.lower())
    return sorted(names, key=rank)


def resolve_run_file(srv):
    """Absolute path of a custom server's run file — the stored "runFile",
    else the best match in its folder. Returns (path, error_message)."""
    d = srv["dir"]
    software = srv.get("software")
    rf = (srv.get("runFile") or "").strip()
    if rf:
        path = rf if os.path.isabs(rf) else os.path.join(d, rf)
        if os.path.isfile(path):
            return path, None
        return None, f"Run file not found: {rf}"
    found = find_run_files(d, software)
    if found:
        return os.path.join(d, found[0]), None
    exts = ", ".join(run_file_extensions(software))
    return None, f"No run file ({exts}) found in the server folder - pick one in the server's settings"


def relative_run_file(server_dir, path):
    """How a run file is stored: relative when it's inside the server folder."""
    path = os.path.abspath(path)
    if is_inside(path, server_dir):
        return os.path.relpath(path, server_dir)
    return path


def is_inside(path, folder):
    try:
        return os.path.commonpath([os.path.realpath(path), os.path.realpath(folder)]) == os.path.realpath(folder)
    except ValueError:
        return False


def build_script_command(path):
    """Command line for a custom run file, by type."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".ps1":
        return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path]
    if ext in (".bat", ".cmd"):
        return ["cmd", "/c", path]
    if ext in (".sh", ".command"):
        if os.access(path, os.X_OK):
            return [path]
        # Not marked executable (copied from a zip, a Windows share, …):
        # run it through a shell instead of failing with "permission denied".
        return [shutil.which("bash") or "sh", path]
    return [path]


def resolve_pumpkin_binary(srv):
    """(path, error_message) of a Pumpkin server's binary: the stored runFile,
    else pumpkin(.exe), else the only pumpkin* executable in the folder."""
    d = srv["dir"]
    rf = (srv.get("runFile") or "").strip()
    if rf:
        path = rf if os.path.isabs(rf) else os.path.join(d, rf)
        return (path, None) if os.path.isfile(path) else (None, f"Pumpkin binary not found: {rf}")
    path = os.path.join(d, PUMPKIN_BINARY)
    if os.path.isfile(path):
        return path, None
    found = find_pumpkin_binaries(d)
    if found:
        return os.path.join(d, found[0]), None
    return None, f"No Pumpkin binary ({PUMPKIN_BINARY}) found in the server folder"


def find_pumpkin_binaries(folder):
    want_exe = sys.platform == "win32"
    try:
        names = [e.name for e in os.scandir(folder)
                 if e.is_file() and e.name.lower().startswith("pumpkin")
                 and e.name.lower().endswith(".exe") == want_exe
                 and not e.name.lower().endswith((".toml", ".log", ".part"))]
    except OSError:
        return []
    return sorted(names, key=lambda n: (n.lower() != PUMPKIN_BINARY, n.lower()))


def build_launch_command(srv):
    """(command, error_message) for starting `srv`, whatever its software."""
    software = srv.get("software")
    if software == "pumpkin":
        path, err = resolve_pumpkin_binary(srv)
        if not path:
            return None, err
        if sys.platform != "win32" and not os.access(path, os.X_OK):
            os.chmod(path, 0o755)
        return [path], None
    if software == "custom":
        path, err = resolve_run_file(srv)
        return (build_script_command(path), None) if path else (None, err)
    if software == "custom-java":
        jar, err = resolve_run_file(srv)
    else:
        jar, err = resolve_jar(srv)
    return (build_java_command(srv, jar), None) if jar else (None, err)


def build_java_command(srv, jar):
    java_path = srv.get("javaPath") or "java"
    # `ram` is the server's total memory budget; the heap is only part of it.
    heap = java_heap(srv)
    return [
        java_path,
        *parse_java_args(srv.get("javaArgs") or ""),
        f"-Xmx{heap}",
        f"-Xms{xms_from_ram(heap)}",
        "-jar", jar, "nogui",
    ]


def human_size(num):
    num = float(num or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024 or unit == "TB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} TB"
