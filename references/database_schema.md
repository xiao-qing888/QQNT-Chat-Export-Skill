# NTQQ Database Schema Reference

## Database Location
`D:\Users\Administrator\Documents\Tencent Files\<QQ号>\nt_qq\nt_db\`

Key databases:
- `nt_msg.db` — main message store (c2c_msg_table, group_msg_table, nt_uid_mapping_table)
- `group_info.db` — group metadata
- `recent_contact.db` — recent contacts
- `profile_info.db` — user profiles

## nt_msg.db: c2c_msg_table Columns

| Index | Column | Type | Description |
|-------|--------|------|-------------|
| 0 | 40001 | INTEGER PK | message id |
| 1 | 40002 | INTEGER | random |
| 2 | 40003 | INTEGER | seq |
| 3 | 40010 | INTEGER | chat type |
| 4 | 40011 | INTEGER | msg type |
| 5 | 40012 | INTEGER | sub msg type |
| 6 | 40013 | INTEGER | sender_flag (0=received, 1/2=sent, 8=forwarded) |
| 7 | 40020 | TEXT | sender_uid (real UID) |
| 8 | 40026 | INTEGER | |
| 9 | 40021 | TEXT | interlocutor_uid (real UID; old msgs use masked UID) |
| 10 | 40027 | INTEGER | |
| 11 | 40040 | INTEGER | |
| 12 | 40041 | INTEGER | status |
| 13 | 40050 | INTEGER | timestamp (unix) |
| 14 | 40052 | INTEGER | |
| 15 | 40090 | TEXT | group name card |
| 16 | 40093 | TEXT | nickname (only self) |
| 17 | 40800 | BLOB | message_body (protobuf Elements) |
| 18 | 40900 | BLOB | forwarded/quoted cache |
| 19 | 40105 | INTEGER | |
| 20 | 40005 | INTEGER | |
| 21 | 40058 | INTEGER | day timestamp |
| 22 | 40006 | INTEGER | |
| 23 | 40100 | INTEGER | @ status |
| 24 | 40600 | BLOB | |
| 25 | 40060 | INTEGER | |
| 31 | 40030 | INTEGER | interlocutor_num (peer QQ number; NULL in old msgs) |
| 32 | 40033 | INTEGER | sender_num (QQ number) |

## nt_uid_mapping_table Columns

| Column | Type | Description |
|--------|------|-------------|
| 48901 | INTEGER PK | id |
| 48902 | TEXT | uid — MASKED uid (u_***********_QQNUM), DO NOT use for filtering |
| 48912 | TEXT | real UID (u_XXXXXXXXXXXXXXXX) |
| 1002 | INTEGER | qq_num |

## Known Issues

### 1. QQNT_Export UID Mapping Bug
`num_to_uid()` returns column 48902 (masked UID), but c2c_msg_table.40021 stores the real UID (column 48912). Filtering by masked UID only matches old messages (pre-2023) where 40030 is NULL.

**Fix**: Query directly with `WHERE [40030] = ? OR [40021] = ?` using QQ number and masked UID.

### 2. Wrong HMAC Algorithm
Tutorials and nt_msg_db_util use `cipher_hmac_algorithm = HMAC_SHA1`, but QQ 9.9.x requires `HMAC_SHA512`.

**Correct PRAGMA order**:
```sql
PRAGMA cipher_page_size = 4096;       -- must be BEFORE key
PRAGMA key = '<key>';
PRAGMA kdf_iter = 4000;
PRAGMA cipher_hmac_algorithm = HMAC_SHA512;
PRAGMA cipher_kdf_algorithm = PBKDF2_HMAC_SHA512;
```

### 3. ASCII Key Corruption
Original `windows_ntqq_get_key.ps1` uses `Encoding.ASCII.GetString` which replaces non-ASCII bytes with `?`. Use the hex-output version (`get_key_hex.ps1`).

### 4. Notice Element Nested Tuple
`BaseNotice._parse_info()` returns `("[提示]", None)` when info is empty, and `_get_content()` wraps it again → nested tuple. Handle in extraction code.

### 5. Database Header
Each .db file has a 1024-byte custom header starting with "SQLite header 3" and "QQ_NT DB". Strip before decrypting. Header contains a 128-char hex string (64 bytes) and protobuf metadata (version, HMAC algo, timestamp).

## Dependencies
- `sqlcipher3` (pre-installed; pysqlcipher3 fails to compile on Windows)
- `protobuf==6.31.1`
- `reportlab` (for PDF)
- QQNT_Export venv Python for element_pb2
