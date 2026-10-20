# QQNT-chat-export v3 使用文档

> 从 Windows NTQQ（QQ NT）本地加密数据库中提取聊天记录，导出为 **TXT / Markdown / JSON**。
> v3 是一次完整重构：修正了解密参数、密钥传递、消息解析等关键错误，并把运行依赖从 8 个缩减到 1 个。

---

## 目录

1. [功能特性](#1-功能特性)
2. [v3 修了什么（重要）](#2-v3-修了什么重要)
3. [系统要求](#3-系统要求)
4. [快速开始](#4-快速开始)
5. [部署到新电脑](#5-部署到新电脑)
6. [导出聊天记录](#6-导出聊天记录)
7. [手动分步执行](#7-手动分步执行)
8. [输出格式说明](#8-输出格式说明)
9. [脚本参考](#9-脚本参考)
10. [常见问题](#10-常见问题)
11. [故障排除](#11-故障排除)
12. [技术细节](#12-技术细节)
13. [自检与回归测试](#13-自检与回归测试)

---

## 1. 功能特性

- **一键部署**：`install.ps1` 自动装 Python、`sqlcipher3`，可选下载 QQNT_Export
- **一键导出**：`run_export.ps1` 一条命令完成 依赖检查 → 密钥提取 → 账号识别 → 解密 → 导出
- **依赖极简**：消息体解析改为内置 protobuf 解码器，**不再需要 protobuf / lxml / SQLAlchemy / pillow 等 8 个包**
- **参数自适配**：解密参数从数据库头部读取并自动探测（`HMAC_SHA1` / `HMAC_SHA512` 等组合逐一验证）
- **三种输出格式**：TXT（原始风格）、Markdown（笔记软件）、JSON（结构化数据）
- **会话枚举**：`-ListPeers` 直接列出本机数据库里的所有单聊对象与消息数
- **自检脚本**：`selftest.py` 13 项端到端测试，改代码后一条命令验证

---

## 2. v3 修了什么（重要）

v2.2 里有若干**必然导致失败**的问题，v3 已全部修正。如果你之前"解密总是失败"或"导出的消息缺失"，多半就是下面这些：

| # | 问题 | 后果 | v3 的做法 |
|---|------|------|-----------|
| 1 | 解密写死 `cipher_hmac_algorithm = HMAC_SHA512` | QQ 9.9.x 实际用的是 **HMAC_SHA1**（数据库头部 1024 字节里明确写着），密钥再对也解不开 | 从数据库头部读取算法 + 自动探测参数，逐一验证直到可读 |
| 2 | 解密把 **hex 文本**当作口令 | QQ 的密钥是 **16 字节原始值**，SQLCipher 会把它送进 PBKDF2；用 32 位 hex 文本推导出的是完全不同的密钥 | 以原始字节作为口令，并保留 raw key / hex 两种兼容形式 |
| 3 | `sqlcipher3-binary` 没有 Windows 轮子 | `install.ps1` 第一步必然失败，只能靠源码编译 | 直接安装 `sqlcipher3`（0.6+ 提供 Windows 官方 wheel） |
| 4 | x64 `CONTEXT` 结构少了 208 字节 | `GetThreadContext` 会越界写托管堆，调试 QQ 时可能崩溃 | 按官方布局用 1232 字节原始缓冲区读写寄存器 |
| 5 | 密钥长度靠"字符串里第一个 0 在哪"猜 | 密钥不含 `\0` 时永远抓不到 | 从 **R9**（`int nKey` 参数）读取长度，另保留 NUL 兜底 |
| 6 | 纯 PowerShell 逐字节扫描 112 MB 的 `wrapper.node` | 静态分析要跑几十分钟（旧日志里跑了 68 秒仍无结果） | 改为 C# 实现，**0.3 秒**完成，结果与旧版一致（RVA 0x21EBE20） |
| 7 | 消息列按**位置下标**读取（`row[13]`、`row[17]`） | QQ 表结构一变，时间戳就读到别的列 | 全部改为按列名（`[40050]` 等）查询 |
| 8 | 过滤条件写死 `u_***********_<QQ>` | 掩码 UID 格式早就变了，只能匹配到旧消息 | QQ 号 + 映射表真实 UID + 掩码后缀，三者取并集 |
| 9 | 引用消息解析对象搞错（把 `Element` 当 `Elements` 解） | 引用的原文全部丢失 | 按 `Element` 正确解析，引用内容完整保留 |
| 10 | 收发方向只看 `40013` | 从手机/另一设备发的消息（`40013=0`）被当成"收" | 优先比较 `40033` 发送者 QQ 号与自己的号码 |
| 11 | Markdown 按 `date[:4]` 统计年份 | 含"未知日期"时统计表错乱 | 只统计有效时间戳 |
| 12 | `.ps1` 文件无 UTF-8 BOM | PowerShell 5.1 按 ANSI 读取，中文变乱码甚至解析失败 | 所有脚本带 BOM，`selftest.py` 会校验 |
| 13 | `run_export.ps1` 用正则解析中文控制台输出取密钥 | 控制台编码一变就取不到密钥 | 密钥写入临时文件传递，正则仅作兜底 |
| 14 | 硬编码 `D:\Users\Administrator\...` | 换台电脑全部失效 | 统一走 `qqcommon.py` / `qqenv.ps1` 的路径与注册表探测 |
| 15 | 只拷贝主库，忽略 `-wal` | QQ 运行期间最新消息在 WAL 里，导出的记录少了最近几天 | 一并复制并回放 WAL |
| 16 | 解密失败会先删掉旧的解密结果 | 一次失败的尝试会毁掉可用的数据库 | 先写 `.tmp`，成功后原子替换 |
| 17 | 调试器无超时、无法附加、句柄泄漏 | 断点没命中就永久卡住；QQ 已运行时行为不可预期 | 超时、附加模式、完整清理（含清除单步标志，避免 QQ 崩溃） |
| 18 | 没有任何自动化测试 | 改动无法验证 | `selftest.py` + `check_deps.py --json` |

---

## 3. 系统要求

| 项目 | 要求 |
|------|------|
| 操作系统 | Windows 10 / 11 |
| QQ 客户端 | QQ NT 9.9.x，且已登录过目标账号 |
| Python | 3.8 及以上（`install.ps1` 会自动安装） |
| PowerShell | 5.0 及以上（Windows 自带；PowerShell 7 也兼容） |
| 必需依赖 | `sqlcipher3`（仅此一个，Windows 有官方 wheel） |
| 可选依赖 | QQNT_Export 的 `emojis.py`（把系统表情显示成中文名） |
| 磁盘空间 | 约 200 MB（Python + 依赖 + 解密结果） |

---

## 4. 快速开始

```powershell
cd scripts

# ① 首次使用：部署环境（安装 Python + sqlcipher3 + 可选 QQNT_Export）
powershell -ExecutionPolicy Bypass -File install.ps1

# ② 看有哪些会话（需要先登录过 QQ）
powershell -ExecutionPolicy Bypass -File run_export.ps1 -ListPeers

# ③ 退出 QQ（托盘图标右键退出），然后导出与某个人的聊天记录
powershell -ExecutionPolicy Bypass -File run_export.ps1 -PeerQQ 10001
```

输出文件（默认在当前目录）：

- `与10001的聊天记录.txt`
- `与10001的聊天记录.md`
- `与10001的聊天记录.json`

> **为什么导出前要退出 QQ？** 提取密钥需要在 QQ 打开数据库的瞬间拦截函数调用，
> 必须由脚本自己启动 QQ 并附加调试器。QQ 正在运行时 Windows 会拒绝第三方调试器附加
> （`DebugActiveProcess` 返回错误 31），所以脚本会直接提示你先退出 QQ。

---

## 5. 部署到新电脑

```powershell
cd scripts
powershell -ExecutionPolicy Bypass -File install.ps1
```

`install.ps1` 依次完成：

| 步骤 | 操作 | 说明 |
|------|------|------|
| 1 | 检查 Python | 真正执行 `--version` 验证，绕开微软商店的假 `python.exe` |
| 2 | 安装 Python | 优先 `winget`，失败则从 python.org 下载官方安装包（用户级静默安装） |
| 3 | 安装 `sqlcipher3` | Windows 上有预编译 wheel，不需要编译环境；失败会提示改用 `--user` |
| 4 | 可选下载 QQNT_Export | 只提供表情中文名；下载失败不影响导出 |
| 5 | 汇总环境 | Python / sqlcipher3 / QQNT_Export / QQ NT / QQ 数据目录 |

参数：

```powershell
.\install.ps1 -DryRun                            # 只检查不安装，看还缺什么
.\install.ps1 -QQNTExportDir "D:\tools\QQNT_Export"
.\install.ps1 -PythonVersion 3.12                # 需要装 Python 时使用的版本
.\install.ps1 -Python "C:\Python312\python.exe"  # 指定已有解释器
.\install.ps1 -SkipPython                        # 跳过 Python 安装检查
.\install.ps1 -SkipQQNTExport                    # 不下载 QQNT_Export
```

部署完成后建议先跑一次自检：

```powershell
python check_deps.py        # 逐项检查运行环境
python selftest.py          # 端到端自测（不需要真实 QQ 数据）
```

---

## 6. 导出聊天记录

### 6.1 基本用法

```powershell
cd scripts
powershell -ExecutionPolicy Bypass -File run_export.ps1 -PeerQQ <对方QQ号>
```

### 6.2 参数

| 参数 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `-PeerQQ` | 是 | — | 对方 QQ 号 |
| `-MyQQ` | 否 | 自动检测 | 自己的 QQ 号（按 `nt_msg.db` 最近活动时间推测） |
| `-Format` | 否 | `all` | `all` / `txt` / `md` / `json`，可用逗号组合如 `txt,md` |
| `-OutputDir` | 否 | 当前目录 | 输出目录 |
| `-Key` | 否 | — | 直接提供密钥，跳过密钥提取 |
| `-DecryptedDb` | 否 | — | 直接用已解密的 `nt_msg.db`，跳过提取与解密 |
| `-Python` | 否 | 自动检测 | 指定 Python 解释器 |
| `-QQNTExport` | 否 | 自动检测 | QQNT_Export 目录（表情中文名） |
| `-TencentFiles` | 否 | 自动检测 | 自定义 `Tencent Files` 目录 |
| `-AllDatabases` | 否 | 关 | 解密 `nt_db` 下全部数据库（默认只解密 `nt_msg.db`） |
| `-Force` | 否 | 关 | 强制重新解密（默认源库有更新时才重解） |
| `-ListPeers` | 否 | 关 | 只列出单聊对象和消息数 |
| `-SkipDepsCheck` | 否 | 关 | 跳过依赖检查（不推荐） |
| `-TimeoutSeconds` | 否 | 180 | 等待密钥断点命中的最长时间 |

### 6.3 示例

```powershell
# 只导出 Markdown 到指定目录
.\run_export.ps1 -PeerQQ 10001 -Format md -OutputDir D:\export

# 已经知道密钥（例如上次提取过），跳过调试
.\run_export.ps1 -PeerQQ 10001 -Key 66643F3F53346E6763244E4175487559

# 复用已解密的数据库（不需要 QQ、不需要密钥）
.\run_export.ps1 -PeerQQ 10001 -DecryptedDb "$env:USERPROFILE\Desktop\QQNT_output\decrypted_10002\nt_msg.db"

# 列出所有会话
.\run_export.ps1 -ListPeers
```

### 6.4 执行流程

1. **依赖检查** — `check_deps.py`（缺失自动 `pip install`）
2. **密钥提取** — `get_key_hex.ps1`：静态分析 `wrapper.node` → 启动 QQ → 断点取密钥
3. **账号识别** — 扫描 `Documents\Tencent Files` 下各账号 `nt_msg.db` 的修改时间
4. **解密** — `decrypt_db.py`：剥离 1024 字节头 → 探测参数 → 逐表导出明文库
5. **导出** — `export_messages.py`：查询 C2C 消息 → 解析消息体 → 写 TXT/MD/JSON

退出码：`0` 成功、`1` 环境/依赖问题、`2` 参数错误、`3` 密钥提取失败、`4` 解密失败。

---

## 7. 手动分步执行

### 7.1 检查依赖

```powershell
python scripts/check_deps.py
python scripts/check_deps.py --no-install     # 只检查
python scripts/check_deps.py --json           # 机器可读
```

### 7.2 提取密钥

```powershell
# 先退出 QQ！
powershell -ExecutionPolicy Bypass -File scripts/get_key_hex.ps1

# 只看静态分析结果（不启动 QQ，用于排查版本变化）
powershell -ExecutionPolicy Bypass -File scripts/get_key_hex.ps1 -NoDebugForKey

# 把密钥写入文件
powershell -ExecutionPolicy Bypass -File scripts/get_key_hex.ps1 -OutFile "$env:TEMP\key.txt" -Quiet
```

输出示例：

```
=== 静态分析结果 ===
  字符串 RVA    : 0x48D89EF
  LEA 指令 RVA  : 0x21EBE35（共 1 处引用）
  函数 RVA      : 0x21EBE20
  耗时          : 266 ms（文件 112.4 MB）

=== 最终结果 ===
  函数 RVA   : 0x21EBE20
  加密密钥   : 66643F3F53346E6763244E4175487559
  密钥长度   : 16 字节
```

### 7.3 解密数据库

```powershell
python scripts/decrypt_db.py <QQ号> <密钥>

# 常用参数
python scripts/decrypt_db.py 10002 <密钥> --only nt_msg.db   # 只解密消息库
python scripts/decrypt_db.py 10002 <密钥> --force            # 覆盖旧结果
python scripts/decrypt_db.py 10002 --list                    # 列出数据库与头部信息
```

参数：`--src` 源目录、`--dst` 输出目录、`--tencent-files`、`--key-format`、`--hmac`、`--kdf-iter`、`--page-size`、`--no-wal`、`-q`。

密钥也可以放在环境变量里：`$env:QQNT_DB_KEY = "..."`。

### 7.4 导出聊天记录

```powershell
python scripts/export_messages.py <解密后的 nt_msg.db> <对方QQ号> <输出目录> --format all --my-qq <自己的QQ号>

python scripts/export_messages.py --list-peers                   # 列出会话
python scripts/export_messages.py --account 10002 --peer 10001    # 自动定位数据库
```

额外参数：`--with-paths`（图片/视频附带本地路径与 URL）、`--bom`（TXT 带 BOM，老记事本友好）、`-q`。

---

## 8. 输出格式说明

### 8.1 TXT

```
QQ聊天记录 - 与 10001
共 121 条消息
时间范围: 2023-03-18 18:39:01 ~ 2026-10-17 17:02:55
导出时间: 2026-10-20 18:39:17
============================================================

===== 2024-01-27 =====

2024-01-27 19:33:06 收
[文本]
不是我买

2024-01-27 19:33:16 发
[文本]
那干啥
```

### 8.2 Markdown

```markdown
# QQ 聊天记录 — 与 10001

- **消息总数**：1768 条
- **时间范围**：2024-01-27 19:33:06 ~ 2026-10-14 15:13:38
- **数据来源**：`nt_msg.db`
- **导出时间**：2026-10-20 18:39:17

---

## 按年统计

| 年份 | 消息数 |
|------|--------|
| 2024 | 572 |

---

## 2024-01-27

**19:33:06 对方**：不是我买
**19:33:16 我**：那干啥
```

非纯文本消息会带类型标记，例如 `` `[图片]` ``、`` `[表情]` ``。

### 8.3 JSON

```json
{
  "meta": {
    "generator": "QQNT-chat-export v3.0.0",
    "my_qq": "10002",
    "peer_qq": "10001",
    "total": 1768,
    "date_range": {"start": "2024-01-27 19:33:06", "end": "2026-10-14 15:13:38"},
    "exported_at": "2026-10-20 18:39:17",
    "source_db": "nt_msg.db",
    "stats": {
      "by_year": {"2024": 572},
      "by_type": {"文本": 1600, "图片": 120, "表情": 48},
      "by_direction": {"收": 900, "发": 868}
    }
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

| 字段 | 说明 |
|------|------|
| `meta.my_qq` / `peer_qq` | 双方 QQ 号 |
| `meta.total` | 消息总数 |
| `meta.date_range` | 时间范围 |
| `meta.stats` | 按年 / 按类型 / 按方向统计 |
| `messages[].id` | 消息 ID（`40001`） |
| `messages[].direction` | `sent` / `received` / `forwarded` / `system` |
| `messages[].type` | 主类型：文本 / 图片 / 表情 / 文件 / 语音 / 视频 / 引用 / 提示 / 红包 / 应用消息 / 聊天记录 / 通话 / 动态 |
| `messages[].elements` | 分段内容（label + content） |

---

## 9. 脚本参考

### 9.1 文件结构

```
QQNT-Chat-Export/
├── README.md                      # 本文档
├── SKILL.md                       # Skill 元数据与速查
├── backup/v2.2-original.zip       # 重构前备份
├── references/
│   └── database_schema.md         # 数据库结构参考
└── scripts/
    ├── install.ps1                # 一键部署
    ├── run_export.ps1             # 统一入口
    ├── get_key_hex.ps1            # 密钥提取（静态分析 + 调试器）
    ├── qqenv.ps1                  # PowerShell 共用函数库
    ├── check_deps.py              # 环境自检
    ├── decrypt_db.py              # 数据库解密
    ├── export_messages.py         # 多格式导出
    ├── qqcommon.py                # Python 共用函数库（路径/账号/密钥）
    ├── qq_elements.py             # 消息体 protobuf 解码（免 protobuf 依赖）
    └── selftest.py                # 端到端自测
```

### 9.2 职责

| 脚本 | 语言 | 职责 |
|------|------|------|
| `install.ps1` | PowerShell | 部署：Python、sqlcipher3、可选 QQNT_Export |
| `run_export.ps1` | PowerShell | 统一入口：检查→取密钥→识别账号→解密→导出 |
| `get_key_hex.ps1` | PowerShell + C# | 静态分析 PE 定位函数 RVA；调试器抓取密钥 |
| `qqenv.ps1` | PowerShell | QQ 安装/数据目录/账号/Python 探测与输出格式化 |
| `check_deps.py` | Python | 环境检查、自动安装、`--json` 输出 |
| `decrypt_db.py` | Python | SQLCipher 解密，参数自动探测，逐表导出明文 |
| `export_messages.py` | Python | 查询 C2C 消息、解析消息体、输出三种格式 |
| `qqcommon.py` | Python | 共用：路径、账号、密钥规范化、格式化 |
| `qq_elements.py` | Python | 消息体 protobuf 解码与渲染（零依赖） |
| `selftest.py` | Python | 合成数据库 + 全流程回归测试 |

---

## 10. 常见问题

### Q: 为什么解密总是失败？

A: 先看 `decrypt_db.py --list` 输出的头部信息（格式版本、HMAC 算法），再看报错：
- "尝试了 N 种密钥/参数组合仍无法读取数据库" → **密钥过期**。QQ 重装、升级或换号都会更换密钥，请重新运行 `get_key_hex.ps1`。
- `install.ps1` 里 sqlcipher3 装不上 → 用 `python -m pip install sqlcipher3`（不是 `sqlcipher3-binary`，后者没有 Windows 版本）。

### Q: 导出的消息比以前少 / 少了最近几天？

A: 检查三件事：
1. 是否在 QQ 运行时复制的数据库（最新消息可能还在 `-wal` 里）—— v3 默认会合并 WAL，但要求源库能被正常复制；
2. 是否用了 `-Force` 重新解密（默认只在源库更新时才重解）；
3. 对方 QQ 号是否正确 —— 用 `run_export.ps1 -ListPeers` 确认。

### Q: 收发方向不对？

A: v3 用 `40033`（发送者 QQ 号）判断方向，需要 `--my-qq` / `-MyQQ`。不提供时会退回标志位（`40013`），个别从其他设备发送的消息可能被标成"收"。

### Q: 密钥里有 `?` 字符是正常的吗？

A: 正常。密钥是 16 字节原始数据，可能恰好包含 `0x3F`（`?`）。请使用 hex 形式的密钥，**不要**手工把 hex 转成 ASCII。

### Q: 不知道对方 QQ 号？

A: `.\run_export.ps1 -ListPeers` 会列出所有单聊对象、消息数和时间范围。

### Q: 提取密钥时提示"检测到 QQ 正在运行"？

A: 请完全退出 QQ（托盘右键退出）后重试。QQ 拒绝第三方调试器附加（错误 31），必须先关闭。若确实想尝试附加，用 `get_key_hex.ps1 -Attach`。

### Q: 提示"未能捕获密钥"？

A:
1. 确认 QQ 能正常启动并登录；
2. 用 `get_key_hex.ps1 -NoDebugForKey` 验证静态分析是否还能定位到函数；
3. 若 QQ 升级后字符串或函数结构变化，`-NoDebugForKey` 会报"未找到字符串/LEA 指令"，此时需要更新工具，或用 `-FunctionRva` 手动指定（配合 IDA 等工具）。

### Q: 群聊能导出吗？

A: 当前版本只导出**单聊（C2C）**。群聊数据在 `group_msg_table`，字段结构类似，但方向、群成员昵称、@ 信息需要额外处理，可用同样的 `qq_elements` 思路自行扩展。

### Q: 表情只显示编号？

A: 安装可选组件 QQNT_Export（`install.ps1` 会自动下载）即可显示成"擦汗-97"这样的中文名。没有它也能正常导出。

---

## 11. 故障排除

### 11.1 依赖问题

```powershell
python check_deps.py            # 逐项检查（会自动安装缺失项）
python check_deps.py --json     # 供脚本消费
python -m pip install sqlcipher3
```

`sqlcipher3` 导入失败常见原因：
- Python 版本太新/太旧没有对应 wheel → 换 3.10~3.13；
- 缺少 VC++ 运行库 → 安装 [Microsoft Visual C++ Redistributable](https://aka.ms/vs/17/release/vc_redist.x64.exe)。

### 11.2 Python 不在 PATH

`install.ps1` 装完 Python 后当前终端可能还没有新 PATH，脚本会自动重新读取注册表里的 PATH 再找一次；如果仍失败，请重开终端，或用 `-Python` 指定：

```powershell
.\run_export.ps1 -PeerQQ 10001 -Python "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
```

### 11.3 解密后部分数据库失败

`nt_db` 下通常有 60+ 个数据库，个别（如 FTS 索引库）可能页损坏。只要 `nt_msg.db` 成功，聊天记录就能导出；`decrypt_db.py` 会列出失败的库名。

### 11.4 消息内容显示 `[未知类型:N]`

说明 QQ 新增了消息段类型。把类型编号反馈即可扩展映射表（`qq_elements.py` 的 `KNOWN_TYPE_LABELS` 与 `_render`）。已知类型 16 是"合并转发的聊天记录"，其正文由相邻的文本段给出。

### 11.5 PowerShell 脚本中文乱码 / 解析报错

`.ps1` 必须是 **UTF-8 with BOM**，否则 PowerShell 5.1 会按 ANSI 解码。`selftest.py` 会检查这一点；手工编辑脚本后请保持 BOM。

### 11.6 调试器相关

- 需要管理员权限时脚本会提示"请尝试以管理员身份运行"；
- 只验证静态分析：`get_key_hex.ps1 -NoDebugForKey`；
- 调试结束后脚本一定会恢复断点字节、清除单步标志，避免影响 QQ。

---

## 12. 技术细节

### 12.1 数据库格式

每个 `.db` 文件前 1024 字节是 QQ 自定义头：

| 偏移 | 内容 |
|------|------|
| 0x00 | `"SQLite header 3\0"`（16 字节） |
| 0x20 | `"QQ_NT DB"`（8 字节标记） |
| 0x28 | 后续 protobuf 元数据的长度（uint32 LE） |
| 0x2C | protobuf：字段 2 = 128 位 hex 随机串，字段 3 = 格式版本（如 `1.1.0.1`），字段 4 = HMAC 算法（如 `HMAC_SHA1`），字段 5 = 创建时间戳 |

剥离 1024 字节后即为标准 SQLCipher 4 数据库。`decrypt_db.py` 会解析这段头，用其中的 HMAC 算法作为首选参数。

### 12.2 解密参数

实测（QQ 9.9.36，2026 年的数据库）：

```
PRAGMA cipher_page_size = 4096;            -- 必须在 key 之前
PRAGMA key = '<16 字节原始密钥>';           -- 原始字节，不是 hex 文本
PRAGMA kdf_iter = 4000;
PRAGMA cipher_hmac_algorithm = HMAC_SHA1;  -- 数据库头部就是这么写的
PRAGMA cipher_kdf_algorithm = PBKDF2_HMAC_SHA512;
```

因为不同版本可能不同，v3 不再写死，而是按"头部提示优先 + 常见组合兜底"依次验证 `sqlite_master` 是否可读，成功即锁定该组合。

### 12.3 密钥提取

1. 静态分析：在 `wrapper.node` 的 `.rdata` 找字符串 `nt_sqlite3_key_v2: db=%p zDb=%s` 的 RVA；在 `.text` 扫描 RIP-relative `lea`（`48/4C 8D /r`，mod=00 rm=101）找到引用它的指令；用 `.pdata` 异常表找到所属函数起始 RVA。
2. 动态调试：以 `DEBUG_ONLY_THIS_PROCESS` 启动 `QQ.exe`，在 `wrapper.node` 加载后向函数入口写 `0xCC`；命中后读取 `R8`（pKey）与 `R9`（nKey），读出 `nKey` 字节即为密钥。
3. 收尾：恢复原字节、修正 RIP、清除单步标志；启动模式结束进程，附加模式 `DebugActiveProcessStop`。

### 12.4 消息表结构

`c2c_msg_table` 关键列：

| 列 | 说明 |
|----|------|
| `40001` | 消息 ID（主键） |
| `40013` | 消息标志（0=收，1/2/5=发，3=系统提示，8=转发） |
| `40020` | 发送者 UID |
| `40021` | 对方 UID（新版是真实 UID，旧版是掩码 UID） |
| `40030` | 对方 QQ 号（旧消息可能为 NULL） |
| `40033` | 发送者 QQ 号（判方向最可靠） |
| `40050` | 时间戳（Unix 秒） |
| `40800` | 消息体（protobuf `Elements`） |

`nt_uid_mapping_table`：`1002` = QQ 号，`48902` = UID（本机数据里真实可用的那个），`48912` = 备用 UID 列（本机数据为空）。

### 12.5 消息体结构

```protobuf
message Elements { repeated Element elements = 40800; }
message Element {
  uint64 id = 45001;
  uint32 type = 45002;   // 1 文本 2 图片 3 文件 4 语音 5 视频 6 表情
                         // 7 引用 8 提示 9 红包 10 应用消息 16 聊天记录 21 通话 26 动态
  string text = 45101;
  ...
}
```

`qq_elements.py` 直接按 wire format 解析，不依赖 `element_pb2`，因此不受 protobuf 版本影响。

### 12.6 第三方组件

- **sqlcipher3**：SQLCipher 的 Python 绑定（解密必需）
- **QQNT_Export**（可选）：只提供 `emojis.py` 表情名称表

---

## 13. 自检与回归测试

```powershell
python scripts/selftest.py        # 13 项检查
python scripts/selftest.py -v     # 显示子进程输出
python scripts/selftest.py --keep # 保留临时目录
```

覆盖内容：

| 检查 | 说明 |
|------|------|
| 密钥规范化 | hex / `x'..'` / 原始文本，三种 PRAGMA 形式顺序 |
| 时间戳转换 | 秒、毫秒、0/None |
| 消息体解析边界 | 空/垃圾/截断/裸 Element |
| QQ 号校验 | 合法/非法输入 |
| QQ 安装检测 | 注册表 + 版本目录 |
| PowerShell 编码/语法 | BOM + 解析 |
| 解密 + 导出 | 合成加密库 → 解密 → 导出，逐项比对内容 |
| 导出错误处理 | 加密库/缺密钥/错密钥/无此会话 |
| 会话列表、环境变量密钥、旧命令行兼容、重复解密跳过 | 行为回归 |

没有 `sqlcipher3` 时解密相关项显示 SKIP，其余照常运行。

---

## 附：版本变更

| 项目 | v2.2 | v3.0（当前） |
|------|------|-------------|
| 解密参数 | 写死 HMAC_SHA512（错误） | 头部读取 + 自动探测 |
| 密钥形式 | hex 文本当口令（错误） | 原始字节口令（并兼容旧形式） |
| pip 依赖 | 8 个 | 1 个（sqlcipher3） |
| 消息体解析 | 依赖 QQNT_Export / element_pb2 | 内置解码器 |
| 密钥提取耗时 | 几分钟以上（PowerShell 扫描） | 0.3 秒（C#） |
| 超时控制 | 无 | 有（默认 180 秒） |
| WAL 最新消息 | 丢失 | 合并 |
| 自动化测试 | 无 | `selftest.py` 13 项 |
| 输出格式 | TXT/MD/JSON | TXT/MD/JSON（+ 统计、消息 ID） |
