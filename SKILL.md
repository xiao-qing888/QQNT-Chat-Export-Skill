---
name: QQNT-chat-export-v2.2
description: "提取Windows NTQQ（QQ NT）本地加密数据库中的聊天记录，支持密钥提取、数据库解密、C2C消息导出为TXT/Markdown/JSON三种格式。v2版本在执行前自动检查并安装所有依赖，提供统一入口脚本一键导出。当用户需要导出QQ聊天记录、解密QQ NT数据库、提取本地QQ消息历史时使用。全程命令行操作，无需GUI。关键技术点：密钥需用hex输出版脚本提取（ASCII版会损坏非ASCII字节）；解密必须用HMAC_SHA512（非教程中的SHA1）；QQNT_Export的UID映射有bug，需直接SQL查询绕过。"
version: "2.2"
---

# QQ NT 聊天记录导出 v2

从Windows NTQQ本地加密数据库中提取完整聊天记录，导出为 **TXT / Markdown / JSON** 三种格式。

**v2 特性**：
- 一键部署脚本 `install.ps1`，在新电脑上自动安装 Python、下载 QQNT_Export、安装全部依赖
- 执行前自动检查所有 Python 依赖，缺失则自动 pip 安装
- 统一入口脚本 `run_export.ps1`，一条命令完成全流程
- 自动检测当前登录的 QQ 账号（按 nt_msg.db 修改时间）
- 支持 TXT / Markdown / JSON 三种输出格式，可单独或全部导出

## 部署到新电脑（首次使用）

在一台全新的 Windows 电脑上，只需运行部署脚本即可完成全部环境配置：

```powershell
# 解压 zip 后进入 scripts 目录
cd scripts
powershell -ExecutionPolicy Bypass -File install.ps1
```

`install.ps1` 会自动完成：
1. **检查/安装 Python 3.8+** — 优先用 winget，失败则从 python.org 下载官方安装包（用户级静默安装）
2. **下载 QQNT_Export** — 从 GitHub（https://github.com/Tealina28/QQNT_Export）下载并解压到桌面 `QQNT_output\QQNT_Export`
3. **安装全部 Python 依赖** — protobuf、tqdm、lxml、humanize、SQLAlchemy、jinja2、pillow、mutagen，以及 sqlcipher3（优先预编译的 sqlcipher3-binary）
4. **验证安装** — 逐项检查导入是否成功，输出通过/失败清单

参数：
- `-QQNTExportDir`：自定义 QQNT_Export 安装路径
- `-PythonVersion`：指定 Python 版本（默认 3.12）
- `-SkipPython`：跳过 Python 安装检查

部署完成后，再运行 `run_export.ps1` 导出聊天记录。

> **注意**：QQ NT 客户端本身需自行安装并登录目标账号，install.ps1 不包含 QQ 客户端的安装。

## 快速开始（推荐）

```powershell
# 进入 scripts 目录后运行
cd scripts
powershell -ExecutionPolicy Bypass -File run_export.ps1 -PeerQQ 1361750460
```

参数说明：
- `-PeerQQ`（必填）：对方 QQ 号
- `-MyQQ`（可选）：自己的 QQ 号，省略则自动检测
- `-Format`（可选）：`all`（默认，三种格式全出）/ `txt` / `md` / `json`
- `-OutputDir`（可选）：输出目录，默认当前目录
- `-QQNTExport`（可选）：QQNT_Export 目录路径
- `-SkipDepsCheck`（可选）：跳过依赖检查（不推荐）

示例：
```powershell
# 只导出 Markdown 到指定目录
.\run_export.ps1 -PeerQQ 1361750460 -Format md -OutputDir D:\export

# 指定自己的账号，导出全部三种格式
.\run_export.ps1 -PeerQQ 1361750460 -MyQQ 3052607264 -Format all
```

## 输出格式说明

### TXT
原始聊天记录风格，按日期分组，每条含时间戳、收发方向和内容：
```
===== 2024-01-27 =====

2024-01-27 19:33:06 发
[文本]
不是我买

2024-01-27 19:33:16 收
[文本]
那干啥
```

### Markdown
适合在笔记软件、GitHub、Obsidian 中阅读，含按年统计表和日期分组：
```markdown
# QQ 聊天记录 — 与 1361750460

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

### JSON
结构化数据，适合程序处理、数据分析或导入其他工具：
```json
{
  "meta": {
    "my_qq": "3052607264",
    "peer_qq": "1361750460",
    "total": 1768,
    "date_range": {"start": "2024-01-27 19:33:06", "end": "2026-10-14 15:13:38"},
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
      "content": "不是我买",
      "elements": [{"label": "[文本]", "content": "不是我买"}]
    }
  ]
}
```

## 手动分步执行

如果需要分步调试，可按以下步骤手动执行。

### 第一步：检查依赖（v2 新增）

```bash
python scripts/check_deps.py
```

检查项：
- Python 版本 >= 3.8
- 必需包：sqlcipher3、protobuf==6.31.1、tqdm、lxml、humanize、SQLAlchemy、jinja2、pillow、mutagen
- QQNT_Export 目录及 element_pb2.py
- QQ NT 安装位置

缺失的包会自动通过 pip 安装。使用 `--no-install` 只检查不安装，`--qqnt-export PATH` 指定 QQNT_Export 路径。

### 第二步：提取数据库密钥

运行hex输出版密钥提取脚本（QQ需处于登录状态）：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/get_key_hex.ps1
```

脚本会：
1. 自动定位QQ安装目录和`wrapper.node`
2. 静态分析找到`nt_sqlite3_key_v2`函数的RVA
3. 启动QQ并附加调试器，在函数入口断点评测R8寄存器中的密钥
4. 输出十六进制密钥

**注意**：原始教程中的脚本用`Encoding.ASCII.GetString`读取密钥，会将非ASCII字节替换为`?`。必须使用本skill中的hex输出版本。

如果QQ自动登录的不是目标账号，需要用户手动切换账号登录后再运行脚本。

### 第三步：解密数据库

```bash
python scripts/decrypt_db.py <QQ号> <密钥>
```

参数：
- `QQ号`：目标账号
- `密钥`：第二步提取的密钥
- `--src`：源nt_db目录（默认自动检测）
- `--dst`：输出目录（默认`decrypted_<QQ号>`）

**关键PRAGMA设置**（QQ 9.9.x）：
```
cipher_page_size=4096 → key → kdf_iter=4000 → cipher_hmac_algorithm=HMAC_SHA512 → cipher_kdf_algorithm=PBKDF2_HMAC_SHA512
```
注意：不是HMAC_SHA1！教程和nt_msg_db_util中的SHA1配置会导致HMAC校验失败。

每个数据库文件有1024字节自定义头部（"SQLite header 3" + "QQ_NT DB"），解密前需剥离。

### 第四步：导出C2C聊天记录（TXT/MD/JSON）

```bash
python scripts/export_messages.py <decrypted_nt_msg.db> <对方QQ号> <输出目录> [--format all]
```

参数：
- `db_path`：解密后的 nt_msg.db 路径
- `peer_qq`：对方 QQ 号
- `output_dir`：输出目录
- `--format`：`all`（默认）/ `txt` / `md` / `json`
- `--my-qq`：自己的 QQ 号（写入 JSON 的 meta 字段）

**为什么不用QQNT_Export工具**：QQNT_Export的`num_to_uid()`从`nt_uid_mapping_table`列48902读取掩码UID（`u_***********_QQ号`），但消息表`c2c_msg_table`列40021存储的是真实UID（列48912）。过滤时只匹配到2023年以前的旧消息（那些消息的`interlocutor_num`列40030为NULL，恰好使用了掩码UID）。

本脚本直接SQL查询：`WHERE [40030]=? OR [40021]=?`，同时匹配QQ号和掩码UID，确保提取全部消息。

## 数据库结构

详细的表结构、列映射和已知问题见 [references/database_schema.md](references/database_schema.md)。

关键点：
- `c2c_msg_table`列40030=对方QQ号，列40021=对方UID，列40050=时间戳，列40800=protobuf消息体，列40013=发送方向
- `nt_uid_mapping_table`列48902=掩码UID（勿用于过滤），列48912=真实UID，列1002=QQ号

## 依赖

- `sqlcipher3`（系统预装；`pysqlcipher3`在Windows上编译失败）
- `protobuf==6.31.1`
- `tqdm`、`lxml`、`humanize`、`SQLAlchemy`、`jinja2`（QQNT_Export 导入链）
- `pillow`、`mutagen`（图片/语音元素解析）
- QQNT_Export的`element_pb2.py`用于解析protobuf消息体（脚本中已硬编码路径，可按需修改）

v2 的 `check_deps.py` 会自动检查并安装以上全部依赖。

## 常见问题

**Q: 解密提示HMAC校验失败？**
A: 确认使用HMAC_SHA512而非SHA1，且PRAGMA顺序正确（cipher_page_size必须在key之前）。

**Q: 导出的消息只有几十条？**
A: QQNT_Export的UID映射bug。使用本skill的`export_messages.py`直接SQL查询。

**Q: 密钥中有`?`字符？**
A: 正常。密钥可能包含字面`?`（0x3F）。用hex输出版本确认字节。

**Q: 不知道对方账号密码？**
A: 不需要。两人之间的C2C对话存储在自己账号的本地数据库中，只需解密自己账号的数据库即可。

**Q: check_deps.py 提示 QQNT_Export 找不到？**
A: 运行 `install.ps1` 即可自动从 GitHub 下载 QQNT_Export。或手动下载 https://github.com/Tealina28/QQNT_Export 并放在 `D:\Users\Administrator\Desktop\QQNT_output\QQNT_Export`，通过 `--qqnt-export` 参数指定路径。该目录需包含 `element_pb2.py`。

**Q: 依赖安装失败怎么办？**
A: 手动运行 `python -m pip install <包名>`，或检查网络/代理设置。`sqlcipher3` 在 Windows 上优先安装 `sqlcipher3-binary`（预编译 wheel），若失败需安装 Visual C++ Build Tools 后再 `pip install sqlcipher3`。

**Q: 如何部署到新电脑？**
A: 解压 zip 后进入 scripts 目录，运行 `install.ps1` 即可自动完成 Python 安装、QQNT_Export 下载和全部依赖安装。详见"部署到新电脑"章节。

## 版本变更记录

| 项目 | v1 | v2.0 | v2.1 | v2.2（当前） |
|------|----|------|------|-------------|
| 依赖检查 | 无 | check_deps.py | 同 v2.0 | 同 v2.0 |
| 一键部署 | 无 | 无 | 无 | **install.ps1 自动装 Python+QQNT_Export+依赖** |
| 入口方式 | 手动4步 | run_export.ps1 | 同 v2.0 | 同 v2.0 |
| 账号检测 | 手动指定 | 自动检测 | 同 v2.0 | 同 v2.0 |
| 输出格式 | TXT + PDF | TXT + PDF | TXT / MD / JSON | 同 v2.1 |
| PDF 生成 | chat_to_pdf.py | 保留 | 已移除 | 已移除 |
| 多格式导出 | 无 | 无 | export_messages.py | 同 v2.1 |
