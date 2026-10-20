#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Export NTQQ C2C chat records to TXT / Markdown / JSON.

Only the Python standard library is required: message bodies are decoded by
:mod:`qq_elements` (no protobuf, no QQNT_Export).

Typical use::

    python export_messages.py --list-peers
    python export_messages.py <nt_msg.db> 10001 ./out --format all --my-qq 10002

Design notes
------------
* Columns are addressed by name (they are numeric strings like ``40050``), never
  by position, so a schema change between QQ versions cannot silently shift the
  timestamp into the nick name column.
* The peer is matched on ``40030`` (QQ number) *and* on the UID column
  ``40021``, including the legacy masked form ``u_xxx_<qq>``; real UIDs are
  looked up in ``nt_uid_mapping_table``.  That is a superset of both the old
  "masked UID only" and "QQ number only" queries.
* Direction is derived from ``40033`` (sender QQ number) when available, which
  is also reliable for messages sent from another device (``40013`` is 0 then).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import qq_elements
import qqcommon
from qq_elements import Element, ElementOptions, ParserStats
from qqcommon import ToolError, human_bytes, unix_to_datetime

BASENAME_TEMPLATE = "与{peer}的聊天记录"

#: column numbers of interest inside c2c_msg_table
COL_ID = "40001"
COL_SENDER_FLAG = "40013"
COL_PEER_UID = "40021"
COL_PEER_NUM = "40030"
COL_SENDER_NUM = "40033"
COL_TIME = "40050"
COL_BODY = "40800"

REQUIRED_COLUMNS = (COL_TIME, COL_BODY)

DIRECTION_CN = {"sent": "发", "received": "收", "forwarded": "转发", "system": "系统"}


# ---------------------------------------------------------------------------
# data model
# ---------------------------------------------------------------------------
@dataclass
class Message:
    message_id: int
    moment: "datetime | None"
    direction: str
    elements: "list[Element]"
    raw_flag: int = 0
    sender_num: int = 0

    @property
    def direction_cn(self) -> str:
        return DIRECTION_CN.get(self.direction, "未知")

    @property
    def timestamp(self) -> str:
        return self.moment.strftime("%Y-%m-%d %H:%M:%S") if self.moment else "时间无效"

    @property
    def date(self) -> str:
        return self.moment.strftime("%Y-%m-%d") if self.moment else "未知日期"

    @property
    def time(self) -> str:
        return self.moment.strftime("%H:%M:%S") if self.moment else "--:--:--"

    @property
    def valid_time(self) -> bool:
        return self.moment is not None

    @property
    def msg_type(self) -> str:
        for element in self.elements:
            kind = element.kind
            if kind and not kind.startswith("未知类型"):
                return kind
        return self.elements[0].kind if self.elements else "空消息"

    @property
    def content(self) -> str:
        parts: "list[str]" = []
        for element in self.elements:
            if element.label:
                parts.append(element.label)
            if element.content:
                parts.append(element.content)
        return "\n".join(parts) if parts else "[空消息]"

    @property
    def display_content(self) -> str:
        """Content without redundant type labels, for Markdown.

        Markdown already shows the primary type as an inline tag, so ``[文本]``
        in front of every line would just be noise.  Labels of *secondary*
        segments (a file next to a text note, a quote) are kept.
        """
        parts: "list[str]" = []
        for element in self.elements:
            kind = element.kind
            if element.label and kind != "文本" and kind != self.msg_type:
                parts.append(element.label)
            if element.content:
                parts.append(element.content)
        if parts:
            return "\n".join(parts)
        if self.elements:
            return self.elements[0].label
        return "[空消息]"

    def to_json(self) -> dict:
        return {
            "id": str(self.message_id),
            "timestamp": self.timestamp,
            "date": self.date,
            "time": self.time,
            "direction": self.direction,
            "direction_cn": self.direction_cn,
            "type": self.msg_type,
            "content": self.content,
            "elements": [
                {"label": element.label, "content": element.content}
                for element in self.elements
            ],
        }


# ---------------------------------------------------------------------------
# database access
# ---------------------------------------------------------------------------
def _open_readonly(db_path: Path):
    uri = db_path.resolve().as_uri() + "?mode=ro&immutable=1"
    return sqlite3.connect(uri, uri=True)


def _check_plaintext(db_path: Path) -> None:
    """Fail fast with an actionable message for encrypted databases."""
    try:
        with open(db_path, "rb") as handle:
            head = handle.read(16)
    except OSError as exc:
        raise ToolError(f"无法读取数据库 {db_path}: {exc}") from exc
    if head.startswith(qqcommon.SQLITE_MAGIC):
        return
    if head.startswith(b"SQLite header 3"):
        raise ToolError(
            f"这是一个加密的 NTQQ 数据库（含 1024 字节自定义头）:\n  {db_path}\n"
            "请先运行 decrypt_db.py 解密，或直接使用 run_export.ps1。"
        )
    raise ToolError(f"不是 SQLite 数据库（文件头 {head[:8]!r}）: {db_path}")


def _table_columns(conn, table: str) -> "list[str]":
    try:
        rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    except Exception as exc:  # pragma: no cover - defensive
        raise ToolError(f"读取表结构失败 ({table}): {exc}") from exc
    return [str(row[1]) for row in rows]


def _has_table(conn, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _uid_candidates(conn, peer_qq: str) -> "set[str]":
    """Collect every UID that can identify the peer in this database."""
    uids: "set[str]" = set()
    if _has_table(conn, "nt_uid_mapping_table"):
        columns = set(_table_columns(conn, "nt_uid_mapping_table"))
        uid_columns = [name for name in ("48902", "48912") if name in columns]
        if uid_columns and "1002" in columns:
            select = ", ".join(f'[{name}]' for name in uid_columns)
            try:
                for row in conn.execute(
                    f"SELECT {select} FROM nt_uid_mapping_table WHERE [1002] = ?",
                    (int(peer_qq),),
                ):
                    for value in row:
                        if isinstance(value, str) and value.strip():
                            uids.add(value.strip())
            except Exception:
                pass
    # Legacy masked UIDs ("u_***********_<qq>") and anything ending in _<qq>
    columns = set(_table_columns(conn, "c2c_msg_table"))
    if COL_PEER_UID in columns:
        try:
            for (value,) in conn.execute(
                f"SELECT DISTINCT [{COL_PEER_UID}] FROM c2c_msg_table "
                f"WHERE [{COL_PEER_UID}] IS NOT NULL"
            ):
                if isinstance(value, str) and value.endswith(f"_{peer_qq}"):
                    uids.add(value)
        except Exception:
            pass
    return uids


def _direction(sender_num: int, sender_flag: int, my_qq: "str | None",
               elements: "list[Element]") -> str:
    """Decide whether a message was sent or received.

    ``40033`` (sender QQ number) is authoritative: ``40013`` is 0 even for some
    messages sent from another device.  ``40013 == 3`` marks system notices.
    """
    if my_qq and sender_num:
        return "sent" if str(sender_num) == str(my_qq) else "received"
    mapping = {0: "received", 1: "sent", 2: "sent", 3: "system", 5: "sent", 8: "forwarded"}
    if sender_flag in mapping:
        return mapping[sender_flag]
    if elements and all(element.type_code == 8 for element in elements):
        return "system"
    return "received"


@dataclass
class QueryResult:
    messages: "list[Message]"
    peer_uids: "set[str]"
    stats: ParserStats


def fetch_messages(db_path: Path, peer_qq: str, my_qq: "str | None",
                   options: ElementOptions) -> QueryResult:
    _check_plaintext(db_path)
    try:
        conn = _open_readonly(db_path)
    except Exception as exc:
        raise ToolError(f"打开数据库失败: {exc}") from exc

    stats = ParserStats()
    try:
        if not _has_table(conn, "c2c_msg_table"):
            raise ToolError("数据库中没有 c2c_msg_table（不是 nt_msg.db？）")
        columns = _table_columns(conn, "c2c_msg_table")
        missing = [name for name in REQUIRED_COLUMNS if name not in columns]
        if missing:
            raise ToolError(
                "c2c_msg_table 缺少必需列: " + ", ".join(missing)
                + "\n请确认这是 QQ NT 的 nt_msg.db。"
            )
        peer_uids = _uid_candidates(conn, peer_qq)

        selected = [name for name in (
            COL_ID, COL_SENDER_FLAG, COL_SENDER_NUM, COL_TIME, COL_BODY
        ) if name in columns]
        select_sql = ", ".join(f'[{name}]' for name in selected)

        where_parts = []
        params: "list[object]" = []
        if COL_PEER_NUM in columns:
            where_parts.append(f'[{COL_PEER_NUM}] = ?')
            params.append(int(peer_qq))
        if peer_uids and COL_PEER_UID in columns:
            placeholders = ", ".join("?" for _ in peer_uids)
            where_parts.append(f'[{COL_PEER_UID}] IN ({placeholders})')
            params.extend(sorted(peer_uids))
        if not where_parts:
            raise ToolError("无法构造查询条件：表中既没有对方 QQ 号列也没有 UID 列")

        order = f'[{COL_TIME}]'
        sql = (
            f"SELECT {select_sql} FROM c2c_msg_table "
            f"WHERE {' OR '.join(where_parts)} "
            f"ORDER BY {order} ASC, [{COL_ID}] ASC"
        )
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    index = {name: position for position, name in enumerate(selected)}
    messages: "list[Message]" = []
    seen: "set[int]" = set()
    for row in rows:
        try:
            message_id = int(row[index[COL_ID]] or 0) if COL_ID in index else 0
        except (TypeError, ValueError):
            message_id = 0
        if message_id:
            if message_id in seen:
                continue
            seen.add(message_id)

        body = row[index[COL_BODY]] if COL_BODY in index else None
        elements = qq_elements.parse_body(body, options, stats)
        try:
            sender_num = int(row[index[COL_SENDER_NUM]] or 0) if COL_SENDER_NUM in index else 0
        except (TypeError, ValueError):
            sender_num = 0
        try:
            sender_flag = int(row[index[COL_SENDER_FLAG]] or 0) if COL_SENDER_FLAG in index else 0
        except (TypeError, ValueError):
            sender_flag = 0
        moment = unix_to_datetime(row[index[COL_TIME]]) if COL_TIME in index else None

        messages.append(Message(
            message_id=message_id,
            moment=moment,
            direction=_direction(sender_num, sender_flag, my_qq, elements),
            elements=elements,
            raw_flag=sender_flag,
            sender_num=sender_num,
        ))

    messages.sort(key=lambda item: (item.moment or datetime.min, item.message_id))
    return QueryResult(messages=messages, peer_uids=peer_uids, stats=stats)


def list_peers(db_path: Path, limit: int = 40) -> "list[tuple[str, int, str, str]]":
    """Return ``(qq, count, first_date, last_date)`` for each C2C peer."""
    _check_plaintext(db_path)
    conn = _open_readonly(db_path)
    try:
        if not _has_table(conn, "c2c_msg_table"):
            raise ToolError("数据库中没有 c2c_msg_table")
        columns = set(_table_columns(conn, "c2c_msg_table"))
        if COL_PEER_NUM not in columns:
            raise ToolError("该数据库没有对方 QQ 号列（40030），无法列出会话")
        rows = conn.execute(
            f"SELECT [{COL_PEER_NUM}], COUNT(*), MIN([{COL_TIME}]), MAX([{COL_TIME}]) "
            f"FROM c2c_msg_table WHERE [{COL_PEER_NUM}] IS NOT NULL "
            f"GROUP BY [{COL_PEER_NUM}] ORDER BY COUNT(*) DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    result = []
    for qq, count, first, last in rows:
        start = unix_to_datetime(first)
        end = unix_to_datetime(last)
        result.append((
            str(qq),
            int(count),
            start.strftime("%Y-%m-%d") if start else "?",
            end.strftime("%Y-%m-%d") if end else "?",
        ))
    return result


# ---------------------------------------------------------------------------
# statistics helpers
# ---------------------------------------------------------------------------
def _date_range(messages: "list[Message]") -> "tuple[str, str]":
    stamps = [item for item in messages if item.valid_time]
    if not stamps:
        return "未知", "未知"
    return stamps[0].timestamp, stamps[-1].timestamp


def _year_stats(messages: "list[Message]") -> "dict[str, int]":
    stats: "dict[str, int]" = {}
    for item in messages:
        if item.valid_time:
            year = item.moment.strftime("%Y")
            stats[year] = stats.get(year, 0) + 1
    return dict(sorted(stats.items()))


def _type_stats(messages: "list[Message]") -> "dict[str, int]":
    stats: "dict[str, int]" = {}
    for item in messages:
        stats[item.msg_type] = stats.get(item.msg_type, 0) + 1
    return dict(sorted(stats.items(), key=lambda pair: -pair[1]))


def _direction_stats(messages: "list[Message]") -> "dict[str, int]":
    stats: "dict[str, int]" = {}
    for item in messages:
        key = item.direction_cn
        stats[key] = stats.get(key, 0) + 1
    return stats


# ---------------------------------------------------------------------------
# writers
# ---------------------------------------------------------------------------
def _write_text(path: Path, text: str, encoding: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding=encoding, newline="\n") as handle:
        handle.write(text if text.endswith("\n") else text + "\n")


def export_txt(messages: "list[Message]", peer_qq: str, path: Path,
               encoding: str = "utf-8") -> Path:
    start, end = _date_range(messages)
    lines = [
        f"QQ聊天记录 - 与 {peer_qq}",
        f"共 {len(messages)} 条消息",
        f"时间范围: {start} ~ {end}",
        f"导出时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "=" * 60,
        "",
    ]
    current_date = ""
    for message in messages:
        if message.date != current_date:
            current_date = message.date
            lines.append(f"===== {current_date} =====")
            lines.append("")
        lines.append(f"{message.timestamp} {message.direction_cn}")
        for element in message.elements:
            if element.label:
                lines.append(element.label)
            if element.content:
                lines.append(element.content)
        if not message.elements:
            lines.append("[空消息]")
        lines.append("")
    _write_text(path, "\n".join(lines), encoding)
    return path


_MD_BLOCK_START = ("#", ">", "-", "+", "*", "|", "=")


def _md_escape(text: str) -> str:
    """Minimal escaping so chat text cannot break the document structure."""
    output = []
    for line in text.split("\n"):
        stripped = line.lstrip()
        if stripped and stripped[0] in _MD_BLOCK_START and not line.startswith("  "):
            line = "\\" + line
        output.append(line.replace("`", "\\`"))
    return "\n".join(output)


def export_md(messages: "list[Message]", peer_qq: str, path: Path,
              source: str = "", encoding: str = "utf-8") -> Path:
    start, end = _date_range(messages)
    lines = [
        f"# QQ 聊天记录 — 与 {peer_qq}",
        "",
        f"- **消息总数**：{len(messages)} 条",
        f"- **时间范围**：{start} ~ {end}",
    ]
    if source:
        lines.append(f"- **数据来源**：`{source}`")
    lines.append(f"- **导出时间**：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines += ["", "---", ""]

    years = _year_stats(messages)
    if years:
        lines += ["## 按年统计", "", "| 年份 | 消息数 |", "|------|--------|"]
        for year, count in years.items():
            lines.append(f"| {year} | {count} |")
        lines += ["", "---", ""]

    current_date = ""
    speakers = {"sent": "我", "received": "对方", "forwarded": "转发", "system": "系统"}
    for message in messages:
        if message.date != current_date:
            current_date = message.date
            lines += [f"## {current_date}", ""]
        speaker = speakers.get(message.direction, message.direction_cn)
        content = _md_escape(message.display_content).replace("\n", "  \n")
        tag = f"`{message.msg_type}` " if message.msg_type not in ("文本", "空消息") else ""
        lines.append(f"**{message.time} {speaker}**：{tag}{content}")
        lines.append("")
    _write_text(path, "\n".join(lines), encoding)
    return path


def export_json(messages: "list[Message]", peer_qq: str, my_qq: str, path: Path,
                source: str = "", encoding: str = "utf-8") -> Path:
    start, end = _date_range(messages)
    payload = {
        "meta": {
            "generator": f"{qqcommon.APP_NAME} v{qqcommon.VERSION}",
            "my_qq": my_qq or "",
            "peer_qq": peer_qq,
            "total": len(messages),
            "date_range": {"start": start, "end": end},
            "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "source_db": source,
            "stats": {
                "by_year": _year_stats(messages),
                "by_type": _type_stats(messages),
                "by_direction": _direction_stats(messages),
            },
        },
        "messages": [message.to_json() for message in messages],
    }
    _write_text(path, json.dumps(payload, ensure_ascii=False, indent=2), encoding)
    return path


# ---------------------------------------------------------------------------
# path / argument resolution
# ---------------------------------------------------------------------------
def resolve_db_path(args) -> Path:
    if args.db_path:
        path = Path(args.db_path).expanduser()
        if not path.is_file():
            raise ToolError(f"数据库不存在: {path}")
        return path

    account = args.account or args.my_qq
    if account:
        candidate = qqcommon.decrypted_dir(account) / "nt_msg.db"
        if candidate.is_file():
            return candidate
        source = (qqcommon.tencent_files_dir(args.tencent_files)
                  / str(account) / "nt_qq" / "nt_db" / "nt_msg.db")
        if source.is_file():
            raise ToolError(
                f"还没解密 {account} 的数据库。请先运行:\n"
                f"  python decrypt_db.py {account} <密钥>"
            )
        raise ToolError(
            f"找不到账号 {account} 的 nt_msg.db\n  {candidate}\n  {source}"
        )

    active = qqcommon.detect_active_account(args.tencent_files)
    if not active:
        raise ToolError("未能自动检测 QQ 账号。请用 --my-qq <QQ号> 指定，或直接传入数据库路径。")
    candidate = qqcommon.decrypted_dir(active.number) / "nt_msg.db"
    if candidate.is_file():
        print(f"[i] 自动选择账号 {active.number}（最近活动 {active.last_active}）")
        return candidate
    raise ToolError(
        f"检测到账号 {active.number}，但其数据库尚未解密。请先运行:\n"
        f"  python decrypt_db.py {active.number} <密钥>"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="export_messages.py",
        description="导出 QQ NT 单聊聊天记录为 TXT / Markdown / JSON",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
               "  python export_messages.py --list-peers\n"
               "  python export_messages.py 10001 out --my-qq 10002\n"
               "  python export_messages.py --account 10002 --peer 10001 --format md\n",
    )
    parser.add_argument("db_path", nargs="?", default="",
                        help="解密后的 nt_msg.db 路径（省略则自动查找）")
    parser.add_argument("peer_qq", nargs="?", default="", help="对方 QQ 号")
    parser.add_argument("output_dir", nargs="?", default="", help="输出目录（默认当前目录）")
    parser.add_argument("--peer", dest="peer_option", default="", help="对方 QQ 号（与位置参数等价）")
    parser.add_argument("--format", dest="formats", default="all",
                        help="输出格式: all / txt / md / json，可用逗号分隔（默认 all）")
    parser.add_argument("--my-qq", default="", help="自己的 QQ 号（写入元数据并判定收发方向）")
    parser.add_argument("--account", default="", help="账号 QQ 号，用于自动定位解密后的数据库")
    parser.add_argument("--tencent-files", default="", help="自定义 Tencent Files 目录")
    parser.add_argument("--qqnt-export", default="", help="QQNT_Export 目录（仅用于表情名称）")
    parser.add_argument("--with-paths", action="store_true",
                        help="在图片/视频消息中附带本地路径与 URL")
    parser.add_argument("--bom", action="store_true", help="输出文件带 UTF-8 BOM（旧版记事本友好）")
    parser.add_argument("--list-peers", action="store_true", help="列出数据库中的单聊对象及消息数")
    parser.add_argument("--limit", type=int, default=40, help="--list-peers 显示数量（默认 40）")
    parser.add_argument("-q", "--quiet", action="store_true", help="只输出结果文件路径")
    return parser


def _parse_formats(text: str) -> "list[str]":
    parts = [part.strip().lower() for part in str(text or "all").split(",") if part.strip()]
    if not parts or "all" in parts:
        return ["txt", "md", "json"]
    invalid = [part for part in parts if part not in ("txt", "md", "json")]
    if invalid:
        raise ToolError(f"未知输出格式: {', '.join(invalid)}（可选 txt / md / json / all）")
    return [name for name in ("txt", "md", "json") if name in parts]


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main(argv: "list[str] | None" = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    qqcommon.setup_console()

    try:
        formats = _parse_formats(args.formats)
        db_path = resolve_db_path(args)
        peer_text = args.peer_option or args.peer_qq
        peer = qqcommon.parse_qq_number(peer_text) if peer_text else ""
        my_qq = qqcommon.parse_qq_number(args.my_qq) if args.my_qq else ""

        if args.list_peers:
            peers = list_peers(db_path, max(1, args.limit))
            if not peers:
                print("数据库中没有任何单聊记录。")
                return 0
            print(f"单聊对象（{db_path}）:")
            print(f"  {'QQ号':<12}{'消息数':>8}   时间范围")
            for qq, count, start, end in peers:
                print(f"  {qq:<12}{count:>8}   {start} ~ {end}")
            print("\n下一步: python export_messages.py --peer <QQ号> --account <自己的QQ号>")
            return 0

        if not peer:
            raise ToolError(
                "缺少对方 QQ 号。用法: export_messages.py <db> <对方QQ号> [输出目录]，"
                "或先运行 --list-peers 查看可用会话。"
            )

        output_dir = Path(args.output_dir).expanduser() if args.output_dir else Path.cwd()
        output_dir.mkdir(parents=True, exist_ok=True)

        qqnt_export = qqcommon.find_qqnt_export(args.qqnt_export or None)
        emoji_names = qq_elements.load_emoji_names(qqnt_export)
        options = ElementOptions(with_paths=args.with_paths, emoji_names=emoji_names)

        if not args.quiet:
            print(f"数据库 : {db_path}")
            print(f"对方 QQ: {peer}")
            print(f"我的 QQ: {my_qq or '未指定（按消息标志位判断收发）'}")
            print("正在读取消息 ...")

        result = fetch_messages(db_path, peer, my_qq or None, options)
        messages = result.messages
        if not messages:
            print(f"没有找到与 {peer} 的聊天记录。", file=sys.stderr)
            peers = list_peers(db_path, 10)
            if peers:
                print("可用会话（前 10 个）:", file=sys.stderr)
                for qq, count, _start, _end in peers:
                    print(f"  {qq}  ({count} 条)", file=sys.stderr)
            print("请确认对方 QQ 号是否正确。", file=sys.stderr)
            return 2

        encoding = "utf-8-sig" if args.bom else "utf-8"
        base = output_dir / BASENAME_TEMPLATE.format(peer=peer)
        source_label = db_path.name

        outputs: "list[Path]" = []
        if "txt" in formats:
            outputs.append(export_txt(messages, peer, base.with_suffix(".txt"), encoding))
        if "md" in formats:
            outputs.append(export_md(messages, peer, base.with_suffix(".md"),
                                     source_label, encoding))
        if "json" in formats:
            outputs.append(export_json(messages, peer, my_qq, base.with_suffix(".json"),
                                       source_label, encoding))

        start, end = _date_range(messages)
        if not args.quiet:
            print(f"共 {len(messages)} 条消息，{start} ~ {end}")
            if result.peer_uids:
                shown = sorted(result.peer_uids)
                more = " ..." if len(shown) > 3 else ""
                print("匹配到的对方 UID: " + ", ".join(shown[:3]) + more)
            if result.stats.unknown_types:
                unknowns = ", ".join(f"{code}×{count}" for code, count
                                     in sorted(result.stats.unknown_types.items()))
                print(f"[提示] 未识别的消息段类型: {unknowns}（已标记为 [未知类型:N]）")
            if result.stats.failures:
                print(f"[提示] {result.stats.failures} 条消息体解析失败（内容可能缺失）")
            print(f"按年统计: {_year_stats(messages)}")
            print()
        for path in outputs:
            size = human_bytes(path.stat().st_size) or "0 B"
            print(f"[{path.suffix.lstrip('.').upper():<4}] {path}  ({size})")
        if not args.quiet:
            print(f"\n完成：{len(outputs)} 个文件已写入 {output_dir}")
        return 0

    except ToolError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("已取消。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
