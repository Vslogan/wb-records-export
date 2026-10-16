# wb-records-export

把 **WorkBuddy**（及同源的 **WorkBuddy AI** / **CodeBuddy CLI**）的全部对话历史导出成
**结构化 markdown 文档**：每个会话一个文件，按客户端里的原有分类（空间 / 空间内的文件夹 /
任务 / 已归档）放进对应文件夹，另附会话索引、可读的总览与机器可读的清单。

- **只读**：不写客户端数据库、不改 `projects/`、不碰凭据；因此**不需要退出客户端**；
- **只依赖标准库**：Python 3.10+，无第三方依赖；
- **不静默丢数据**：认不出的记录类型、超长工具输出、没配到结果的调用都会写明并留档。

> 非官方工具，未获 WorkBuddy 或其厂商背书。参考项目：
> [`Guyungy/wb-account-sync`](https://github.com/Guyungy/wb-account-sync)（账号切换与跨 App 打通）。
> 那个工具的落点是**写**（迁移会话归属）；本工具的落点是**读**（把历史留成能长期保存的文档）。

---

## 30 秒上手

```bash
git clone https://github.com/Vslogan/wb-records-export.git
cd wb-records-export

python3 export-records.py --selftest           # 先用合成数据自检，不碰你的数据
python3 export-records.py --probe              # 看看数据目录与 sessions 表的列
python3 export-records.py --out ./workbuddy-export
```

导出结果：

```text
workbuddy-export/
├── README.md                      # 本次导出的总览：统计、目录结构说明、数据缺口
├── index.md                       # 全部会话的索引表（可点击跳转）
├── export-manifest.json           # 机器可读清单（含 schema 与每条会话的元数据）
├── 空间/
│   └── 房子/
│       ├── 每周例事/2026-07-01_整理周报_ab12cd34.md
│       └── 已归档/2026-06-02_去年复盘_9f8e7d6c.md
├── 任务/2026-07-03_临时问题_11aa22bb.md
├── 未收录/2026-07-04_CLI 直接开的会话_33cc44dd.md
├── 已删除/…                        # 仅在 --include-deleted 时出现
└── _attachments/<会话 id>/…        # 仅在 --copy-attachments 时出现
```

同时装了 WorkBuddy 与 WorkBuddy AI 时，两边都会导出，并在顶层分成 `wb/` 与 `wb_ai/`：

```bash
python3 export-records.py --out ./export-all            # 默认两个客户端都扫
python3 export-records.py --client wb_ai --out ./export-ai
```

---

## 分类从哪来（这是本工具的核心）

WorkBuddy 的左侧边栏就是它自己的分类体系，[官方文档](https://www.workbuddy.cn/docs/workbuddy/Task-Management)
写得很直白：列表分「**任务**」与「**空间**」两个板块，空间里还有**文件夹**（分组），
任务可以**归档**（归档后去「系统设置 - 数据管理」里查看）。这些都能在本地数据里找到对应：

| 客户端里的分类 | 本地数据里的对应物 | 导出目录 |
|---|---|---|
| 空间 | `workbuddy.db` → `workspaces` 表（按 `cwd` 对齐） | `空间/<空间名>/` |
| 空间内的文件夹/分组 | `sessions.group_id` / `sessions.group_title` | 空间下的子目录 |
| 任务（不属于空间） | `is_playground=1`，或 `cwd` 落在 `~/WorkBuddy/_temp`、时间戳目录里 | `任务/` |
| 已归档 | `sessions.status = 'archived'` | 空间下的 `已归档/`（或用 `--archive-layout top` 提到顶层） |
| 磁盘有正文、库里没行 | `projects/**/<id>.jsonl` 存在但 `sessions` 没有对应行 | `未收录/` |
| 已删除 | `deleted_at` 有值且不是 `-1` | `已删除/`（默认跳过，见下） |

`workspaces` 行缺失时用 `basename(cwd)` 兜底 —— 客户端也是按工作目录分组的，
所以这条兜底不会把会话放错板块。

**关于 `deleted_at = -1`**：WorkBuddy 对部分云同步的行用 `-1` 表示「未删除」，
所以判定是「有值且不是 `-1` 才算删除」。这条规则在第三方实现里被明确写进注释，
不加的话会把一批正常会话错判成已删除。

---

## 每个文件里有什么

````markdown
---
title: 整理周报
session_id: s-normal
client: WorkBuddy
client_home: /Users/me/.workbuddy
category: 空间/房子/每周例事
space: 房子
group: 每周例事
archived: false
pinned: false
created_at: "2026-07-01T09:00:00+08:00"
updated_at: "2026-07-02T18:20:11+08:00"
cwd: /Users/me/cc/房子
model: auto
records: 128
user_messages: 41
tool_calls: 63
tokens_total: 1203456
transcript: projects/Users-me-cc-房子/s-normal.jsonl
db_transport: local        # sessions 表里其余列原样保留（前缀 db_）
db_visibility: private
...

# 整理周报

**WorkBuddy** · 分类：`空间/房子/每周例事` · 2026-07-01 09:00:00 → 2026-07-02 18:20:11 · …
> 工作目录：`/Users/me/cc/房子`
> 正文：`projects/…/s-normal.jsonl`（1.2MB）

## 对话

### 👤 用户 · 2026-07-01 09:00:01

把上周的周报汇总成一份 markdown

<details>
<summary>💭 思考 · 2026-07-01 09:00:02</summary>
…
</details>

<details>
<summary>🔧 工具调用 `Glob` · 2026-07-01 09:00:03</summary>

**参数**

```json
{"pattern": "周报/*.md"}
```

**结果**

```text
周报/第 38 周.md
```

</details>

### 🤖 助手 · 2026-07-01 09:00:05

找到 1 份周报，正在合并。

## 附录（元信息与未展开内容）

### 任务清单（`tasks/<id>/*.json`）
### 产物索引（`artifact-index/<id>.json`）
### 子 Agent 转录 → `…/subagents/agent-xxxx.md`
<details><summary>📎 元信息原文（`<id>.meta.json`）</summary>…</details>
<details><summary>未识别记录 · 第 41 行 · type=…</summary>…</details>
````

几个刻意的取舍：

* **用户输入会剥掉信封。** 实测约 97% 的 user 记录把真实输入包在 `<system-reminder>` 里，
  真正的输入在 `<user_query>…</user_query>`；默认只显示真实输入，
  加 `--with-reminder-context` 可以把原始信封还原到附录里。
* **思考和工具输出折叠而不删除**，长输出按 `--max-tool-output` 截断并写明原长。
* **未识别的记录类型进附录**，原文 JSON 一字不改（客户端升版本出现新类型时，
  你仍然拿得到数据）。
* **工具调用与结果按 `callId` 配对**，孤立的调用/结果都会标出来。
* **附件默认只留路径**，`--copy-attachments` 才把 `blobs/` 里的文件复制进 `_attachments/`。

---

## 常用参数

| 参数 | 作用 |
|---|---|
| `--out DIR` | 导出目录（默认 `./workbuddy-export`） |
| `--home PATH` | 显式指定数据目录（可重复）；不猜候选路径 |
| `--client {wb,wb_ai,codebuddy}` | 只导出指定客户端（可重复） |
| `--include-codebuddy` | 连 `~/.codebuddy`（同款 CLI）一起导出 |
| `--include-deleted` | 连软删除的会话一起导出（默认跳过，放进 `已删除/`） |
| `--no-archived` | 跳过已归档会话 |
| `--layout flat` | 全部平铺到一个目录，分类只写在文件头与索引里 |
| `--archive-layout top` | 归档会话统一放到顶层 `已归档/` |
| `--ascii-paths` | 分类目录用英文（`spaces/tasks/archived/unlisted/deleted`） |
| `--no-group-folders` | 不为「空间内的文件夹」建子目录（分类串里仍保留） |
| `--copy-attachments` | 复制正文引用的附件到 `_attachments/` |
| `--no-thinking` / `--no-tools` / `--no-tool-arguments` | 精简正文 |
| `--max-tool-output N` | 单条工具结果的字符上限（0 = 不截断） |
| `--limit N` | 每个客户端最多导出多少条（调试用） |
| `--dry-run` | 只算不写，连输出目录都不创建 |
| `--clean --yes-i-mean-it` | 导出前清空输出目录（要显式二次确认） |
| `--json` | 摘要以 JSON 打到 stdout（进度信息走 stderr） |
| `--probe` | 只打印数据目录与 `sessions` 表结构（不含任何正文） |
| `--selftest` | 用临时目录里的合成数据跑一遍完整导出并自检 |

### 它读什么、不读什么

读：`<home>/workbuddy.db`（只读打开，必要时先做 db+wal+shm 快照）、
`<home>/projects/**`（正文与 `tool-results/`、`subagents/`）、`<home>/tasks/`、
`<home>/artifact-index/`，以及正文里引用的 `<home>/blobs/**`（仅在 `--copy-attachments` 时）。

不读：`connectors/**`（加密凭据）、`logs/`、`traces/`（按进程分桶的遥测）、
`app/`（应用缓存）。自检里有一条**安全断言**：即使数据目录里存在
`connectors/<uid>/.master.key`，导出结果里也不能出现它的内容。

---

## 与 WorkBuddy 的兼容性

`sessions` 表的列在不同客户端版本间变过（第三方实现记录：5.6.x 从 30 列扩到 40 列，
新增 `transport / conversation_origin / visibility / group_id / group_title / … / unread`）。
本工具**不写死列名**：

* 先读 `PRAGMA table_info`，再按名字模式识别「归档 / 删除 / 置顶 / 分组 / 空间」；
* 其余列一律以 `db_` 前缀原样写进文件头 —— 新版本多出来的列不会丢；
* 数据库读不动（客户端持锁、文件损坏、根本不是 SQLite）时自动降级为**只读磁盘正文**
  模式，并给出可读告警；
* `--probe` 会把实际读到的表与列打出来，方便对齐版本差异。

正文格式同理：主路径按 WorkBuddy 的 item 流解析
（`message` / `reasoning` / `function_call` / `function_call_result` / `summary` / …），
同时保留 Claude Code 形状（`type=user/assistant` + `message.content` 块）的兼容路径。

---

## 测试

```bash
python3 -m unittest discover -s tests -v      # 118 项
python3 export-records.py --selftest          # 端到端自检（合成数据，25 项断言）
```

测试与自检全部跑在**临时目录里的合成数据**上，不会读你的真实数据；夹具刻意照实测
schema 造（40 列 `sessions`、`workspaces`、子 Agent 转录、半截 JSON 行、软删除行、
`deleted_at = -1` 的云侧行、`connectors/` 里的假凭据……），覆盖分类映射、归档放法、
重名去重、路径安全、凭据不外泄等分支。

---

## 已知边界

* **导出的是本地数据。** 客户端未落盘的内容（例如只在云端的历史）读不到；
  屏幕之外的 UI 状态（未读标记的云端同步状态）也一样。
* **管理后台的「已归档」视图可能含云端记录。** 本工具按本地 `status` 判定归档，
  与客户端界面偶尔可能有出入，但会话文件里写了判定依据（`archived_reason`）。
* **附件按引用路径复制**，`blobs/` 被清理过的历史附件无法找回。
* **软删除的会话默认不导出**（它们在客户端里也不显示），需要时加 `--include-deleted`。
* `workspaces` 表不存在或为空时，空间名会退化成 `cwd` 的目录名。

## 免责声明

本项目仅用于导出**你自己有权访问的本机数据**。请遵守 WorkBuddy 的服务条款与当地法律。
作者不对因使用本工具造成的任何损失负责。
