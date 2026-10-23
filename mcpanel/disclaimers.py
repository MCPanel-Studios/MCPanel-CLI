"""Interactive third-party disclaimers, and how long an answer counts.

Both `mcpanel mclib` (addons) and `mcpanel install plugin|mod` ask before
downloading third-party code. Accepting with `y` isn't asked again for
SNOOZE_SECONDS; `a` ("always") hides that disclaimer for good. The answers are
kept in <userData>/disclaimers.json, per topic ("addons", "plugins").

Only a human at a terminal is ever covered by this: the `api` surface always
needs an explicit `--yes`, and the desktop app / WebUI keep their own
"don't show again" state for their dialogs.
"""

import json
import os
import textwrap
import time

from . import paths

SNOOZE_SECONDS = 15 * 60
_FILE = "disclaimers.json"


def _path():
    return os.path.join(paths.USER_DATA, _FILE)


def _load():
    try:
        with open(_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data):
    try:
        os.makedirs(paths.USER_DATA, exist_ok=True)
        tmp = f"{_path()}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, _path())
    except OSError:
        pass


def accepted(topic):
    """True while an earlier answer still covers `topic`."""
    rec = _load().get(topic)
    if not isinstance(rec, dict):
        return False
    return bool(rec.get("hidden")) or float(rec.get("until") or 0) > time.time()


def remember(topic, forever=False):
    data = _load()
    data[topic] = {"hidden": True} if forever else {"until": time.time() + SNOOZE_SECONDS}
    _save(data)


def ask(topic, text, heading="THIRD-PARTY SOFTWARE", extra=None, input_fn=input, style=None):
    """Show `text` and ask y/N/a. Skipped (True) while a previous answer is
    still valid. `extra` lines (e.g. the dependencies that will be installed)
    are printed after the disclaimer — they're only informational, so a
    remembered answer skips them too. The TUI passes its own `input_fn` and
    (heading, body) `style` colour functions."""
    if accepted(topic):
        return True
    head_style, body_style = style or (str, str)
    print()
    print(head_style(f"  ⚠  {heading}"))
    for para in text.split("\n"):
        for line in textwrap.wrap(para, 76) or [""]:
            print(body_style("  " + line))
    for line in extra or []:
        print("  " + line)
    print()
    try:
        answer = (input_fn("  Accept and continue? [y/N/a = always, don't ask again] ") or "").strip().lower()
    except EOFError:
        return False
    if answer in ("a", "always"):
        remember(topic, forever=True)
        return True
    if answer in ("y", "yes"):
        remember(topic)
        return True
    return False
