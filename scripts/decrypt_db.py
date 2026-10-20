"""
Decrypt all NTQQ databases for a given account.
Correct PRAGMA for QQ 9.9.x: cipher_hmac_algorithm = HMAC_SHA512 (NOT HMAC_SHA1).

Usage:
    python decrypt_db.py <qq_number> <key> [--src SRC_DIR] [--dst DST_DIR]

Arguments:
    qq_number  - QQ account number
    key        - Database encryption key (from get_key_hex.ps1)
    --src      - Source nt_db directory (default: auto-detect)
    --dst      - Output directory for decrypted databases
"""
import os, sys, argparse, sqlite3
import sqlcipher3.dbapi2 as sc

HEADER_SIZE = 1024


def decrypt_db(src_path, dst_path, key):
    """Decrypt a single database file."""
    clear_path = dst_path + ".clear"
    with open(src_path, "rb") as f:
        f.seek(HEADER_SIZE)
        with open(clear_path, "wb") as out:
            out.write(f.read())

    conn = sc.connect(clear_path, isolation_level=None)
    conn.execute("PRAGMA cipher_page_size = 4096;")
    conn.execute(f"PRAGMA key = '{key}';")
    conn.execute("PRAGMA kdf_iter = 4000;")
    conn.execute("PRAGMA cipher_hmac_algorithm = HMAC_SHA512;")
    conn.execute("PRAGMA cipher_kdf_algorithm = PBKDF2_HMAC_SHA512;")

    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()]

    plain = sqlite3.connect(dst_path)
    plain.execute("PRAGMA journal_mode = WAL;")
    plain.execute("PRAGMA synchronous = NORMAL;")

    total_rows = 0
    for table in tables:
        if table.startswith("sqlite_"):
            continue
        try:
            ddl = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()[0]
            plain.execute(ddl)
            ncols = len(conn.execute(f"PRAGMA table_info('{table}')").fetchall())
            ph = ",".join("?" * ncols)
            last_rowid = 0
            while True:
                rows = conn.execute(
                    f'SELECT rowid, * FROM "{table}" WHERE rowid > ? ORDER BY rowid LIMIT 5000',
                    (last_rowid,)
                ).fetchall()
                if not rows:
                    break
                last_rowid = rows[-1][0]
                plain.executemany(
                    f'INSERT OR IGNORE INTO "{table}" VALUES ({ph})',
                    [r[1:] for r in rows]
                )
                total_rows += len(rows)
            plain.commit()
        except Exception as e:
            print(f"    Warning: table {table}: {e}")

    for (ddl,) in conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"
    ).fetchall():
        try:
            plain.execute(ddl)
        except:
            pass
    plain.commit()
    conn.close()
    plain.close()
    os.remove(clear_path)
    return total_rows, len(tables)


def main():
    parser = argparse.ArgumentParser(description="Decrypt NTQQ databases")
    parser.add_argument("qq_number", help="QQ account number")
    parser.add_argument("key", help="Database encryption key")
    parser.add_argument("--src", help="Source nt_db directory")
    parser.add_argument("--dst", help="Output directory")
    args = parser.parse_args()

    if args.src:
        src_dir = args.src
    else:
        src_dir = rf"D:\Users\Administrator\Documents\Tencent Files\{args.qq_number}\nt_qq\nt_db"

    if args.dst:
        dst_dir = args.dst
    else:
        dst_dir = rf"D:\Users\Administrator\Desktop\QQNT_output\decrypted_{args.qq_number}"

    os.makedirs(dst_dir, exist_ok=True)
    db_files = [f for f in os.listdir(src_dir) if f.endswith(".db")]
    print(f"Found {len(db_files)} databases in {src_dir}")

    success, failed = 0, []
    for db_file in sorted(db_files):
        src = os.path.join(src_dir, db_file)
        dst = os.path.join(dst_dir, db_file)
        if os.path.exists(dst):
            print(f"  [skip] {db_file}")
            success += 1
            continue
        try:
            rows, ntables = decrypt_db(src, dst, args.key)
            print(f"  [ok] {db_file}: {ntables} tables, {rows:,} rows")
            success += 1
        except Exception as e:
            print(f"  [FAIL] {db_file}: {str(e)[:100]}")
            failed.append(db_file)
            if os.path.exists(dst):
                os.remove(dst)

    print(f"\nDone: {success}/{len(db_files)} decrypted")
    if failed:
        print(f"Failed: {failed}")


if __name__ == "__main__":
    main()
