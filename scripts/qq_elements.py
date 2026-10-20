#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Dependency-free decoder for the protobuf message bodies stored by NTQQ.

The message body column (``40800``) of ``c2c_msg_table`` / ``group_msg_table``
holds a protobuf message::

    message Elements { repeated Element elements = 40800; }

``element.proto`` of the QQNT_Export project documents the ``Element`` fields;
the numbers used below were taken from it and verified against real databases.

Why not use QQNT_Export / ``element_pb2``?
    * the generated module is pinned to one protobuf runtime version and breaks
      after a protobuf upgrade ("[解析错误]" everywhere);
    * its renderer returns nested tuples / bare strings, which loses content;
    * the exporter package drags in lxml, humanize, SQLAlchemy, pillow, mutagen.

This module parses the wire format directly, so the export works with the
Python standard library alone.  ``QQNT_Export/emojis.py`` is loaded only to
give system emojis friendly names; that is entirely optional.
"""

from __future__ import annotations

import importlib.util
import json
import re
from dataclasses import dataclass, field
from html import unescape
from pathlib import Path

from qqcommon import human_bytes

__all__ = [
    "Element",
    "ElementOptions",
    "ParserStats",
    "parse_body",
    "parse_single_element",
    "load_emoji_names",
    "KNOWN_TYPE_LABELS",
]

# ---------------------------------------------------------------------------
# Wire format primitives
# ---------------------------------------------------------------------------
_WIRE_VARINT = 0
_WIRE_FIXED64 = 1
_WIRE_BYTES = 2
_WIRE_FIXED32 = 5

#: protobuf field numbers inside the message body / Element message
FIELD_ELEMENTS = 40800

F_ID = 45001
F_TYPE = 45002
F_TEXT = 45101
F_FILE_NAME = 45402
F_FILE_SIZE = 45405
F_VIDEO_LEN = 45410
F_ORIGINAL = 45418
F_MD5 = 45406
F_SENDER_NUM = 47403
F_QUOTED_TS = 47404
F_QUOTED_ELEMENT = 47423
F_INTERLOCUTOR_NUM = 47411
F_EMOJI_ID = 47601
F_EMOJI_TEXT = 47602
F_APPLICATION = 47901
F_CALL_STATUS = 48153
F_CALL_TEXT = 48157
F_FEED_TITLE = 48175
F_FEED_CONTENT = 48176
F_FEED_TEXT = 48178
F_FEED_URL = 48180
F_NOTICE_INFO = 48214
F_NOTICE_INFO2 = 48271
F_REDPACKET = 48403
F_REDPACKET_GREETING = 48443
F_REDPACKET_PROMPT = 48444
F_REDPACKET_SUMMARY = 48448
F_IMAGE_TEXT = 45815
F_IMAGE_PATH = 45812
F_IMAGE_URL = 45804
F_VOICE_TEXT = 45923
F_VOICE_LEN = 45906
F_VIDEO_PATH = 46403

#: Element.type -> Chinese label
KNOWN_TYPE_LABELS = {
    1: "文本",
    2: "图片",
    3: "文件",
    4: "语音",
    5: "视频",
    6: "表情",
    7: "引用",
    8: "提示",
    9: "红包",
    10: "应用消息",
    16: "聊天记录",
    21: "通话",
    26: "动态",
}


def _read_varint(buffer: bytes, index: int):
    value = 0
    shift = 0
    length = len(buffer)
    while index < length:
        byte = buffer[index]
        index += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, index
        shift += 7
        if shift > 63:
            raise ValueError("varint overflow")
    raise ValueError("truncated varint")


def iter_fields(buffer: bytes):
    """Yield ``(field_number, wire_type, value)`` for a protobuf buffer.

    Unknown/unsupported wire types simply end the iteration; QQ occasionally
    appends garbage to old rows and we must never raise on those.
    """
    index = 0
    length = len(buffer)
    while index < length:
        try:
            key, index = _read_varint(buffer, index)
        except ValueError:
            return
        field_number = key >> 3
        wire_type = key & 0x07
        if field_number == 0:
            return
        if wire_type == _WIRE_VARINT:
            try:
                value, index = _read_varint(buffer, index)
            except ValueError:
                return
            yield field_number, wire_type, value
        elif wire_type == _WIRE_BYTES:
            try:
                size, index = _read_varint(buffer, index)
            except ValueError:
                return
            if size < 0 or index + size > length:
                return
            yield field_number, wire_type, buffer[index:index + size]
            index += size
        elif wire_type == _WIRE_FIXED64:
            if index + 8 > length:
                return
            yield field_number, wire_type, buffer[index:index + 8]
            index += 8
        elif wire_type == _WIRE_FIXED32:
            if index + 4 > length:
                return
            yield field_number, wire_type, buffer[index:index + 4]
            index += 4
        else:
            return


def _collect(buffer: bytes):
    """Group fields into ``{number: [values]}`` keeping declaration order."""
    grouped: "dict[int, list]" = {}
    for number, _wire, value in iter_fields(buffer):
        grouped.setdefault(number, []).append(value)
    return grouped


def _text(values, number: int, default: str = "") -> str:
    for value in values.get(number, ()):
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
    return default


def _int(values, number: int, default: int = 0) -> int:
    for value in values.get(number, ()):
        if isinstance(value, int):
            return value
    return default


def _bytes(values, number: int):
    for value in values.get(number, ()):
        if isinstance(value, bytes):
            return value
    return None


# ---------------------------------------------------------------------------
# Public data structures
# ---------------------------------------------------------------------------
@dataclass
class Element:
    """One rendered message segment."""

    label: str
    content: str
    type_code: "int | None" = None

    @property
    def kind(self) -> str:
        """Bare type name without the surrounding brackets."""
        text = self.label.strip()
        if text.startswith("[") and text.endswith("]"):
            return text[1:-1]
        return text


@dataclass
class ElementOptions:
    """Rendering switches."""

    with_paths: bool = False
    emoji_names: "dict[int, str]" = field(default_factory=dict)
    max_application_chars: int = 400


@dataclass
class ParserStats:
    """Counters that help diagnosing unsupported message types."""

    bodies: int = 0
    failures: int = 0
    unknown_types: "dict[int, int]" = field(default_factory=dict)

    def note_unknown(self, type_code: int) -> None:
        self.unknown_types[type_code] = self.unknown_types.get(type_code, 0) + 1


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------
def _clean(text: str, limit: "int | None" = None) -> str:
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(
        char for char in text if char in "\n\t" or ord(char) >= 32
    )
    text = text.replace("\u3000", " ")
    if limit and len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text.strip("\n")


def _join_lines(*parts) -> str:
    return "\n".join(part for part in parts if part)


def _hms(seconds) -> str:
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return ""
    if total <= 0:
        return ""
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{secs:02}"


def _notice_text(info: str, info2: str) -> str:
    """Extract the human readable part of a system notice element."""
    info = (info or "").strip()
    info2 = (info2 or "").strip()
    if info:
        text = info.replace("\\/", "/")
        pieces = [unescape(item) for item in re.findall(r'txt="([^"]*)"', text)]
        if pieces:
            return _clean(" ".join(pieces))
        stripped = re.sub(r"<[^>]+>", "", text)
        return _clean(unescape(stripped))
    if info2:
        candidate = info2.replace("\\/", "/")
        try:
            data = json.loads(candidate)
        except ValueError:
            try:
                import ast

                data = ast.literal_eval(candidate)
            except (ValueError, SyntaxError):
                data = None
        if isinstance(data, dict):
            items = data.get("items") or []
            pieces = []
            for item in items:
                if isinstance(item, dict):
                    value = item.get("txt")
                    if value:
                        pieces.append(str(value))
            if pieces:
                return _clean(" ".join(pieces))
        return _clean(candidate)
    return ""


def _application_text(raw: str, limit: int) -> str:
    """Pull the ``prompt`` out of an application/ark card."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    try:
        data = json.loads(raw.replace("\\/", "/"))
    except ValueError:
        data = None
    if isinstance(data, dict):
        prompt = data.get("prompt") or data.get("desc") or data.get("title")
        if prompt:
            return _clean(str(prompt), limit)
        for key in ("meta", "config"):
            nested = data.get(key)
            if isinstance(nested, dict):
                for value in nested.values():
                    if isinstance(value, dict):
                        prompt = value.get("title") or value.get("desc")
                        if prompt:
                            return _clean(str(prompt), limit)
    match = re.search(r'prompt="([^"]{0,200})"', raw)
    if match:
        return _clean(unescape(match.group(1)), limit)
    return _clean(raw, limit)


def _emoji_name(options: ElementOptions, emoji_id: int, emoji_text: str) -> str:
    text = _clean(emoji_text)
    if text:
        return text
    return options.emoji_names.get(int(emoji_id or 0), "")


# ---------------------------------------------------------------------------
# Element rendering
# ---------------------------------------------------------------------------
def _render(fields: dict, options: ElementOptions, stats: ParserStats) -> Element:
    type_code = _int(fields, F_TYPE)
    label = KNOWN_TYPE_LABELS.get(type_code)
    if label is None:
        stats.note_unknown(type_code)
        return Element(label=f"[未知类型:{type_code}]", content="", type_code=type_code)

    if type_code == 1:  # text
        return Element("[文本]", _clean(_text(fields, F_TEXT)), 1)

    if type_code == 2:  # image
        size = human_bytes(_int(fields, F_FILE_SIZE))
        head = " ".join(part for part in (_clean(_text(fields, F_IMAGE_TEXT)), size) if part)
        parts = [head]
        if options.with_paths:
            parts.append(_clean(_text(fields, F_IMAGE_PATH)))
            parts.append(_clean(_text(fields, F_IMAGE_URL)))
        return Element("[图片]", _join_lines(*parts), 2)

    if type_code == 3:  # file
        size = human_bytes(_int(fields, F_FILE_SIZE))
        name = _clean(_text(fields, F_FILE_NAME))
        return Element("[文件]", _clean(" ".join(part for part in (name, size) if part)), 3)

    if type_code == 4:  # voice
        seconds = _int(fields, F_VOICE_LEN)
        duration = f"{seconds}″" if seconds else ""
        spoken = _clean(_text(fields, F_VOICE_TEXT))
        return Element("[语音]", " ".join(part for part in (duration, spoken) if part), 4)

    if type_code == 5:  # video
        duration = _hms(_int(fields, F_VIDEO_LEN))
        name = _clean(_text(fields, F_FILE_NAME))
        size = human_bytes(_int(fields, F_FILE_SIZE))
        parts = [_clean(" ".join(part for part in (duration, name, size) if part))]
        if options.with_paths:
            parts.append(_clean(_text(fields, F_VIDEO_PATH)))
        return Element("[视频]", _join_lines(*parts), 5)

    if type_code == 6:  # emoji
        emoji_id = _int(fields, F_EMOJI_ID)
        name = _emoji_name(options, emoji_id, _text(fields, F_EMOJI_TEXT))
        content = f"{name}-{emoji_id}" if name else (f"ID:{emoji_id}" if emoji_id else "")
        return Element("[表情]", content, 6)

    if type_code == 7:  # quote / reply
        nested = _bytes(fields, F_QUOTED_ELEMENT)
        content = ""
        if nested:
            inner = _render(_collect(nested), options, stats)
            if inner.kind in ("文本", "表情", "图片", "文件", "语音", "视频"):
                # 文本类的被引用内容自带可读性，不必再套一层 [文本]
                content = inner.content or inner.label
            else:
                content = _join_lines(inner.label, inner.content)
        return Element("[引用]", content, 7)

    if type_code == 8:  # system notice
        content = _notice_text(_text(fields, F_NOTICE_INFO), _text(fields, F_NOTICE_INFO2))
        return Element("[提示]", content, 8)

    if type_code == 9:  # red packet
        packet = _collect(_bytes(fields, F_REDPACKET) or b"")
        summary = _clean(_text(packet, F_REDPACKET_SUMMARY))
        prompt = _clean(_text(packet, F_REDPACKET_PROMPT))
        greeting = _clean(_text(packet, F_REDPACKET_GREETING))
        return Element("[红包]", _clean(" ".join(p for p in (summary, greeting, prompt) if p)), 9)

    if type_code == 10:  # application / ark card
        content = _application_text(
            _text(fields, F_APPLICATION), options.max_application_chars
        )
        return Element("[应用消息]", content, 10)

    if type_code == 16:  # merged forward ("聊天记录")
        return Element("[聊天记录]", "合并转发的聊天记录，请用手机 QQ 查看", 16)

    if type_code == 21:  # call
        status = _clean(_text(fields, F_CALL_STATUS))
        text = _clean(_text(fields, F_CALL_TEXT))
        if text == status:  # QQ stores the same text in both fields
            text = ""
        return Element("[通话]", " ".join(part for part in (status, text) if part), 21)

    if type_code == 26:  # feed / channel card
        title = _clean(_text(_collect(_bytes(fields, F_FEED_TITLE) or b""), F_FEED_TEXT))
        body = _clean(_text(_collect(_bytes(fields, F_FEED_CONTENT) or b""), F_FEED_TEXT))
        url = _clean(_text(fields, F_FEED_URL))
        return Element("[动态]", _join_lines(title, body, url), 26)

    return Element(f"[{label}]", "", type_code)


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------
def parse_body(body: bytes, options: "ElementOptions | None" = None,
               stats: "ParserStats | None" = None) -> "list[Element]":
    """Decode one ``40800`` blob into a list of Elements."""
    options = options or ElementOptions()
    stats = stats or ParserStats()
    if not body:
        return []
    if not isinstance(body, (bytes, bytearray)):
        return []
    stats.bodies += 1
    body = bytes(body)
    try:
        elements = [value for number, wire, value in iter_fields(body)
                    if number == FIELD_ELEMENTS and wire == _WIRE_BYTES]
        if elements:
            rendered = []
            for blob in elements:
                rendered.append(_render(_collect(blob), options, stats))
            return rendered
        # Some rows store a bare Element (no Elements wrapper).
        fields = _collect(body)
        if F_TYPE in fields or F_TEXT in fields:
            return [_render(fields, options, stats)]
    except Exception:  # never let one broken row kill the export
        stats.failures += 1
    return []


def parse_single_element(blob: bytes, options: "ElementOptions | None" = None) -> "Element | None":
    """Decode a bare Element blob (used by tests and by quoted elements)."""
    options = options or ElementOptions()
    stats = ParserStats()
    try:
        return _render(_collect(bytes(blob)), options, stats)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Optional: friendly names for system emojis from QQNT_Export/emojis.py
# ---------------------------------------------------------------------------
def load_emoji_names(qqnt_export_dir) -> "dict[int, str]":
    if not qqnt_export_dir:
        return {}
    path = Path(qqnt_export_dir) / "emojis.py"
    if not path.is_file():
        return {}
    try:
        spec = importlib.util.spec_from_file_location("_qqnt_export_emojis", path)
        if spec is None or spec.loader is None:
            return {}
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        table = getattr(module, "emojis", None)
        if isinstance(table, dict):
            return {int(key): str(value) for key, value in table.items()
                    if str(key).lstrip("-").isdigit()}
    except Exception:
        return {}
    return {}
