#!/usr/bin/env python3
r"""
Auto-updater for Flowseal/zapret-discord-youtube.

One invocation = one check:
  1. Read the latest version from the repo's version.txt (the same source
     zapret itself uses — no GitHub API, no token, no rate limit).
  2. Read the installed version from <install_path>\service.bat (LOCAL_VERSION).
  3. If they differ -> stop zapret (kill winws.exe + WinDivert service),
     wipe the install folder (keeping user files), unpack the new zip,
     and restart the same config that was running.

Overlapping runs are prevented by a single-instance lock (see acquire_lock).

Designed to be triggered hourly by Task Scheduler (see install_task.ps1),
but can also be run manually:  python updater.py  [--force] [--config PATH]
"""

import argparse
import ctypes
import fnmatch
import json
import logging
import msvcrt
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from ctypes import wintypes
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")
STATE_PATH = os.path.join(SCRIPT_DIR, "state.json")
LOG_PATH = os.path.join(SCRIPT_DIR, "updater.log")
LOCK_PATH = os.path.join(SCRIPT_DIR, "updater.lock")

# --- Repo identity (this tool targets exactly one project; not "config") ---
REPO = "Flowseal/zapret-discord-youtube"
VERSION_URL = "https://raw.githubusercontent.com/%s/main/.service/version.txt" % REPO
ASSET_NAME_TEMPLATE = "zapret-discord-youtube-{version}.zip"
REQUEST_TIMEOUT_SEC = 30

USER_AGENT = "zapret-auto-updater/1.0 (+https://github.com/Flowseal/zapret-discord-youtube)"
WINDOW_TITLE_PREFIX = "zapret: "

log = logging.getLogger("zapret-updater")


# --------------------------------------------------------------------------- #
# Setup helpers
# --------------------------------------------------------------------------- #
def setup_logging():
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")

    fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)

    # Console handler is only useful when launched from a terminal.
    if sys.stdout is not None:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        log.addHandler(sh)


def load_config(path=CONFIG_PATH):
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    if not cfg.get("install_path"):
        raise ValueError("config.json: 'install_path' is required")
    cfg.setdefault("preserve_globs", [])
    cfg.setdefault("default_config", None)
    cfg.setdefault("restart_after_update", True)
    return cfg


def load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state):
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
    except OSError as e:
        log.warning("Could not write state file: %s", e)


# --------------------------------------------------------------------------- #
# Single-instance lock
# --------------------------------------------------------------------------- #
def acquire_lock():
    """Prevent overlapping runs (the hourly tick firing while a previous run
    is mid-update, or a manual run racing the scheduled task — exactly the
    window where two processes could wipe/extract the same folder at once).

    Returns the open lock-file handle to keep, or None if another instance
    already holds it. Uses an OS-level byte-range lock, so it is released
    automatically when this process exits — even on crash — with no stale
    lock file to clean up."""
    f = open(LOCK_PATH, "w")
    try:
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        f.close()
        return None
    return f


def release_lock(f):
    try:
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    f.close()


# --------------------------------------------------------------------------- #
# GitHub
# --------------------------------------------------------------------------- #
def _http_get(url, headers, timeout, binary=False):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    return data if binary else data.decode("utf-8")


def get_latest_release():
    """Return (version, download_url, asset_name) for the latest release.

    Mirrors exactly what zapret itself uses: a plain version.txt on
    raw.githubusercontent.com (no API, no token, no rate limit) plus the
    deterministic release-download URL built from that version."""
    version = _http_get(
        VERSION_URL,
        {"User-Agent": USER_AGENT, "Cache-Control": "no-cache"},
        REQUEST_TIMEOUT_SEC,
    ).strip()
    if not version:
        raise RuntimeError("version.txt was empty")
    name = ASSET_NAME_TEMPLATE.format(version=version)
    url = "https://github.com/%s/releases/download/%s/%s" % (REPO, version, name)
    return version, url, name


def download(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SEC) as resp, \
            open(dest, "wb") as out:
        shutil.copyfileobj(resp, out, length=1024 * 256)
    log.info("Downloaded %.1f MB -> %s", os.path.getsize(dest) / 1e6, os.path.basename(dest))


# --------------------------------------------------------------------------- #
# Installed version
# --------------------------------------------------------------------------- #
def read_installed_version(install_path):
    """Parse LOCAL_VERSION from service.bat. None if not installed."""
    service_bat = os.path.join(install_path, "service.bat")
    try:
        with open(service_bat, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except OSError:
        return None
    m = re.search(r'set\s+"?LOCAL_VERSION=([^"\r\n]+)"?', content, re.IGNORECASE)
    return m.group(1).strip() if m else None


# --------------------------------------------------------------------------- #
# Detecting / stopping / starting zapret
# --------------------------------------------------------------------------- #
def winws_running():
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq winws.exe", "/NH"],
            capture_output=True, text=True, timeout=20,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "winws.exe" in out.lower()


def _enum_window_titles():
    titles = []
    user32 = ctypes.windll.user32
    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _lparam):
        length = user32.GetWindowTextLengthW(hwnd)
        if length > 0:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            if buf.value:
                titles.append(buf.value)
        return True

    user32.EnumWindows(EnumProc(cb), 0)
    return titles


def _config_from_title(title):
    """Extract the config base name from a 'zapret: <name>' window title.
    Returns the name (e.g. 'general (ALT)') or None if it doesn't match."""
    if title and title.startswith(WINDOW_TITLE_PREFIX):
        name = title[len(WINDOW_TITLE_PREFIX):].strip()
        if name:
            return name
    return None


def detect_running_config():
    """Return the config base name from the winws window title
    'zapret: <name>', or None if not found."""
    try:
        for title in _enum_window_titles():
            name = _config_from_title(title)
            if name:
                return name
    except Exception as e:  # ctypes quirks shouldn't crash the updater
        log.warning("Window enumeration failed: %s", e)
    return None


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30)


def stop_zapret():
    """Kill winws.exe (no graceful shutdown) and free the WinDivert driver
    so the binaries/.sys can be overwritten. Best-effort."""
    log.info("Stopping zapret (taskkill winws.exe + WinDivert service)...")
    _run(["taskkill", "/F", "/IM", "winws.exe", "/T"])
    for svc in ("WinDivert", "WinDivert14"):
        _run(["net", "stop", svc])
        _run(["sc", "delete", svc])
    # Give the OS a moment to release file handles / unload the driver.
    time.sleep(2)


def launch_config(install_path, config_name):
    bat = os.path.join(install_path, config_name + ".bat")
    if not os.path.isfile(bat):
        log.warning("Cannot restart: '%s' not found", bat)
        return False
    log.info("Restarting zapret with '%s.bat'", config_name)
    CREATE_NEW_CONSOLE = 0x00000010
    subprocess.Popen(["cmd", "/c", bat], cwd=install_path,
                     creationflags=CREATE_NEW_CONSOLE)
    return True


# --------------------------------------------------------------------------- #
# Filesystem update
# --------------------------------------------------------------------------- #
def looks_like_zapret_dir(path):
    """Guard against wiping the wrong folder if install_path is misconfigured."""
    if not os.path.isdir(path):
        return True  # fresh install, nothing to lose
    if not os.listdir(path):
        return True  # empty
    return any(os.path.exists(os.path.join(path, m))
               for m in ("service.bat", "bin", "general.bat"))


def backup_preserved(install_path, globs, backup_dir):
    saved = []
    for pattern in globs:
        norm = pattern.replace("/", os.sep)
        for root, _dirs, files in os.walk(install_path):
            for fname in files:
                full = os.path.join(root, fname)
                rel = os.path.relpath(full, install_path)
                if fnmatch.fnmatch(rel.replace(os.sep, "/"), pattern) or \
                        fnmatch.fnmatch(rel, norm):
                    dst = os.path.join(backup_dir, rel)
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copy2(full, dst)
                    saved.append(rel)
    if saved:
        log.info("Preserved %d user file(s): %s", len(saved), ", ".join(saved))
    return saved


def restore_preserved(install_path, backup_dir, saved):
    for rel in saved:
        src = os.path.join(backup_dir, rel)
        dst = os.path.join(install_path, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
    if saved:
        log.info("Restored %d user file(s)", len(saved))


def _force_remove(path, attempts=3):
    for i in range(attempts):
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.chmod(path, 0o777)
                os.remove(path)
            return
        except OSError as e:
            if i == attempts - 1:
                raise
            log.warning("Delete retry for %s (%s)", path, e)
            time.sleep(1)


def wipe_dir_contents(path):
    for entry in os.listdir(path):
        _force_remove(os.path.join(path, entry))
    log.info("Wiped contents of %s", path)


def extract_into(zip_path, install_path):
    import zipfile
    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(tmp)
            names = [n for n in zf.namelist() if n.strip("/")]
        # Detect a single top-level wrapper folder.
        tops = {n.split("/")[0] for n in names}
        src_root = tmp
        if len(tops) == 1:
            candidate = os.path.join(tmp, tops.pop())
            if os.path.isdir(candidate):
                src_root = candidate
        for item in os.listdir(src_root):
            s = os.path.join(src_root, item)
            d = os.path.join(install_path, item)
            if os.path.isdir(s):
                shutil.copytree(s, d, dirs_exist_ok=True)
            else:
                shutil.copy2(s, d)
    log.info("Extracted new version into %s", install_path)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def perform_update(cfg, version, url, asset_name, was_running, restart_config):
    install_path = cfg["install_path"]

    if not looks_like_zapret_dir(install_path):
        raise RuntimeError(
            "Refusing to wipe %r: it is not empty and does not look like a "
            "zapret folder (no service.bat/bin/general.bat). Check install_path "
            "in config.json." % install_path)

    work = tempfile.mkdtemp(prefix="zapret_upd_")
    backup_dir = os.path.join(work, "preserve")
    os.makedirs(backup_dir, exist_ok=True)
    zip_path = os.path.join(work, asset_name)
    try:
        download(url, zip_path)

        if was_running:
            stop_zapret()

        os.makedirs(install_path, exist_ok=True)
        saved = backup_preserved(install_path, cfg["preserve_globs"], backup_dir)
        wipe_dir_contents(install_path)
        extract_into(zip_path, install_path)
        restore_preserved(install_path, backup_dir, saved)

        log.info("Updated to %s", version)

        if was_running and cfg["restart_after_update"]:
            if restart_config:
                launch_config(install_path, restart_config)
            else:
                log.warning("zapret was running but no config known to restart "
                            "with (set 'default_config' in config.json).")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description="Auto-updater for zapret-discord-youtube")
    parser.add_argument("--force", action="store_true",
                       help="reinstall the latest version even if already current")
    parser.add_argument("--config", default=CONFIG_PATH, help="path to config.json")
    args = parser.parse_args()

    setup_logging()

    lock = acquire_lock()
    if lock is None:
        log.info("Another updater run is in progress; skipping this one.")
        return 0
    try:
        return _check_and_update(args)
    finally:
        release_lock(lock)


def _check_and_update(args):
    try:
        cfg = load_config(args.config)
    except Exception as e:
        log.error("Config error: %s", e)
        return 2

    log.info("=== check started ===")
    state = load_state()

    # Remember which config is running so we can restart the same one later.
    running_cfg = detect_running_config()
    if running_cfg:
        state["last_config"] = running_cfg
        log.info("Running config detected: %s", running_cfg)

    try:
        version, url, asset_name = get_latest_release()
    except Exception as e:
        log.error("Could not fetch latest release: %s", e)
        return 1
    log.info("Latest release: %s (%s)", version, asset_name)

    installed = read_installed_version(cfg["install_path"])
    log.info("Installed version: %s", installed or "(none)")

    if installed == version and not args.force:
        log.info("Already up to date. Nothing to do.")
        save_state(state)
        return 0

    restart_config = state.get("last_config") or cfg["default_config"]
    was_running = winws_running()
    log.info("Update needed (%s -> %s). winws running: %s",
             installed or "none", version, was_running)

    try:
        perform_update(cfg, version, url, asset_name, was_running, restart_config)
    except Exception as e:
        log.exception("Update FAILED: %s", e)
        return 1

    state["last_known_version"] = version
    state["last_update_utc"] = datetime.now(timezone.utc).isoformat()
    save_state(state)
    log.info("=== check finished ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
