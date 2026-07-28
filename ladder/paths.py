"""Where Forts is installed, and which account is playing.

A hardcoded install path works on exactly one machine. Steam sits on a
second drive for plenty of people, so this searches: the FORTS_DIR
environment variable first, then the Steam path from the registry plus every
library in steamapps/libraryfolders.vdf, then the usual defaults.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

APP_ID = 410900

_DEFAULTS = (
    r"C:\Program Files (x86)\Steam\steamapps\common\Forts",
    r"C:\Program Files\Steam\steamapps\common\Forts",
    r"D:\SteamLibrary\steamapps\common\Forts",
    r"E:\SteamLibrary\steamapps\common\Forts",
)


def _looks_like_forts(p: Path) -> bool:
    """Only a directory with both Forts.exe and data/ is an installation."""
    return (p / "Forts.exe").exists() and (p / "data").is_dir()


def _steam_root() -> Path | None:
    try:
        import winreg
        for hive, key in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
                          (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam")):
            try:
                with winreg.OpenKey(hive, key) as k:
                    for value in ("SteamPath", "InstallPath"):
                        try:
                            p = Path(winreg.QueryValueEx(k, value)[0])
                            if p.is_dir():
                                return p
                        except FileNotFoundError:
                            continue
            except OSError:
                continue
    except ImportError:          # not Windows
        pass
    return None


def _library_folders(steam: Path) -> list[Path]:
    """Read steamapps/libraryfolders.vdf.

    Parsed with a regex rather than a VDF library: exactly one field is
    needed, and a dependency for three lines is a bad trade in a project
    other people have to install.
    """
    vdf = steam / "steamapps" / "libraryfolders.vdf"
    if not vdf.exists():
        return []
    try:
        text = vdf.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [Path(m) for m in re.findall(r'"path"\s+"([^"]+)"', text)]


@lru_cache(maxsize=1)
def find_forts_dir() -> Path | None:
    env = os.environ.get("FORTS_DIR")
    if env:
        p = Path(env)
        # A set-but-wrong variable is a user error and is not silently
        # ignored.
        return p if _looks_like_forts(p) else None

    steam = _steam_root()
    roots: list[Path] = []
    if steam:
        roots.append(steam)
        roots.extend(_library_folders(steam))
    for root in roots:
        cand = Path(str(root).replace("\\\\", "\\")) / "steamapps" / "common" / "Forts"
        if _looks_like_forts(cand):
            return cand

    for d in _DEFAULTS:
        p = Path(d)
        if _looks_like_forts(p):
            return p
    return None


def forts_dir_or_die() -> Path:
    p = find_forts_dir()
    if p is None:
        raise SystemExit(
            "Forts not found.\n"
            "Set the path explicitly, for example:\n"
            '    set FORTS_DIR=D:\\SteamLibrary\\steamapps\\common\\Forts')
    return p


def user_dirs(forts: Path | None = None) -> list[Path]:
    """All account directories (`users/<steamid64>`), newest log first."""
    forts = forts or find_forts_dir()
    if forts is None:
        return []
    dirs = [d for d in (forts / "users").glob("*")
            if d.is_dir() and re.fullmatch(r"\d{17}", d.name)]

    def logtime(d: Path) -> float:
        log = d / "log.txt"
        try:
            return log.stat().st_mtime
        except OSError:
            return 0.0

    return sorted(dirs, key=logtime, reverse=True)


def active_user_dir(forts: Path | None = None) -> Path | None:
    """The most recently active account, decided by the newest log."""
    dirs = user_dirs(forts)
    return dirs[0] if dirs else None
