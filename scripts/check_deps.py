#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Environment self-check for QQNT-chat-export.

Checks the Python version, the (single) required package ``sqlcipher3``, the
optional QQNT_Export data directory, the QQ NT installation and the QQ data
directory.  Missing packages can be installed automatically.

Usage::

    python check_deps.py                 # check + auto install
    python check_deps.py --no-install    # check only
    python check_deps.py --json          # machine readable result

Exit codes:
    0  everything needed for the export is available
    1  a required component is missing (and could not be installed)
"""

from __future__ import annotations

import argparse
import importlib
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import qqcommon
from qqcommon import ToolError

MIN_PYTHON = (3, 8)

#: packages that are really required.  ``sqlcipher3`` ships Windows wheels for
#: 0.6+; ``sqlcipher3-binary`` only has manylinux wheels (a common trap).
REQUIRED_PACKAGES = [
    ("sqlcipher3", "sqlcipher3", "SQLCipher 绑定（数据库解密）", True),
]

#: nice-to-have packages, never fatal
OPTIONAL_PACKAGES = [
    ("protobuf", "protobuf", "仅在直接使用 QQNT_Export 时需要，本工具已不再依赖"),
    ("lxml", "lxml", "仅 QQNT_Export 需要"),
    ("humanize", "humanize", "仅 QQNT_Export 需要"),
    ("sqlalchemy", "SQLAlchemy", "仅 QQNT_Export 需要"),
    ("jinja2", "jinja2", "仅 QQNT_Export 需要"),
    ("PIL", "pillow", "仅 QQNT_Export 需要"),
    ("mutagen", "mutagen", "仅 QQNT_Export 需要"),
    ("tqdm", "tqdm", "仅 QQNT_Export 需要"),
]


class Reporter:
    """Collects check results; prints either human readable or JSON."""

    def __init__(self, quiet: bool = False, as_json: bool = False):
        self.quiet = quiet
        self.as_json = as_json
        self.items: "list[dict]" = []
        self.ok = True
        try:
            self.use_color = bool(sys.stdout.isatty())
        except Exception:
            self.use_color = False

    def section(self, title: str) -> None:
        if not self.as_json and not self.quiet:
            print(f"\n--- {title} ---")

    def add(self, level: str, name: str, detail: str = "", fix: str = "") -> None:
        self.items.append({"level": level, "name": name, "detail": detail, "fix": fix})
        if level == "fail":
            self.ok = False
        if self.as_json or self.quiet:
            return
        tag = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL", "info": "INFO", "skip": "SKIP"}[level]
        color = {"ok": "\033[32m", "warn": "\033[33m", "fail": "\033[31m",
                 "info": "\033[36m", "skip": "\033[90m"}[level]
        reset = "\033[0m" if self.use_color else ""
        prefix = color if self.use_color else ""
        line = f"[{tag}] {name}"
        if detail:
            line += f" — {detail}"
        print(f"{prefix}{line}{reset}")
        if fix:
            print(f"       提示: {fix}")


def python_version_ok(reporter: Reporter) -> bool:
    version = sys.version_info
    text = f"{version.major}.{version.minor}.{version.micro}"
    if (version.major, version.minor) >= MIN_PYTHON:
        reporter.add("ok", f"Python {text}", str(Path(sys.executable)))
        return True
    reporter.add("fail", f"Python {text} 版本过低",
                 f"需要 >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]}",
                 "安装新版 Python: https://www.python.org/downloads/")
    return False


def _distribution_version(name: str) -> str:
    try:
        from importlib.metadata import version

        return version(name)
    except Exception:
        return ""


def _import_ok(import_name: str) -> "tuple[bool, str]":
    try:
        module = importlib.import_module(import_name)
    except ImportError as exc:
        return False, str(exc)
    except Exception as exc:  # broken DLL, missing VC runtime, ...
        return False, f"{type(exc).__name__}: {exc}"
    version = getattr(module, "__version__", "") or _distribution_version(
        import_name.split(".")[0])
    return True, str(version)


def install_package(pip_name: str, reporter: Reporter) -> bool:
    """pip install with a --user retry, using this interpreter."""
    commands = [[sys.executable, "-m", "pip", "install", pip_name]]
    for index, command in enumerate(commands):
        if not reporter.as_json and not reporter.quiet:
            print(f"       正在安装 {pip_name} ...", flush=True)
        try:
            result = subprocess.run(
                command + ["--disable-pip-version-check", "--no-input"],
                capture_output=True, text=True, timeout=600,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            reporter.add("fail", f"pip 调用失败: {pip_name}", str(exc))
            return False
        if result.returncode == 0:
            return True
        tail = (result.stderr or result.stdout or "").strip().splitlines()
        message = tail[-1][:200] if tail else f"退出码 {result.returncode}"
        # permission problems -> retry into the user site-packages
        if index == 0 and ("permission" in message.lower() or "denied" in message.lower()):
            commands.append([sys.executable, "-m", "pip", "install", "--user", pip_name])
            continue
        reporter.add("fail", f"安装失败: {pip_name}", message,
                     "检查网络/代理，或手动运行: python -m pip install " + pip_name)
        return False
    return False


def check_required_packages(reporter: Reporter, auto_install: bool) -> bool:
    all_ok = True
    for import_name, pip_name, purpose, critical in REQUIRED_PACKAGES:
        ok, detail = _import_ok(import_name)
        if ok:
            reporter.add("ok", pip_name, f"{purpose} {detail}".strip())
            continue
        reporter.add("warn", f"{pip_name} 未安装", f"{purpose}；{detail}")
        if not auto_install:
            reporter.add("fail", f"{pip_name} 缺失（未启用自动安装）", purpose,
                         f"python -m pip install {pip_name}")
            all_ok = False
            continue
        if install_package(pip_name, reporter):
            ok, detail = _import_ok(import_name)
            if ok:
                reporter.add("ok", f"{pip_name}（刚刚安装）", detail)
                continue
        if critical:
            all_ok = False
    return all_ok


def check_optional_packages(reporter: Reporter) -> None:
    missing = [pip_name for import_name, pip_name, _purpose in OPTIONAL_PACKAGES
               if not _import_ok(import_name)[0]]
    if missing:
        reporter.add("info", "可选组件未安装: " + ", ".join(missing),
                     "仅 QQNT_Export 需要，本工具的导出功能不依赖它们")


def check_qqnt_export(reporter: Reporter, explicit: str) -> None:
    path = qqcommon.find_qqnt_export(explicit or None)
    if not path:
        reporter.add(
            "info", "QQNT_Export 未找到",
            "可选组件：仅用于把系统表情显示成中文名（如 擦汗）",
            "如需表情名称，运行 install.ps1 或设置 QQNT_EXPORT_DIR",
        )
        return
    emoji_file = path / "emojis.py"
    if emoji_file.is_file():
        reporter.add("ok", "QQNT_Export（表情名称）", str(path))
    else:
        reporter.add("warn", "QQNT_Export 缺少 emojis.py", str(path),
                     "重新运行 install.ps1 下载完整目录")


def check_qq_install(reporter: Reporter) -> None:
    try:
        install = qqcommon.detect_qq_install()
    except ToolError as exc:
        reporter.add("warn", "QQ NT 检测失败", str(exc))
        return
    if not install:
        reporter.add("warn", "未找到 QQ NT 安装",
                     "提取密钥需要已安装并登录的 QQ NT",
                     "从 https://im.qq.com 安装 QQ NT")
        return
    detail = str(install.install_dir)
    if install.version:
        detail += f"（版本 {install.version}）"
    reporter.add("ok", "QQ NT 已安装", detail)


def check_tencent_files(reporter: Reporter, explicit: str) -> None:
    try:
        root = qqcommon.tencent_files_dir(explicit or None)
    except ToolError as exc:
        reporter.add("warn", "未找到 QQ 数据目录", str(exc).splitlines()[0],
                     "设置环境变量 QQ_TENCENT_FILES 或使用 --tencent-files")
        return
    try:
        accounts = qqcommon.list_accounts(explicit or None)
    except ToolError:
        accounts = []
    if not accounts:
        reporter.add("warn", "QQ 数据目录下没有账号", str(root),
                     "先登录一次 QQ NT，让其创建 nt_db 目录")
        return
    active = qqcommon.detect_active_account(explicit or None)
    names = ", ".join(account.number for account in accounts[:6])
    more = " ..." if len(accounts) > 6 else ""
    reporter.add("ok", f"QQ 数据目录（{len(accounts)} 个账号）", f"{root}\n        账号: {names}{more}")
    if active:
        reporter.add("info", "最近活跃账号", f"{active.number}（{active.last_active}）")


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_deps.py",
        description="检查 QQNT-chat-export 的运行环境",
    )
    parser.add_argument("--qqnt-export", default="", help="QQNT_Export 目录（可选组件）")
    parser.add_argument("--tencent-files", default="", help="自定义 Tencent Files 目录")
    parser.add_argument("--no-install", action="store_true", help="只检查，不自动安装")
    parser.add_argument("--json", action="store_true", help="输出 JSON（便于脚本调用）")
    parser.add_argument("-q", "--quiet", action="store_true", help="只输出结论")
    args = parser.parse_args(argv)
    qqcommon.setup_console()

    as_json = bool(args.json)
    reporter = Reporter(quiet=args.quiet, as_json=as_json)
    if not as_json and not args.quiet:
        print("=" * 64)
        print(f" {qqcommon.APP_NAME} v{qqcommon.VERSION} 环境检查")
        print("=" * 64)
        print(qqcommon.describe_python())

    reporter.section("Python")
    python_ok = python_version_ok(reporter)

    reporter.section("必需依赖")
    packages_ok = check_required_packages(reporter, auto_install=not args.no_install)

    reporter.section("可选组件")
    check_optional_packages(reporter)
    check_qqnt_export(reporter, args.qqnt_export)

    reporter.section("QQ 环境")
    check_qq_install(reporter)
    check_tencent_files(reporter, args.tencent_files)

    ok = python_ok and packages_ok and reporter.ok
    if as_json:
        print(json.dumps({"ok": ok, "items": reporter.items}, ensure_ascii=False, indent=2))
    elif not args.quiet:
        print()
        print("=" * 64)
        if ok:
            print(" 环境就绪：可以运行 run_export.ps1 导出聊天记录")
        else:
            print(" 环境不完整：请按上面的提示处理后再试")
            print(" TIP: 运行 install.ps1 可自动安装缺失组件")
        print("=" * 64)
    else:
        print("OK" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
