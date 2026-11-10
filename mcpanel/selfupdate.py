"""`mcpanel updateapp` — fetch the newest release of MCPanel, MCPanel-CLI or
MCPanel-WebUI from GitHub and install it.

The desktop app's and the WebUI's "Update" buttons run this same command
(`mcpanel api updateapp -p …`), so there is one implementation of how each
product gets installed:

  * cli     — pip installs the release's source archive into the interpreter
              running this CLI (on Windows from a detached process, because
              the running mcpanel.exe can't be overwritten).
  * mcpanel — downloads the release asset for this platform: replaces the
              AppImage, or installs the .rpm/.deb (sudo / pkexec), runs the
              NSIS setup on Windows, copies the .app out of the .dmg on macOS.
  * <path>  — an MCPanel-WebUI folder: `git pull`s the release tag into it
              (or unpacks its source archive there), then runs `npm install`.

The desktop app passes MCPANEL_APP_VERSION / MCPANEL_APP_EXE because the CLI
can't find those on its own; the WebUI passes its own folder as the path.
Without -p, the WebUI folder recorded in <data>/webui.json (written by the
WebUI at startup) is updated along with everything else.

Every GitHub URL goes through http's MCPanel-Studios → DippyCoder fallback.
"""

import json
import os
import platform
import re
import shutil
import site
import subprocess
import sys
import tempfile
from urllib.parse import quote

from . import __version__, addons, applog, http, paths
from .errors import CLIError
from .util import compare_versions

PRODUCTS = {
    "mcpanel": {"name": "MCPanel", "repo": "MCPanel"},
    "cli": {"name": "MCPanel-CLI", "repo": "mcpanel-cli"},
    "webui": {"name": "MCPanel-WebUI", "repo": "MCPanel-WebUI"},
}
# Values -p takes by name; anything else is a path to an MCPanel-WebUI folder.
ALIASES = {
    "mcpanel": "mcpanel",
    "mcpanel-cli": "cli", "cli": "cli",
}

WEBUI_RECORD = "webui.json"

# The Tauri bundles' package name (kebab-cased productName). The old Electron
# 1.x app was packaged as "mcpanel" — a different package, which `rpm -U`
# would install the new app next to rather than replace, so it isn't matched.
DESKTOP_PACKAGE = "mc-panel"


# ─── helpers ─────────────────────────────────────────────────────────────────

def _repo_url(product, rest=""):
    return f"https://github.com/DippyCoder/{PRODUCTS[product]['repo']}{rest}"


def _latest_release(product):
    url = f"https://api.github.com/repos/DippyCoder/{PRODUCTS[product]['repo']}/releases/latest"
    try:
        rel = http.fetch_json(url)
    except Exception as e:
        raise CLIError("update_check_failed",
                       f"Could not fetch the latest {PRODUCTS[product]['name']} release: {e}")
    if not isinstance(rel, dict) or not rel.get("tag_name"):
        raise CLIError("update_check_failed",
                       f"GitHub returned no {PRODUCTS[product]['name']} release.")
    return rel


def _parse_app_version(v):
    """MCPanel version "X.Y.Z" or "X.Y.Z-bN" → (X, Y, Z, N), N = inf for a full
    release (a beta comes before its full release). None when unparsable."""
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)(?:-b(\d+))?$", str(v or "").strip(), re.I)
    if not m:
        return None
    return (int(m[1]), int(m[2]), int(m[3]), int(m[4]) if m[4] is not None else float("inf"))


def _mcpanel_release(current):
    """The MCPanel release to update to: the newest full release, or on a beta
    build also newer betas of that same version (2.4.1-b1 → 2.4.1-b2).
    Mirrors pickAppRelease() in the desktop app's tauri-bridge.js."""
    url = f"https://api.github.com/repos/DippyCoder/{PRODUCTS['mcpanel']['repo']}/releases?per_page=50"
    try:
        rels = http.fetch_json(url)
    except Exception as e:
        raise CLIError("update_check_failed", f"Could not fetch the MCPanel releases: {e}")
    cur = _parse_app_version(current)
    best, best_v = None, None
    for r in rels if isinstance(rels, list) else []:
        if not isinstance(r, dict) or r.get("draft") or not r.get("tag_name"):
            continue
        v = _parse_app_version(r["tag_name"])
        if not v:
            continue
        if v[3] != float("inf"):
            if not cur or cur[3] == float("inf") or v[:3] != cur[:3]:
                continue
        elif r.get("prerelease"):
            continue
        if best_v is None or v > best_v:
            best, best_v = r, v
    if not best:
        raise CLIError("update_check_failed", "GitHub returned no MCPanel release.")
    return best


def _ver(tag):
    return str(tag or "").strip().lstrip("vV")


def _result(product, current, latest, updated, message, **extra):
    out = {
        "product": product,
        "name": PRODUCTS[product]["name"],
        "current": current,
        "latest": latest,
        "updated": updated,
        "message": message,
    }
    out.update(extra)
    return out


def _up_to_date(product, current, latest):
    return _result(product, current, latest, False,
                   f"{PRODUCTS[product]['name']} is up to date (v{current})")


def _run(cmd, cwd=None, env=None):
    """(ok, combined output) — never raises for a missing program."""
    try:
        p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    except OSError as e:
        return False, str(e)
    return p.returncode == 0, ((p.stdout or "") + (p.stderr or "")).strip()


def _tail(text, lines=6):
    return "\n".join(str(text).strip().splitlines()[-lines:])


def _download(url, dest, progress, label):
    def on_pct(pct):
        if progress:
            progress(min(pct, 99), f"Downloading {label}… {pct}%")
    if progress:
        progress(0, f"Downloading {label}…")
    try:
        http.download_file(url, dest, on_pct)
    except Exception as e:
        raise CLIError("download_failed", f"Could not download {label}: {e}")


def _progress(progress, pct, status):
    if progress:
        progress(pct, status)


# ─── MCPanel-CLI ─────────────────────────────────────────────────────────────

def _cli_install_mode():
    """pip flags matching how this CLI was installed, or None when it runs
    from a source checkout pip doesn't own."""
    here = os.path.abspath(os.path.dirname(__file__))
    if not any(part in here for part in ("site-packages", "dist-packages")):
        return None
    if sys.prefix != getattr(sys, "base_prefix", sys.prefix):
        return []                                   # virtualenv / pipx
    try:
        user_site = os.path.abspath(site.getusersitepackages())
    except Exception:
        user_site = None
    return ["--user"] if user_site and here.startswith(user_site) else []


def _update_cli(progress):
    rel = _latest_release("cli")
    latest = _ver(rel["tag_name"])
    current = __version__
    if compare_versions(latest, current) <= 0:
        return _up_to_date("cli", current, latest)

    mode = _cli_install_mode()
    if mode is None:
        raise CLIError("update_unsupported",
                       "This MCPanel-CLI runs from a source checkout, not a pip install - "
                       "update it with `git pull` instead.")

    archive = _repo_url("cli", f"/archive/refs/tags/{quote(rel['tag_name'], safe='')}.zip")
    base = [sys.executable, "-m", "pip", "install", "--upgrade",
            "--disable-pip-version-check"] + mode

    if os.name == "nt":
        # The running mcpanel.exe launcher is locked until this process exits,
        # so pip has to run after us: a detached interpreter waits a moment,
        # then installs from the first archive URL that works.
        script = (
            "import subprocess, sys, time\n"
            "time.sleep(3)\n"
            f"for url in {http.github_candidates(archive)!r}:\n"
            f"    if subprocess.call({base!r} + [url]) == 0:\n"
            "        break\n"
        )
        flags = 0x00000008 | 0x00000200 | 0x08000000   # DETACHED | NEW_GROUP | NO_WINDOW
        try:
            subprocess.Popen([sys.executable, "-c", script], creationflags=flags,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, close_fds=True)
        except OSError as e:
            raise CLIError("update_failed", f"Could not start the MCPanel-CLI update: {e}")
        applog.info(f"updateapp: MCPanel-CLI {current} → {latest} started in the background")
        return _result("cli", current, latest, True,
                       f"MCPanel-CLI v{latest} is installing in the background - "
                       "it's ready in a few seconds", background=True)

    _progress(progress, 10, f"Installing MCPanel-CLI v{latest}…")
    last = ""
    for url in http.github_candidates(archive):
        ok, out = _run(base + [url])
        if not ok and "externally-managed-environment" in out:
            # PEP 668 distros refuse even --user installs without this flag;
            # the CLI was installed the same way, so this replaces it in kind.
            ok, out = _run(base + ["--break-system-packages", url])
        if ok:
            applog.info(f"updateapp: MCPanel-CLI {current} → {latest} from {url}")
            _progress(progress, 100, f"MCPanel-CLI v{latest} installed")
            return _result("cli", current, latest, True, f"MCPanel-CLI updated to v{latest}")
        last = out
    raise CLIError("update_failed", f"pip could not install MCPanel-CLI v{latest}:\n{_tail(last)}")


# ─── MCPanel (desktop) ───────────────────────────────────────────────────────

def _arch_keys():
    m = platform.machine().lower()
    if m in ("x86_64", "amd64", "x64"):
        return ("x86_64", "amd64", "x64")
    if m in ("aarch64", "arm64"):
        return ("aarch64", "arm64")
    return (m,)


def _pick_asset(rel, suffix):
    assets = [a for a in (rel.get("assets") or [])
              if isinstance(a, dict) and a.get("browser_download_url")
              and str(a.get("name", "")).lower().endswith(suffix.lower())]
    if not assets:
        raise CLIError("update_unsupported",
                       f"MCPanel {rel['tag_name']} has no '{suffix}' download for this system.")
    keys = _arch_keys()
    for a in assets:
        if any(k in str(a["name"]).lower() for k in keys):
            return a
    if len(assets) == 1:
        return assets[0]
    raise CLIError("update_unsupported",
                   f"MCPanel {rel['tag_name']} has no '{suffix}' download for {platform.machine()}.")


def _pkg_version(cmd):
    ok, out = _run(cmd)
    return out.strip() if ok and out.strip() else None


def _as_root(cmd):
    """Prefix `cmd` so it runs as root: directly when we already are, sudo
    from an interactive terminal, pkexec (a graphical password prompt) when
    the desktop app runs us."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return cmd
    if sys.stdin and sys.stdin.isatty() and shutil.which("sudo"):
        return ["sudo"] + cmd
    if shutil.which("pkexec"):
        return ["pkexec"] + cmd
    raise CLIError("update_unsupported",
                   "Installing the MCPanel package needs root, but neither sudo (in a terminal) "
                   "nor pkexec is available. Run `mcpanel updateapp -p mcpanel` in a terminal.")


def _desktop_target():
    """(kind, current version or None, detail) for this machine's MCPanel install."""
    current = os.environ.get("MCPANEL_APP_VERSION") or None
    if sys.platform == "win32":
        return "nsis", current, None
    if sys.platform == "darwin":
        exe = os.environ.get("MCPANEL_APP_EXE", "")
        bundle = exe.split(".app/")[0] + ".app" if ".app/" in exe else "/Applications/MCPanel.app"
        if not current:
            plist = os.path.join(bundle, "Contents", "Info.plist")
            try:
                import plistlib
                with open(plist, "rb") as f:
                    current = plistlib.load(f).get("CFBundleShortVersionString")
            except Exception:
                pass
        return "dmg", current, bundle
    appimage = os.environ.get("APPIMAGE")
    if appimage and os.path.isfile(appimage):
        return "appimage", current, appimage
    v = _pkg_version(["rpm", "-q", "--qf", "%{VERSION}", DESKTOP_PACKAGE]) if shutil.which("rpm") else None
    if v:
        return "rpm", current or v, None
    v = _pkg_version(["dpkg-query", "-W", "-f", "${Version}", DESKTOP_PACKAGE]) if shutil.which("dpkg-query") else None
    if v:
        return "deb", current or v, None
    raise CLIError("not_installed",
                   f"No MCPanel install was found (no {DESKTOP_PACKAGE} rpm/deb package). For the AppImage, "
                   "use the Update button in MCPanel's settings.")


def _update_mcpanel(progress):
    kind, current, detail = _desktop_target()
    rel = _mcpanel_release(current)
    latest = _ver(rel["tag_name"])
    lv, cv = _parse_app_version(latest), _parse_app_version(current)
    if current and (lv <= cv if lv and cv else compare_versions(latest, current) <= 0):
        return _up_to_date("mcpanel", current, latest)

    suffix = {"nsis": "-setup.exe", "dmg": ".dmg", "appimage": ".AppImage",
              "rpm": ".rpm", "deb": ".deb"}[kind]
    asset = _pick_asset(rel, suffix)
    tmpdir = tempfile.mkdtemp(prefix="mcpanel-update-")
    keep_tmp = False
    try:
        file = os.path.join(tmpdir, os.path.basename(str(asset["name"])))
        _download(asset["browser_download_url"], file, progress, f"MCPanel v{latest}")
        _progress(progress, 99, f"Installing MCPanel v{latest}…")

        if kind == "appimage":
            # Swap the file in place; the running AppImage keeps its mounted
            # copy until it exits, so this is safe while MCPanel is open.
            new = detail + ".new"
            shutil.copyfile(file, new)
            os.chmod(new, 0o755)
            os.replace(new, detail)
        elif kind in ("rpm", "deb"):
            cmd = ["rpm", "-U", file] if kind == "rpm" else ["dpkg", "-i", file]
            ok, out = _run(_as_root(cmd))
            if not ok:
                raise CLIError("update_failed", f"Installing MCPanel v{latest} failed:\n{_tail(out)}")
        elif kind == "dmg":
            _install_dmg(file, detail, latest)
        else:
            # The NSIS installer can't replace MCPanel's files while it runs,
            # so it is started detached in passive mode and MCPanel quits.
            flags = 0x00000008 | 0x00000200              # DETACHED | NEW_GROUP
            try:
                subprocess.Popen([file, "/P"], creationflags=flags, close_fds=True)
            except OSError as e:
                raise CLIError("update_failed", f"Could not start the MCPanel installer: {e}")
            keep_tmp = True                              # the installer is still reading it
            applog.info(f"updateapp: MCPanel {current} → {latest} installer started")
            return _result("mcpanel", current, latest, True,
                           f"The MCPanel v{latest} installer has started - MCPanel closes so it "
                           "can finish", restartRequired=True, quitApp=True)
    finally:
        if not keep_tmp:
            shutil.rmtree(tmpdir, ignore_errors=True)

    applog.info(f"updateapp: MCPanel {current} → {latest} ({kind})")
    _progress(progress, 100, f"MCPanel v{latest} installed")
    return _result("mcpanel", current, latest, True,
                   f"MCPanel updated to v{latest} - restart MCPanel to use it",
                   restartRequired=True)


def _install_dmg(dmg, bundle, latest):
    mount = tempfile.mkdtemp(prefix="mcpanel-dmg-")
    ok, out = _run(["hdiutil", "attach", "-nobrowse", "-readonly", "-mountpoint", mount, dmg])
    if not ok:
        raise CLIError("update_failed", f"Could not open the MCPanel v{latest} disk image:\n{_tail(out)}")
    try:
        apps = [n for n in os.listdir(mount) if n.endswith(".app")]
        if not apps:
            raise CLIError("update_failed", f"The MCPanel v{latest} disk image contains no app.")
        staged = bundle + ".new"
        shutil.rmtree(staged, ignore_errors=True)
        ok, out = _run(["ditto", os.path.join(mount, apps[0]), staged])
        if not ok:
            raise CLIError("update_failed", f"Could not copy MCPanel v{latest} to {bundle}:\n{_tail(out)}")
        shutil.rmtree(bundle, ignore_errors=True)
        os.replace(staged, bundle)
    finally:
        _run(["hdiutil", "detach", mount, "-quiet"])
        shutil.rmtree(mount, ignore_errors=True)


# ─── MCPanel-WebUI ───────────────────────────────────────────────────────────

def _webui_dir(d=None):
    """(folder, installed version) — `d` from -p, else the recorded folder."""
    if d is None:
        try:
            with open(os.path.join(paths.USER_DATA, WEBUI_RECORD), encoding="utf-8") as f:
                d = (json.load(f) or {}).get("dir")
        except (OSError, ValueError):
            d = None
    if not d:
        raise CLIError("not_installed",
                       "MCPanel-WebUI's folder is unknown - start the WebUI once (it records "
                       "where it lives), or pass it: mcpanel updateapp -p <path>")
    d = os.path.abspath(os.path.expanduser(d))
    try:
        with open(os.path.join(d, "package.json"), encoding="utf-8") as f:
            pkg = json.load(f)
    except (OSError, ValueError):
        pkg = {}
    if pkg.get("name") != "mcpanel-webui":
        raise CLIError("not_installed", f"{d} is not an MCPanel-WebUI folder.")
    return d, str(pkg.get("version") or "0")


def _copy_tree_over(src, dst):
    for root, dirs, files in os.walk(src):
        dirs[:] = [x for x in dirs if x not in ("node_modules", ".git")]
        rel = os.path.relpath(root, src)
        target = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(target, exist_ok=True)
        for name in files:
            shutil.copy2(os.path.join(root, name), os.path.join(target, name))


def _update_webui(progress, directory=None):
    d, current = _webui_dir(directory)
    rel = _latest_release("webui")
    latest = _ver(rel["tag_name"])
    if compare_versions(latest, current) <= 0:
        return _up_to_date("webui", current, latest)
    tag = rel["tag_name"]

    if os.path.isdir(os.path.join(d, ".git")) and shutil.which("git"):
        _progress(progress, 10, f"Pulling MCPanel-WebUI {tag}…")
        last = ""
        # A repository that doesn't exist (yet) answers like a private one, so
        # git would sit waiting for a username instead of falling back.
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        for url in http.github_candidates(_repo_url("webui", ".git")):
            ok, out = _run(["git", "-C", d, "pull", "--ff-only", url, f"refs/tags/{tag}"], env=env)
            if ok:
                break
            last = out
        else:
            raise CLIError("update_failed",
                           f"git could not update MCPanel-WebUI to {tag} (local changes?):\n{_tail(last)}")
    else:
        tmpdir = tempfile.mkdtemp(prefix="mcpanel-webui-")
        try:
            archive = os.path.join(tmpdir, "webui.zip")
            _download(_repo_url("webui", f"/archive/refs/tags/{quote(tag, safe='')}.zip"),
                      archive, progress, f"MCPanel-WebUI {tag}")
            unpacked = os.path.join(tmpdir, "unpacked")
            os.makedirs(unpacked)
            addons._safe_extract_zip(archive, unpacked)
            tops = os.listdir(unpacked)
            src = os.path.join(unpacked, tops[0]) if len(tops) == 1 else unpacked
            _copy_tree_over(src, d)
        except CLIError:
            raise
        except Exception as e:
            raise CLIError("update_failed", f"Could not unpack MCPanel-WebUI {tag}: {e}")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    npm = shutil.which("npm")
    if not npm:
        raise CLIError("update_failed",
                       f"MCPanel-WebUI {tag} was downloaded, but npm is missing - run `npm install` in {d}.")
    _progress(progress, 90, "Installing MCPanel-WebUI dependencies…")
    ok, out = _run([npm, "install", "--omit=dev", "--no-audit", "--no-fund"], cwd=d)
    if not ok:
        raise CLIError("update_failed", f"`npm install` failed in {d}:\n{_tail(out)}")

    applog.info(f"updateapp: MCPanel-WebUI {current} → {latest}")
    _progress(progress, 100, f"MCPanel-WebUI v{latest} installed")
    return _result("webui", current, latest, True,
                   f"MCPanel-WebUI updated to v{latest} - restart the WebUI to use it",
                   restartRequired=True)


# ─── command ─────────────────────────────────────────────────────────────────

_UPDATERS = {"mcpanel": _update_mcpanel, "cli": _update_cli, "webui": _update_webui}


def update_app(args, progress=None):
    """Backs `mcpanel updateapp [-p <mcpanel|cli|path>]`. With -p: that
    product's result (or its error); a path updates the MCPanel-WebUI in that
    folder. Without: every product, skipping ones not installed here —
    MCPanel-CLI last, so the others run on the code that started them."""
    product = getattr(args, "product", None)
    if product:
        key = ALIASES.get(product.lower())
        if key:
            return _UPDATERS[key](progress)
        if not os.path.isdir(os.path.expanduser(product)):
            raise CLIError("invalid_arguments",
                           f"'{product}' is neither mcpanel, mcpanel-cli / cli nor an existing "
                           "MCPanel-WebUI folder.")
        return _update_webui(progress, product)

    results = []
    for key in ("mcpanel", "webui", "cli"):
        try:
            results.append(_UPDATERS[key](progress))
        except CLIError as e:
            if e.code == "not_installed":
                continue
            results.append(dict(e.to_dict(), product=key, name=PRODUCTS[key]["name"]))
    out = {"results": results}
    failed = [r for r in results if r.get("error")]
    if failed:
        out.update(error="; ".join(f"{r['name']}: {r['error']}" for r in failed), code="update_failed")
    return out
