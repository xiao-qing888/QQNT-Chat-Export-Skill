#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared helpers for the QQNT chat export toolchain.

This module is deliberately standard-library only.  Every other script in the
project imports it (including the dependency self-check), so importing it must
never fail because of a missing third-party package.

Contents
--------
* console/permission setup helpers
* Windows shell folder resolution (Desktop / Documents, OneDrive aware)
* Tencent Files & QQ account detection
* QQ NT installation detection (registry + common install roots)
* QQNT_Export lookup
* encryption key normalisation + SQLCipher PRAGMA key variants
* small formatting helpers
"""

from __future__ import annotations

import ctypes
import dataclasses
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

APP_NAME = "QQNT-chat-export"
VERSION = "3.0.0"

SQLITE_MAGIC = b"SQLite format 3\x00"

#: name of the folder that holds decrypted databases under the work root
DECRYPTED_PREFIX = "decrypted_"

__all__ = [
    "APP_NAME",
    "VERSION",
    "SQLITE_MAGIC",
    "DECRYPTED_PREFIX",
    "ToolError",
    "Account",
    "QQInstall",
    "KeySpec",
    "setup_console",
    "known_folder",
    "user_desktop_dir",
    "user_documents_dir",
    "work_root",
    "decrypted_dir",
    "tencent_files_dir",
    "list_accounts",
    "detect_active_account",
    "detect_qq_install",
    "find_qqnt_export",
    "normalize_key",
    "key_pragma_candidates",
    "parse_qq_number",
    "unix_to_datetime",
    "human_bytes",
    "describe_python",
]


class ToolError(RuntimeError):
    """Fatal, user-facing error; the message is already actionable."""


# ---------------------------------------------------------------------------
# console / python
# ---------------------------------------------------------------------------
def setup_console(utf8: bool = False) -> None:
    """Make console output never crash on characters the code page lacks."""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        try:
            if utf8:
                stream.reconfigure(encoding="utf-8", errors="replace")
            else:
                stream.reconfigure(errors="replace")
        except (AttributeError, OSError, ValueError):
            pass


def describe_python() -> str:
    return f"Python {sys.version.split()[0]} ({sys.executable})"


# ---------------------------------------------------------------------------
# Windows shell folders
# ---------------------------------------------------------------------------
_FOLDER_IDS = {
    "desktop": "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}",
    "documents": "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}",
}


class _GUID(ctypes.Structure):
    _fields_ = [
        ("data1", ctypes.c_uint32),
        ("data2", ctypes.c_uint16),
        ("data3", ctypes.c_uint16),
        ("data4", ctypes.c_ubyte * 8),
    ]


def known_folder(name: str) -> "Path | None":
    """Resolve a Windows known folder (handles OneDrive redirection)."""
    guid_text = _FOLDER_IDS.get(name)
    if not guid_text or sys.platform != "win32":
        return None
    try:
        guid = _GUID()
        if ctypes.windll.ole32.CLSIDFromString(
            ctypes.c_wchar_p(guid_text), ctypes.byref(guid)
        ) != 0:
            return None
        shell32 = ctypes.windll.shell32
        shell32.SHGetKnownFolderPath.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        shell32.SHGetKnownFolderPath.restype = ctypes.c_long
        out = ctypes.c_void_p()
        hr = shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(out))
        if hr != 0 or not out.value:
            return None
        try:
            text = ctypes.wstring_at(out.value)
        finally:
            ctypes.windll.ole32.CoTaskMemFree(out)
        return Path(text) if text else None
    except Exception:
        return None


def _folder(env_var: str, known: str, fallback: str) -> Path:
    override = os.environ.get(env_var)
    if override:
        return Path(override).expanduser()
    resolved = known_folder(known)
    if resolved:
        return resolved
    return Path.home() / fallback


def user_desktop_dir() -> Path:
    return _folder("QQNT_DESKTOP_DIR", "desktop", "Desktop")


def user_documents_dir() -> Path:
    return _folder("QQNT_DOCUMENTS_DIR", "documents", "Documents")


def work_root() -> Path:
    """Directory that holds ``QQNT_Export`` and the decrypted databases."""
    override = os.environ.get("QQNT_OUTPUT_DIR")
    if override:
        return Path(override).expanduser()
    return user_desktop_dir() / "QQNT_output"


def decrypted_dir(account: "str | int", root: "Path | str | None" = None) -> Path:
    base = Path(root) if root else work_root()
    return base / f"{DECRYPTED_PREFIX}{account}"


# ---------------------------------------------------------------------------
# Tencent Files & accounts
# ---------------------------------------------------------------------------
def tencent_files_dir(explicit: "str | Path | None" = None) -> Path:
    """Locate ``Documents\\Tencent Files`` (raises ToolError when absent)."""
    attempts = []
    for candidate in (
        explicit,
        os.environ.get("QQ_TENCENT_FILES"),
        user_documents_dir() / "Tencent Files",
        Path.home() / "Documents" / "Tencent Files",
    ):
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        attempts.append(str(path))
        if path.is_dir():
            return path
    raise ToolError(
        "找不到 QQ 数据目录（Tencent Files）。已尝试:\n  "
        + "\n  ".join(attempts)
        + "\n请用 --tencent-files <目录> 或环境变量 QQ_TENCENT_FILES 指定。"
    )


@dataclasses.dataclass(frozen=True)
class Account:
    """A QQ account directory with a local message database."""

    number: str
    db_dir: Path
    msg_db: Path
    mtime: float

    @property
    def nt_qq_dir(self) -> Path:
        return self.db_dir.parent.parent

    @property
    def last_active(self) -> str:
        return datetime.fromtimestamp(self.mtime).strftime("%Y-%m-%d %H:%M:%S")


def _db_activity(msg_db: Path) -> float:
    """Newest mtime of the db plus its WAL/SHM sidecars (QQ keeps them hot)."""
    best = 0.0
    for suffix in ("", "-wal", "-shm"):
        path = Path(str(msg_db) + suffix) if suffix else msg_db
        try:
            best = max(best, path.stat().st_mtime)
        except OSError:
            continue
    return best


def list_accounts(tencent_files: "str | Path | None" = None) -> "list[Account]":
    root = tencent_files_dir(tencent_files)
    accounts: "list[Account]" = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or not child.name.isdigit():
            continue
        db_dir = child / "nt_qq" / "nt_db"
        msg_db = db_dir / "nt_msg.db"
        if msg_db.is_file():
            accounts.append(Account(child.name, db_dir, msg_db, _db_activity(msg_db)))
    return accounts


def detect_active_account(
    tencent_files: "str | Path | None" = None, exclude: "set[str] | tuple[str, ...]" = ()
) -> "Account | None":
    accounts = [a for a in list_accounts(tencent_files) if a.number not in set(exclude)]
    if not accounts:
        return None
    return max(accounts, key=lambda acc: acc.mtime)


# ---------------------------------------------------------------------------
# QQ NT installation
# ---------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class QQInstall:
    install_dir: Path
    version: "str | None"
    qq_exe: "Path | None"
    wrapper_node: Path
    source: str

    @property
    def app_dir(self) -> Path:
        return self.wrapper_node.parent


_UNINSTALL_KEYS = (
    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\QQ",
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\QQ",
    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\NTQQ",
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\NTQQ",
)


def _path_from_uninstall_value(raw: str) -> "Path | None":
    text = str(raw or "").strip()
    if not text:
        return None
    if text.startswith('"'):
        end = text.find('"', 1)
        text = text[1:end] if end > 0 else text.strip('"')
    else:
        match = re.match(r"^(.*?\.exe)", text, re.IGNORECASE)
        text = match.group(1) if match else text.split(" ")[0]
    text = text.split(",")[0].strip()
    if not text:
        return None
    path = Path(text)
    # Uninstall.exe / QQ.exe live in the installation root.
    return path.parent if path.suffix.lower() == ".exe" else path


def _registry_install_dirs() -> "list[Path]":
    if sys.platform != "win32":
        return []
    try:
        import winreg
    except ImportError:  # pragma: no cover - non-Windows
        return []
    found: "list[Path]" = []
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for key in _UNINSTALL_KEYS:
            try:
                with winreg.OpenKey(hive, key) as handle:
                    for value_name in ("InstallLocation", "UninstallString", "DisplayIcon"):
                        try:
                            raw, _kind = winreg.QueryValueEx(handle, value_name)
                        except OSError:
                            continue
                        path = _path_from_uninstall_value(raw)
                        if path and path not in found:
                            found.append(path)
            except OSError:
                continue
    return found


def _common_install_dirs() -> "list[Path]":
    dirs: "list[Path]" = []
    for env_name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = os.environ.get(env_name)
        if base:
            dirs.append(Path(base) / "Tencent" / "QQNT")
    dirs.append(Path(r"D:\Program Files\Tencent\QQNT"))
    dirs.append(Path(r"C:\Program Files\Tencent\QQNT"))
    return dirs


def _normalize_version(text: str) -> str:
    return re.sub(r"[^0-9a-z]", "", (text or "").lower())


def _version_sort_key(name: str):
    key = []
    for part in re.split(r"[.\-_+ ]+", name):
        if not part:
            continue
        key.append((0, int(part)) if part.isdigit() else (1, part))
    return key


def _config_version(install_dir: Path) -> "str | None":
    config = install_dir / "config.json"
    try:
        data = json.loads(config.read_text(encoding="utf-8-sig", errors="replace"))
    except (OSError, ValueError):
        return None
    if isinstance(data, dict):
        for key in ("curVersion", "CurVersion", "version", "Version"):
            value = data.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _pick_version_dir(install_dir: Path) -> "tuple[Path | None, str | None]":
    versions = install_dir / "versions"
    if not versions.is_dir():
        # Older/alternative layout: wrapper.node directly under resources\app
        if (install_dir / "resources" / "app" / "wrapper.node").is_file():
            return install_dir, None
        return None, None
    dirs = [d for d in versions.iterdir() if d.is_dir()]
    if not dirs:
        return None, None
    hint = _config_version(install_dir)
    if hint:
        wanted = _normalize_version(hint)
        for candidate in dirs:
            if _normalize_version(candidate.name) == wanted:
                return candidate, candidate.name
    def has_node(path: Path) -> bool:
        return (path / "resources" / "app" / "wrapper.node").is_file()
    pool = [d for d in dirs if has_node(d)] or dirs
    best = max(pool, key=lambda d: _version_sort_key(d.name))
    return best, best.name


def detect_qq_install(wrapper_node: "str | Path | None" = None) -> "QQInstall | None":
    """Find the QQ NT installation, including ``wrapper.node``."""
    if wrapper_node:
        path = Path(wrapper_node).expanduser()
        if not path.is_file():
            raise ToolError(f"找不到 wrapper.node: {path}")
        parents = path.parents
        install_dir = parents[4] if len(parents) > 4 else (parents[-1] if parents else path.parent)
        return QQInstall(install_dir, None, None, path, "explicit")

    for install_dir in _registry_install_dirs() + _common_install_dirs():
        if not install_dir.is_dir():
            continue
        version_dir, version = _pick_version_dir(install_dir)
        if not version_dir:
            continue
        node = version_dir / "resources" / "app" / "wrapper.node"
        if not node.is_file():
            continue
        exe = install_dir / "QQ.exe"
        return QQInstall(
            install_dir=install_dir,
            version=version,
            qq_exe=exe if exe.is_file() else None,
            wrapper_node=node,
            source="registry/standard paths",
        )
    return None


# ---------------------------------------------------------------------------
# QQNT_Export (optional: only used for friendly emoji names)
# ---------------------------------------------------------------------------
def find_qqnt_export(explicit: "str | Path | None" = None) -> "Path | None":
    candidates = [
        explicit,
        os.environ.get("QQNT_EXPORT_DIR"),
        work_root() / "QQNT_Export",
        Path(__file__).resolve().parent.parent / "vendor" / "QQNT_Export",
        Path(__file__).resolve().parent.parent / "QQNT_Export",
    ]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if (path / "emojis.py").is_file() or (path / "element_pb2.py").is_file():
            return path
    return None


# ---------------------------------------------------------------------------
# Encryption key handling
# ---------------------------------------------------------------------------
_HEX_ONLY = re.compile(r"^[0-9a-fA-F]+$")
_HEX_LENGTHS = (16, 24, 32, 48, 64)


@dataclasses.dataclass(frozen=True)
class KeySpec:
    """A normalised database key.

    ``raw`` is what QQ passed to ``sqlite3_key``; ``text`` is a 1:1
    latin-1 view of those bytes so it can be embedded in a SQL literal.
    """

    raw: bytes
    hex: str
    text: str
    ascii_only: bool

    @property
    def length(self) -> int:
        return len(self.raw)

    def masked(self) -> str:
        """Short printable form for logs (never print the whole key)."""
        if len(self.hex) <= 8:
            return self.hex
        return f"{self.hex[:6]}...{self.hex[-4:]}"

    def describe(self) -> str:
        kind = "ASCII" if self.ascii_only else "BINARY"
        return f"{self.length} 字节 {kind} 密钥 (hex {self.masked()})"


def normalize_key(value: "str | bytes") -> KeySpec:
    """Accept hex, ``x'..'`` or raw text keys and return a KeySpec."""
    if isinstance(value, bytes):
        raw = value
    else:
        text = (value or "").strip()
        if not text:
            raise ToolError("密钥为空，请用 get_key_hex.ps1 重新提取")
        if len(text) >= 4 and text[0] in "xX" and text[1] in "'\"" and text[-1] == text[1]:
            text = text[2:-1]
        if len(text) in _HEX_LENGTHS and _HEX_ONLY.match(text):
            raw = bytes.fromhex(text)
        else:
            raw = text.encode("utf-8")
    if not raw:
        raise ToolError("密钥为空，请用 get_key_hex.ps1 重新提取")
    if len(raw) > 64:
        raise ToolError(f"密钥过长（{len(raw)} 字节），请确认传入的是 hex 密钥而不是整行日志")
    return KeySpec(
        raw=raw,
        hex=raw.hex(),
        text=raw.decode("latin-1"),
        ascii_only=all(byte < 0x80 for byte in raw),
    )


def key_pragma_candidates(spec: KeySpec, preferred: str = "auto") -> "list[tuple[str, str, str]]":
    """Return ordered ``(mode, sql, description)`` PRAGMA key attempts.

    SQLCipher hashes the *passphrase bytes* with PBKDF2; QQ handed over the raw
    16 bytes, so the passphrase form must reproduce those exact bytes.  For
    binary keys a ``x'..'`` (raw key, no KDF) fallback is offered as well, and
    the historic "hex text as passphrase" form is kept last for compatibility.
    """
    escaped = spec.text.replace("'", "''")
    variants: "list[tuple[str, str, str]]" = []
    if spec.ascii_only:
        variants.append(("passphrase", f"PRAGMA key = '{escaped}';", "原始字节作为口令"))
    variants.append(("rawkey", "PRAGMA key = \"x'%s'\";" % spec.hex, "原始字节作为 raw key"))
    variants.append(("hexpassphrase", f"PRAGMA key = '{spec.hex}';", "hex 文本作为口令"))
    if preferred and preferred != "auto":
        preferred_modes = {part.strip() for part in preferred.split(",") if part.strip()}
        filtered = [item for item in variants if item[0] in preferred_modes]
        if filtered:
            return filtered
    return variants


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------
def parse_qq_number(value: "str | int") -> str:
    """Validate a QQ number and return it as a digits-only string."""
    text = str(value).strip().lstrip("+")
    if not text.isdigit():
        raise ToolError(f"QQ 号必须是纯数字: {value!r}")
    if not 4 <= len(text) <= 12:
        raise ToolError(f"QQ 号长度异常: {value!r}")
    return text


def unix_to_datetime(value) -> "datetime | None":
    """Convert a QQ timestamp (seconds, tolerate ms) to a datetime."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    if number > 10 ** 12:  # milliseconds
        number //= 1000
    if number > 253402300799:  # 9999-12-31
        return None
    try:
        return datetime.fromtimestamp(number)
    except (OverflowError, OSError, ValueError):
        return None


def human_bytes(size) -> str:
    """Format a byte count like ``350.96 KiB`` (binary units)."""
    try:
        number = float(size)
    except (TypeError, ValueError):
        return ""
    if number <= 0:
        return ""
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if number < 1024 or unit == "TiB":
            return f"{number:.0f} {unit}" if unit == "B" else f"{number:.2f} {unit}"
        number /= 1024
    return ""
