"""
Export QQ C2C messages from a decrypted nt_msg.db to TXT / Markdown / JSON.

Bypasses QQNT_Export's broken UID mapping by querying interlocutor_num (col 40030)
and masked UID (col 40021) directly.

Usage:
    python export_messages.py <decrypted_nt_msg.db> <peer_qq_number> <output_dir>
                              [--format txt|md|json|all]

Column indices (by CREATE TABLE order):
  0:40001(id) 6:40013(sender_flag) 13:40050(time) 16:40093(nickname)
  17:40800(message_body protobuf) 31:40030(interlocutor_num) 32:40033(sender_num)
"""
import sys
import os
import json
import sqlite3
import argparse
from datetime import datetime
from collections import Counter

# QQNT_Export must be importable for protobuf parsing
QQNT_EXPORT_PATH = r"D:\Users\Administrator\Desktop\QQNT_output\QQNT_Export"
if os.path.exists(QQNT_EXPORT_PATH):
    sys.path.insert(0, QQNT_EXPORT_PATH)

import element_pb2
from exporter.txt.elements import (
    Text, Image, File, Voice, Video, Emoji, Notice, RedPacket, Application, Call, Feed
)

ELEMENTS_MAP = {
    1: Text, 2: Image, 3: File, 4: Voice, 5: Video, 6: Emoji,
    8: Notice, 9: RedPacket, 10: Application, 21: Call, 26: Feed,
}

IDX_TIME = 13
IDX_SENDER_FLAG = 6
IDX_BODY = 17


def parse_elements(body_bytes):
    """Parse protobuf body into list of (label, content)."""
    results = []
    if not body_bytes:
        return results
    elems = element_pb2.Elements()
    try:
        elems.ParseFromString(body_bytes)
    except Exception:
        return results
    for element in elems.elements:
        try:
            if element.type in ELEMENTS_MAP:
                handler = ELEMENTS_MAP[element.type](element)
                ct = handler.content
                if isinstance(ct, tuple):
                    label = str(ct[0]) if ct[0] is not None else ""
                    inner = ct[1]
                    if isinstance(inner, tuple):
                        content = str(inner[0]) if inner and inner[0] is not None else ""
                    else:
                        content = str(inner) if inner is not None else ""
                else:
                    label = str(ct) if ct is not None else ""
                    content = ""
                results.append((label, content))
            elif element.type == 7:
                sub = parse_elements(element.quotedElement.SerializeToString())
                results.append(("[被引用的消息]" + (sub[0][0] if sub else ""),
                                sub[0][1] if sub else ""))
            else:
                results.append((f"[未知类型:{element.type}]", ""))
        except Exception as e:
            results.append((f"[解析错误:{element.type}]", str(e)))
    return results


def direction_of(sender_flag):
    """Convert sender_flag to human-readable direction."""
    if sender_flag == 0:
        return "收"
    elif sender_flag in (1, 2):
        return "发"
    elif sender_flag == 8:
        return "转发"
    return f"未知({sender_flag})"


def direction_en(direction_cn):
    """Chinese direction to English key for JSON."""
    return {"收": "received", "发": "sent", "转发": "forwarded"}.get(
        direction_cn, "unknown")


def row_to_message(row):
    """Convert a database row to a structured message dict."""
    try:
        ts = datetime.fromtimestamp(row[IDX_TIME])
        ts_str = ts.strftime("%Y-%m-%d %H:%M:%S")
        date_str = ts.strftime("%Y-%m-%d")
        time_str = ts.strftime("%H:%M:%S")
    except Exception:
        ts_str = f"时间错误({row[IDX_TIME]})"
        date_str = "未知"
        time_str = "未知"

    direction = direction_of(row[IDX_SENDER_FLAG])
    elements = parse_elements(row[IDX_BODY])

    # Build combined text content
    parts = []
    msg_type = "文本"
    for label, content in elements:
        if label:
            parts.append(label)
        if content:
            parts.append(content)
    if not elements:
        parts.append("[空消息]")

    # Determine primary type from first element label
    if elements and elements[0][0]:
        raw_type = elements[0][0].strip("[]")
        if raw_type:
            msg_type = raw_type

    return {
        "timestamp": ts_str,
        "date": date_str,
        "time": time_str,
        "direction": direction,
        "direction_en": direction_en(direction),
        "type": msg_type,
        "content": "\n".join(parts),
        "elements": [{"label": l, "content": c} for l, c in elements],
    }


def fetch_messages(db_path, peer_qq):
    """Query all C2C messages with the given peer."""
    conn = sqlite3.connect(db_path)
    masked_uid = f"u_***********_{peer_qq}"
    rows = conn.execute(
        "SELECT * FROM c2c_msg_table WHERE [40030] = ? OR [40021] = ? ORDER BY [40050] ASC",
        (peer_qq, masked_uid)
    ).fetchall()
    conn.close()

    messages = [row_to_message(r) for r in rows]
    return messages


# ---------------------------------------------------------------------------
# Format: TXT (original style)
# ---------------------------------------------------------------------------
def export_txt(messages, peer_qq, output_path):
    lines = [
        f"QQ聊天记录 - 与 {peer_qq}",
        f"共 {len(messages)} 条消息",
        "=" * 60,
        "",
    ]
    current_date = ""
    for msg in messages:
        if msg["date"] != current_date:
            current_date = msg["date"]
            lines.append(f"===== {current_date} =====")
            lines.append("")
        lines.append(f"{msg['timestamp']} {msg['direction']}")
        for elem in msg["elements"]:
            if elem["label"]:
                lines.append(elem["label"])
            if elem["content"]:
                lines.append(elem["content"])
        if not msg["elements"]:
            lines.append("[空消息]")
        lines.append("")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return output_path


# ---------------------------------------------------------------------------
# Format: Markdown
# ---------------------------------------------------------------------------
def export_md(messages, peer_qq, output_path):
    valid = [m for m in messages if m["date"] != "未知"]
    date_start = valid[0]["date"] if valid else "未知"
    date_end = valid[-1]["date"] if valid else "未知"

    lines = [
        f"# QQ 聊天记录 — 与 {peer_qq}",
        "",
        f"- **消息总数**：{len(messages)} 条",
        f"- **时间范围**：{date_start} ~ {date_end}",
        "",
        "---",
        "",
    ]

    # Year summary
    years = Counter(m["date"][:4] for m in valid)
    if years:
        lines.append("## 按年统计")
        lines.append("")
        lines.append("| 年份 | 消息数 |")
        lines.append("|------|--------|")
        for y in sorted(years):
            lines.append(f"| {y} | {years[y]} |")
        lines.append("")
        lines.append("---")
        lines.append("")

    current_date = ""
    for msg in messages:
        if msg["date"] != current_date:
            current_date = msg["date"]
            lines.append(f"## {current_date}")
            lines.append("")

        speaker = "我" if msg["direction"] == "发" else (
            "对方" if msg["direction"] == "收" else msg["direction"])
        # Markdown-safe content
        content = msg["content"].replace("\n", "  \n")
        type_tag = f"`{msg['type']}` " if msg["type"] != "文本" else ""
        lines.append(f"**{msg['time']} {speaker}**：{type_tag}{content}")
        lines.append("")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return output_path


# ---------------------------------------------------------------------------
# Format: JSON
# ---------------------------------------------------------------------------
def export_json(messages, peer_qq, my_qq, output_path):
    valid = [m for m in messages if m["date"] != "未知"]
    date_start = valid[0]["timestamp"] if valid else None
    date_end = valid[-1]["timestamp"] if valid else None

    # Strip internal helper fields for clean output
    clean_messages = []
    for m in messages:
        clean_messages.append({
            "timestamp": m["timestamp"],
            "date": m["date"],
            "time": m["time"],
            "direction": m["direction_en"],
            "direction_cn": m["direction"],
            "type": m["type"],
            "content": m["content"],
            "elements": m["elements"],
        })

    data = {
        "meta": {
            "my_qq": my_qq,
            "peer_qq": peer_qq,
            "total": len(messages),
            "date_range": {
                "start": date_start,
                "end": date_end,
            },
            "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        },
        "messages": clean_messages,
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return output_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Export QQ C2C messages to TXT / Markdown / JSON")
    parser.add_argument("db_path", help="Path to decrypted nt_msg.db")
    parser.add_argument("peer_qq", help="Peer QQ number")
    parser.add_argument("output_dir", help="Output directory")
    parser.add_argument("--format", choices=["txt", "md", "json", "all"],
                        default="all", help="Output format (default: all)")
    parser.add_argument("--my-qq", default="", help="Your QQ number (for JSON meta)")
    args = parser.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)

    print(f"Querying messages with peer {args.peer_qq} ...")
    messages = fetch_messages(args.db_path, args.peer_qq)
    print(f"Found {len(messages)} messages")

    valid = [m for m in messages if m["date"] != "未知"]
    if valid:
        print(f"Date range: {valid[0]['timestamp']} ~ {valid[-1]['timestamp']}")
        years = Counter(m["date"][:4] for m in valid)
        print(f"By year: {dict(sorted(years.items()))}")

    outputs = []
    base = os.path.join(args.output_dir, f"与{args.peer_qq}的聊天记录")

    if args.format in ("txt", "all"):
        p = export_txt(messages, args.peer_qq, base + ".txt")
        outputs.append(p)
        print(f"[TXT]  {p} ({os.path.getsize(p):,} bytes)")

    if args.format in ("md", "all"):
        p = export_md(messages, args.peer_qq, base + ".md")
        outputs.append(p)
        print(f"[MD]   {p} ({os.path.getsize(p):,} bytes)")

    if args.format in ("json", "all"):
        p = export_json(messages, args.peer_qq, args.my_qq, base + ".json")
        outputs.append(p)
        print(f"[JSON] {p} ({os.path.getsize(p):,} bytes)")

    print(f"\nDone: {len(outputs)} file(s) exported to {args.output_dir}")


if __name__ == "__main__":
    main()
