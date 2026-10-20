---
name: QQNT-chat-export-v3
description: "从Windows NTQQ（QQ NT）本地加密数据库中提取单聊聊天记录，支持密钥提取（静态分析+DLL调试器）、SQLCipher数据库解密、C2C消息导出为TXT/Markdown/JSON三种格式。v3完成重构：内置protobuf解码器（唯一pip依赖是sqlcipher3）、解密参数按数据库头部自动探测、静态分析用C#实现（0.3秒完成112MB文件）、支持WAL合并与端到端自测。当用户需要导出QQ聊天记录、解密QQ NT数据库、提取本地QQ消息历史、从wrapper.node提取数据库密钥时使用。全程命令行操作，无需GUI。关键技术点：QQ数据库头部声明HMAC_SHA1（不是SHA512）；密钥是16字节原始值而非hex文本；QQ拒绝第三方调试器附加（必须先退出QQ再由脚本启动）；.ps1必须带UTF-8 BOM。"
version: "3.0"
---

# QQ NT 聊天记录导出 v3

从 Windows NTQQ 本地加密数据库中提取聊天记录，导出为 **TXT / Markdown / JSON**。

**v3 特性**：
- 一键部署 `install.ps1`：装 Python、`sqlcipher3`（Windows 有官方 wheel），可选下载 QQNT_Export
- 一键导出 `run_export.ps1`：依赖检查 → 密钥提取 → 账号识别 → 解密 → 导出
- 消息体解析内置（`qq_elements.py`），**不再依赖 protobuf / lxml / SQLAlchemy / pillow 等**
- 解密参数从数据库头部读取 + 自动探测，密钥以原始字节传递
- 静态分析改为 C# 实现，112 MB 的 `wrapper.node` 0.3 秒完成
- `-ListPeers` 直接列出会话；`selftest.py` 13 项端到端自测

## 部署到新电脑（首次使用）

```powershell
cd scripts
powershell -ExecutionPolicy Bypass -File install.ps1
```

`install.ps1` 会：
1. 检查/安装 Python 3.8+（优先 winget，失败则官方安装包静默安装；用真实 `--version` 验证，绕开商店假 python）
2. 安装 `sqlcipher3`（**不要用 `sqlcipher3-binary`：它只有 manylinux 轮子，Windows 上永远装不上**）
3. 可选下载 QQNT_Export（仅用于表情中文名）
4. 汇总 Python / sqlcipher3 / QQNT_Export / QQ NT / QQ 数据目录

参数：`-DryRun`（只检查）、`-QQNTExportDir`、`-PythonVersion`、`-Python`、`-SkipPython`、`-SkipQQNTExport`。

> **注意**：QQ NT 客户端需自行安装并登录；`install.ps1` 不安装 QQ。

## 快速开始（推荐）

```powershell
cd scripts
powershell -ExecutionPolicy Bypass -File run_export.ps1 -ListPeers      # ① 看有哪些会话
# ② 完全退出 QQ（托盘右键退出）
powershell -ExecutionPolicy Bypass -File run_export.ps1 -PeerQQ 1361750460
```

参数说明：
- `-PeerQQ`：对方 QQ 号（配合 `-ListPeers` 查）
- `-MyQQ`：自己的 QQ 号，省略则自动检测（按 `nt_msg.db` 最近活动时间）
- `-Format`：`all`（默认）/ `txt` / `md` / `json`，可逗号组合
- `-OutputDir`：输出目录，默认当前目录
- `-Key`：直接给密钥，跳过提取
- `-DecryptedDb`：直接用已解密的 `nt_msg.db`，跳过提取与解密
- `-Python`、`-QQNTExport`、`-TencentFiles`：覆盖自动探测
- `-AllDatabases`：解密全部库（默认只解 `nt_msg.db`）
- `-Force`：强制重解（默认源库更新时才重解）
- `-ListPeers`、`-SkipDepsCheck`、`-TimeoutSeconds`

退出码：`0` 成功 / `1` 环境问题 / `2` 参数错误 / `3` 密钥提取失败 / `4` 解密失败。

## 输出格式说明

### TXT

```
QQ聊天记录 - 与 1361750460
共 121 条消息
时间范围: 2023-03-18 18:39:01 ~ 2026-10-17 17:02:55
导出时间: 2026-10-20 18:39:17
============================================================

===== 2024-01-27 =====

2024-01-27 19:33:06 收
[文本]
不是我买
```

### Markdown

```markdown
# QQ 聊天记录 — 与 1361750460

- **消息总数**：1768 条
- **时间范围**：2024-01-27 19:33:06 ~ 2026-10-14 15:13:38
- **数据来源**：`nt_msg.db`

## 按年统计
| 年份 | 消息数 |
|------|--------|
| 2024 | 572 |

## 2024-01-27

**19:33:06 对方**：不是我买
```

### JSON

```json
{
  "meta": {
    "generator": "QQNT-chat-export v3.0.0",
    "my_qq": "3052607264",
    "peer_qq": "1361750460",
    "total": 1768,
    "date_range": {"start": "2024-01-27 19:33:06", "end": "2026-10-14 15:13:38"},
    "exported_at": "2026-10-20 18:39:17",
    "source_db": "nt_msg.db",
    "stats": {"by_year": {"2024": 572}, "by_type": {"文本": 1600}, "by_direction": {"收": 900, "发": 868}}
  },
  "messages": [
    {
      "id": "7399284464009183099",
      "timestamp": "2024-01-27 19:33:06",
      "date": "2024-01-27",
      "time": "19:33:06",
      "direction": "received",
      "direction_cn": "收",
      "type": "文本",
      "content": "[文本]\n不是我买",
      "elements": [{"label": "[文本]", "content": "不是我买"}]
    }
  ]
}
```

## 手动分步执行

### 第一步：检查依赖

```powershell
python scripts/check_deps.py            # 缺失会自动 pip 安装
python scripts/check_deps.py --no-install
python scripts/check_deps.py --json
```

检查项：Python ≥ 3.8、`sqlcipher3`（必需）、QQNT_Export（可选）、QQ NT 安装、Tencent Files 与账号。

### 第二步：提取数据库密钥

**先完全退出 QQ**，然后：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/get_key_hex.ps1
```

脚本会：
1. 自动定位 QQ 安装目录与 `wrapper.node`（注册表 + config.json + 常见路径）
2. 静态分析：`.rdata` 找 `nt_sqlite3_key_v2: db=%p zDb=%s` → `.text` 扫 RIP-relative LEA → `.pdata` 定位函数 RVA（C#，约 0.3 秒）
3. 以调试方式启动 `QQ.exe`，在函数入口下断点
4. 命中后从 `R8`（pKey）与 `R9`（nKey）读取密钥

常用参数：
- `-NoDebugForKey`：只做静态分析（验证 QQ 版本是否仍支持）
- `-OutFile <path>`：把密钥写进文件
- `-Quiet` / `-AsJson`：只输出密钥 / JSON
- `-Attach`：附加到已运行的 QQ（QQ 通常会拒绝，返回错误 31）
- `-TimeoutSeconds`：等待断点的时间（默认 180 秒）
- `-FunctionRva` / `-ModuleName` / `-QQExePath`：高级覆盖

**注意**：密钥是 16 字节原始值，可能包含 `?`（0x3F）等字符。请直接使用 hex 字符串，不要手工转 ASCII。

### 第三步：解密数据库

```powershell
python scripts/decrypt_db.py <QQ号> <密钥>
python scripts/decrypt_db.py <QQ号> <密钥> --only nt_msg.db     # 只解消息库（最快）
python scripts/decrypt_db.py <QQ号> --list                     # 查看库与头部信息
```

- 每个 `.db` 前有 1024 字节 QQ 自定义头，解密前剥离
- PRAGMA 顺序：`cipher_page_size` → `key` → `kdf_iter` → `cipher_hmac_algorithm` → `cipher_kdf_algorithm`
- **HMAC 算法以数据库头部声明为准**（本机实测为 `HMAC_SHA1`），并与 `HMAC_SHA512` 等组合一起自动探测
- 密钥以 **原始 16 字节** 作为口令；hex 文本形式仅作兼容兜底
- 默认合并 `-wal`，避免丢失最新消息
- 先写 `.tmp` 再原子替换，失败不会破坏上一次的解密结果

密钥也可用环境变量：`$env:QQNT_DB_KEY = "..."`。

### 第四步：导出聊天记录

```powershell
python scripts/export_messages.py <解密后的 nt_msg.db> <对方QQ号> <输出目录> --format all --my-qq <自己的QQ号>
python scripts/export_messages.py --list-peers
python scripts/export_messages.py --account <自己QQ> --peer <对方QQ>
```

参数：`--format`（all/txt/md/json）、`--my-qq`、`--account`、`--tencent-files`、`--qqnt-export`、`--with-paths`、`--bom`、`--list-peers`、`--limit`、`-q`。

## 数据库结构

详细表结构见 [references/database_schema.md](references/database_schema.md)。

关键点：
- `c2c_msg_table`：`40030` = 对方 QQ 号，`40021` = 对方 UID，`40033` = 发送者 QQ 号，`40050` = 时间戳，`40800` = protobuf 消息体，`40013` = 消息标志
- `nt_uid_mapping_table`：`1002` = QQ 号，`48902` = UID（本机真实可用的列），`48912` = 备用 UID 列
- 查询会话：`WHERE [40030] = ? OR [40021] IN (映射表 UID, 以 _QQ 结尾的掩码 UID)`
- 判方向：`40033 == 自己的 QQ` → 发；`40013 == 3` 且只有提示段 → 系统
- 消息体：`Elements { repeated Element elements = 40800 }`，`Element.type`：1 文本 / 2 图片 / 3 文件 / 4 语音 / 5 视频 / 6 表情 / 7 引用 / 8 提示 / 9 红包 / 10 应用消息 / 16 聊天记录 / 21 通话 / 26 动态

## 依赖

- **必需**：`sqlcipher3`（Python 绑定，Windows 有官方 wheel）
- **可选**：QQNT_Export 的 `emojis.py`（把系统表情显示成中文名）
- **不需要**：protobuf（内置解码器）、lxml、humanize、SQLAlchemy、jinja2、pillow、mutagen

## 常见问题

**Q: 解密报"尝试了 N 种密钥/参数组合仍无法读取数据库"？**
A: 密钥过期（QQ 重装/升级会换密钥）。重新运行 `get_key_hex.ps1`。用 `decrypt_db.py --list` 可看到数据库头部声明的格式版本与 HMAC 算法。

**Q: 装 sqlcipher3 总是失败？**
A: 用 `python -m pip install sqlcipher3`；`sqlcipher3-binary` 没有 Windows 轮子。失败时检查 Python 版本（3.10–3.13 有 wheel）与 VC++ 运行库。

**Q: 提取密钥提示"检测到 QQ 正在运行"？**
A: 必须先完全退出 QQ。QQ 拒绝第三方调试器附加（`DebugActiveProcess` 返回错误 31），只能由脚本自己启动 QQ。

**Q: 导出消息变少/少了最近几天？**
A: ① 用 `-Force` 重新解密；② 确认选择了正确账号；③ 最新消息可能在 `-wal` 中（v3 默认合并，但源库需可复制）。

**Q: 表情只显示编号？**
A: 安装可选组件 QQNT_Export（`install.ps1` 自动下载 `emojis.py`）。

**Q: PowerShell 脚本中文乱码/解析失败？**
A: `.ps1` 必须是 UTF-8 **带 BOM**。`selftest.py` 会检查所有脚本的 BOM 与语法。

**Q: 怎么验证改动没破坏功能？**
A: `python scripts/selftest.py`（13 项，含合成加密库的完整解密→导出流程；没有 sqlcipher3 时相关项 SKIP）。

## 版本变更记录

| 项目 | v2.0 | v2.2 | v3.0（当前） |
|------|------|------|-------------|
| 依赖检查 | check_deps.py | 同 v2.0 | 重写：必需/可选分离、`--json`、注册表探测 |
| 一键部署 | 无 | install.ps1 | 重写：TLS 1.2、真实 Python 校验、Windows wheel |
| 入口方式 | run_export.ps1 | 同 v2.0 | 重写：密钥走文件、退出码、复用已解密库 |
| 密钥提取 | PowerShell 逐字节扫描 | 同 v2.0 | C# 实现（0.3 秒）、CONTEXT 修正、R9 取长度、超时/附加 |
| 解密参数 | HMAC_SHA512（错误） | 同 v2.0 | 头部读取 + 自动探测（含 HMAC_SHA1） |
| 密钥形式 | hex 文本当口令 | 同 v2.0 | 原始字节口令 + 兼容形式 |
| 消息解析 | QQNT_Export + element_pb2 | 同 v2.0 | 内置 protobuf 解码器（零依赖） |
| 消息过滤 | 掩码 UID（bug） | 直接 SQL（仍有掩码写死） | QQ 号 + 真实 UID + 掩码并集 |
| 收发方向 | 仅 40013 | 同 v2.0 | 优先 40033 发送者号码 |
| 最新消息 | 忽略 WAL | 同 v2.0 | 合并 WAL |
| 输出格式 | TXT + PDF | TXT / MD / JSON | TXT / MD / JSON + 统计 + 消息 ID |
| 自动化测试 | 无 | 无 | selftest.py 13 项 |
