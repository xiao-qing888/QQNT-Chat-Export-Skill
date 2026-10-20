#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Self-test for the QQNT chat export toolchain.

The test is hermetic: it builds a synthetic NTQQ database (encrypted with the
same SQLCipher parameters QQ uses, wrapped in a 1024-byte QQ header), runs
``decrypt_db.py`` and ``export_messages.py`` as subprocesses, and verifies the
produced files.

Run it after changing any script::

    python selftest.py            # everything
    python selftest.py -v         # show subprocess output too

The decryption tests need ``sqlcipher3``; without it they are reported as
SKIP and only the export/parsing tests run.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import traceback
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import qq_elements  # noqa: E402
import qqcommon  # noqa: E402
from qq_elements import ElementOptions, parse_body  # noqa: E402

PASSED = 0
FAILED = 0
SKIPPED = 0
FAILURES: "list[str]" = []
VERBOSE = False

MY_QQ = "10002"
PEER_QQ = "10001"
OTHER_QQ = "20002"
KEY_TEXT = "fd??S4ngc$NAuHuY"  # 16 bytes, exactly like a real QQ key
KEY_ENV = "QQNT_DB_KEY"

# ---------------------------------------------------------------------------
# tiny protobuf encoder (mirrors element.proto)
# ---------------------------------------------------------------------------
F_ELEMENTS = 40800
F_ID = 45001
F_TYPE = 45002
F_TEXT = 45101
F_FILE_NAME = 45402
F_FILE_SIZE = 45405
F_VIDEO_LEN = 45410
F_QUOTED_ELEMENT = 47423
F_EMOJI_ID = 47601
F_EMOJI_TEXT = 47602
F_APPLICATION = 47901
F_CALL_STATUS = 48153
F_CALL_TEXT = 48157
F_NOTICE_INFO = 48214
F_IMAGE_TEXT = 45815
F_IMAGE_PATH = 45812
F_VOICE_TEXT = 45923
F_VOICE_LEN = 45906

STRING_FIELDS = {
    "text": F_TEXT,
    "file_name": F_FILE_NAME,
    "emoji_text": F_EMOJI_TEXT,
    "application": F_APPLICATION,
    "call_status": F_CALL_STATUS,
    "call_text": F_CALL_TEXT,
    "notice_info": F_NOTICE_INFO,
    "image_text": F_IMAGE_TEXT,
    "image_path": F_IMAGE_PATH,
    "voice_text": F_VOICE_TEXT,
}
VARINT_FIELDS = {
    "file_size": F_FILE_SIZE,
    "video_len": F_VIDEO_LEN,
    "emoji_id": F_EMOJI_ID,
    "voice_len": F_VOICE_LEN,
}


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _tag(field: int, wire: int) -> bytes:
    return _varint((field << 3) | wire)


def f_varint(field: int, value: int) -> bytes:
    return _tag(field, 0) + _varint(int(value))


def f_bytes(field: int, data: bytes) -> bytes:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return _tag(field, 2) + _varint(len(data)) + data


def element(type_code: int, nested: "bytes | None" = None, **values) -> bytes:
    out = f_varint(F_ID, 1) + f_varint(F_TYPE, type_code)
    for name, value in values.items():
        if name == "text":
            out += f_bytes(F_TEXT, str(value))
        elif name in STRING_FIELDS:
            out += f_bytes(STRING_FIELDS[name], str(value))
        elif name in VARINT_FIELDS:
            out += f_varint(VARINT_FIELDS[name], int(value))
        else:  # pragma: no cover - keeps typos loud
            raise KeyError(f"unknown element field {name}")
    if nested:
        out += f_bytes(F_QUOTED_ELEMENT, nested)
    return out


def body(*elements: bytes) -> bytes:
    return b"".join(f_bytes(F_ELEMENTS, item) for item in elements)


# ---------------------------------------------------------------------------
# synthetic database
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE c2c_msg_table(
  [40001] INTEGER PRIMARY KEY,[40002] INTEGER,[40003] INTEGER,[40010] INTEGER,
  [40011] INTEGER,[40012] INTEGER,[40013] INTEGER,[40020] TEXT,[40026] INTEGER,
  [40021] TEXT,[40027] INTEGER,[40040] INTEGER,[40041] INTEGER,[40050] INTEGER,
  [40052] INTEGER,[40090] TEXT,[40093] TEXT,[40800] BLOB,[40900] BLOB,
  [40105] INTEGER,[40005] INTEGER,[40058] INTEGER,[40006] INTEGER,[40100] INTEGER,
  [40600] BLOB,[40060] INTEGER,[40850] INTEGER,[40851] INTEGER,[40601] BLOB,
  [40801] BLOB,[40605] BLOB,[40030] INTEGER,[40033] INTEGER,[40062] BLOB,
  [40083] INTEGER,[40084] INTEGER,[40008] INTEGER,[40009] INTEGER,[40722] BLOB);
CREATE TABLE nt_uid_mapping_table(
  [48901] INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,[48902] TEXT,[48912] TEXT,[1002] INTEGER);
CREATE TABLE group_msg_table([40001] INTEGER PRIMARY KEY,[40050] INTEGER,[40800] BLOB);
"""

PEER_UID = "u_realPeerUid000000000001"
MY_UID = "u_realMineUid000000000002"


def _ts(year, month, day, hour, minute, second) -> int:
    return int(datetime(year, month, day, hour, minute, second).timestamp())


def build_rows() -> "list[tuple]":
    """Return rows for c2c_msg_table: (dict of column -> value)."""
    quote_inner = element(1, text="原始消息")
    rows = [
        # received text
        dict(id=1, flag=0, sender=PEER_QQ, peer=PEER_QQ, uid=PEER_UID,
             ts=_ts(2024, 1, 27, 19, 33, 6), body=body(element(1, text="不是我买"))),
        # sent text
        dict(id=2, flag=1, sender=MY_QQ, peer=PEER_QQ, uid=PEER_UID,
             ts=_ts(2024, 1, 27, 19, 33, 16), body=body(element(1, text="那干啥"))),
        # image (with size + path)
        dict(id=3, flag=2, sender=MY_QQ, peer=PEER_QQ, uid=PEER_UID,
             ts=_ts(2024, 1, 27, 19, 34, 0),
             body=body(element(2, image_text="[动画表情]", file_size=359360,
                               image_path=r"D:\Pics\a.jpg"))),
        # system emoji -> friendly name comes from QQNT_Export/emojis.py when present
        dict(id=4, flag=0, sender=PEER_QQ, peer=PEER_QQ, uid=PEER_UID,
             ts=_ts(2024, 1, 27, 19, 35, 0), body=body(element(6, emoji_id=97))),
        # quote + text in one message
        dict(id=5, flag=0, sender=PEER_QQ, peer=PEER_QQ, uid=PEER_UID,
             ts=_ts(2024, 1, 27, 19, 36, 0),
             body=body(element(7, nested=quote_inner), element(1, text="我引用一下"))),
        # notice
        dict(id=6, flag=3, sender=0, peer=PEER_QQ, uid=PEER_UID,
             ts=_ts(2024, 1, 27, 19, 37, 0),
             body=body(element(8, notice_info='<qqsys><nor txt="你撤回了一条消息"/></qqsys>'))),
        # application card
        dict(id=7, flag=0, sender=PEER_QQ, peer=PEER_QQ, uid=PEER_UID,
             ts=_ts(2024, 1, 27, 19, 38, 0),
             body=body(element(10, application='{"prompt":"推荐联系人：Tsuki"}'))),
        # merged forward chat record
        dict(id=8, flag=0, sender=PEER_QQ, peer=PEER_QQ, uid=PEER_UID,
             ts=_ts(2024, 1, 27, 19, 39, 0), body=body(element(16))),
        # unknown element type
        dict(id=9, flag=0, sender=PEER_QQ, peer=PEER_QQ, uid=PEER_UID,
             ts=_ts(2024, 1, 27, 19, 40, 0), body=body(element(99, text="新类型"))),
        # voice + file
        dict(id=10, flag=0, sender=PEER_QQ, peer=PEER_QQ, uid=PEER_UID,
             ts=_ts(2024, 1, 27, 19, 41, 0),
             body=body(element(4, voice_len=4, voice_text="语音转文字"),
                       element(3, file_name="报表.xlsx", file_size=58465))),
        # empty body
        dict(id=11, flag=0, sender=PEER_QQ, peer=PEER_QQ, uid=PEER_UID,
             ts=_ts(2024, 1, 27, 19, 42, 0), body=None),
        # invalid timestamp (0)
        dict(id=12, flag=0, sender=PEER_QQ, peer=PEER_QQ, uid=PEER_UID, ts=0,
             body=body(element(1, text="时间戳缺失"))),
        # millisecond timestamp
        dict(id=13, flag=0, sender=PEER_QQ, peer=PEER_QQ, uid=PEER_UID,
             ts=_ts(2023, 11, 15, 6, 13, 20) * 1000,
             body=body(element(1, text="毫秒时间戳"))),
        # legacy row: peer stored as masked UID only
        dict(id=14, flag=0, sender=PEER_QQ, peer=None, uid=f"u_***********_{PEER_QQ}",
             ts=_ts(2022, 5, 1, 8, 0, 0), body=body(element(1, text="旧消息"))),
        # legacy row: peer stored as real UID only (looked up via mapping table)
        dict(id=15, flag=0, sender=PEER_QQ, peer=None, uid=PEER_UID,
             ts=_ts(2022, 5, 2, 8, 0, 0), body=body(element(1, text="更旧的消息"))),
        # another peer -> must be excluded
        dict(id=16, flag=0, sender=OTHER_QQ, peer=OTHER_QQ, uid="u_other", ts=_ts(2024, 1, 27, 20, 0, 0),
             body=body(element(1, text="别人的消息"))),
    ]
    return rows


def fill_database(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(SCHEMA)
        conn.execute("INSERT INTO nt_uid_mapping_table([48902],[48912],[1002]) VALUES(?,?,?)",
                     (PEER_UID, None, int(PEER_QQ)))
        conn.execute("INSERT INTO nt_uid_mapping_table([48902],[48912],[1002]) VALUES(?,?,?)",
                     (MY_UID, None, int(MY_QQ)))
        for row in build_rows():
            conn.execute(
                "INSERT INTO c2c_msg_table([40001],[40013],[40020],[40021],[40030],"
                "[40033],[40050],[40093],[40800]) VALUES(?,?,?,?,?,?,?,?,?)",
                (row["id"], row["flag"], "" if not row["sender"] else PEER_UID,
                 row["uid"], row["peer"], row["sender"], row["ts"], "", row["body"]),
            )
        conn.commit()
    finally:
        conn.close()


def qq_header(version: str = "1.1.0.1", hmac: str = "HMAC_SHA1") -> bytes:
    payload = (f_bytes(2, "ab" * 64) + f_bytes(3, version) + f_bytes(4, hmac)
               + f_varint(5, 1722780151))
    head = bytearray(b"SQLite header 3\x00")
    head += b"\x04\x00\x10\x00" + b"\x00" * 12
    head += b"QQ_NT DB" + len(payload).to_bytes(4, "little") + payload
    return bytes(head).ljust(1024, b"\x00")


# ---------------------------------------------------------------------------
# test harness
# ---------------------------------------------------------------------------
def check(name: str, func) -> None:
    global PASSED, FAILED, SKIPPED
    try:
        result = func()
    except SkipTest as exc:
        SKIPPED += 1
        print(f"SKIP  {name}: {exc}")
        return
    except Exception as exc:
        FAILED += 1
        FAILURES.append(f"{name}: {exc}")
        print(f"FAIL  {name}: {exc}")
        if VERBOSE:
            traceback.print_exc()
        return
    PASSED += 1
    print(f"ok    {name}" + (f"  ({result})" if result else ""))


class SkipTest(Exception):
    pass


def assert_true(condition, message="assertion failed") -> None:
    if not condition:
        raise AssertionError(message)


def assert_in(needle, haystack) -> None:
    if needle not in haystack:
        raise AssertionError(f"缺少 {needle!r}")


def run_script(script: str, args: "list[str]", env: "dict | None" = None,
               expect: "int | None" = 0, stdin_env: bool = True):
    command = [sys.executable, str(HERE / script)] + args
    environment = dict(os.environ)
    environment["PYTHONIOENCODING"] = "utf-8"  # make child output decodable
    if stdin_env:
        environment.pop(KEY_ENV, None)
    if env:
        environment.update(env)
    completed = subprocess.run(command, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", env=environment,
                               cwd=str(HERE))
    if VERBOSE:
        print(f"  $ {' '.join(command)}")
        print("    stdout:", completed.stdout.strip()[:800])
        if completed.stderr.strip():
            print("    stderr:", completed.stderr.strip()[:800])
    if expect is not None and completed.returncode != expect:
        raise AssertionError(
            f"{script} 退出码 {completed.returncode}（期望 {expect}）\n"
            f"stdout: {completed.stdout[-800:]}\nstderr: {completed.stderr[-800:]}"
        )
    return completed


def have_sqlcipher() -> bool:
    try:
        import sqlcipher3.dbapi2  # noqa: F401

        return True
    except Exception:
        return False


def encrypt_database(plain: Path, target: Path, key_text: str) -> None:
    """Encrypt ``plain`` exactly like QQ does, then prepend the QQ header."""
    import sqlcipher3.dbapi2 as backend

    clear = target.with_suffix(".enc")
    if clear.exists():
        clear.unlink()
    conn = backend.connect(str(clear), isolation_level=None)
    try:
        conn.execute("PRAGMA cipher_page_size = 4096;")
        conn.execute("PRAGMA key = '%s';" % key_text.replace("'", "''"))
        conn.execute("PRAGMA kdf_iter = 4000;")
        conn.execute("PRAGMA cipher_hmac_algorithm = HMAC_SHA1;")
        conn.execute("PRAGMA cipher_kdf_algorithm = PBKDF2_HMAC_SHA512;")
        source = sqlite3.connect(str(plain))
        try:
            for (ddl,) in source.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL "
                "AND name NOT LIKE 'sqlite_%'"
            ):
                conn.execute(ddl)
            for row in source.execute(
                "SELECT [40001],[40013],[40020],[40021],[40030],[40033],[40050],[40093],[40800] "
                "FROM c2c_msg_table ORDER BY [40001]"
            ):
                conn.execute(
                    "INSERT INTO c2c_msg_table([40001],[40013],[40020],[40021],[40030],"
                    "[40033],[40050],[40093],[40800]) VALUES(?,?,?,?,?,?,?,?,?)", row)
            for row in source.execute("SELECT * FROM nt_uid_mapping_table"):
                conn.execute("INSERT INTO nt_uid_mapping_table VALUES(?,?,?,?)", row)
        finally:
            source.close()
        conn.commit()
    finally:
        conn.close()
    with open(clear, "rb") as handle:
        cipher_bytes = handle.read()
    clear.unlink()
    target.write_bytes(qq_header() + cipher_bytes)


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------
def test_key_normalisation() -> str:
    spec = qqcommon.normalize_key(KEY_TEXT)
    assert_true(spec.raw == KEY_TEXT.encode(), "raw bytes mismatch")
    assert_true(spec.hex == KEY_TEXT.encode().hex(), "hex mismatch")
    assert_true(spec.ascii_only, "ascii flag")
    hex_spec = qqcommon.normalize_key(KEY_TEXT.encode().hex())
    assert_true(hex_spec.raw == spec.raw, "hex input should decode to same key")
    wrapped = qqcommon.normalize_key("x'" + KEY_TEXT.encode().hex() + "'")
    assert_true(wrapped.raw == spec.raw, "x'..' wrapper")
    modes = [mode for mode, _sql, _note in qqcommon.key_pragma_candidates(spec)]
    assert_true(modes[0] == "passphrase", f"passphrase first, got {modes}")
    assert_true("rawkey" in modes and "hexpassphrase" in modes, modes)
    try:
        qqcommon.normalize_key("")
    except qqcommon.ToolError:
        return "passphrase/rawkey/hex 三种形式"
    raise AssertionError("empty key must raise")


def test_timestamps() -> str:
    assert_true(qqcommon.unix_to_datetime(0) is None, "0 -> None")
    assert_true(qqcommon.unix_to_datetime(None) is None, "None -> None")
    seconds = qqcommon.unix_to_datetime(1700000000)
    millis = qqcommon.unix_to_datetime(1700000000000)
    assert_true(seconds is not None and millis is not None, "both parse")
    assert_true(abs((seconds - millis).total_seconds()) < 2, "ms conversion")
    return "秒/毫秒/无效值"


def test_human_bytes() -> str:
    assert_true(qqcommon.human_bytes(359360) == "350.94 KiB", qqcommon.human_bytes(359360))
    assert_true(qqcommon.human_bytes(0) == "", "0 -> empty")
    assert_true(qqcommon.human_bytes(None) == "", "None -> empty")
    return qqcommon.human_bytes(359360)


def test_element_parser_edges() -> str:
    options = ElementOptions()
    assert_true(parse_body(b"") == [], "empty body")
    assert_true(parse_body(b"\xff\xff\xff\xff\xff\xff\xff\xff") == [], "garbage body")
    truncated = f_bytes(F_ELEMENTS, b"\x8a\x80\x01\x08")[:-2]
    assert_true(parse_body(truncated) == [], "truncated body")
    single = parse_body(body(element(1, text="裸文本")))
    assert_true(len(single) == 1 and single[0].content == "裸文本", single)
    direct = parse_body(element(1, text="无包装"))
    assert_true(len(direct) == 1 and direct[0].content == "无包装", direct)
    return "空/垃圾/截断/裸元素"


def test_qq_number() -> str:
    assert_true(qqcommon.parse_qq_number("10001") == "10001", "digits")
    assert_true(qqcommon.parse_qq_number(10001) == "10001", "int input")
    for bad in ("abc", "12", "10001x", ""):
        try:
            qqcommon.parse_qq_number(bad)
        except qqcommon.ToolError:
            continue
        raise AssertionError(f"{bad!r} should be rejected")
    return "合法/非法"


def test_qq_install_detection() -> str:
    install = qqcommon.detect_qq_install()
    if not install:
        raise SkipTest("本机未安装 QQ NT")
    assert_true(install.wrapper_node.is_file(), "wrapper.node should exist")
    assert_true(install.wrapper_node.name == "wrapper.node", "name")
    return f"{install.version or '?'} @ {install.install_dir}"


def test_decrypt_and_export(temp: Path) -> str:
    if not have_sqlcipher():
        raise SkipTest("未安装 sqlcipher3")

    tencent = temp / "Tencent Files"
    account_dir = tencent / MY_QQ / "nt_qq" / "nt_db"
    account_dir.mkdir(parents=True)
    plain = temp / "plain.db"
    fill_database(plain)

    encrypted = account_dir / "nt_msg.db"
    encrypt_database(plain, encrypted, KEY_TEXT)
    header = encrypted.read_bytes()[:1024]
    assert_true(header.startswith(b"SQLite header 3\x00"), "QQ header prefix")
    assert_true(b"QQ_NT DB" in header, "QQ header marker")

    out_dir = temp / "decrypted"
    result = run_script("decrypt_db.py", [
        MY_QQ, KEY_TEXT, "--src", str(account_dir), "--dst", str(out_dir),
        "--quiet",
    ])
    assert_in("成功 1", result.stdout)
    decrypted = out_dir / "nt_msg.db"
    assert_true(decrypted.is_file(), "decrypted db missing")
    assert_true(decrypted.read_bytes()[:16].startswith(b"SQLite format 3"), "not plaintext")

    conn = sqlite3.connect(str(decrypted))
    try:
        rows = conn.execute("SELECT COUNT(*) FROM c2c_msg_table").fetchone()[0]
        assert_true(rows == len(build_rows()), f"row count {rows}")
        assert_true(conn.execute("SELECT COUNT(*) FROM nt_uid_mapping_table").fetchone()[0] == 2,
                    "mapping rows")
    finally:
        conn.close()

    # -- export -----------------------------------------------------------
    export_dir = temp / "export"
    result = run_script("export_messages.py", [
        str(decrypted), PEER_QQ, str(export_dir), "--format", "all", "--my-qq", MY_QQ,
    ])
    assert_in("共 15 条消息", result.stdout)

    txt = (export_dir / f"与{PEER_QQ}的聊天记录.txt").read_text(encoding="utf-8")
    md = (export_dir / f"与{PEER_QQ}的聊天记录.md").read_text(encoding="utf-8")
    data = json.loads((export_dir / f"与{PEER_QQ}的聊天记录.json").read_text(encoding="utf-8"))

    assert_true(data["meta"]["total"] == 15, data["meta"]["total"])
    assert_true(data["meta"]["peer_qq"] == PEER_QQ, "meta.peer_qq")
    assert_true(data["meta"]["my_qq"] == MY_QQ, "meta.my_qq")
    assert_true(data["meta"]["stats"]["by_year"] == {"2022": 2, "2023": 1, "2024": 11},
                data["meta"]["stats"]["by_year"])

    for needle in ("[文本]", "[图片]", "[表情]", "[引用]", "[提示]", "[应用消息]",
                   "[聊天记录]", "[语音]", "[文件]", "未知类型:99"):
        assert_in(needle, txt)
    assert_in("不是我买", txt)
    assert_in("原始消息", txt)          # quoted content is now preserved
    assert_in("你撤回了一条消息", txt)   # notice text extracted from XML
    assert_in("推荐联系人：Tsuki", txt)
    assert_in("毫秒时间戳", txt)
    assert_in("350.94 KiB", txt)
    assert_in("===== 2024-01-27 =====", txt)
    assert_true("别人的消息" not in txt, "other peer leaked into export")
    assert_true("2024-01-27 19:33:06 收" in txt, "received direction")
    assert_true("2024-01-27 19:33:16 发" in txt, "sent direction")
    assert_true("时间无效" in txt, "invalid timestamp should be flagged")
    assert_true("系统" in txt, "system notice direction")

    assert_in("| 2024 | 11 |", md)
    assert_in("**19:33:06 对方**", md)

    directions = data["meta"]["stats"]["by_direction"]
    assert_true(directions.get("收", 0) >= 8, directions)
    assert_true(directions.get("发", 0) >= 2, directions)
    assert_true(directions.get("系统", 0) == 1, directions)

    quoted = [m for m in data["messages"] if m["type"] == "引用"]
    assert_true(len(quoted) == 1, "quote message")
    assert_in("原始消息", quoted[0]["elements"][0]["content"])
    assert_true(quoted[0]["elements"][0]["label"] == "[引用]", "quote label")
    return "解密 16 行 → 导出 15 条"


def test_export_guards(temp: Path) -> str:
    if not have_sqlcipher():
        raise SkipTest("未安装 sqlcipher3")
    tencent = temp / "Tencent Files"
    account_dir = tencent / MY_QQ / "nt_qq" / "nt_db"
    if not account_dir.is_dir():
        raise SkipTest("解密测试未运行")

    encrypted = account_dir / "nt_msg.db"

    # 1) exporting an encrypted database must fail with a clear hint
    result = run_script("export_messages.py", [str(encrypted), PEER_QQ, str(temp / "x")],
                        expect=1)
    assert_in("加密", result.stderr)

    # 2) a missing key for decryption must fail fast
    result = run_script("decrypt_db.py", [MY_QQ, "--src", str(account_dir),
                                          "--dst", str(temp / "nokey")], expect=1)
    assert_in("密钥", result.stderr)

    # 3) a wrong key must not create a broken output file
    wrong = "0000000000000000"
    out_dir = temp / "wrongkey"
    result = run_script("decrypt_db.py", [MY_QQ, wrong, "--src", str(account_dir),
                                          "--dst", str(out_dir), "--quiet"], expect=1)
    assert_in("解密失败", result.stderr)
    assert_true(not (out_dir / "nt_msg.db").exists(), "wrong key left a file behind")

    # 4) unknown peer -> exit code 2 with suggestions
    decrypted = temp / "decrypted" / "nt_msg.db"
    result = run_script("export_messages.py", [str(decrypted), "999999", str(temp / "y")],
                        expect=2)
    assert_in("没有找到", result.stderr)
    return "加密库/缺密钥/错密钥/无此会话"


def test_list_peers(temp: Path) -> str:
    decrypted = temp / "decrypted" / "nt_msg.db"
    if not decrypted.is_file():
        raise SkipTest("解密测试未运行")
    result = run_script("export_messages.py", [str(decrypted), "--list-peers"])
    assert_in(PEER_QQ, result.stdout)
    assert_in(OTHER_QQ, result.stdout)
    return "两个会话"


def test_key_from_env(temp: Path) -> str:
    if not have_sqlcipher():
        raise SkipTest("未安装 sqlcipher3")
    account_dir = temp / "Tencent Files" / MY_QQ / "nt_qq" / "nt_db"
    if not account_dir.is_dir():
        raise SkipTest("解密测试未运行")
    out_dir = temp / "envkey"
    run_script("decrypt_db.py", [MY_QQ, "--src", str(account_dir), "--dst", str(out_dir)],
               env={KEY_ENV: KEY_TEXT}, expect=0)
    assert_true((out_dir / "nt_msg.db").is_file(), "env key decryption failed")
    return "QQNT_DB_KEY 生效"


def test_compat_cli(temp: Path) -> str:
    """The v2.2 command line must keep working: db, peer, outdir, --format."""
    decrypted = temp / "decrypted" / "nt_msg.db"
    if not decrypted.is_file():
        raise SkipTest("解密测试未运行")
    out = temp / "compat"
    result = run_script("export_messages.py", [str(decrypted), PEER_QQ, str(out),
                                               "--format", "md"])
    assert_true((out / f"与{PEER_QQ}的聊天记录.md").is_file(), "md not written")
    assert_true(not (out / f"与{PEER_QQ}的聊天记录.json").exists(), "json must not be written")
    assert_in("按年统计", (out / f"与{PEER_QQ}的聊天记录.md").read_text(encoding="utf-8"))
    return "--format md"


def test_decrypt_skip_existing(temp: Path) -> str:
    if not have_sqlcipher():
        raise SkipTest("未安装 sqlcipher3")
    account_dir = temp / "Tencent Files" / MY_QQ / "nt_qq" / "nt_db"
    out_dir = temp / "decrypted"
    if not (out_dir / "nt_msg.db").is_file():
        raise SkipTest("解密测试未运行")
    before = (out_dir / "nt_msg.db").stat().st_mtime
    result = run_script("decrypt_db.py", [MY_QQ, KEY_TEXT, "--src", str(account_dir),
                                          "--dst", str(out_dir), "--quiet"], expect=0)
    assert_in("跳过 1", result.stdout)
    assert_true((out_dir / "nt_msg.db").stat().st_mtime == before, "file was rewritten")
    return "已存在时跳过"


def test_powershell_scripts() -> str:
    """PowerShell 5.1 decodes BOM-less UTF-8 as ANSI; Chinese text then breaks parsing."""
    scripts = sorted(HERE.glob("*.ps1"))
    if not scripts:
        raise SkipTest("没有 PowerShell 脚本")
    missing_bom = [path.name for path in scripts
                   if not path.read_bytes()[:3] == b"\xef\xbb\xbf"]
    assert_true(not missing_bom, "缺少 UTF-8 BOM（PS 5.1 会乱码）: " + ", ".join(missing_bom))

    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if not powershell:
        return f"{len(scripts)} 个脚本 BOM 正确（未找到 PowerShell，跳过语法检查）"

    failures = []
    for path in scripts:
        command = ("$e=$null; [void][System.Management.Automation.Language.Parser]::ParseFile("
                   f"'{path}', [ref]$null, [ref]$e); "
                   "if ($e.Count -gt 0) { $e[0].Message; exit 1 }")
        completed = subprocess.run([powershell, "-NoProfile", "-Command", command],
                                   capture_output=True, text=True,
                                   encoding="utf-8", errors="replace")
        if completed.returncode != 0:
            failures.append(f"{path.name}: {completed.stdout.strip()[:120]}")
    assert_true(not failures, "语法错误: " + "; ".join(failures))
    return f"{len(scripts)} 个脚本 BOM 与语法均正常"


def big_marker() -> None:  # pragma: no cover - kept for readability
    pass


def main() -> int:
    global VERBOSE
    parser = argparse.ArgumentParser(description="QQNT chat export self-test")
    parser.add_argument("-v", "--verbose", action="store_true", help="print subprocess output")
    parser.add_argument("--keep", action="store_true", help="keep the temporary directory")
    args = parser.parse_args()
    VERBOSE = args.verbose
    qqcommon.setup_console()

    print(f"Python : {sys.version.split()[0]} ({sys.executable})")
    print(f"加密库 : {'sqlcipher3 可用' if have_sqlcipher() else '未安装（解密测试将跳过）'}")
    print(f"QQNT   : {qqcommon.find_qqnt_export() or '未找到 QQNT_Export（表情名称将退化为编号）'}")
    print("-" * 60)

    temp = Path(tempfile.mkdtemp(prefix="qqnt_selftest_"))
    try:
        check("密钥规范化", test_key_normalisation)
        check("时间戳转换", test_timestamps)
        check("文件大小格式化", test_human_bytes)
        check("消息体解析边界", test_element_parser_edges)
        check("QQ 号校验", test_qq_number)
        check("QQ 安装检测", test_qq_install_detection)
        check("PowerShell 脚本编码/语法", test_powershell_scripts)
        check("解密 + 导出（合成库）", lambda: test_decrypt_and_export(temp))
        check("导出错误处理", lambda: test_export_guards(temp))
        check("会话列表", lambda: test_list_peers(temp))
        check("环境变量密钥", lambda: test_key_from_env(temp))
        check("兼容旧命令行", lambda: test_compat_cli(temp))
        check("重复解密跳过", lambda: test_decrypt_skip_existing(temp))
    finally:
        if args.keep:
            print(f"\n临时目录保留在: {temp}")
        else:
            shutil.rmtree(temp, ignore_errors=True)

    print("-" * 60)
    print(f"通过 {PASSED}，失败 {FAILED}，跳过 {SKIPPED}")
    if FAILURES:
        print("\n失败详情:")
        for item in FAILURES:
            print("  -", item)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
