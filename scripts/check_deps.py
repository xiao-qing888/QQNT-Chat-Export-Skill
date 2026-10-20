"""
Dependency checker for qq-nt-chat-export v2.

Checks Python environment, required pip packages, and the QQNT_Export
protobuf module. Automatically installs missing packages via pip.

Usage:
    python check_deps.py [--qqnt-export PATH] [--no-install]

Exit codes:
    0 - all dependencies satisfied (or successfully installed)
    1 - critical dependency missing and cannot be resolved
"""
import sys
import os
import importlib
import subprocess
import argparse

# ---------------------------------------------------------------------------
# Required pip packages: (import_name, pip_name, min_version_or_None, critical)
# critical=True means the whole workflow cannot run without it.
# ---------------------------------------------------------------------------
REQUIRED_PACKAGES = [
    ("sqlcipher3", "sqlcipher3", None, True),       # database decryption
    ("google.protobuf", "protobuf", "6.31.1", True),  # message body parsing
    ("tqdm", "tqdm", None, False),                   # QQNT_Export import chain
    ("lxml", "lxml", None, False),                   # QQNT_Export import chain
    ("humanize", "humanize", None, False),           # QQNT_Export import chain
    ("sqlalchemy", "SQLAlchemy", None, False),       # QQNT_Export import chain
    ("jinja2", "jinja2", None, False),               # QQNT_Export import chain
    ("PIL", "pillow", None, False),                  # image element parsing
    ("mutagen", "mutagen", None, False),             # voice/video element parsing
]

# Default QQNT_Export location (contains element_pb2.py and exporter package)
DEFAULT_QQNT_EXPORT = r"D:\Users\Administrator\Desktop\QQNT_output\QQNT_Export"


def check_python_version():
    """Return True if Python >= 3.8."""
    v = sys.version_info
    ok = v.major >= 3 and v.minor >= 8
    print(f"[{'OK' if ok else 'FAIL'}] Python {v.major}.{v.minor}.{v.micro} "
          f"(need >= 3.8)")
    return ok


def package_installed(import_name, min_version=None):
    """Check if a package is importable and optionally meets min version."""
    try:
        mod = importlib.import_module(import_name)
    except ImportError:
        return False
    if min_version:
        ver = getattr(mod, "__version__", None)
        if ver is None:
            # try pkg_resources
            try:
                import pkg_resources
                ver = pkg_resources.get_distribution(import_name).version
            except Exception:
                return True  # can't verify version, assume ok
        try:
            from packaging.version import parse as _parse
            return _parse(ver) >= _parse(min_version)
        except Exception:
            # fallback: simple string compare for major.minor
            return ver >= min_version
    return True


def install_package(pip_name, version=None):
    """Install a package via pip. Return True on success."""
    spec = f"{pip_name}=={version}" if version else pip_name
    print(f"  -> installing {spec} ...")
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", spec],
            capture_output=True, text=True, timeout=300
        )
        if r.returncode == 0:
            print(f"     installed OK")
            return True
        else:
            err = (r.stderr or "").strip().splitlines()
            last = err[-1] if err else "unknown error"
            print(f"     FAILED: {last[:120]}")
            return False
    except Exception as e:
        print(f"     FAILED: {e}")
        return False


def check_packages(auto_install=True):
    """Check all required packages, install missing ones. Return True if all ok."""
    all_ok = True
    for import_name, pip_name, min_ver, critical in REQUIRED_PACKAGES:
        if package_installed(import_name, min_ver):
            ver_tag = f" (>={min_ver})" if min_ver else ""
            print(f"[OK]   {pip_name}{ver_tag}")
            continue
        tag = "critical" if critical else "optional"
        print(f"[MISS] {pip_name} ({tag})")
        if auto_install:
            if install_package(pip_name, min_ver):
                # re-check after install
                if package_installed(import_name, min_ver):
                    print(f"[OK]   {pip_name} (installed now)")
                    continue
            if critical:
                all_ok = False
        else:
            if critical:
                all_ok = False
    return all_ok


def check_qqnt_export(path):
    """Check QQNT_Export directory and element_pb2.py. Return True if ok."""
    if not os.path.isdir(path):
        print(f"[FAIL] QQNT_Export directory not found: {path}")
        print(f"       Run install.ps1 to auto-download, or set --qqnt-export to the correct path.")
        print(f"       Source: https://github.com/Tealina28/QQNT_Export")
        return False
    elem = os.path.join(path, "element_pb2.py")
    if not os.path.isfile(elem):
        print(f"[FAIL] element_pb2.py not found in {path}")
        print(f"       Re-run install.ps1 to repair the QQNT_Export installation.")
        return False
    exporter_init = os.path.join(path, "exporter", "txt", "__init__.py")
    if not os.path.isfile(exporter_init):
        print(f"[WARN] exporter.txt package incomplete in {path} (some message types may fail)")
    print(f"[OK]   QQNT_Export at {path}")
    return True


def check_qq_installed():
    """Check if QQ NT is installed. Non-critical (key extraction needs it)."""
    candidates = [
        r"D:\Program Files\Tencent\QQNT\QQ.exe",
        r"C:\Program Files\Tencent\QQNT\QQ.exe",
        r"C:\Program Files (x86)\Tencent\QQNT\QQ.exe",
    ]
    for p in candidates:
        if os.path.isfile(p):
            print(f"[OK]   QQ NT installed: {p}")
            return True
    # try registry-less: common locations
    print("[WARN] QQ NT not found in standard paths; key extraction may fail")
    return False


def main():
    parser = argparse.ArgumentParser(description="Check qq-nt-chat-export dependencies")
    parser.add_argument("--qqnt-export", default=DEFAULT_QQNT_EXPORT,
                        help="Path to QQNT_Export directory")
    parser.add_argument("--no-install", action="store_true",
                        help="Do not auto-install missing packages")
    args = parser.parse_args()

    print("=" * 60)
    print("qq-nt-chat-export v2 - Dependency Check")
    print("=" * 60)

    results = []

    print("\n--- Python ---")
    results.append(check_python_version())

    print("\n--- Python packages ---")
    results.append(check_packages(auto_install=not args.no_install))

    print("\n--- QQNT_Export (protobuf module) ---")
    results.append(check_qqnt_export(args.qqnt_export))

    print("\n--- QQ NT installation ---")
    check_qq_installed()  # non-critical, don't add to results

    print("\n" + "=" * 60)
    if all(results):
        print("ALL DEPENDENCIES SATISFIED")
        print("=" * 60)
        sys.exit(0)
    else:
        print("SOME CRITICAL DEPENDENCIES ARE MISSING")
        print("Resolve the issues above before running the export.")
        print("TIP: Run install.ps1 to auto-install all software and dependencies.")
        print("=" * 60)
        sys.exit(1)


if __name__ == "__main__":
    main()
