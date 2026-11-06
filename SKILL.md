---
name: lark-mail-sync
description: This skill should be used when syncing Lark Mail settlement confirmation emails into the Lark Base (bitable) settlement table — matching emails whose subject or body contains "confirm" (case-insensitive), parsing container/TK/quantity lines, allocating invoice amounts by quantity, uploading invoice PDF attachments, and extracting vendor/currency/invoice-date fields from the PDF. Also covers archiving fully-processed settlement emails out of the inbox (move to the ARCHIVED folder, strictly limited to emails already recorded in the table), and guiding the first-time enterprise custom-app (cli_ credentials) application and permission setup. Covers environment setup, connectivity probe, incremental sync, and state rebuild after reinstall or machine change. Strictly incremental and append-only: it must never run reset-table (wipes the whole table) or backfill (full re-import that duplicates rows when local state is empty). Triggers include 邮件同步, 同步邮件, 跑一轮同步, 结算邮件, confirm邮件, TK录入, 结算录入, 多维表同步, 邮件同步SKILL, 归档已处理邮件, 归档邮件, 申请应用, 自建应用, 申请权限, lark mail sync.
agent_created: true
---

# Lark 邮件 → 多维表 结算同步（confirm-TK 录入）

## 一、首要红线（绝对不可违反）

1. **绝不执行 `reset-table`**。它会清空多维表全部记录并删除字段，无任何确认能挽回。
2. **绝不执行 `backfill`**（全量回填）。本机去重只认本地游标 `data/state.json`；
   新机器/重装后 state 为空，此时 backfill 会把历史邮件**整表重复录入**。
   首次装机请用 `rebuild-state`（见步骤 4），日常一律 `sync`。
3. **绝不读取、打印、转发 `.env` 里的凭证**（app_secret 等）。诊断配置问题时只报告
   「哪个键缺失/为空」，不回显值。
4. **对多维表的写入只经本工具**。不要直接调 Lark API 改表、不要手工拼 record JSON 写入。
5. **失败即停、如实报告**。某轮 sync 报错时把完整错误贴给用户并停止，不要静默重试；
   连续失败 3 次应怀疑凭证/网络/权限问题，进入故障手册排查。
6. **绝不修改、删除多维表里已有记录**。本工具只追加；录入错误的处理方式是提示用户
   在多维表界面人工处理（表是共享的，别人也在用）。

## 二、适用场景

- 业务说「同步一下邮件 / 跑一轮同步 / 录入结算邮件」→ 执行增量 `sync`
- 业务说「归档已处理邮件 / 收件箱清一清」→ 先跑 `sync`（确保没有漏录的），再执行 `archive`
- 业务说「还没应用 / 怎么申请应用 / 要权限」→ 按 `references/应用申请指引.md` 分步引导
  （建应用 → 开 `mail:user_mailbox.message:readonly` + `bitable:app` → 发布审批 →
  可用范围加自己 → 管理员加文档应用；全程不要替业务保管/回显 App Secret）
- 业务问「环境装好了没 / 连得上吗」→ 执行 `probe` 自检
- 业务换了电脑、重装、或怀疑漏同步 → `rebuild-state` 后 `sync`
- 业务问「某列是怎么填的 / 为什么这行有异常报错」→ 查 `references/录入口径.md`

**标准调用话术（用户可直接复制）**：

> 请用【邮件同步 SKILL】跑一轮同步。

## 三、环境要求

- Windows，Python **3.9 或更高**（命令行 `py -3 --version` 可查）
- 能联网（访问 open.larksuite.com 与 PyPI；有 git 更新方便，没有也能用 ZIP 方式装）
- 一个能读写本地文件、执行命令的 AI 助手（WorkBuddy 这类）
- **业务自己申请的企业自建应用凭证**（cli_ 开头的 App ID + App Secret，
  申请流程见 `references/应用申请指引.md`；同一个应用既读邮件又写表，
  `.env` 里 BITABLE_APP_ID/SECRET 留空）
- 管理员发的目标表信息（BITABLE_APP_TOKEN / BITABLE_TABLE_ID，非机密）

## 四、装机流程（每台新电脑一次）

本 skill 从 GitHub 分发：**仓库根目录 = skill 本体 = 工作目录**（单层结构，
`.env`、`data\`、`python_venv\` 都在本文件夹内，已在 .gitignore 里，更新不会覆盖）。
下文 `<根>` 指本文件夹的绝对路径，正常就是
`C:\Users\<用户名>\.workbuddy\skills\lark-mail-sync`。

0. **（仅当本文件夹还不在 skills 目录时）先安装**：把本仓库放入
   `%USERPROFILE%\.workbuddy\skills\lark-mail-sync`——
   `git clone <仓库地址> "%USERPROFILE%\.workbuddy\skills\lark-mail-sync"`，
   或浏览器 Download ZIP → 解压文件夹改名 `lark-mail-sync` → 挪进 skills 目录。
   详见 `README.md` 安装一节。
1. **装依赖**：双击 `<根>\setup.bat`（或由你执行同等命令：建 `python_venv` + 装依赖 + 跑离线自测）。
2. **配凭证**：双击 `<根>\config-env.bat` 生成 `.env` 并打开记事本——
   填**业务自己申请的应用凭证**（LARK_APP_ID/SECRET）+ 业务邮箱
   （MAIL_USER_MAILBOX_ID）+ 管理员发的表信息（BITABLE_APP_TOKEN/TABLE_ID）；
   BITABLE_APP_ID/SECRET 留空。还没申请过应用 → 先走
   `references/应用申请指引.md`。
3. **自检**：执行 probe（命令见下）。「双应用 token OK + 邮箱可读 + 字段齐全」四行全过才算通。
4. **恢复游标（关键步骤，跳过会导致重复录入）**：执行 `rebuild-state`——从多维表「消息ID」列
   反向重建本机去重游标。输出 `已从多维表重建状态：N 个消息ID。`（N 与表内已有记录数同量级为正常）。
5. **首轮同步**：执行 `sync`。增量拉取新到的 confirm 邮件并写入多维表。

## 五、命令参考（在本节命令前必须先 `cd` 到工作目录）

CMD 形式：

```bat
cd /d <根>
set PYTHONPATH=<根>
python_venv\Scripts\python -m lark_mail_sync --env-file .env probe
python_venv\Scripts\python -m lark_mail_sync --env-file .env sync
python_venv\Scripts\python -m lark_mail_sync --env-file .env archive
python_venv\Scripts\python -m lark_mail_sync --env-file .env rebuild-state
python_venv\Scripts\python -m lark_mail_sync --env-file .env fields --ensure
```

bash 形式（路径正斜杠）：

```bash
cd "<根>"
export PYTHONPATH="<根>"
python_venv/Scripts/python -m lark_mail_sync --env-file .env probe
python_venv/Scripts/python -m lark_mail_sync --env-file .env sync
python_venv/Scripts/python -m lark_mail_sync --env-file .env archive
python_venv/Scripts/python -m lark_mail_sync --env-file .env rebuild-state
```

| 命令 | 作用 | 何时用 |
|---|---|---|
| `probe` | 连通/权限自检（token、邮箱可读、表字段齐全） | 装机后、排障时 |
| `sync` | **增量同步**新到的 confirm 邮件 | 日常，唯一常用命令 |
| `archive` | 把**已处理且已写入表**的邮件移入归档文件夹 ARCHIVED（只动自己邮箱，不碰表；未处理完的组一律不动） | `sync` 之后做邮箱整理时 |
| `rebuild-state` | 从表内「消息ID」列重建本机游标 | 换电脑/重装/删了 data 目录后，`sync` 之前 |
| `fields --ensure` | 补建多维表缺失字段 | probe 报缺字段时 |
| 命令前加全局 `--dry-run` | 只打印将做什么，不执行 | 业务想先看一眼时 |

全局旗标 `--dry-run` 放在**子命令前面**（`--env-file .env --dry-run sync`）。

**不要执行**：`backfill`、`reset-table`、`watch`（常驻进程交给人工决定，AI 会话里不挂后台）。

## 六、输出解读

- `sync` 正常输出为每封新邮件的录入计划/结果行；`无待处理 confirm 邮件。`= 邮箱里没有新确认邮件，正常。
- 某些邮件会以「异常报错」列落表（**这是设计行为，不是故障**）：未识别 TK 行、正文与 PDF 均无金额、
  同名邮件无附件等。落表后由人工在多维表里补，详见 `references/录入口径.md`。
- 每行都会写「消息ID」列（系统幂等列，用户界面上通常已隐藏，别当成脏数据删掉——
  rebuild-state 靠它恢复游标）。

## 七、故障手册

| 现象 | 原因 | 解法 |
|---|---|---|
| probe 报 `[mail] 缺权限：99991672` | 应用的邮件权限没发布通过，或「可用范围」没加业务本人 | 按 `references/应用申请指引.md` 第 3、4 步补齐；不是本机问题，别重装 |
| probe 报 `缺少必填配置 XXX` | `.env` 没填全 | 对照 `.env.example` 补齐缺失键 |
| probe 报多维表失败（权限/403 类） | 应用未加为该表「文档应用」，或 bitable:app 权限没发布 | 按 `references/应用申请指引.md` 第 2、5 步处理（管理员把应用加为文档应用·可编辑） |
| `[token]` 行失败 | app_id/app_secret 不匹配或网络不通 | 先测网络（open.larksuite.com），再核对凭证 |
| 换电脑/删了 data 目录后想同步 | 本机游标没了 | 先 `rebuild-state` 再 `sync`；**绝不能 backfill** |
| 表里出现重复行 | 在空游标机器上跑过 backfill，或游标损坏 | 停止自动处理；提示用户人工在多维表删重复行，然后 `rebuild-state` 重建游标 |
| 某行异常报错=「同名邮件无附件」 | confirm 邮件与带 PDF 的转发间隔几秒先后到达，同步正好夹在中间（罕见） | 把该组行连同「消息ID」情况报给管理员处理，不要自行删行后重跑 backfill |
| pip 安装超时/失败 | 网络问题 | `setup.bat` 会自动换清华镜像重试；仍失败让用户检查网络/代理 |
| 输出中文乱码 | 控制台代码页 | 运行前 `chcp 65001`；双击 `sync.bat` 已内置 |
| 想看「这轮会录什么」 | — | `--dry-run sync` |

## 八、数据口径

录入哪些列、金额怎么摊分、发票字段从 PDF 怎么识别、防重规则——全部在
**`references/录入口径.md`**。回答业务任何「这列为什么是这个值」之前先读它；
口径本身不要凭感觉改，业务要改口径时让用户找管理员（口径改动需重发新版包）。
应用申请/凭证/权限问题——全部在 **`references/应用申请指引.md`**（含常见卡点速查）。

## 九、目录结构（仓库根目录 = skill 本体 = 工作目录）

```
<根>\                          ← 仓库克隆/下载到 .workbuddy\skills\lark-mail-sync
├── SKILL.md                   ← 本文件
├── references\应用申请指引.md   ← 业务自己申请企业自建应用（cli_ 凭证）的分步指引
├── references\录入口径.md      ← 数据口径与防重规则
├── lark_mail_sync\            ← Python 源码
├── tests\                     ← 离线自测（无需网络）
├── requirements.txt
├── requirements-dev.txt
├── .env.example               ← 配置模板（凭证留空，等管理员另发）
├── setup.bat                  ← 一键装依赖 + 自测
├── config-env.bat             ← 生成 .env 并打开记事本
├── sync.bat                   ← 双击跑一轮增量同步（不想说话时的手动入口）
├── 使用指引.txt                ← 给人看的快速上手
├── README.md                  ← 仓库首页（安装/更新说明）
├── .env / data\ / python_venv\ ← 装机后本地生成（.gitignore 已排除，更新不覆盖）
└── .gitignore
```

> 注意：`data\state.json` 是本机去重游标，删除会导致下次 sync 无法识别已录邮件
> （补救=`rebuild-state`），平时不要动它。
> 版本更新：`git pull` 后重跑 `setup.bat`（ZIP 装机则重下载整包，`.env` 先备份再贴回）。
