#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Decrypt NTQQ databases (SQLCipher) into plain SQLite files.

Usage::

    python decrypt_db.py                       # auto: active account, key from env
    python decrypt_db.py 10002 <key>           # explicit account + key
    python decrypt_db.py 10002 <key> --src ... --dst ... --only nt_msg.db

Facts this implementation is based on (verified against real databases):

* every ``.db`` starts with a 1024-byte custom header.  Bytes 0-15 are
  ``SQLite header 3\\0``, bytes 32-39 are ``QQ_NT DB``, byte 44 starts a
  protobuf blob that declares the format version (e.g. ``1.1.0.1``) and the
  HMAC algorithm (``HMAC_SHA1``) used by the file.  The header is stripped
  before opening the file with SQLCipher.
* ``PRAGMA cipher_page_size`` must be set *before* ``PRAGMA key``.
* the key extracted from QQ is the raw 16-byte passphrase; SQLCipher runs it
  through PBKDF2, so the PRAGMA literal has to contain those exact bytes (the
  hex text would hash to a different key).
* a live QQ keeps recent messages in ``-wal``; the sidecar files are copied and
  recovered, otherwise the newest messages would silently be missing.

The cipher parameters are probed instead of hard-coded, because the "known
good" values differ between QQ versions.  The header metadata is used as a hint
and every candidate is verified by reading ``sqlite_master``.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import qqcommon
from qqcommon import KeySpec, ToolError, human_bytes, normalize_key
from qq_elements import iter_fields

HEADER_SIZE = 1024
HEADER_MAGIC = b"SQLite header 3\x00"
HEADER_MARKER = b"QQ_NT DB"
MARKER_OFFSET = 0x20
LENGTH_OFFSET = 0x28
PAYLOAD_OFFSET = 0x2C

#: protobuf field numbers inside the header metadata blob
H_FIELD_INFO = 2
H_FIELD_VERSION = 3
H_FIELD_HMAC = 4
H_FIELD_TIMESTAMP = 5

BATCH_SIZE = 20000
COPY_CHUNK = 8 * 1024 * 1024

#: known SQLCipher parameter sets, most likely first
PARAM_SETS = (
    ("HMAC_SHA1", "PBKDF2_HMAC_SHA512", 4000),
    ("HMAC_SHA1", "PBKDF2_HMAC_SHA1", 4000),
    ("HMAC_SHA512", "PBKDF2_HMAC_SHA512", 4000),
    ("HMAC_SHA256", "PBKDF2_HMAC_SHA256", 4000),
    ("HMAC_SHA512", "PBKDF2_HMAC_SHA512", 256000),
    ("HMAC_SHA1", "PBKDF2_HMAC_SHA512", 64000),
)


# ---------------------------------------------------------------------------
# header parsing
# ---------------------------------------------------------------------------
@dataclass
class DbHeader:
    size: int
    version: str = ""
    hmac: str = ""
    created: int = 0
    info: str = ""

    def describe(self) -> str:
        parts = [f"header={self.size}B"]
        if self.version:
            parts.append(f"格式={self.version}")
        if self.hmac:
            parts.append(f"HMAC={self.hmac}")
        if self.created:
            parts.append("创建=" + time.strftime("%Y-%m-%d", time.localtime(self.created)))
        return " ".join(parts)


def read_db_header(path: Path) -> "DbHeader | None":
    """Return parsed QQ header metadata, or None for a plain SQLite file."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(HEADER_SIZE)
    except OSError as exc:
        raise ToolError(f"无法读取 {path}: {exc}") from exc
    if head.startswith(qqcommon.SQLITE_MAGIC):
        return None
    if not head.startswith(HEADER_MAGIC):
        raise ToolError(f"{path.name}: 既不是 SQLite 也不是 NTQQ 加密数据库")
    header = DbHeader(size=HEADER_SIZE)
    if head[MARKER_OFFSET:MARKER_OFFSET + 8] == HEADER_MARKER:
        length = int.from_bytes(head[LENGTH_OFFSET:LENGTH_OFFSET + 4], "little")
        payload = head[PAYLOAD_OFFSET:PAYLOAD_OFFSET + length]
        try:
            for field, wire, value in iter_fields(payload):
                if field == H_FIELD_VERSION and wire == 2 and isinstance(value, bytes):
                    header.version = value.decode("utf-8", "replace")
                elif field == H_FIELD_HMAC and wire == 2 and isinstance(value, bytes):
                    header.hmac = value.decode("utf-8", "replace")
                elif field == H_FIELD_INFO and wire == 2 and isinstance(value, bytes):
                    header.info = value.decode("utf-8", "replace")
                elif field == H_FIELD_TIMESTAMP and wire == 0 and isinstance(value, int):
                    header.created = value
        except Exception:
            pass
    return header


# ---------------------------------------------------------------------------
# cipher backend
# ---------------------------------------------------------------------------
def _load_cipher():
    try:
        import sqlcipher3.dbapi2 as backend
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise ToolError(
            "缺少 sqlcipher3，无法解密数据库。请运行:\n"
            "  python -m pip install sqlcipher3\n"
            "（Windows 上有官方预编译 wheel；sqlcipher3-binary 没有 Windows 版本）"
        ) from exc
    return backend


@dataclass(frozen=True)
class Recipe:
    """A working combination of key literal and SQLCipher parameters."""

    key_mode: str
    key_sql: str
    hmac: str
    kdf: str
    kdf_iter: int
    page_size: int

    def describe(self) -> str:
        return (f"{self.key_mode} page={self.page_size} kdf_iter={self.kdf_iter} "
                f"{self.hmac}/{self.kdf}")


def _apply(conn, recipe: Recipe) -> None:
    try:  # SQLCipher >= 4.5: keep its internal "hmac check failed" chatter quiet
        conn.execute("PRAGMA cipher_log_level = NONE;")
    except Exception:
        pass
    conn.execute(f"PRAGMA cipher_page_size = {recipe.page_size};")
    conn.execute(recipe.key_sql)
    conn.execute(f"PRAGMA kdf_iter = {recipe.kdf_iter};")
    conn.execute(f"PRAGMA cipher_hmac_algorithm = {recipe.hmac};")
    conn.execute(f"PRAGMA cipher_kdf_algorithm = {recipe.kdf};")


def _candidate_recipes(spec: KeySpec, header: "DbHeader | None", preferred: str,
                       page_size: int, kdf_iter: int, hmac: str):
    """Yield Recipe candidates, with the header-declared algorithm first."""
    params: "list[tuple[str, str, int]]" = []
    for wanted in (hmac.upper() if hmac else "", header.hmac.upper() if header and header.hmac else ""):
        if not wanted:
            continue
        for entry in PARAM_SETS:
            if entry[0] == wanted:
                params.append((entry[0], entry[1], kdf_iter or entry[2]))
    for entry in PARAM_SETS:
        params.append((entry[0], entry[1], kdf_iter or entry[2]))

    seen = set()
    for hmac_alg, kdf_alg, iterations in params:
        for mode, sql, _note in qqcommon.key_pragma_candidates(spec, preferred):
            recipe = Recipe(mode, sql, hmac_alg, kdf_alg, iterations, page_size)
            signature = (recipe.key_mode, recipe.hmac, recipe.kdf, recipe.kdf_iter,
                         recipe.page_size)
            if signature in seen:
                continue
            seen.add(signature)
            yield recipe


def _cleanup_sidecars(path: Path) -> None:
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists():
            try:
                sidecar.unlink()
            except OSError:
                pass


@contextlib.contextmanager
def _silence_native_stderr(enabled: bool = True):
    """Hide SQLCipher's C-level logging while probing wrong keys."""
    if not enabled or not hasattr(os, "dup2"):
        yield
        return
    saved = devnull = None
    try:
        sys.stderr.flush()
        saved = os.dup(2)
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 2)
    except OSError:
        if saved is not None:
            os.close(saved)
        if devnull is not None:
            os.close(devnull)
        yield
        return
    try:
        yield
    finally:
        try:
            os.dup2(saved, 2)
        except OSError:
            pass
        for handle in (saved, devnull):
            try:
                if handle is not None:
                    os.close(handle)
            except OSError:
                pass


def recipe_works(backend, clear_path: Path, recipe: Recipe) -> bool:
    conn = None
    try:
        with _silence_native_stderr():
            conn = backend.connect(str(clear_path), isolation_level=None)
            _apply(conn, recipe)
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return True
    except Exception:
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        _cleanup_sidecars(clear_path)


def probe_recipe(backend, clear_path: Path, spec: KeySpec, header: "DbHeader | None",
                 preferred: str, page_size: int, kdf_iter: int, hmac: str) -> Recipe:
    """Find the first recipe that can actually read the database."""
    attempts = 0
    for recipe in _candidate_recipes(spec, header, preferred, page_size, kdf_iter, hmac):
        attempts += 1
        if recipe_works(backend, clear_path, recipe):
            return recipe
    raise ToolError(
        f"解密失败：尝试了 {attempts} 种密钥/参数组合仍无法读取数据库。\n"
        + (f"文件头信息: {header.describe()}\n" if header else "")
        + "最常见的原因是密钥已过期（QQ 重装或升级会更换密钥）。\n"
          "请重新运行 get_key_hex.ps1 提取当前密钥，再执行解密。"
    )


# ---------------------------------------------------------------------------
# file preparation / export
# ---------------------------------------------------------------------------
def prepare_workfile(src: Path, work_dir: Path, use_wal: bool,
                     header: "DbHeader | None") -> Path:
    """Copy the database into the work dir while stripping the QQ header."""
    target = work_dir / (src.name + ".clear")
    skip = header.size if header else 0
    with open(src, "rb") as source:
        if skip:
            source.seek(skip)
        with open(target, "wb") as destination:
            while True:
                chunk = source.read(COPY_CHUNK)
                if not chunk:
                    break
                destination.write(chunk)
    if use_wal:
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(src) + suffix)
            if sidecar.is_file():
                try:
                    shutil.copyfile(sidecar, Path(str(target) + suffix))
                except OSError:
                    pass
    return target


def _create_table(plain, ddl: str) -> None:
    statement = ddl.strip()
    if not statement:
        return
    if "create table if not exists" not in statement.lower():
        lowered = statement.lower()
        if lowered.startswith("create table"):
            statement = "CREATE TABLE IF NOT EXISTS" + statement[len("CREATE TABLE"):]
    plain.execute(statement)


def export_tables(backend, clear_path: Path, recipe: Recipe, dst_path: Path,
                  progress=None) -> "tuple[int, int, list[str]]":
    """Stream every table of the encrypted database into a plain SQLite file.

    Writes to ``<dst>.tmp`` first and replaces the target only on success, so a
    failed run never destroys a previous (good) decrypted database.
    """
    tmp_path = Path(str(dst_path) + ".tmp")
    for path in (tmp_path, dst_path):
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass
        _cleanup_sidecars(path)

    conn = backend.connect(str(clear_path), isolation_level=None)
    warnings: "list[str]" = []
    total_rows = 0
    try:
        _apply(conn, recipe)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        except Exception:
            pass
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()]

        plain = sqlite3.connect(str(tmp_path), isolation_level=None)
        try:
            plain.execute("PRAGMA journal_mode = WAL;")
            plain.execute("PRAGMA synchronous = NORMAL;")
            plain.execute("PRAGMA cache_size = -65536;")

            for table in tables:
                if str(table).startswith("sqlite_"):
                    continue
                ddl_row = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone()
                ddl = ddl_row[0] if ddl_row and ddl_row[0] else ""
                if not ddl:
                    continue
                try:
                    _create_table(plain, ddl)
                except Exception as exc:
                    warnings.append(f"{table}: 建表失败 ({str(exc)[:60]})")
                    continue

                try:
                    column_count = len(conn.execute(
                        f'PRAGMA table_info("{table}")').fetchall())
                    placeholders = ",".join("?" * column_count)
                    insert_sql = f'INSERT OR IGNORE INTO "{table}" VALUES ({placeholders})'
                    rows_written = 0
                    if "without rowid" in ddl.lower():
                        rows = conn.execute(f'SELECT * FROM "{table}"').fetchall()
                        if rows:
                            plain.executemany(insert_sql, rows)
                            rows_written = len(rows)
                    else:
                        last_rowid = 0
                        while True:
                            batch = conn.execute(
                                f'SELECT rowid, * FROM "{table}" WHERE rowid > ? '
                                f"ORDER BY rowid LIMIT ?",
                                (last_rowid, BATCH_SIZE),
                            ).fetchall()
                            if not batch:
                                break
                            last_rowid = batch[-1][0]
                            plain.executemany(insert_sql, [row[1:] for row in batch])
                            rows_written += len(batch)
                            if progress:
                                progress(table, rows_written)
                    plain.commit()
                    total_rows += rows_written
                    if progress:
                        progress(table, rows_written, done=True)
                except Exception as exc:
                    warnings.append(f"{table}: {str(exc)[:80]}")
                    plain.commit()

            try:
                index_rows = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"
                ).fetchall()
            except Exception:
                index_rows = []
            for (ddl,) in index_rows:
                try:
                    plain.execute(ddl)
                except Exception:
                    pass
            plain.commit()

            # leave a single self-contained file behind
            try:
                plain.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                plain.execute("PRAGMA journal_mode = DELETE;")
            except Exception:
                pass
        finally:
            plain.close()
    finally:
        try:
            conn.close()
        except Exception:
            pass

    # 全部成功后才替换目标文件，避免中途失败留下半个数据库
    os.replace(str(tmp_path), str(dst_path))
    _cleanup_sidecars(dst_path)
    return total_rows, len(tables), warnings


# ---------------------------------------------------------------------------
# per-file pipeline
# ---------------------------------------------------------------------------
@dataclass
class DbResult:
    name: str
    status: str
    rows: int = 0
    tables: int = 0
    note: str = ""
    seconds: float = 0.0


def decrypt_one(backend, src: Path, dst: Path, spec: "KeySpec | None", args,
                cache: "dict[str, Recipe]") -> DbResult:
    started = time.time()
    header = read_db_header(src)
    if header is None:  # already plain SQLite
        shutil.copyfile(src, dst)
        return DbResult(src.name, "plain", note="已是明文，直接复制",
                        seconds=time.time() - started)

    if spec is None:
        raise ToolError("数据库已加密，但未提供密钥")

    with tempfile.TemporaryDirectory(prefix="qqdec_") as work:
        clear = prepare_workfile(src, Path(work), args.with_wal, header)
        recipe = cache.get("recipe")
        if recipe is None or not recipe_works(backend, clear, recipe):
            recipe = probe_recipe(backend, clear, spec, header, args.key_format,
                                  args.page_size, args.kdf_iter or 0, args.hmac)
            cache["recipe"] = recipe
        rows, tables, warnings = export_tables(
            backend, clear, recipe, dst,
            progress=None if args.quiet else _make_progress(),
        )
    note = recipe.describe()
    if warnings:
        note += f"；{len(warnings)} 个警告"
        for warning in warnings[:3]:
            print(f"      [警告] {warning}", flush=True)
    return DbResult(src.name, "ok", rows=rows, tables=tables, note=note,
                    seconds=time.time() - started)


def _make_progress():
    state = {"last": 0.0}

    def report(table, rows, done=False):
        now = time.time()
        if done or now - state["last"] > 3.0:
            state["last"] = now
            print(f"      {table}: {rows:,} 行" + ("" if done else " ..."), flush=True)

    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="decrypt_db.py",
        description="解密 NTQQ 数据库（SQLCipher → 明文 SQLite）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
               "  python decrypt_db.py 10002 ABCDEF0123456789\n"
               "  python decrypt_db.py 10002 ABCDEF0123456789 --only nt_msg.db\n"
               "  python decrypt_db.py --list\n",
    )
    parser.add_argument("account", nargs="?", default="", help="自己的 QQ 号（省略则自动检测）")
    parser.add_argument("key", nargs="?", default="",
                        help="数据库密钥（hex 或原始文本，也可用 QQNT_DB_KEY 环境变量）")
    parser.add_argument("--src", default="", help="源 nt_db 目录")
    parser.add_argument("--dst", default="", help="输出目录")
    parser.add_argument("--tencent-files", default="", help="自定义 Tencent Files 目录")
    parser.add_argument("--only", default="", help="只解密指定数据库，逗号分隔（如 nt_msg.db）")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的输出文件")
    parser.add_argument("--no-wal", dest="with_wal", action="store_false", default=True,
                        help="不合并 -wal 中的最新消息（默认合并）")
    parser.add_argument("--key-format", default="auto",
                        choices=("auto", "passphrase", "rawkey", "hexpassphrase"),
                        help="密钥在 PRAGMA 中的形式（默认自动尝试）")
    parser.add_argument("--hmac", default="", help="强制 cipher_hmac_algorithm（如 HMAC_SHA1）")
    parser.add_argument("--kdf-iter", type=int, default=0, help="强制 kdf_iter")
    parser.add_argument("--page-size", type=int, default=4096, help="cipher_page_size（默认 4096）")
    parser.add_argument("--list", action="store_true", help="只列出可解密的数据库文件")
    parser.add_argument("-q", "--quiet", action="store_true", help="精简输出")
    return parser


def _resolve_account(args) -> str:
    if args.account:
        return qqcommon.parse_qq_number(args.account)
    active = qqcommon.detect_active_account(args.tencent_files)
    if not active:
        raise ToolError("未能自动检测 QQ 账号，请显式传入 QQ 号。")
    if not args.quiet:
        print(f"[i] 自动选择账号 {active.number}（最近活动 {active.last_active}）")
    return active.number


def _resolve_src(args, account: str) -> Path:
    if args.src:
        path = Path(args.src).expanduser()
        if not path.is_dir():
            raise ToolError(f"源目录不存在: {path}")
        return path
    path = qqcommon.tencent_files_dir(args.tencent_files) / account / "nt_qq" / "nt_db"
    if not path.is_dir():
        raise ToolError(f"找不到账号 {account} 的数据库目录: {path}")
    return path


def main(argv: "list[str] | None" = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    qqcommon.setup_console()

    try:
        account = _resolve_account(args)
        src_dir = _resolve_src(args, account)
        dst_dir = Path(args.dst).expanduser() if args.dst else qqcommon.decrypted_dir(account)
        dst_dir.mkdir(parents=True, exist_ok=True)

        files = sorted(path for path in src_dir.iterdir()
                       if path.is_file() and path.suffix.lower() == ".db")
        if args.only:
            wanted = {name.strip().lower() for name in args.only.split(",") if name.strip()}
            files = [path for path in files if path.name.lower() in wanted]
        if not files:
            raise ToolError(f"{src_dir} 下没有匹配的 .db 文件")

        if args.list:
            print(f"账号 {account} 的数据库（{src_dir}）:")
            for path in files:
                try:
                    header = read_db_header(path)
                except ToolError as exc:
                    print(f"  {path.name:<28}{human_bytes(path.stat().st_size):>10}  无法识别（{exc}）")
                    continue
                state = "明文" if header is None else header.describe()
                print(f"  {path.name:<28}{human_bytes(path.stat().st_size):>10}  {state}")
            return 0

        # 逐个判断库的类型，遇到无法识别的文件只跳过它，不影响其它库
        encrypted: "list[Path]" = []
        invalid: "list[str]" = []
        for path in files:
            try:
                if read_db_header(path) is not None:
                    encrypted.append(path)
            except ToolError:
                invalid.append(path.name)
        if invalid:
            print(f"[警告] 跳过无法识别的文件: {', '.join(invalid)}")

        spec = None
        if encrypted:
            key_text = args.key or os.environ.get("QQNT_DB_KEY", "")
            if not key_text:
                raise ToolError(
                    "缺少密钥。请传入密钥参数，或设置环境变量 QQNT_DB_KEY，\n"
                    "或直接使用 run_export.ps1（会自动提取密钥）。"
                )
            spec = normalize_key(key_text)

        if not args.quiet:
            total = sum(path.stat().st_size for path in files)
            print(f"账号   : {account}")
            print(f"源目录 : {src_dir}")
            print(f"输出   : {dst_dir}")
            print(f"数据库 : {len(files)} 个，共 {human_bytes(total)}")
            if spec:
                print(f"密钥   : {spec.describe()}")
            print()

        backend = _load_cipher() if encrypted else None
        cache: "dict[str, Recipe]" = {}
        results: "list[DbResult]" = []

        for path in files:
            if path.name in invalid:
                continue
            dst = dst_dir / path.name
            if dst.exists() and not args.force:
                results.append(DbResult(path.name, "skip", note="已存在（--force 可覆盖）"))
                if not args.quiet:
                    print(f"  [跳过] {path.name}")
                continue
            if not args.quiet:
                print(f"  [{path.name}] 解密中 ...", flush=True)
            try:
                result = decrypt_one(backend, path, dst, spec, args, cache)
                results.append(result)
                if not args.quiet:
                    if result.status == "ok":
                        print(f"     完成：{result.tables} 张表，{result.rows:,} 行，"
                              f"{result.seconds:.1f}s（{result.note}）", flush=True)
                    else:
                        print(f"     {result.note}", flush=True)
            except ToolError:
                raise
            except Exception as exc:
                results.append(DbResult(path.name, "fail", note=str(exc)[:120]))
                if dst.exists():
                    try:
                        dst.unlink()
                    except OSError:
                        pass
                if not args.quiet:
                    print(f"     失败：{str(exc)[:160]}", flush=True)

        ok = [item for item in results if item.status in ("ok", "plain")]
        failed = [item for item in results if item.status == "fail"]
        skipped = [item for item in results if item.status == "skip"]
        msg_db_failed = any(item.name == "nt_msg.db" and item.status == "fail"
                            for item in results)
        msg_db_ok = (dst_dir / "nt_msg.db").is_file() and not msg_db_failed

        print()
        print(f"完成：成功 {len(ok)}，跳过 {len(skipped)}，失败 {len(failed)}（共 {len(results)}）")
        for item in failed:
            print(f"  [失败] {item.name}: {item.note}")
        if not msg_db_ok:
            print("错误：nt_msg.db 未能解密，无法导出聊天记录。", file=sys.stderr)
            return 1
        return 0 if not failed else 3

    except ToolError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("已取消。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
