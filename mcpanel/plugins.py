"""Plugin / Mod search and install via Modrinth, Hangar, and SpigotMC (Spiget)."""

import os
import re
import sys
from urllib.parse import quote

from . import disclaimers, pluginlist
from .http import fetch_json, fetch_json_with_headers, download_file
from .config import load_config, find_server
from .errors import fail, CLIError

MODDED_SOFTWARES = {"fabric"}

# Maps software names to Modrinth loader tags
MODRINTH_LOADERS = {
    "fabric": ["fabric"],
    "velocity": ["velocity"],
    "paper": ["paper", "purpur", "spigot", "bukkit"],
    "purpur": ["paper", "purpur", "spigot", "bukkit"],
    "folia": ["folia", "paper"],
    "leaf": ["paper", "purpur", "spigot", "bukkit"],
    "spigot": ["spigot", "bukkit"],
}


# Custom servers run whatever the user brought — MCPanel can't know what
# loader (if any) is inside, so the plugin manager is off for them.
NO_PLUGIN_SOFTWARES = {"custom", "custom-java", "pumpkin"}
# Velocity plugins only exist on Modrinth and Hangar.
SPIGOTMC_SOFTWARES_EXCLUDED = {"velocity"}


def _hangar_platform(software):
    return "VELOCITY" if software == "velocity" else "PAPER"


def _mc_filter(software, mc_version):
    """The Minecraft version to filter by. A Velocity server's "version" is
    Velocity's own (3.4.0), which matches no Minecraft version."""
    return None if software == "velocity" else mc_version


def _check_platform(software, platform):
    if software in NO_PLUGIN_SOFTWARES:
        raise CLIError("plugins_unsupported", "Plugins and mods can't be managed for custom servers")
    if platform == "spigotmc" and software in SPIGOTMC_SOFTWARES_EXCLUDED:
        raise CLIError("unknown_platform", "SpigotMC has no Velocity plugins - use Modrinth or Hangar")


def _dest_dir(software):
    return "mods" if software in MODDED_SOFTWARES else "plugins"


def _safe_filename(name):
    return "".join(c if c.isalnum() or c in "._- " else "_" for c in name).strip()


def _jar_filename(name, fallback):
    """File names come from remote APIs (and the user's slug); reduce them to
    a single, plain path component so `../../x.jar` can't escape plugins/."""
    base = os.path.basename(str(name or "").replace("\\", "/"))
    base = _safe_filename(base).lstrip(".")
    if not base:
        base = _safe_filename(fallback) or "plugin"
    if not base.lower().endswith((".jar", ".zip")):
        base += ".jar"
    return base


def _hangar_owner_slug(slug, owner=None):
    """Hangar projects are addressed as owner/slug. Accepts "owner/slug" as
    the slug, or a bare slug plus --owner (falling back to slug as owner)."""
    if "/" in slug:
        o, slug = slug.split("/", 1)
        return (owner or o), slug
    return (owner or slug), slug


def _html_to_text(raw_html):
    """Best-effort plain-text rendering of a description that may contain
    markdown or raw HTML (Spiget resource descriptions are base64-encoded
    HTML) — good enough for a details popup, no need for a real markdown/HTML
    dependency just to display body text."""
    import re
    import html as _html_mod
    text = re.sub(r"(?is)<br\s*/?>", "\n", raw_html)
    text = re.sub(r"(?is)</(p|li|div|h[1-6])>", "\n\n", text)
    text = re.sub(r"(?s)<[^>]+>", "", text)
    text = _html_mod.unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ─── Modrinth ─────────────────────────────────────────────────────────────────

def search_modrinth(query, software="paper", mc_version=None, limit=20, offset=0):
    import json as _json
    from urllib.parse import quote
    facets = []
    if software in MODDED_SOFTWARES:
        facets.append(["project_type:mod"])
        facets.append(["categories:fabric"])
    else:
        facets.append(["project_type:plugin"])
        loaders = MODRINTH_LOADERS.get(software, ["paper"])
        facets.append([f"categories:{loaders[0]}"])
    if mc_version:
        facets.append([f"versions:{mc_version}"])
    encoded_facets = quote(_json.dumps(facets))
    url = (
        "https://api.modrinth.com/v2/search"
        f"?query={quote(query)}&limit={limit}&offset={offset}&facets={encoded_facets}"
    )
    data = fetch_json(url)
    results = [
        {
            "slug": h["slug"],
            "name": h["title"],
            "author": h["author"],
            "downloads": h.get("downloads", 0),
            "updatedAt": h.get("date_modified", ""),
            "latestVersion": h.get("latest_version", ""),
            "description": h.get("description", ""),
            "platform": "modrinth",
            "iconUrl": h.get("icon_url"),
        }
        for h in data.get("hits", [])
    ]
    # Modrinth silently clamps `limit` to 100 per request regardless of what's
    # asked, so "hasMore" can't just check len(results) == limit — use the
    # real total instead.
    has_more = offset + len(results) < data.get("total_hits", 0)
    return results, has_more


def _modrinth_file(v):
    """The download + what the .list record needs from a Modrinth version."""
    files = v.get("files", [])
    primary = next((f for f in files if f.get("primary")), files[0] if files else None)
    if not primary:
        raise RuntimeError("No download file found")
    return {
        "url": primary["url"], "filename": primary.get("filename"),
        "version": v.get("version_number") or "", "versionId": v.get("id"),
        "projectId": v.get("project_id"),
        # Only required dependencies get installed; optional/incompatible/
        # embedded ones are the user's call.
        "deps": [{"platform": "modrinth", "projectId": d.get("project_id"), "versionId": d.get("version_id")}
                 for d in v.get("dependencies") or []
                 if d.get("dependency_type") == "required" and (d.get("project_id") or d.get("version_id"))],
    }


def get_modrinth_download(slug, software="paper", mc_version=None):
    import json as _json
    from urllib.parse import quote
    loaders = MODRINTH_LOADERS.get(software, ["paper"])
    params = f"loaders={quote(_json.dumps(loaders))}"
    if mc_version:
        params += f"&game_versions={quote(_json.dumps([mc_version]))}"
    url = f"https://api.modrinth.com/v2/project/{quote(slug, safe='')}/version?{params}"
    versions = fetch_json(url)
    if not versions:
        raise RuntimeError(f"No compatible version found for {slug} on MC {mc_version}")
    return _modrinth_file(versions[0])


def get_modrinth_version_download(version_id):
    """Fetch the exact file for a version the user picked from the version-
    history list — no loader/mc_version compatibility filtering, since they
    already chose this version deliberately."""
    v = fetch_json(f"https://api.modrinth.com/v2/version/{quote(str(version_id), safe='')}")
    try:
        return _modrinth_file(v)
    except RuntimeError:
        raise RuntimeError("No download file found for that version")


def get_modrinth_info(slug, limit=25, offset=0):
    slug = quote(slug, safe="")
    all_versions = fetch_json(f"https://api.modrinth.com/v2/project/{slug}/version")
    page = all_versions[offset:offset + limit]
    result = {
        "websiteUrl": f"https://modrinth.com/project/{slug}",
        "versions": [
            {
                "id": v["id"],
                "name": v.get("version_number") or "",
                "date": v.get("date_published", ""),
                "changelog": v.get("changelog") or "",
            }
            for v in page
        ],
        "hasMoreVersions": offset + limit < len(all_versions),
    }
    if offset == 0:
        # Only needed once — skip on "load more versions" pages.
        project = fetch_json(f"https://api.modrinth.com/v2/project/{slug}")
        result["longDescription"] = project.get("body") or ""
    return result


# ─── Hangar ───────────────────────────────────────────────────────────────────

def search_hangar(query, mc_version=None, limit=20, offset=0, software="paper"):
    url = (
        f"https://hangar.papermc.io/api/v1/projects"
        f"?query={quote(query)}&platform={_hangar_platform(software)}&limit={limit}&offset={offset}"
    )
    if mc_version:
        url += f"&version={quote(mc_version)}"
    data = fetch_json(url)
    results = [
        {
            "slug": p.get("namespace", {}).get("slug", p["name"]),
            "name": p["name"],
            "author": p.get("namespace", {}).get("owner", ""),
            "downloads": p.get("stats", {}).get("downloads", 0),
            "updatedAt": p.get("lastUpdated", ""),
            "latestVersion": "",
            "description": p.get("description", ""),
            "platform": "hangar",
            "ownerName": p.get("namespace", {}).get("owner", ""),
            "iconUrl": p.get("avatarUrl"),
        }
        for p in data.get("result", [])
    ]
    # Hangar silently clamps `limit` to 50 per request regardless of what's
    # asked, so "hasMore" can't just check len(results) == limit — use the
    # real total instead.
    has_more = offset + len(results) < data.get("pagination", {}).get("count", 0)
    return results, has_more


def get_hangar_download(slug, owner, mc_version=None, pin_version=None, software="paper"):
    hp = _hangar_platform(software)
    owner, slug = quote(owner, safe=""), quote(slug, safe="")
    base = f"https://hangar.papermc.io/api/v1/projects/{owner}/{slug}/versions"
    if pin_version:
        v = fetch_json(f"{base}/{quote(str(pin_version), safe='')}")
    else:
        data = fetch_json(f"{base}?platform={hp}&limit=1")
        results = data.get("result", [])
        if not results:
            raise RuntimeError(f"No versions found for {slug} on Hangar")
        v = results[0]
    version = quote(str(v["name"]), safe="")
    dl_url = (
        f"https://hangar.papermc.io/api/v1/projects/{owner}/{slug}"
        f"/versions/{version}/{hp}/download"
    )
    deps = []
    for d in (v.get("pluginDependencies") or {}).get(hp) or []:
        if not d.get("required"):
            continue
        # Dependencies hosted on Hangar carry a projectId; the rest only have
        # an external link and can't be installed automatically.
        deps.append({"platform": "hangar", "projectId": d.get("projectId"),
                     "name": d.get("name") or "", "externalUrl": d.get("externalUrl")})
    return {"url": dl_url, "filename": f"{slug}-{v['name']}.jar",
            "version": v["name"], "versionId": v["name"], "deps": deps}


def get_hangar_info(slug, owner, limit=25, offset=0):
    owner, slug = quote(owner, safe=""), quote(slug, safe="")
    url = (
        f"https://hangar.papermc.io/api/v1/projects/{owner}/{slug}/versions"
        f"?limit={limit}&offset={offset}"
    )
    data = fetch_json(url)
    results = data.get("result", [])
    total = data.get("pagination", {}).get("count", 0)
    result = {
        "websiteUrl": f"https://hangar.papermc.io/{owner}/{slug}",
        "versions": [
            {
                "id": v["name"],
                "name": v["name"],
                "date": v.get("createdAt", ""),
                "changelog": v.get("description") or "",
            }
            for v in results
        ],
        "hasMoreVersions": offset + len(results) < total,
    }
    if offset == 0:
        # Only needed once — skip on "load more versions" pages.
        project = fetch_json(f"https://hangar.papermc.io/api/v1/projects/{owner}/{slug}")
        result["longDescription"] = project.get("mainPageContent") or ""
    return result


# ─── SpigotMC (Spiget) ────────────────────────────────────────────────────────

def _resolve_spiget_authors(author_ids):
    """Spiget's search/browse results only ever embed {"id": N} for the
    author — the real username needs a separate per-author call. Resolve the
    unique ids in this batch concurrently instead of N sequential requests."""
    import concurrent.futures
    names = {}

    def _fetch(author_id):
        try:
            data = fetch_json(f"https://api.spiget.org/v2/authors/{author_id}?fields=name")
            return author_id, data.get("name")
        except Exception:
            return author_id, None

    unique_ids = {i for i in author_ids if i is not None}
    if not unique_ids:
        return names
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        for author_id, name in pool.map(_fetch, unique_ids):
            names[author_id] = name
    return names


def search_spiget(query, limit=20, offset=0):
    import datetime as _dt
    from urllib.parse import quote

    page = offset // limit + 1 if limit else 1
    # Explicit `fields` both trims the (otherwise base64-icon-bloated) default
    # payload and makes sure `external`/`file` are there — Spiget's own docs
    # warn to check `external` before downloading: externally-hosted resources
    # (a large share of popular plugins, e.g. EssentialsX) redirect their
    # `/download` endpoint to a human-facing release page, not a jar.
    # `icon.url` (dot-notation) pulls just the relative image path — the bare
    # `icon` field would also embed a base64-encoded copy of the same image.
    fields = "id,name,tag,downloads,updateDate,external,file,premium,author,version,icon.url"
    if query:
        # Real text search — Spiget has no "free only" search variant, so
        # premium resources are filtered out below instead.
        url = (
            f"https://api.spiget.org/v2/search/resources/{quote(query, safe='')}"
            f"?field=name&size={limit}&page={page}&sort=-downloads&fields={fields}"
        )
    else:
        # No search term: browse the free-resources list directly (real
        # pagination, premium already excluded) instead of faking a "popular"
        # text search, which only ever matched literal name/tag hits.
        url = (
            f"https://api.spiget.org/v2/resources/free"
            f"?size={limit}&page={page}&sort=-downloads&fields={fields}"
        )
    data, headers = fetch_json_with_headers(url)
    total = int(headers.get("X-Total") or 0)

    author_names = _resolve_spiget_authors(r.get("author", {}).get("id") for r in data)

    results = []
    for r in data:
        if query and r.get("premium"):
            continue
        ts = r.get("updateDate", 0)
        updated = ""
        if ts:
            try:
                updated = _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
            except Exception:
                pass
        author_id = r.get("author", {}).get("id")
        icon_path = (r.get("icon") or {}).get("url")
        results.append({
            "slug": str(r["id"]),
            "name": r.get("name", ""),
            "author": author_names.get(author_id) or "Unknown",
            "downloads": r.get("downloads", 0),
            "updatedAt": updated,
            "latestVersion": "",
            "description": r.get("tag", ""),
            "platform": "spigotmc",
            "external": r.get("external", False),
            "externalUrl": (r.get("file") or {}).get("externalUrl"),
            "iconUrl": f"https://www.spigotmc.org/{icon_path}" if icon_path else None,
        })
    has_more = offset + len(data) < total
    return results, has_more


def get_spiget_download(resource_id, pin_version=None):
    """Resolve the download for a Spiget resource — raises with a clear,
    actionable message for externally-hosted resources instead of silently
    saving whatever the redirect target actually is (often an HTML release
    page, not a jar). Some external resources do link straight to a .jar
    file though (e.g. a GitHub release asset) — those are still safe to
    download directly from that URL, so only the rest get refused."""
    resource_id = quote(str(resource_id), safe="")
    if pin_version is not None:
        pin_version = quote(str(pin_version), safe="")
    detail = fetch_json(f"https://api.spiget.org/v2/resources/{resource_id}")
    name = detail.get("name") or f"spigot-{resource_id}"
    if detail.get("external"):
        ext_url = (detail.get("file") or {}).get("externalUrl")
        if ext_url and ext_url.lower().split("?")[0].endswith(".jar"):
            ver_id = str((detail.get("version") or {}).get("id") or "")
            return {"url": ext_url, "filename": f"{_safe_filename(name)}.jar", "name": name,
                    "version": ver_id, "versionId": ver_id, "deps": []}
        hint = f" Download it manually from: {ext_url}" if ext_url else (
            f" Download it manually from: https://www.spigotmc.org/resources/{resource_id}/"
        )
        raise RuntimeError(f"'{name}' is hosted externally and can't be auto-installed.{hint}")
    url = (
        f"https://api.spiget.org/v2/resources/{resource_id}/versions/{pin_version}/download"
        if pin_version else
        f"https://api.spiget.org/v2/resources/{resource_id}/download"
    )
    # Spiget exposes no dependency information at all.
    out = {"url": url, "filename": f"{_safe_filename(name)}.jar", "name": name, "deps": []}
    try:
        v = fetch_json(f"https://api.spiget.org/v2/resources/{resource_id}/versions/{pin_version or 'latest'}")
        out["version"], out["versionId"] = v.get("name") or "", str(v.get("id") or "")
    except Exception:
        out["version"], out["versionId"] = "", str(pin_version or "")
    return out


def get_spiget_info(resource_id, limit=25, offset=0):
    import datetime as _dt
    resource_id = quote(str(resource_id), safe="")
    page = offset // limit + 1 if limit else 1
    url = (
        f"https://api.spiget.org/v2/resources/{resource_id}/versions"
        f"?size={limit}&page={page}&sort=-releaseDate"
    )
    data, headers = fetch_json_with_headers(url)
    total = int(headers.get("X-Total") or 0)
    versions = []
    for v in data:
        ts = v.get("releaseDate", 0)
        date = ""
        if ts:
            try:
                date = _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
            except Exception:
                pass
        # Spiget's version-list/detail endpoints don't expose a changelog field.
        versions.append({"id": v["id"], "name": v.get("name", ""), "date": date, "changelog": ""})
    result = {
        "websiteUrl": f"https://www.spigotmc.org/resources/{resource_id}/",
        "versions": versions,
        "hasMoreVersions": offset + len(data) < total,
    }
    if offset == 0:
        # Only needed once — skip on "load more versions" pages. Spiget's
        # description is base64-encoded HTML; decode + strip tags for display.
        import base64
        detail = fetch_json(f"https://api.spiget.org/v2/resources/{resource_id}?fields=description")
        raw = detail.get("description") or ""
        try:
            html_text = base64.b64decode(raw).decode("utf-8", "replace")
            result["longDescription"] = _html_to_text(html_text)
        except Exception:
            result["longDescription"] = ""
    return result


# ─── Unified search / install ─────────────────────────────────────────────────

def search_plugins(args, progress=None):
    platform = args.platform.lower()
    query = getattr(args, "query", "") or ""
    mc_version = getattr(args, "mc_version", None)
    limit = getattr(args, "limit", 20)
    offset = getattr(args, "offset", 0) or 0

    software = getattr(args, "software", None)
    cfg = load_config()
    if getattr(args, "id", None):
        srv = find_server(cfg, args.id)
        if srv:
            if not mc_version:
                mc_version = srv.get("version")
            software = software or srv.get("software")
    software = software or "paper"
    try:
        _check_platform(software, _norm_platform(platform))
    except CLIError as e:
        return fail(e.code, str(e))
    mc_version = _mc_filter(software, mc_version)

    if platform == "modrinth":
        results, has_more = search_modrinth(query, software, mc_version, limit, offset)
    elif platform == "hangar":
        results, has_more = search_hangar(query, mc_version, limit, offset, software)
    elif platform in ("spigotmc", "spiget"):
        results, has_more = search_spiget(query, limit, offset)
    else:
        return fail("unknown_platform", f"Unknown platform '{platform}'. One of: modrinth, hangar, spigotmc")
    return {"results": results, "hasMore": has_more}


def get_plugin_info(args, progress=None):
    platform = args.platform.lower()
    slug = args.slug
    limit = getattr(args, "limit", 25)
    offset = getattr(args, "offset", 0) or 0

    try:
        if platform == "modrinth":
            return get_modrinth_info(slug, limit, offset)
        elif platform == "hangar":
            owner, name = _hangar_owner_slug(slug, getattr(args, "owner", None))
            return get_hangar_info(name, owner, limit, offset)
        elif platform in ("spigotmc", "spiget"):
            return get_spiget_info(slug, limit, offset)
        else:
            return fail("unknown_platform", f"Unknown platform '{platform}'. One of: modrinth, hangar, spigotmc")
    except Exception as e:
        return fail("operation_failed", str(e))


# ─── installing: target, resolution, dependencies ────────────────────────────

PLATFORM_NAMES = {"modrinth": "Modrinth", "hangar": "Hangar", "spigotmc": "SpigotMC"}
PLATFORM_TERMS = {
    "modrinth": "https://modrinth.com/legal/terms",
    "hangar": "https://hangar.papermc.io/terms",
    "spigotmc": "https://www.spigotmc.org/help/terms",
}

# Dependency chains are short in practice; the caps only stop a broken or
# circular index from turning one install into hundreds of downloads.
MAX_DEP_DEPTH = 5
MAX_DEPS = 25


def _norm_platform(platform):
    platform = (platform or "").lower()
    return "spigotmc" if platform == "spiget" else platform


def _kind(software):
    return "mod" if software in MODDED_SOFTWARES else "plugin"


def disclaimer_text(platform, kind):
    site = PLATFORM_NAMES.get(platform, platform)
    return (
        f"You are about to download and run THIRD-PARTY software from {site}. "
        f"{kind.capitalize()}s are not written, reviewed or endorsed by the MCPanel "
        f"developers - {site} only hosts them. A {kind} runs inside your Minecraft "
        f"server with the same permissions as the server itself and can read, change "
        f"or delete anything the server can, including your worlds. The creators of "
        f"MCPanel are not liable for any harm, damage or data loss caused by a {kind}. "
        f"Only install {kind}s from authors you trust."
    )


def _target(args):
    """Where a command acts: {"base", "software", "mcVersion", "dir", "dirName"}
    for -id <server> or --profile-id <profile>. Raises CLIError."""
    import json as _json
    from . import paths
    server_id = getattr(args, "id", None)
    profile_id = getattr(args, "profile_id", None)
    mc_version = getattr(args, "mc_version", None)
    if server_id:
        srv = find_server(load_config(), server_id)
        if not srv:
            raise CLIError("server_not_found", f"Server not found: {server_id}")
        base, software = srv["dir"], srv.get("software", "paper")
        mc_version = mc_version or srv.get("version")
    elif profile_id:
        if not paths.is_valid_id(profile_id):
            raise CLIError("invalid_id", f"Invalid profile id: {profile_id!r}")
        base = os.path.join(paths.PROFILES_DIR, profile_id)
        if not os.path.exists(base):
            raise CLIError("profile_not_found", f"Profile not found: {profile_id}")
        # A profile restricted to exactly one software installs like a server
        # of it would (mods/ for Fabric); anything else gets plugins/.
        software = "paper"
        try:
            with open(os.path.join(base, "profile.json"), "r", encoding="utf-8") as f:
                sw = _json.load(f).get("software")
            if isinstance(sw, list) and len(sw) == 1 and isinstance(sw[0], str):
                software = sw[0]
        except (OSError, ValueError, AttributeError):
            pass
    else:
        raise CLIError("install_target_required")
    if software in NO_PLUGIN_SOFTWARES:
        raise CLIError("plugins_unsupported", "Plugins and mods can't be managed for custom servers")
    mc_version = _mc_filter(software, mc_version)
    dir_name = _dest_dir(software)
    return {"base": base, "software": software, "mcVersion": mc_version,
            "dir": os.path.join(base, dir_name), "dirName": dir_name}


def _resolve(platform, pid, software, mc_version, owner=None, pin=None):
    """Everything needed to download one project and record it: url,
    filename, id/owner/name, version/versionId and its raw required deps."""
    _check_platform(software, platform)
    mc_version = _mc_filter(software, mc_version)
    if platform == "modrinth":
        info = get_modrinth_version_download(pin) if pin else get_modrinth_download(pid, software, mc_version)
        info["id"], info["name"] = pid, pid
        try:
            # Canonical slug + title — dependencies arrive as project ids,
            # and the UI matches installed rows by slug.
            proj = fetch_json(f"https://api.modrinth.com/v2/project/{quote(str(info.get('projectId') or pid), safe='')}")
            info["id"], info["name"] = proj.get("slug") or pid, proj.get("title") or pid
            info["projectId"] = proj.get("id") or info.get("projectId")
        except Exception:
            pass
    elif platform == "hangar":
        owner, name = _hangar_owner_slug(pid, owner)
        info = get_hangar_download(name, owner, mc_version, pin, software)
        info.update(id=name, owner=owner, name=name)
    elif platform == "spigotmc":
        info = get_spiget_download(pid, pin)
        info["id"] = str(pid)
    else:
        raise CLIError("unknown_platform", f"Unknown platform '{platform}'. One of: modrinth, hangar, spigotmc")
    info["platform"] = platform
    info["filename"] = _jar_filename(info.get("filename"), info["id"])
    return info


def _resolve_dep(dep, software, mc_version):
    """_resolve() for a raw dependency, or None when it isn't on the platform."""
    if dep["platform"] == "modrinth":
        pid = dep.get("projectId")
        if not pid:
            pid = fetch_json(f"https://api.modrinth.com/v2/version/{quote(str(dep['versionId']), safe='')}")["project_id"]
        return _resolve("modrinth", pid, software, mc_version, pin=dep.get("versionId"))
    if dep["platform"] == "hangar":
        if dep.get("projectId") is None:
            return None
        ns = fetch_json(f"https://hangar.papermc.io/api/v1/projects/{quote(str(dep['projectId']), safe='')}")["namespace"]
        return _resolve("hangar", ns["slug"], software, mc_version, owner=ns["owner"])
    return None


def _is_present(records, plugin_dir, info):
    """Already installed — from this platform, or the same project from
    another one (ViaVersion from Hangar satisfies a Modrinth dependency on
    ViaVersion; matched by id/name, which is all platforms have in common)."""
    if pluginlist.find(records, info["platform"], info["id"]):
        return True
    if os.path.isfile(os.path.join(plugin_dir, info["filename"])):
        return True
    names = {str(info["id"]).lower(), str(info.get("name") or "").lower()} - {""}
    return any({str(r.get("id", "")).lower(), str(r.get("name", "")).lower()} & names for r in records)


def _plan_dependencies(root, target, records):
    """(to_install, missing): the required dependencies of `root`, recursively,
    minus the ones already installed. Each raw dep gets a "resolvedId" so the
    record can list readable ids instead of Modrinth project ids."""
    plan, missing, seen = [], [], {(root["platform"], str(root["id"]).lower())}
    queue = [(root, 1)]
    while queue:
        info, depth = queue.pop(0)
        for d in info.get("deps") or []:
            key = (d["platform"], str(d.get("projectId") or d.get("versionId") or d.get("name")).lower())
            done = d.get("projectId") and pluginlist.find(records, d["platform"], d["projectId"])
            if done:
                d["resolvedId"] = done["id"]
            if done or key in seen:
                continue
            seen.add(key)
            label = d.get("name") or d.get("projectId") or d.get("versionId")
            if depth > MAX_DEP_DEPTH or len(plan) >= MAX_DEPS:
                missing.append({"name": label, "reason": "dependency chain too long"})
                continue
            try:
                dep = _resolve_dep(d, target["software"], target["mcVersion"])
            except Exception as e:
                missing.append({"name": label, "reason": str(e)})
                continue
            if dep is None:
                missing.append({"name": label, "url": d.get("externalUrl"),
                                "reason": "not hosted on " + PLATFORM_NAMES.get(d["platform"], d["platform"])})
                continue
            d["resolvedId"] = dep["id"]
            dkey = (dep["platform"], str(dep["id"]).lower())
            if dkey in seen and dkey != key:
                continue
            seen.add(dkey)
            if _is_present(records, target["dir"], dep):
                continue
            plan.append(dep)
            queue.append((dep, depth + 1))
    return plan, missing


def _record(info):
    rec = {
        "filename": info["filename"], "platform": info["platform"], "id": info["id"],
        "owner": info.get("owner"), "name": info.get("name") or info["id"],
        "version": info.get("version") or "", "version_id": str(info.get("versionId") or ""),
        "project_id": info.get("projectId"),
        "dependencies": [str(d.get("resolvedId") or d.get("name") or d.get("projectId") or d.get("versionId"))
                         for d in info.get("deps") or []],
    }
    return {k: v for k, v in rec.items() if v is not None}


def _summary(info, target):
    return {"platform": info["platform"], "id": info["id"], "name": info.get("name") or info["id"],
            "version": info.get("version") or "", "filename": info["filename"],
            "path": f"{target['dirName']}/{info['filename']}"}


def _download(info, target, records, progress=None):
    """Download one resolved project, drop the jar of the version it replaces
    and write its .list record."""
    try:
        os.makedirs(target["dir"], exist_ok=True)
    except OSError as e:
        raise CLIError("operation_failed", f"Cannot create {target['dir']}: {e}")
    dest = os.path.join(target["dir"], info["filename"])
    if progress:
        progress(0, f"Downloading {info['filename']}…")
    try:
        download_file(info["url"], dest,
                      (lambda p: progress(p, f"Downloading… {p}%")) if progress else None)
    except Exception as e:
        raise CLIError("download_failed", f"Download failed: {e}")
    old = pluginlist.find(records, info["platform"], info["id"])
    old_file = (old or {}).get("filename") or ""
    # A new version usually has a new file name — both jars would be loaded.
    if old_file and old_file != info["filename"] and os.path.basename(old_file) == old_file:
        try:
            os.remove(os.path.join(target["dir"], old_file))
        except OSError:
            pass
    pluginlist.write(target["dir"], _record(info))
    records[:] = [r for r in records if r is not old] + [pluginlist.read_file(
        pluginlist.record_path(target["dir"], info["platform"], info["id"]))]


def _install_deps(plan, target, records, progress=None):
    installed, failed = [], []
    for dep in plan:
        try:
            _download(dep, target, records, progress)
            installed.append(_summary(dep, target))
        except CLIError as e:
            failed.append({"name": dep.get("name") or dep["id"], "error": e.message})
    return installed, failed


def install_plugin(args, progress=None):
    platform = _norm_platform(args.platform)
    slug = args.slug
    target = _target(args)
    kind = _kind(target["software"])

    try:
        info = _resolve(platform, slug, target["software"], target["mcVersion"],
                        owner=getattr(args, "owner", None), pin=getattr(args, "version", None))
    except CLIError:
        raise
    except Exception as e:
        return fail("plugin_unavailable", str(e))

    records = pluginlist.load(target["dir"])
    plan, missing = ([], []) if getattr(args, "no_deps", False) else _plan_dependencies(info, target, records)
    deps_out = [{"platform": d["platform"], "id": d["id"], "name": d.get("name") or d["id"],
                 "version": d.get("version") or ""} for d in plan]

    if not getattr(args, "yes", False):
        text = disclaimer_text(platform, kind)
        interactive = not getattr(args, "json", False) and sys.stdin.isatty()
        if not interactive:
            return fail("disclaimer_required",
                        f"Installing third-party {kind}s requires accepting the disclaimer - "
                        "rerun with --yes to accept it.",
                        disclaimer=text, tos=PLATFORM_TERMS.get(platform), platform=platform,
                        platformName=PLATFORM_NAMES.get(platform, platform), kind=kind,
                        name=info.get("name") or info["id"],
                        dependencies=deps_out, missingDependencies=missing)
        extra = []
        if deps_out:
            extra = ["", "Also installs these required dependencies:"] + [
                f"  • {d['name']} {d['version']}".rstrip() for d in deps_out]
        if not disclaimers.ask("plugins", text, extra=extra):
            return fail("disclaimer_declined", "Cancelled - the third-party disclaimer was not accepted.")

    _download(info, target, records, progress)
    installed, failed = _install_deps(plan, target, records, progress)

    if progress:
        progress(100, "Done!")

    out = _summary(info, target)
    out.update(success=True, slug=slug, path=os.path.join(target["dir"], info["filename"]),
               relPath=f"{target['dirName']}/{info['filename']}",
               dependencies=installed, failedDependencies=failed, missingDependencies=missing)
    return out


# ─── installed plugins: list / uninstall / update ────────────────────────────

# Jars put into plugins/ or mods/ some other way (by hand, the file manager,
# a profile) have no .list record. They're listed as platform "local": they
# can be uninstalled, but MCPanel doesn't know where they came from, so they
# can't be updated.
LOCAL = "local"


def _yml_value(text, key):
    """A top-level `key: value` from plugin.yml, without a YAML parser."""
    m = re.search(rf"^{key}:[ \t]*(.+?)[ \t]*$", text, re.M)
    if not m:
        return None
    v = m.group(1).split(" #")[0].strip()
    return v[1:-1] if len(v) > 1 and v[0] == v[-1] and v[0] in "'\"" else v or None


def _jar_metadata(path):
    """(name, version) from a plugin/mod jar's descriptor, or (None, None)."""
    import json as _json
    import zipfile
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            for desc in ("paper-plugin.yml", "plugin.yml", "bungee.yml"):
                if desc in names:
                    text = z.read(desc).decode("utf-8", "replace")
                    return _yml_value(text, "name"), _yml_value(text, "version")
            for desc in ("velocity-plugin.json", "fabric.mod.json", "quilt.mod.json"):
                if desc in names:
                    d = _json.loads(z.read(desc).decode("utf-8", "replace"))
                    if desc == "quilt.mod.json":
                        ql = d.get("quilt_loader") or {}
                        d = {**ql, **(ql.get("metadata") or {})}
                    return d.get("name") or d.get("id"), d.get("version")
    except Exception:
        pass
    return None, None


def list_plugins(args, progress=None):
    target = _target(args)
    recs = pluginlist.load(target["dir"])
    tracked = set()
    for r in recs:
        r["path"] = f"{target['dirName']}/{r['filename']}"
        tracked.add(r["filename"])
    try:
        jars = sorted(f for f in os.listdir(target["dir"])
                      if f.lower().endswith(".jar") and os.path.isfile(os.path.join(target["dir"], f)))
    except OSError:
        jars = []
    for f in jars:
        if f in tracked:
            continue
        name, version = _jar_metadata(os.path.join(target["dir"], f))
        recs.append({"platform": LOCAL, "id": f, "filename": f, "name": name or os.path.splitext(f)[0],
                     "version": version or "", "dependencies": [], "path": f"{target['dirName']}/{f}"})
    return {"kind": _kind(target["software"]), "dir": target["dirName"], "plugins": recs}


def _installed_record(args, target):
    platform = _norm_platform(args.platform)
    if platform == LOCAL:
        fname = args.slug
        if os.path.basename(fname) != fname or not fname.lower().endswith(".jar") \
                or not os.path.isfile(os.path.join(target["dir"], fname)):
            raise CLIError("plugin_not_installed", f"No {fname!r} in {target['dirName']}/")
        return {"platform": LOCAL, "id": fname, "filename": fname, "name": fname}
    rec = pluginlist.find(pluginlist.load(target["dir"]), platform, args.slug)
    if not rec:
        raise CLIError("plugin_not_installed",
                       f"'{args.slug}' from {PLATFORM_NAMES.get(platform, platform)} isn't installed here "
                       "(only plugins/mods installed through MCPanel are tracked).")
    return rec


def uninstall_plugin(args, progress=None):
    target = _target(args)
    rec = _installed_record(args, target)
    fname = rec["filename"]
    if os.path.basename(fname) != fname:
        return fail("invalid_path", f"Refusing to delete {fname!r}")
    try:
        os.remove(os.path.join(target["dir"], fname))
    except FileNotFoundError:
        pass
    except OSError as e:
        return fail("operation_failed", f"Cannot delete {fname}: {e}")
    pluginlist.remove(target["dir"], rec)
    return {"success": True, "platform": rec["platform"], "id": rec["id"], "name": rec.get("name") or rec["id"],
            "filename": fname, "path": f"{target['dirName']}/{fname}"}


def _update_one(rec, target, records, progress=None):
    """Update one record to the newest compatible version (and pull in any
    dependency it newly requires). Returns a result dict."""
    base = {"platform": rec["platform"], "id": rec["id"], "name": rec.get("name") or rec["id"],
            "previousVersion": rec.get("version") or ""}
    try:
        info = _resolve(rec["platform"], rec["id"], target["software"], target["mcVersion"], owner=rec.get("owner"))
    except Exception as e:
        return dict(base, error=getattr(e, "message", None) or str(e))
    same = (info.get("versionId") and str(info["versionId"]) == str(rec.get("version_id") or "")
            and info["filename"] == rec["filename"])
    if same:
        return dict(base, unchanged=True, version=rec.get("version") or "")
    plan, missing = _plan_dependencies(info, target, records)
    try:
        _download(info, target, records, progress)
    except CLIError as e:
        return dict(base, error=e.message)
    installed, failed = _install_deps(plan, target, records, progress)
    return dict(base, success=True, version=info.get("version") or "", filename=info["filename"],
                path=f"{target['dirName']}/{info['filename']}",
                dependencies=installed, failedDependencies=failed, missingDependencies=missing)


def update_plugin(args, progress=None):
    target = _target(args)
    records = pluginlist.load(target["dir"])
    if getattr(args, "all", False):
        results = [_update_one(r, target, records, progress) for r in list(records)]
        return {"updated": [r for r in results if r.get("success")],
                "unchanged": [r for r in results if r.get("unchanged")],
                "failed": [r for r in results if r.get("error")]}
    if not getattr(args, "platform", None) or not getattr(args, "slug", None):
        return fail("invalid_arguments", "Give <platform> <id> of the plugin/mod to update, or --all.")
    rec = _installed_record(args, target)
    if rec["platform"] == LOCAL:
        return fail("plugin_unavailable", f"{rec['filename']} wasn't installed through MCPanel, so it can't be "
                                          "updated - it's unknown where it came from.")
    result = _update_one(rec, target, records, progress)
    if result.get("error"):
        return fail("plugin_unavailable", result["error"], **{k: v for k, v in result.items() if k != "error"})
    if result.get("unchanged"):
        result["message"] = f"{result['name']} is already up to date ({result['version'] or 'latest'})."
    return result
