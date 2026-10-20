# QQNT-chat-export-v2 使用文档

> 从 Windows NTQQ（QQ NT）本地加密数据库中提取完整聊天记录，导出为 **TXT / Markdown / JSON** 三种格式。

---

## 目录

1. [功能特性](#1-功能特性)
2. [系统要求](#2-系统要求)
3. [快速开始](#3-快速开始)
4. [部署到新电脑](#4-部署到新电脑)
5. [导出聊天记录](#5-导出聊天记录)
6. [手动分步执行](#6-手动分步执行)
7. [输出格式说明](#7-输出格式说明)
8. [脚本参考](#8-脚本参考)
9. [常见问题](#9-常见问题)
10. [故障排除](#10-故障排除)
11. [技术细节](#11-技术细节)

---

## 1. 功能特性

- **一键部署**：`install.ps1` 自动安装 Python、下载 QQNT_Export、安装全部依赖
- **依赖自检**：`check_deps.py` 执行前检查所有依赖，缺失自动安装
- **一键导出**：`run_export.ps1` 一条命令完成密钥提取→解密→导出全流程
- **自动识别账号**：按 `nt_msg.db` 修改时间自动检测当前登录的 QQ 账号
- **三种输出格式**：TXT（原始风格）、Markdown（适合笔记软件）、JSON（结构化数据）
- **完整消息提取**：直接 SQL 查询绕过 QQNT_Export 的 UID 映射 bug，确保不遗漏消息

---

## 2. 系统要求

| 项目 | 要求 |
|------|------|
| 操作系统 | Windows 10 / 11 |
| QQ 客户端 | QQ NT（9.9.x），已登录目标账号 |
| Python | 3.8 及以上（`install.ps1` 会自动安装） |
| PowerShell | 5.0 及以上（Windows 自带） |
| 磁盘空间 | 约 500 MB（含 Python 和依赖） |
| 网络 | 部署时需要联网下载依赖 |

---

## 3. 快速开始

### 3.1 首次使用（部署环境）

```powershell
# 解压 zip 后进入 scripts 目录
cd scripts

# 运行部署脚本，自动安装所有软件和依赖
powershell -ExecutionPolicy Bypass -File install.ps1
```

### 3.2 导出聊天记录

```powershell
# 导出与指定 QQ 号的聊天记录（默认输出 TXT + MD + JSON 三种格式）
powershell -ExecutionPolicy Bypass -File run_export.ps1 -PeerQQ 10001
```

导出完成后，文件会出现在当前目录下：
- `与10001的聊天记录.txt`
- `与10001的聊天记录.md`
- `与10001的聊天记录.json`

---

## 4. 部署到新电脑

在一台全新的 Windows 电脑上，只需运行 `install.ps1` 即可完成全部环境配置。

### 4.1 部署脚本做了什么

| 步骤 | 操作 | 说明 |
|------|------|------|
| 1 | 检查 Python | 已安装则跳过，未安装则自动下载安装 |
| 2 | 安装 Python | 优先用 winget，失败则从 python.org 下载官方安装包（用户级静默安装） |
| 3 | 下载 QQNT_Export | 从 GitHub 下载并解压到 `桌面\QQNT_output\QQNT_Export` |
| 4 | 安装 pip 依赖 | protobuf、tqdm、lxml、humanize、SQLAlchemy、jinja2、pillow、mutagen |
| 5 | 安装 sqlcipher3 | 优先 `sqlcipher3-binary`（预编译），失败再试源码编译 |
| 6 | 验证安装 | 逐项检查 import 是否成功 |

### 4.2 部署脚本参数

```powershell
# 自定义 QQNT_Export 安装路径
.\install.ps1 -QQNTExportDir "D:\tools\QQNT_Export"

# 指定 Python 版本（默认 3.12）
.\install.ps1 -PythonVersion "3.11"

# 跳过 Python 安装检查（Python 已安装但不在 PATH 时使用）
.\install.ps1 -SkipPython
```

### 4.3 注意事项

- QQ NT 客户端本身需自行安装并登录目标账号，`install.ps1` 不包含 QQ 客户端的安装
- 如果 Python 是新安装的，部署完成后可能需要重启终端才能使 PATH 生效
- `sqlcipher3` 源码编译需要 Visual C++ Build Tools，建议优先使用预编译的 `sqlcipher3-binary`

---

## 5. 导出聊天记录

### 5.1 基本用法

```powershell
cd scripts
powershell -ExecutionPolicy Bypass -File run_export.ps1 -PeerQQ <对方QQ号>
```

### 5.2 完整参数

| 参数 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `-PeerQQ` | 是 | — | 对方 QQ 号 |
| `-MyQQ` | 否 | 自动检测 | 自己的 QQ 号，省略则按 nt_msg.db 修改时间自动检测 |
| `-Format` | 否 | `all` | 输出格式：`all` / `txt` / `md` / `json` |
| `-OutputDir` | 否 | 当前目录 | 输出文件目录 |
| `-QQNTExport` | 否 | `桌面\QQNT_output\QQNT_Export` | QQNT_Export 目录路径 |
| `-SkipDepsCheck` | 否 | 关闭 | 跳过依赖检查（不推荐） |

### 5.3 使用示例

```powershell
# 导出全部三种格式（默认）
.\run_export.ps1 -PeerQQ 10001

# 只导出 Markdown
.\run_export.ps1 -PeerQQ 10001 -Format md

# 导出到指定目录
.\run_export.ps1 -PeerQQ 10001 -OutputDir D:\export

# 指定自己的 QQ 号（自动检测失败时使用）
.\run_export.ps1 -PeerQQ 10001 -MyQQ 10002

# 只导出 JSON 到指定目录
.\run_export.ps1 -PeerQQ 10001 -Format json -OutputDir D:\export
```

### 5.4 执行流程

`run_export.ps1` 按以下步骤执行：

1. **依赖检查** — 运行 `check_deps.py`，缺失则自动安装
2. **提取密钥** — 运行 `get_key_hex.ps1`，启动 QQ 并附加调试器提取数据库加密密钥
3. **检测账号** — 扫描 `Documents\Tencent Files` 下各账号的 `nt_msg.db` 修改时间，取最新的作为当前账号
4. **解密数据库** — 运行 `decrypt_db.py`，用 HMAC_SHA512 解密全部数据库文件
5. **导出消息** — 运行 `export_messages.py`，提取 C2C 消息并输出为指定格式

---

## 6. 手动分步执行

如果需要调试或分步控制，可以手动执行每个脚本。

### 6.1 检查依赖

```bash
python scripts/check_deps.py
```

可选参数：
- `--qqnt-export PATH`：指定 QQNT_Export 路径
- `--no-install`：只检查不安装

### 6.2 提取数据库密钥

```powershell
powershell -ExecutionPolicy Bypass -File scripts/get_key_hex.ps1
```

输出十六进制密钥，例如：`66643F3F53346E6763244E4175487559`

> QQ 需处于登录状态。脚本会启动一个新的 QQ 进程并附加调试器，在函数入口断点评测密钥。

### 6.3 解密数据库

```bash
python scripts/decrypt_db.py <我的QQ号> <密钥>
```

可选参数：
- `--src`：源 nt_db 目录（默认自动检测 `Documents\Tencent Files\<QQ号>\nt_qq\nt_db`）
- `--dst`：输出目录（默认 `桌面\QQNT_output\decrypted_<QQ号>`）

### 6.4 导出聊天记录

```bash
python scripts/export_messages.py <解密后的nt_msg.db> <对方QQ号> <输出目录> --format all
```

可选参数：
- `--format`：`all`（默认）/ `txt` / `md` / `json`
- `--my-qq`：自己的 QQ 号（写入 JSON 的 meta 字段）

---

## 7. 输出格式说明

### 7.1 TXT

原始聊天记录风格，按日期分组，每条含时间戳、收发方向和内容。

```
===== 2024-01-27 =====

2024-01-27 19:33:06 发
[文本]
不是我买

2024-01-27 19:33:16 收
[文本]
那干啥
```

### 7.2 Markdown

适合在笔记软件（Obsidian、Typora、VS Code）、GitHub 中阅读，包含按年统计表和日期分组。

```markdown
# QQ 聊天记录 — 与 10001

- **消息总数**：1768 条
- **时间范围**：2024-01-27 ~ 2026-10-14

## 按年统计
| 年份 | 消息数 |
|------|--------|
| 2024 | 572 |

## 2024-01-27

**19:33:06 我**：不是我买
**19:33:16 对方**：那干啥
```

### 7.3 JSON

结构化数据，适合程序处理、数据分析或导入其他工具。

```json
{
  "meta": {
    "my_qq": "10002",
    "peer_qq": "10001",
    "total": 1768,
    "date_range": {
      "start": "2024-01-27 19:33:06",
      "end": "2026-10-14 15:13:38"
    },
    "exported_at": "2026-10-17 12:00:00"
  },
  "messages": [
    {
      "timestamp": "2024-01-27 19:33:06",
      "date": "2024-01-27",
      "time": "19:33:06",
      "direction": "sent",
      "direction_cn": "发",
      "type": "文本",
      "content": "[文本]\n不是我买",
      "elements": [
        {"label": "[文本]", "content": "不是我买"}
      ]
    }
  ]
}
```

**JSON 字段说明**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `meta.my_qq` | string | 自己的 QQ 号 |
| `meta.peer_qq` | string | 对方的 QQ 号 |
| `meta.total` | int | 消息总数 |
| `meta.date_range` | object | 时间范围（start / end） |
| `meta.exported_at` | string | 导出时间 |
| `messages[].timestamp` | string | 完整时间戳 |
| `messages[].date` | string | 日期 |
| `messages[].time` | string | 时间 |
| `messages[].direction` | string | 方向（sent / received / forwarded） |
| `messages[].direction_cn` | string | 方向（发 / 收 / 转发） |
| `messages[].type` | string | 消息类型（文本 / 表情 / 图片 / 文件 等） |
| `messages[].content` | string | 合并后的文本内容 |
| `messages[].elements` | array | 原始元素列表（label / content） |

---

## 8. 脚本参考

### 文件结构

```
QQNT-chat-export-v2/
├── README.md                    # 本文档
├── SKILL.md                     # Skill 元数据和技术说明
├── scripts/
│   ├── install.ps1              # 一键部署脚本（新电脑首次使用）
│   ├── check_deps.py            # 依赖检查与自动安装
│   ├── run_export.ps1           # 统一入口（一键导出）
│   ├── get_key_hex.ps1          # 数据库密钥提取（hex 输出）
│   ├── decrypt_db.py            # 数据库解密
│   └── export_messages.py       # 多格式消息导出（TXT/MD/JSON）
└── references/
    └── database_schema.md       # 数据库表结构参考
```

### 各脚本职责

| 脚本 | 语言 | 职责 |
|------|------|------|
| `install.ps1` | PowerShell | 部署：安装 Python、下载 QQNT_Export、安装全部依赖 |
| `check_deps.py` | Python | 检查 Python 版本、pip 包、QQNT_Export、QQ 安装 |
| `run_export.ps1` | PowerShell | 统一入口：依赖检查→密钥提取→账号检测→解密→导出 |
| `get_key_hex.ps1` | PowerShell | 静态分析 wrapper.node + 动态调试提取数据库密钥 |
| `decrypt_db.py` | Python | 用 sqlcipher3 解密 nt_db 下的全部 .db 文件 |
| `export_messages.py` | Python | 从解密后的 nt_msg.db 提取 C2C 消息，输出 TXT/MD/JSON |

---

## 9. 常见问题

### Q: 导出的消息只有几十条？

A: 这是 QQNT_Export 的 UID 映射 bug。本 skill 的 `export_messages.py` 已绕过该问题，直接用 SQL 查询 `WHERE [40030]=? OR [40021]=?` 同时匹配 QQ 号和掩码 UID，确保提取全部消息。如果你手动使用了 QQNT_Export 工具，可能会遇到此问题。

### Q: 解密提示 HMAC 校验失败？

A: 确认使用的是 HMAC_SHA512 而非 HMAC_SHA1。QQ 9.9.x 的正确 PRAGMA 顺序为：
```
cipher_page_size=4096 → key → kdf_iter=4000 → cipher_hmac_algorithm=HMAC_SHA512 → cipher_kdf_algorithm=PBKDF2_HMAC_SHA512
```
`cipher_page_size` 必须在 `key` 之前设置。

### Q: 密钥中有 `?` 字符？

A: 正常。密钥可能包含字面 `?`（0x3F）。请使用 hex 输出版本的密钥（`get_key_hex.ps1`），不要使用 ASCII 版本，因为 ASCII 版本会将非 ASCII 字节替换为 `?`，导致密钥损坏。

### Q: 不知道对方账号密码？

A: 不需要。两人之间的 C2C 对话存储在自己账号的本地数据库中，只需解密自己账号的数据库即可。

### Q: check_deps.py 提示 QQNT_Export 找不到？

A: 运行 `install.ps1` 即可自动从 GitHub 下载。或手动下载 https://github.com/Tealina28/QQNT_Export 并解压到 `桌面\QQNT_output\QQNT_Export`，通过 `--qqnt-export` 参数指定路径。

### Q: 自动检测不到我的 QQ 账号？

A: 用 `-MyQQ` 参数手动指定：
```powershell
.\run_export.ps1 -PeerQQ 10001 -MyQQ 你的QQ号
```

### Q: 密钥提取时 QQ 启动了但没反应？

A: `get_key_hex.ps1` 会启动一个新的 QQ 进程并附加调试器。如果 QQ 自动登录的不是目标账号，需要先手动切换账号登录，再运行脚本。脚本在断点命中后会自动提取密钥并终止调试进程。

---

## 10. 故障排除

### 10.1 依赖安装失败

```
# 手动安装单个包
python -m pip install <包名>

# 检查网络/代理
pip config list

# sqlcipher3 编译失败时，使用预编译版
python -m pip install sqlcipher3-binary
```

### 10.2 Python 不在 PATH

如果 `install.ps1` 安装了 Python 但后续脚本提示找不到 python：
1. 关闭当前终端，重新打开
2. 或手动指定 Python 路径：`$env:LOCALAPPDATA\Programs\Python\Python312\python.exe`

### 10.3 解密后部分数据库失败

解密时可能有 1-4 个数据库文件 HMAC 校验失败（如 `msg_fts.db`、`nt_flash_transfer.db`），这是正常的——这些数据库可能使用不同的密钥或已损坏。只要 `nt_msg.db` 解密成功，就能正常导出聊天记录。

### 10.4 导出的消息内容为空或显示 [解析错误]

这通常是因为 QQNT_Export 的 `element_pb2.py` 版本与当前 QQ 版本不匹配。尝试更新 QQNT_Export 到最新版本：
```powershell
# 删除旧版本后重新运行 install.ps1
Remove-Item -Recurse -Force "$env:USERPROFILE\Desktop\QQNT_output\QQNT_Export"
.\install.ps1
```

### 10.5 密钥提取失败

- 确认 QQ NT 已安装且能正常启动
- 确认 QQ 版本为 9.9.x（脚本通过静态分析 `wrapper.node` 定位函数，版本变化可能导致 RVA 偏移）
- 尝试以管理员身份运行 PowerShell

---

## 11. 技术细节

### 11.1 数据库加密

QQ NT 的数据库使用 SQLCipher 加密，每个 `.db` 文件有 1024 字节自定义头部（包含 "SQLite header 3" 和 "QQ_NT DB" 标识），解密前需剥离。

加密参数（QQ 9.9.x）：
- 页面大小：4096
- KDF 迭代次数：4000
- HMAC 算法：HMAC_SHA512
- KDF 算法：PBKDF2_HMAC_SHA512

### 11.2 密钥提取

`get_key_hex.ps1` 通过以下步骤提取密钥：
1. 静态分析 `wrapper.node`，找到 `nt_sqlite3_key_v2` 字符串的 RVA
2. 找到引用该字符串的 LEA 指令，定位到 `nt_sqlite3_key_v2` 函数
3. 启动 QQ 进程并附加调试器，在函数入口设置断点
4. 断点命中时读取 R8 寄存器中的密钥（hex 格式）

### 11.3 消息表结构

`c2c_msg_table` 关键列：

| 列名 | 说明 |
|------|------|
| 40001 | 消息 ID |
| 40013 | 发送方向（0=收，1/2=发，8=转发） |
| 40021 | 对方 UID（真实 UID，旧消息为掩码 UID） |
| 40030 | 对方 QQ 号（旧消息为 NULL） |
| 40050 | 时间戳（Unix） |
| 40800 | 消息体（protobuf Elements） |

### 11.4 QQNT_Export UID 映射 bug

QQNT_Export 的 `num_to_uid()` 从 `nt_uid_mapping_table` 列 48902 读取掩码 UID（`u_***********_QQ号`），但消息表 `c2c_msg_table` 列 40021 存储的是真实 UID。过滤时只匹配到 2023 年以前的旧消息（那些消息的 `interlocutor_num` 列 40030 为 NULL，恰好使用了掩码 UID）。

本 skill 的解决方案：直接 SQL 查询 `WHERE [40030]=? OR [40021]=?`，同时匹配 QQ 号和掩码 UID。

### 11.5 第三方依赖

- **QQNT_Export**：https://github.com/Tealina28/QQNT_Export — 提供 `element_pb2.py`（protobuf 消息体定义）和 `exporter.txt.elements`（消息元素解析器）
- **sqlcipher3**：SQLCipher 的 Python 绑定，用于解密加密的 SQLite 数据库

