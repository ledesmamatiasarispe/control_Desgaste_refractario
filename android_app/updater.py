"""
Auto-updater: descarga e instala el APK del último release de GitHub.

Flujo:
  1. Al arrancar, GET /repos/<owner>/<repo>/releases/latest  (GitHub API)
  2. Comparar tag_name con VERSION local
  3. Si hay versión nueva: descargar el APK asset al directorio de caché
  4. Lanzar el intent de instalación del sistema (reemplaza la app)

Requiere (buildozer.spec):
  android.permissions = REQUEST_INSTALL_PACKAGES, INTERNET
  p4a.hook = p4a_hook.py          ← agrega <provider> FileProvider al manifest
"""

import io
import logging
import os
import pathlib
import sys
import threading
from dataclasses import dataclass
from typing import Callable, Optional

import requests

from version import GITHUB_REPO, GITHUB_BRANCH, VERSION

log = logging.getLogger(__name__)

_API_LATEST   = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
_APPLIED_FILE = pathlib.Path(os.environ.get("TMPDIR", "/tmp")) / ".refractory_applied_version"


def inject_update_path():
    """No-op: this app installs updates via system installer, no hot-patching."""
    pass


def get_applied_version() -> Optional[str]:
    """Return version string if an update was applied this session, else None."""
    try:
        return _APPLIED_FILE.read_text().strip() or None
    except Exception:
        return None
_TIMEOUT_API = 10   # seconds
_TIMEOUT_DL  = 120  # seconds for APK download

# ── helpers ───────────────────────────────────────────────────────────────────

def _apk_cache_dir() -> pathlib.Path:
    """Writable directory: app cache on Android, local temp on desktop."""
    if sys.platform == "android":
        # Android: app's private cache — no storage permission needed
        try:
            from jnius import autoclass
            ctx = autoclass("org.kivy.android.PythonActivity").mActivity
            return pathlib.Path(ctx.getCacheDir().getAbsolutePath())
        except Exception:
            pass
    return pathlib.Path(os.environ.get("TMPDIR", "/tmp"))


# ── public API ────────────────────────────────────────────────────────────────

@dataclass
class UpdateInfo:
    remote_version: str
    current_version: str
    apk_url:         str
    apk_name:        str
    release_notes:   str
    is_newer:        bool


def check_in_background(on_update_available: Callable[[UpdateInfo], None]):
    """Non-blocking: check GitHub in a daemon thread, call callback if newer."""
    t = threading.Thread(
        target=_bg_check, args=(on_update_available,), daemon=True
    )
    t.start()


def download_and_install(
    info: UpdateInfo,
    progress_cb: Optional[Callable[[int, str], None]] = None,
):
    """Download APK and trigger system installer. Runs synchronously — call from a thread."""
    def cb(pct, msg):
        log.info(f"[update {pct}%] {msg}")
        if progress_cb:
            progress_cb(pct, msg)

    cb(0, "Descargando APK…")
    apk_path = _download_apk(info.apk_url, info.apk_name, cb)
    if apk_path is None:
        cb(100, "Error al descargar el APK.")
        return False

    cb(100, "Lanzando instalador…")
    _install_apk(str(apk_path))
    return True


# ── internals ─────────────────────────────────────────────────────────────────

def _bg_check(callback: Callable[[UpdateInfo], None]):
    info = _fetch_release_info()
    if info and info.is_newer:
        log.info(f"Update available: {info.current_version} → {info.remote_version}")
        callback(info)
    elif info:
        log.info(f"Up to date ({info.current_version})")
    else:
        log.warning("Could not reach GitHub releases API")


def _fetch_release_info() -> Optional[UpdateInfo]:
    try:
        r = requests.get(_API_LATEST, timeout=_TIMEOUT_API,
                         headers={"Accept": "application/vnd.github+json"})
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.warning(f"GitHub API error: {e}")
        return None

    remote_ver = data.get("tag_name", "").lstrip("v")
    if not remote_ver:
        return None

    # Find the APK asset
    apk_url = apk_name = ""
    for asset in data.get("assets", []):
        if asset["name"].endswith(".apk"):
            apk_url  = asset["browser_download_url"]
            apk_name = asset["name"]
            break

    if not apk_url:
        log.warning("No APK asset found in latest release")
        return None

    return UpdateInfo(
        remote_version  = remote_ver,
        current_version = VERSION,
        apk_url         = apk_url,
        apk_name        = apk_name,
        release_notes   = data.get("body", ""),
        is_newer        = _version_gt(remote_ver, VERSION),
    )


def _download_apk(
    url: str,
    filename: str,
    cb: Callable[[int, str], None],
) -> Optional[pathlib.Path]:
    dest = _apk_cache_dir() / filename
    try:
        r = requests.get(url, stream=True, timeout=_TIMEOUT_DL)
        r.raise_for_status()

        total = int(r.headers.get("content-length", 0))
        done  = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=131072):   # 128 KB chunks
                f.write(chunk)
                done += len(chunk)
                if total:
                    pct = int(done / total * 95)
                    mb  = done / 1_048_576
                    cb(pct, f"Descargando… {mb:.1f} MB")

        log.info(f"APK saved to {dest} ({done} bytes)")
        return dest
    except Exception as e:
        log.error(f"APK download failed: {e}")
        return None


def _install_apk(apk_path: str):
    """Trigger Android package installer for the downloaded APK."""
    if sys.platform != "android":
        log.info(f"[desktop] Would install APK: {apk_path}")
        return

    try:
        from jnius import autoclass

        PythonActivity = autoclass("org.kivy.android.PythonActivity")
        context        = PythonActivity.mActivity

        Intent   = autoclass("android.content.Intent")
        File     = autoclass("java.io.File")
        Build    = autoclass("android.os.Build")

        apk_file = File(apk_path)

        # Android 7+ (API 24) requires FileProvider content:// URI
        if Build.VERSION.SDK_INT >= 24:
            FileProvider = autoclass("androidx.core.content.FileProvider")
            authority    = f"{context.getPackageName()}.fileprovider"
            apk_uri      = FileProvider.getUriForFile(context, authority, apk_file)
        else:
            Uri     = autoclass("android.net.Uri")
            apk_uri = Uri.fromFile(apk_file)

        intent = Intent(Intent.ACTION_VIEW)
        intent.setDataAndType(
            apk_uri, "application/vnd.android.package-archive"
        )
        intent.addFlags(
            Intent.FLAG_ACTIVITY_NEW_TASK
            | Intent.FLAG_GRANT_READ_URI_PERMISSION
        )
        context.startActivity(intent)
        log.info("Installer launched")

    except Exception as e:
        log.error(f"Could not launch installer: {e}")
        # Fallback: open download URL in browser so user can install manually
        _open_url_fallback(f"https://github.com/{GITHUB_REPO}/releases/latest")


def _open_url_fallback(url: str):
    try:
        from jnius import autoclass
        Intent = autoclass("android.content.Intent")
        Uri    = autoclass("android.net.Uri")
        PythonActivity = autoclass("org.kivy.android.PythonActivity")
        intent = Intent(Intent.ACTION_VIEW, Uri.parse(url))
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        PythonActivity.mActivity.startActivity(intent)
    except Exception as e:
        log.error(f"URL fallback failed: {e}")


def _version_gt(a: str, b: str) -> bool:
    """Returns True if version a > version b (semver: 1.2.3)."""
    try:
        return tuple(int(x) for x in a.split(".")) > \
               tuple(int(x) for x in b.split("."))
    except Exception:
        return a != b
