"""Client-side helpers for talking to running server supervisors.

In the Electron app the main process stays alive and keeps a `runningServers`
map in memory. A CLI invocation is short-lived, so instead each running server
is owned by a detached supervisor daemon (see supervisor.py). State lives on
disk under run/:

    run/<id>.json        ← {supervisorPid, javaPid, port, started}
    run/<id>.sock        ← unix control socket (cmd / kill / status)
    run/<id>.log.jsonl   ← one JSON log record per line {time,text,type}
"""

import json
import os
import re
import socket
import sys
import time

from . import paths

# Unix-domain sockets are used on POSIX; TCP loopback on Windows.
_USE_UNIX_SOCKET = sys.platform != "win32" and hasattr(socket, "AF_UNIX")


def _run_file(server_id, suffix):
    # Ids arrive straight from the API (`stop -id ...`); one containing a path
    # separator would address files outside run/.
    if not paths.is_valid_id(server_id):
        raise ValueError(f"Invalid server id: {server_id!r}")
    return os.path.join(paths.RUN_DIR, server_id + suffix)


def state_path(server_id):
    return _run_file(server_id, ".json")


def sock_path(server_id):
    return _run_file(server_id, ".sock")


def port_path(server_id):
    """Path to the TCP control port file (Windows only)."""
    return _run_file(server_id, ".port")


def log_path(server_id):
    return _run_file(server_id, ".log.jsonl")


def boot_err_path(server_id):
    return _run_file(server_id, ".boot.err")


def lock_path(server_id):
    """Held by the supervisor for its whole lifetime (contains its pid), so a
    second supervisor for the same server can't start while one is alive —
    including one that is still tearing down after java exited."""
    return _run_file(server_id, ".lock")


def _pid_alive(pid):
    if not pid:
        return False
    try:
        if sys.platform == "win32":
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not handle:
                return False
            code = ctypes.c_ulong(0)
            ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(handle)
            return code.value == STILL_ACTIVE
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def read_state(server_id):
    try:
        with open(state_path(server_id), "r", encoding="utf-8") as f:
            st = json.load(f)
        return st if isinstance(st, dict) else None
    except Exception:
        return None


def write_state(server_id, state):
    """Atomic, so a reader never sees a half-written state file."""
    path = state_path(server_id)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, path)


def supervisor_pid(server_id):
    try:
        with open(lock_path(server_id), "r", encoding="utf-8") as f:
            return int(f.read().strip() or 0) or None
    except (OSError, ValueError):
        return None


def supervisor_alive(server_id):
    return _pid_alive(supervisor_pid(server_id))


def wait_supervisor_exit(server_id, timeout=10.0):
    """Block until the previous supervisor (if any) has finished tearing down.
    Returns False if it is still alive after `timeout`."""
    deadline = time.time() + timeout
    while supervisor_alive(server_id):
        if time.time() >= deadline:
            return False
        time.sleep(0.15)
    return True


def is_running(server_id):
    st = read_state(server_id)
    if not st:
        return False
    if _pid_alive(st.get("javaPid")):
        return True
    # Java is gone. If its supervisor is still alive it is mid-teardown and
    # will clean up after itself — deleting its files from here raced with
    # that and, on restart, with the *next* supervisor's freshly written ones.
    if not _pid_alive(st.get("supervisorPid")):
        cleanup_state(server_id)
    return False


def cleanup_state(server_id):
    if not paths.is_valid_id(server_id):
        return
    cpu_sample = _run_file(server_id, ".cpu")
    for p in (state_path(server_id), sock_path(server_id), port_path(server_id), cpu_sample):
        try:
            os.remove(p)
        except OSError:
            pass


def purge(server_id):
    """Remove every run/<id>.* file (state, logs, archived sessions, boot
    error, lock) — for a server that has been deleted."""
    if not paths.is_valid_id(server_id):
        return
    # Exact suffixes, so purging "a" can't take "a.b"'s files with it.
    own = re.compile(re.escape(server_id)
                     + r"\.(json|sock|port|cpu|lock|boot\.err|log(\.\d+)?\.jsonl)$")
    try:
        names = os.listdir(paths.RUN_DIR)
    except OSError:
        return
    for name in names:
        if own.match(name):
            try:
                os.remove(os.path.join(paths.RUN_DIR, name))
            except OSError:
                pass


def tree_pids(pid):
    """`pid` and all its descendants (best effort, [] if it's gone)."""
    pid = int(pid)
    children = {}
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            class PROCESSENTRY32(ctypes.Structure):
                _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                            ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_void_p),
                            ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                            ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                            ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_char * 260)]
            k32 = ctypes.windll.kernel32
            k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
            snap = k32.CreateToolhelp32Snapshot(0x2, 0)          # TH32CS_SNAPPROCESS
            if snap in (None, wintypes.HANDLE(-1).value):
                return [pid]
            try:
                e = PROCESSENTRY32()
                e.dwSize = ctypes.sizeof(e)
                ok = k32.Process32First(snap, ctypes.byref(e))
                while ok:
                    children.setdefault(e.th32ParentProcessID, []).append(e.th32ProcessID)
                    ok = k32.Process32Next(snap, ctypes.byref(e))
            finally:
                k32.CloseHandle(snap)
        elif os.path.isdir("/proc"):
            for name in os.listdir("/proc"):
                if not name.isdigit():
                    continue
                try:
                    with open(f"/proc/{name}/stat", "r") as f:
                        ppid = int(f.read().rpartition(")")[2].split()[1])
                except (OSError, ValueError, IndexError):
                    continue
                children.setdefault(ppid, []).append(int(name))
        else:
            import subprocess
            out = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True).stdout
            for line in out.splitlines():
                parts = line.split()
                if len(parts) == 2:
                    children.setdefault(int(parts[1]), []).append(int(parts[0]))
    except Exception:
        return [pid]
    out, todo, seen = [], [pid], set()
    while todo:
        p = todo.pop()
        if p in seen:
            continue
        seen.add(p)
        out.append(p)
        todo.extend(children.get(p, []))
    return out


def process_rss(pid):
    """Resident memory of one process in bytes, or None."""
    try:
        if sys.platform == "win32":
            import ctypes

            class PMC(ctypes.Structure):
                _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION
            if not handle:
                return None
            try:
                pmc = PMC()
                pmc.cb = ctypes.sizeof(pmc)
                if not ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb):
                    return None
                return pmc.WorkingSetSize
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        if os.path.isdir("/proc"):
            with open(f"/proc/{int(pid)}/statm", "r") as f:
                return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
        import subprocess
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(int(pid))], capture_output=True, text=True).stdout
        return int(out.strip()) * 1024 if out.strip() else None
    except (OSError, ValueError):
        return None


def tree_rss(pid):
    """Resident memory of `pid` and everything it started, in bytes."""
    total = 0
    for p in tree_pids(pid):
        total += process_rss(p) or 0
    return total


def kill_tree(pid):
    """Kill `pid` and everything it started. Used for custom servers, where
    `pid` is a start script and java (or whatever it runs) is its child: the
    script runs in its own session/process group on POSIX (see supervisor)."""
    pid = int(pid)
    if sys.platform == "win32":
        import subprocess
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return
    import signal
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        os.kill(pid, signal.SIGKILL)


def kill_orphan(server_id):
    """Last resort for a java process whose supervisor died (e.g. kill -9):
    nothing is listening on the control socket any more, so signal java
    directly. Returns True if a kill was sent."""
    st = read_state(server_id)
    if not st or _pid_alive(st.get("supervisorPid")):
        return False
    pid = st.get("javaPid")
    if not _pid_alive(pid):
        return False
    try:
        if st.get("processTree"):
            kill_tree(pid)
        elif sys.platform == "win32":
            import ctypes
            PROCESS_TERMINATE = 0x0001
            handle = ctypes.windll.kernel32.OpenProcess(PROCESS_TERMINATE, False, int(pid))
            if not handle:
                return False
            ctypes.windll.kernel32.TerminateProcess(handle, 1)
            ctypes.windll.kernel32.CloseHandle(handle)
        else:
            import signal
            os.kill(int(pid), signal.SIGKILL)
    except (OSError, ValueError):
        return False
    deadline = time.time() + 5
    while _pid_alive(pid) and time.time() < deadline:
        time.sleep(0.1)
    cleanup_state(server_id)
    return True


def send_request(server_id, obj, timeout=5.0):
    """Send a single JSON request to the supervisor and return its JSON reply."""
    try:
        if _USE_UNIX_SOCKET:
            sp = sock_path(server_id)
            if not os.path.exists(sp):
                return {"ok": False, "error": "Not running"}
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            addr = sp
        else:
            pp = port_path(server_id)
            if not os.path.exists(pp):
                return {"ok": False, "error": "Not running"}
            with open(pp, "r", encoding="utf-8") as f:
                addr = ("127.0.0.1", int(f.read().strip()))
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.settimeout(timeout)
            s.connect(addr)
            s.sendall((json.dumps(obj) + "\n").encode("utf-8"))
            data = b""
            while b"\n" not in data:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
        finally:
            s.close()
        if not data:
            return {"ok": False, "error": "No response"}
        return json.loads(data.decode("utf-8", "replace").splitlines()[0])
    except Exception as e:
        return {"ok": False, "error": str(e)}


def read_log_file(path):
    """Read a .log.jsonl file and return a list of {time, text, type} records."""
    out = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    out.append({"time": int(time.time() * 1000), "text": line, "type": "out"})
    except FileNotFoundError:
        pass
    return out


def read_log(server_id):
    """Return the full structured log for the current/last run."""
    return read_log_file(log_path(server_id))


_SESSION_KEEP = 5


def session_log_paths(server_id):
    """Return archived session log paths sorted newest-first."""
    prefix = server_id + ".log."
    suffix = ".jsonl"
    result = []
    try:
        for name in os.listdir(paths.RUN_DIR):
            if name.startswith(prefix) and name.endswith(suffix):
                mid = name[len(prefix):-len(suffix)]
                if mid.isdigit():
                    result.append((int(mid), os.path.join(paths.RUN_DIR, name)))
    except OSError:
        pass
    return [p for _, p in sorted(result, reverse=True)]


def rotate_log(server_id):
    """Archive current log and prune old sessions, keeping _SESSION_KEEP total."""
    current = log_path(server_id)
    if os.path.exists(current):
        ts = int(time.time() * 1000)
        archived = os.path.join(paths.RUN_DIR, f"{server_id}.log.{ts}.jsonl")
        try:
            os.rename(current, archived)
        except OSError:
            pass
    # Keep at most SESSION_KEEP-1 archives (the new active session will be the Nth)
    for excess in session_log_paths(server_id)[_SESSION_KEEP - 1:]:
        try:
            os.remove(excess)
        except OSError:
            pass
