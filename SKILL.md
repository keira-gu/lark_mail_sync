---
name: lark-mail-sync
description: This skill should be used when syncing Lark Mail settlement confirmation emails into the Lark Base (bitable) settlement table — matching emails whose subject or body contains "confirm" (case-insensitive), parsing container/TK/quantity lines, allocating invoice amounts by quantity, uploading invoice PDF attachments, and extracting vendor/currency/invoice-date fields from the PDF. Also covers archiving fully-processed settlement emails out of the inbox (move to the ARCHIVED folder, strictly limited to emails already recorded in the table), and creating GoFreight AP entries (建立应付) from bitable rows — plan the submission list first, require explicit user confirmation before submitting, dedup by invoice|TK, auto-screenshot failures. Also covers guiding the first-time enterprise custom-app (cli_ credentials) application and permission setup, filling .env from user-pasted credentials (parsing the bitable link for APP_TOKEN/TABLE_ID), connectivity probe, incremental sync, and state rebuild after reinstall or machine change. Strictly incremental and append-only: it must never run reset-table (wipes the whole table) or backfill (full re-import that duplicates rows when local state is empty). Triggers include 邮件同步, 同步邮件, 跑一轮同步, 结算邮件, confirm邮件, TK录入, 结算录入, 多维表同步, 邮件同步SKILL, 归档已处理邮件, 归档邮件, 建立应付, 录应付, 应付录入, GoFreight, gofreight, 建立应付清单, 申请应用, 自建应用, 申请权限, 填配置, lark mail sync.
agent_created: true
---

# Lark 邮件 → 多维表 → GoFreight 结算闭环（confirm-TK 录入 + 建立应付）

## 一、首要红线（绝对不可违反）

1. **绝不执行 `reset-table`**。它会清空多维表全部记录并删除字段，无任何确认能挽回。
2. **绝不执行 `backfill`**（全量回填）。本机去重只认本地游标 `data/state.json`；
   新机器/重装后 state 为空，此时 backfill 会把历史邮件**整表重复录入**。
   首次装机请用 `rebuild-state`（见步骤 4），日常一律 `sync`。
3. **绝不读取、打印、转发 `.env` 里的凭证**（app_secret、GoFreight 密码等）。诊断配置问题时只报告
   「哪个键缺失/为空」，不回显值。
4. **对多维表的写入只经本工具**。不要直接调 Lark API 改表、不要手工拼 record JSON 写入。
5. **失败即停、如实报告**。某轮 sync 报错时把完整错误贴给用户并停止，不要静默重试；
   连续失败 3 次应怀疑凭证/网络/权限问题，进入故障手册排查。
6. **绝不修改、删除多维表里已有记录**。本工具只追加；录入错误的处理方式是提示用户
   在多维表界面人工处理（表是共享的，别人也在用）。
7. **建立应付必须两段式**：先跑 `plan` 把清单（发票号/TK/供应商/金额/日期）给用户看，
   **用户明确回复确认后才**执行 `submit --yes`。用户只说「建立应付」= 只出计划等确认，
   绝不自行加 `--yes`。
8. **绝不提交无供应商映射的行**。plan 清单里注明「无映射」的服务商，说明管理员还没配
   GoFreight 编号——转告用户找管理员，绝不猜编号、绝不绕过。
9. **`data/gofreight_state.json` 是建立应付防重账本，绝不删除**。删了会重复建立应付。
10. **submit 期间该 GoFreight 账号不能在别处登录**（单会话互踢）。开始提交前提醒用户
    关掉其他浏览器里登录的 GoFreight；提交中提示「重新登录」属正常（约 15 分钟过期自动重登）。

## 二、适用场景

- 业务说「同步一下邮件 / 跑一轮同步 / 录入结算邮件」→ 执行增量 `sync`
- 业务说「归档已处理邮件 / 收件箱清一清」→ 先跑 `sync`（确保没有漏录的），再执行 `archive`
- 业务说「建立应付 / 把表里的发票录进 GoFreight / 出应付清单」→ 先跑 `gofreight_ap plan`
  出清单，**等用户确认一句后**再 `submit --yes`（口径与流程见 `references/建立应付指引.md`）
- 业务说「还没应用 / 怎么申请应用 / 要权限」→ 按 `references/应用申请指引.md` 分步引导
  （建应用 → 添加机器人能力 → 开 6 个权限（见指引第 2 步权限表）→ 发布审批 →
  可用范围加自己 → 管理员加文档应用；全程不要替业务保管/回显 App Secret）
- 业务发来 App ID / App Secret / 邮箱 / 结算表链接 让你「填配置」→ 按四、装机流程第 2 步
  代填 `.env`（表链接自动解析，见该步说明），填完跑 `probe` 自检，按故障手册告诉用户
  是前置哪一步没做对
- 业务问「环境装好了没 / 连得上吗」→ 执行 `probe` 自检
- 业务换了电脑、重装、或怀疑漏同步 → `rebuild-state` 后 `sync`
- 业务问「某列是怎么填的 / 为什么这行有异常报错」→ 查 `references/录入口径.md`
- 业务问「建立应付的金额/日期/费用代码是怎么定的」→ 查 `references/建立应付指引.md`

**标准调用话术（用户可直接复制）**：

> 请用【邮件同步 SKILL】跑一轮同步。
> 请用【邮件同步 SKILL】出一份建立应付清单。　（确认后才会真提交）
> 请用【邮件同步 SKILL】帮我填好配置，再检查一遍是不是都能正常用。

## 三、环境要求

- Windows，Python **3.9 或更高**（命令行 `py -3 --version` 可查）
- 能联网（访问 open.larksuite.com 与 PyPI；有 git 更新方便，没有也能用 ZIP 方式装）
- 一个能读写本地文件、执行命令的 AI 助手（WorkBuddy 这类）
- **业务自己申请的企业自建应用凭证**（cli_ 开头的 App ID + App Secret，
  申请流程见 `references/应用申请指引.md`；同一个应用既读邮件又写表，
  `.env` 里 BITABLE_APP_ID/SECRET 留空）
- 管理员发的目标表信息（BITABLE_APP_TOKEN / BITABLE_TABLE_ID，非机密；给链接也行，
  你按装机流程第 2 步自己解析）
- **建立应付（可选）**：本机装有 Chrome 或 Edge；`requirements-gofreight.txt` 已装
  （setup.bat 会尽力自动装，失败不影响邮件同步）；管理员另发的 GoFreight 配置
  （地址 / 账号 / 密码 / 费用代码 / 供应商映射）

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
1. **装依赖**：双击 `<根>\setup.bat`（或由你执行同等命令：建 `python_venv` + 装依赖 +
   尽力装建立应付可选组件 + 跑离线自测）。
2. **配凭证**：两种方式任选——
   - **对话代填（推荐）**：用户把 4 样信息发给你（App ID、App Secret、邮箱、结算表链接），
     你直接写 `.env`（从 `.env.example` 复制起手）。**结算表链接自动解析，不让用户手抄**：
     链接里 `/base/` 后面那一段 = `BITABLE_APP_TOKEN`（到 `?` 为止）；
     `table=` 后面 `tbl` 开头那串 = `BITABLE_TABLE_ID`；`view=` 忽略。
     写完只回「已填好，缺哪些项」，**绝不回显 Secret**。
   - **手动**：双击 `<根>\config-env.bat` 生成 `.env` 并打开记事本，让用户照 `.env.example`
     注释自己填。
   配置键：LARK_APP_ID/SECRET（业务自己的应用）+ MAIL_USER_MAILBOX_ID（业务邮箱）+
   BITABLE_APP_TOKEN/TABLE_ID（管理员发或链接解析）；BITABLE_APP_ID/SECRET 留空。
   要用建立应付的话，再让管理员发 GoFreight 五样（BASE_URL/USER/PASSWORD/FEE_CODE/
   VENDOR_MAP），同样写进 `.env`。还没申请过应用 → 先走 `references/应用申请指引.md`。
3. **自检**：执行 probe（命令见下）。「双应用 token OK + 邮箱可读 + 字段齐全」四行全过才算通。
   有失败项 → 对照第七节故障手册告诉用户是前置哪一步漏了。
4. **恢复游标（关键步骤，跳过会导致重复录入）**：执行 `rebuild-state`——从多维表「消息ID」列
   反向重建本机去重游标。输出 `已从多维表重建状态：N 个消息ID。`（N 与表内已有记录数同量级为正常）。
5. **首轮同步**：执行 `sync`。增量拉取新到的 confirm 邮件并写入多维表。
6. **（可选）首轮建立应付**：执行 `gofreight_ap plan` 出清单。新机器防重账本是空的，清单可能
   列出别人已录过的行——直接提交也安全（提交前会自动查该 TK 会计页，已有同发票号就跳过
   防双录并记账）；管理员明确告知哪些已录过时，也可先 `mark --invoice N --tk TK-x` 补记再提交。

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
python_venv\Scripts\python -m gofreight_ap --env-file .env plan
python_venv\Scripts\python -m gofreight_ap --env-file .env submit
python_venv\Scripts\python -m gofreight_ap --env-file .env submit --yes
python_venv\Scripts\python -m gofreight_ap --env-file .env mark --invoice 1001 --tk TK-20990011
```

bash 形式（路径正斜杠）：

```bash
cd "<根>"
export PYTHONPATH="<根>"
python_venv/Scripts/python -m lark_mail_sync --env-file .env probe
python_venv/Scripts/python -m lark_mail_sync --env-file .env sync
python_venv/Scripts/python -m lark_mail_sync --env-file .env archive
python_venv/Scripts/python -m lark_mail_sync --env-file .env rebuild-state
python_venv/Scripts/python -m gofreight_ap --env-file .env plan
python_venv/Scripts/python -m gofreight_ap --env-file .env submit --yes
```

| 命令 | 作用 | 何时用 |
|---|---|---|
| `probe` | 连通/权限自检（token、邮箱可读、表字段齐全） | 装机后、排障时 |
| `sync` | **增量同步**新到的 confirm 邮件 | 日常，唯一常用命令 |
| `archive` | 把**已处理且已写入表**的邮件移入归档文件夹 ARCHIVED（只动自己邮箱，不碰表；未处理完的组一律不动） | `sync` 之后做邮箱整理时 |
| `rebuild-state` | 从表内「消息ID」列重建本机游标 | 换电脑/重装/删了 data 目录后，`sync` 之前 |
| `fields --ensure` | 补建多维表缺失字段 | probe 报缺字段时 |
| `gofreight_ap plan` | 读多维表 → 打印待录/跳过清单（只读不写） | 每轮建立应付的**第一步** |
| `gofreight_ap submit` | 不带 `--yes` 时同样只出计划 | 想再看一遍清单时 |
| `gofreight_ap submit --yes` | **逐行真提交**（每行独立，失败自动跳下一行并截图） | 用户确认计划之后 |
| `gofreight_ap submit --yes --dry` | 只填表单不点储存（联调） | 几乎不用 |
| `gofreight_ap mark --invoice N --tk TK-x` | 手工补记「已建立应付」进防重账本 | 管理员告知某笔已录过时 |
| 命令前加全局 `--dry-run`（lark_mail_sync）/ 过滤参数 `--invoice`/`--tk`/`--limit`（gofreight_ap） | 只打印将做什么 / 缩小本轮范围 | 业务想先看一眼或分批录时 |

全局旗标 `--dry-run` 放在**子命令前面**（`--env-file .env --dry-run sync`）；
`gofreight_ap` 的过滤参数放在子命令后面（`plan --invoice 1001 --tk TK-20990011`）。

**不要执行**：`backfill`、`reset-table`、`watch`（常驻进程交给人工决定，AI 会话里不挂后台）。

## 六、输出解读

- `sync` 正常输出为每封新邮件的录入计划/结果行；`无待处理 confirm 邮件。`= 邮箱里没有新确认邮件，正常。
- 某些邮件会以「异常报错」列落表（**这是设计行为，不是故障**）：未识别 TK 行、正文与 PDF 均无金额、
  同名邮件无附件等。落表后由人工在多维表里补，详见 `references/录入口径.md`。
- 每行都会写「消息ID」列（系统幂等列，用户界面上通常已隐藏，别当成脏数据删掉——
  rebuild-state 靠它恢复游标）。
- `gofreight_ap plan` 输出 = 待录清单（发票号 | TK | 供应商→编号 | 金额币种 | 开票/到期日）
  + 跳过清单及原因（已建立应付 / 服务商无映射 / 表内异常未清 / 缺发票号TK / 摊分金额缺失 /
  开票日期缺失 / DUE 缺失）。**跳过是保护，不是故障**；「无映射」转管理员。
- `gofreight_ap submit --yes`：每行成功即记入防重账本；失败行自动截图
  `runtime-outputs/gofreight/FAIL_*.png` 并继续下一行，结束后汇总「成功 X/N」。
  出现「会计页已有该发票号…跳过防双录」= 别人已手工录过，正常跳过并记账。

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
| sync 总说「无待处理」但邮箱里明明有 | 6 个权限没开齐（常见漏了 subject/body 读取，返回空但不报错） | 按 `references/应用申请指引.md` 第 2 步补齐权限并重新发布 |
| pip 安装超时/失败 | 网络问题 | `setup.bat` 会自动换清华镜像重试；仍失败让用户检查网络/代理 |
| 输出中文乱码 | 控制台代码页 | 运行前 `chcp 65001`；双击 `sync.bat` 已内置 |
| 建立应付报 playwright 缺失 | 可选组件没装上 | `python_venv\Scripts\pip install -r requirements-gofreight.txt`（需本机有 Chrome/Edge） |
| 建立应付 FAIL_login 登录失败 | 密码错，或该账号在别的浏览器登录被互踢 | 核对 `.env` 三项；关掉别处的 GoFreight 登录再重跑失败的行 |
| plan 清单里「服务商无映射」 | 管理员还没配这家的 GoFreight 编号 | 转告用户找管理员拿 search/code 配进 `GOFREIGHT_VENDOR_MAP`；绝不猜编号提交 |
| 某行 FAIL_amt 金额对不上 | 表内摊分金额与 GoFreight 算出的不一致 | 该行转人工：保留截图（`runtime-outputs/gofreight/`），提醒用户核对表内数据，别反复重试 |
| 提交中提示「会话过期，重新登录」 | 正常（会话约 15 分钟过期，自动重登） | 无需处理；若重登也失败按 FAIL_login 排查 |
| 想看「这轮会录什么」 | — | `sync` 用 `--dry-run`；建立应付直接 `plan`（本来就只读） |

## 八、数据口径

录入哪些列、金额怎么摊分、发票字段从 PDF 怎么识别、防重规则——全部在
**`references/录入口径.md`**。回答业务任何「这列为什么是这个值」之前先读它；
口径本身不要凭感觉改，业务要改口径时让用户找管理员（口径改动需重发新版包）。
建立应付的口径（发布日期/费用代码/数量单价/币种/防重/供应商映射）——全部在
**`references/建立应付指引.md`**。
应用申请/凭证/权限问题——全部在 **`references/应用申请指引.md`**（含常见卡点速查）。

## 九、目录结构（仓库根目录 = skill 本体 = 工作目录）

```
<根>\                          ← 仓库克隆/下载到 .workbuddy\skills\lark-mail-sync
├── SKILL.md                   ← 本文件
├── references\应用申请指引.md   ← 业务自己申请企业自建应用（cli_ 凭证）的分步指引
├── references\录入口径.md      ← 数据口径与防重规则
├── references\建立应付指引.md   ← 建立应付口径、确认流程、防重与故障处理
├── lark_mail_sync\            ← Python 源码（邮件→多维表）
├── gofreight_ap\              ← Python 源码（多维表→GoFreight 建立应付）
├── tests\ tests_internal\     ← 离线自测（无需网络）
├── requirements.txt           ← 核心依赖
├── requirements-dev.txt       ← 核心依赖 + pytest（setup.bat 装这份）
├── requirements-gofreight.txt ← 建立应付可选组件（playwright，setup.bat 尽力装）
├── .env.example               ← 配置模板（凭证留空，等管理员另发）
├── setup.bat                  ← 一键装依赖 + 自测
├── config-env.bat             ← 生成 .env 并打开记事本
├── sync.bat                   ← 双击跑一轮增量同步（不想说话时的手动入口）
├── 使用指引.txt                ← 给人看的快速上手
├── README.md                  ← 仓库首页（安装/更新说明）
├── .env / data\ / python_venv\ / runtime-outputs\ ← 装机后本地生成（.gitignore 已排除，更新不覆盖）
└── .gitignore
```

> 注意：`data\state.json` 是邮件同步的本机去重游标，`data\gofreight_state.json` 是建立应付
> 的防重账本——**两个都不能删**（删了的补救分别是 `rebuild-state` / 找管理员核对已录清单）。
> 版本更新：`git pull` 后重跑 `setup.bat`（ZIP 装机则重下载整包，`.env` 先备份再贴回）。
