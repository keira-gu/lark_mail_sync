# 结算邮件自动录入工具（lark-mail-sync）

监听你的 Lark 邮箱，把 **confirm 结算邮件**自动录入多维表：
TK 号/数量解析 → 发票金额按数量摊分 → 发票 PDF 挂附件 → 从 PDF 识别服务商/币种/开票日期。
解析失败的邮件不丢弃，带「异常报错」落表等人工补。

开源公开仓库（MIT 协议）——**下载不需要 GitHub 账号**，凭证在自己手里，仓库里没有任何机密。

> 第一次用？先看 **`使用指引.txt`**——一页纯文本，六步上手。
> 还没有应用凭证？先看 **`references/应用申请指引.md`**——自己申请企业自建应用（cli_ 凭证），15-30 分钟。
> 给 AI 看的操作说明书在 **`SKILL.md`**（红线、命令、故障手册）。

## 安装（下载本仓库即安装）

本仓库**根目录就是 AI 助手（WorkBuddy）的 skill 本体**。把它放到固定位置即完成安装：

```bat
git clone <本仓库地址> "%USERPROFILE%\.workbuddy\skills\lark-mail-sync"
```

没有 git：浏览器打开本仓库 → 绿色 **Code** 按钮 → **Download ZIP** →
解压出的文件夹**改名为 `lark-mail-sync`** → 挪进 `C:\Users\<你的用户名>\.workbuddy\skills\`。

也可以直接对 AI 说：**「请按 README 把 lark-mail-sync 从 GitHub 装到 .workbuddy\skills 下」**。

## 装好之后

1. **申请自己的应用**（还没申请过的话）——按 `references/应用申请指引.md`：
   建应用 → 开 `mail:user_mailbox.message:readonly` + `bitable:app` 两个权限 →
   发布（管理员审批）→ 可用范围加自己 → 管理员把应用加为多维表文档应用
2. **双击 `setup.bat`**——建运行环境 + 离线自测（一次，约 1-3 分钟）
3. **双击 `config-env.bat`**——把自己应用的凭证和邮箱、目标表信息填进 `.env` 并保存
4. **首次**在对话里说：「请用【邮件同步 SKILL】做一次环境自检，然后恢复同步状态，再跑一轮同步」
   （自检 probe → 恢复游标 rebuild-state → 同步 sync）
5. **以后**只需说：「请用【邮件同步 SKILL】跑一轮同步」（或双击 `sync.bat`）；
   想整理收件箱再说一句「请用【邮件同步 SKILL】归档已处理邮件」（只动已入表的结算邮件）

## 更新版本

```bat
cd /d "%USERPROFILE%\.workbuddy\skills\lark-mail-sync"
git pull
setup.bat
```

`.env`、`data\`（去重游标）不会被更新覆盖。

## 红线（务必遵守）

- **绝不让 AI 执行 `backfill`（全量回填）或 `reset-table`（清空表格）**。
  前者在去重游标为空的机器上会把历史邮件整表重复录入，后者直接清空共享多维表。
- `data\state.json` 是本机去重游标，别删；误删后说「恢复同步状态」即可。
- 填好的 `.env` 等于钥匙：不截图、不转发、不提交。

## 出问题

让 AI 按 `SKILL.md` 第七节「故障手册」排查；数据口径疑问见 `references\录入口径.md`。
