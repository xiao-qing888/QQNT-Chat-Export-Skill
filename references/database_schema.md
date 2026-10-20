# NTQQ 数据库结构参考（v3 实测校正）

> 本文档内容基于本机 QQ NT 9.9.36 的真实数据库核对。旧版文档中关于
> "掩码 UID 必须用于过滤"、"必须使用 HMAC_SHA512" 的说法与实测不符，已更正。

## 1. 数据库位置

```
<Documents>\Tencent Files\<QQ号>\nt_qq\nt_db\
```

`Documents` 应通过 Windows 已知文件夹接口获取（`qqcommon.user_documents_dir()`），
支持 OneDrive 重定向；也可用环境变量 `QQ_TENCENT_FILES` 覆盖。

| 数据库 | 内容 |
|--------|------|
| `nt_msg.db` | 消息主库：`c2c_msg_table`、`group_msg_table`、`nt_uid_mapping_table` 等 |
| `group_info.db` | 群资料 |
| `recent_contact.db` | 最近会话 |
| `profile_info.db` | 用户资料 |
| `buddy_msg_fts.db` / `group_msg_fts.db` | 全文索引（可能页损坏，解密失败无影响） |
| `login.db` | 位于 `<Tencent Files>\nt_qq\global\nt_db\` |

实际数量通常 60+（QQ 9.9.x）。每个库独立加密。

## 2. 文件头（1024 字节）

| 偏移 | 长度 | 内容 |
|------|------|------|
| `0x00` | 16 | `"SQLite header 3\0"` |
| `0x10` | 4 | `04 00 10 00`（页大小 0x1000 = 4096 相关标志） |
| `0x20` | 8 | `"QQ_NT DB"` 标记 |
| `0x28` | 4 | 紧随其后的 protobuf 元数据长度（uint32 LE，实测 157） |
| `0x2C` | n | protobuf：字段 2 = 128 字符 hex 串（64 字节随机值）、字段 3 = 格式版本、字段 4 = HMAC 算法、字段 5 = 创建时间戳 |
| 其余 | — | 全 0 补齐到 1024 字节 |

实测样本（账号 2045689098，2024-08-04 创建）：

```
格式版本 = 1.1.0.1
HMAC     = HMAC_SHA1
```

**结论**：HMAC 算法以文件头字段 4 为准；QQ 9.9.36 的库是 `HMAC_SHA1`。
旧文档写的"必须 HMAC_SHA512"会直接导致 `hmac check failed for pgno=1`。

## 3. 加密参数

```
PRAGMA cipher_page_size = 4096;            -- 必须在 key 之前
PRAGMA key = '<16 字节原始密钥>';           -- 口令是原始字节，不是 hex 文本
PRAGMA kdf_iter = 4000;
PRAGMA cipher_hmac_algorithm = HMAC_SHA1;  -- 见文件头
PRAGMA cipher_kdf_algorithm = PBKDF2_HMAC_SHA512;
```

要点：

1. SQLCipher 把 `PRAGMA key` 的内容当**口令**送进 PBKDF2。QQ 传给 `sqlite3_key_v2`
   的是 16 字节原始数据，因此 Python 侧必须使用**同一批字节**；用 32 字符的 hex 文本
   会推导出另一个密钥。
2. 若密钥含非 ASCII 字节，口令形式无法用文本表达，此时可尝试 raw key 形式
   `PRAGMA key = "x'<hex>'"`（跳过 KDF，语义不同，仅作兜底）。
3. `decrypt_db.py` 会按"头部提示 → 常见组合"的顺序自动验证，
   成功读取 `sqlite_master` 即锁定参数组合。

## 4. `c2c_msg_table` 列

| 列 | 类型 | 说明 |
|----|------|------|
| `40001` | INTEGER PK | 消息 ID |
| `40003` | INTEGER | 序号（seq） |
| `40010` | INTEGER | 会话类型（1=单聊，100/103=系统/子类型） |
| `40011` | INTEGER | 子类型（2=普通文本，11=?, 1=?, 9=?） |
| `40013` | INTEGER | 消息标志 |
| `40020` | TEXT | 发送者 UID |
| `40021` | TEXT | 会话对方 UID（新版为真实 UID；旧消息可能是掩码 UID） |
| `40030` | INTEGER | 会话对方 QQ 号（旧消息为 NULL） |
| `40033` | INTEGER | 发送者 QQ 号（自己发的 = 自己的号；系统消息 = 0） |
| `40050` | INTEGER | 时间戳（Unix 秒；个别行可能是毫秒） |
| `40090` | TEXT | 群名片（单聊为空） |
| `40093` | TEXT | 昵称（仅自己发的消息有值） |
| `40800` | BLOB | 消息体（protobuf `Elements`） |
| `40900` | BLOB | 转发/引用缓存（实测 86 行非空，未解析） |
| `40600` `40605` `40801` | BLOB | 群消息相关扩展 |

### 4.1 `40013` 实测分布（账号 2045689098，3045 行）

| 值 | 含义 | 判方向 |
|----|------|--------|
| 0 | 接收（其中 6 行 `40033` 是自己的号，属于"另一设备发送"，实际是发） | 收 |
| 1 | 发出 | 发 |
| 2 | 发出（占比最大，1270 行） | 发 |
| 3 | 系统提示（`40033=0`，仅含 type 8 提示段） | 系统 |
| 5 | 发出（应用消息） | 发 |
| 8 | 转发 | 转发 |

**因此不要只用 `40013` 判断方向**，应优先比较 `40033` 与自己 QQ 号。

## 5. `nt_uid_mapping_table` 列

| 列 | 说明 |
|----|------|
| `48901` | 自增主键 |
| `48902` | UID（本机 232 行全部有值，形如 `u_7re_ea--mQ-lUdj9Vi35Cw`） |
| `48912` | 备用 UID 列（本机全部为 NULL） |
| `1002` | QQ 号 |

### 5.1 与 `c2c_msg_table.40021` 的关系（实测）

`40021` 的值与映射表 `48902` **完全一致**，例如：

```
40030 = 3052607264  →  40021 = u_7re_ea--mQ-lUdj9Vi35Cw  →  48902 (1002=3052607264) 同值
```

所以：

- 用映射表 `48902` 的 UID 过滤 `40021` 是有效的；
- 旧文档说的 `u_***********_<QQ>` 掩码形式在新库中**已不存在**，
  但为了兼容旧备份，`export_messages.py` 仍会把"以 `_<QQ>` 结尾"的 UID 一并纳入查询；
- 最终查询条件是三者的并集：`[40030] = QQ` OR `[40021] IN (映射 UID, 掩码 UID)`。

## 6. 消息体 `40800`

```protobuf
message Elements { repeated Element elements = 40800; }

message Element {
  uint64 id = 45001;
  uint32 type = 45002;

  string text = 45101;              // 文本

  bytes  md5HexStr = 45406;         // 图片
  uint64 original = 45418;
  string originImageMd5 = 45424;
  string imageUrlLow = 45802;
  string imageUrlHigh = 45803;
  string imageUrlOrigin = 45804;
  string imageFilePath = 45812;
  string imageText = 45815;

  string fileName = 45402;          // 文件
  uint64 fileSize = 45405;

  string voiceText = 45923;         // 语音
  uint64 voiceLen = 45906;

  string videoPath = 46403;         // 视频
  uint64 videoLen = 45410;
  uint64 videoWidth = 45411;
  uint64 videoHeight = 45412;

  uint32 emojiId = 47601;           // 表情
  string emojiText = 47602;

  string senderUid = 40020;         // 引用
  string interlocutorUid = 40021;
  uint32 senderNum = 47403;
  uint32 quotedTimestamp = 47404;
  uint32 interlocutorNum = 47411;
  Element quotedElement = 47423;    // 注意：嵌套的是 Element，不是 Elements

  string noticeInfo = 48214;        // 提示（XML，取 nor 标签的 txt 属性）
  string noticeInfo2 = 48271;       // 提示（Python dict 字面量）

  RedPacket redPacket = 48403;      // 红包
  string applicationMessage = 47901; // 应用消息（JSON，取 prompt）
  string callStatus = 48153;        // 通话
  string callText = 48157;
  FeedMessage feedTitle = 48175;    // 动态
  FeedMessage feedContent = 48176;
  string feedUrl = 48180;
}

message FeedMessage { string text = 48178; }
message RedPacket { string greeting = 48443; string prompt = 48444; string redPacketType = 48445; string summary = 48448; }
```

### 6.1 实测类型分布（账号 2045689098，2975 个非空消息体）

| type | 数量 | 渲染 |
|------|------|------|
| 1 文本 | 2213 | `[文本]` |
| 2 图片 | 485 | `[图片]` + 大小（`--with-paths` 可带路径/URL） |
| 6 表情 | 95 | `[表情]` + 中文名-编号 |
| 16 聊天记录 | 23 | `[聊天记录]` + 提示文本 |
| 10 应用消息 | 104 | `[应用消息]` + prompt |
| 7 引用 | 67 | `[引用]` + 被引用内容 |
| 8 提示 | 64 | `[提示]` + 提示文本 |
| 4 语音 | 20 | `[语音]` + 秒数 |
| 3 文件 | 12 | `[文件]` + 文件名 + 大小 |
| 21 通话 | 12 | `[通话]` |
| 5 视频 | 6 | `[视频]` + 时长/文件名/大小 |
| 11 | 1 | `[未知类型:11]`（无字段，无法判断语义） |
| 解析失败 | 1 | 该行消息体损坏（跳过，不影响其他行） |

### 6.2 已知坑

1. **引用**：`quotedElement`（47423）里是 `Element`，直接当 `Elements` 解析会得到空结果。
2. **提示**：`noticeInfo` 是 XML，`nor` 标签的 `txt` 属性才是正文；`noticeInfo2` 是
   Python dict 字面量（`{'items': [{'txt': ...}]}`）。旧实现返回嵌套元组导致内容丢失。
3. **聊天记录（16）**：元素本身没有内容，正文在其后的 type 1 文本段里。
4. **应用消息**：`applicationMessage` 是 JSON，取 `prompt`；渲染函数返回值不是元组。

## 7. WAL

QQ 运行时会写 `nt_msg.db-wal`（实测 490 KB，比主库新 45 分钟）。只复制主库会丢失
最新消息。`decrypt_db.py` 会把 `-wal`/`-shm` 一并复制到临时目录，打开时执行
`PRAGMA wal_checkpoint(TRUNCATE)` 回放。

## 8. 依赖

- `sqlcipher3`（0.6+，Windows 有官方 wheel）
- QQNT_Export 的 `emojis.py`（可选，仅表情中文名）
- **不再需要**：protobuf、lxml、humanize、SQLAlchemy、jinja2、pillow、mutagen、reportlab
