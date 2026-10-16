# 数据格式速查（实测口径）

本文件记录导出器依赖的**磁盘事实**，以及每条事实的来源与置信度。写在这里是因为
这些结论决定了导出结果对不对；客户端升级后如果对不上，应该先改这里再改代码。

调查日期：**2026-10-16**。参考实现与文档见文末「证据来源」。

## 1. 数据目录

| 客户端 | 数据目录 | 置信度 |
|---|---|---|
| WorkBuddy | `~/.workbuddy` | 高（macOS 实测） |
| WorkBuddy AI | `~/.workbuddy-ai` | 高（macOS 实测） |
| CodeBuddy CLI（WorkBuddy 内置的同款 CLI） | `~/.codebuddy` | 高 |

* 客户端内置的 CLI 认 `WORKBUDDY_CONFIG_DIR` / `CODEBUDDY_CONFIG_DIR` 环境变量；
  本工具也认这两个变量（以及 `WORKBUDDY_HOME`），并用 `--home` 支持任意路径。
* Windows 侧的候选是推断值（`%APPDATA%\WorkBuddy` 等），未在真机验证；
  `--home` 是兜底手段。

目录布局：

```text
<home>/
  workbuddy.db                              # 会话索引（SQLite，WAL）
  workbuddy.db-wal  workbuddy.db-shm        # WAL 三件套
  projects/<cwd-slug>/
      <session-id>.jsonl                    # 对话正文（一行一条 JSON）
      <session-id>.meta.json                # 正文配套元信息
      <session-id>.file-rollback.ndjson     # 文件回滚记录
      <session-id>/tool-results/            # 工具结果落盘
      <session-id>/subagents/agent-*.jsonl  # 子 Agent 转录（同格式）
  projects/<session-id>.jsonl               # 也见过直接躺在 projects/ 下的正文
  tasks/<session-id>/<n>.json               # 任务清单
  changes-detail/<session-id>/              # 改动详情
  changes-index/<session-id>/               # 改动索引
  file-history/<session-id>/                # 文件历史快照
  artifact-index/<session-id>.json          # 产物索引
  blobs/**                                  # 内容寻址的大对象（附件）
  memory/<uid>_memory.md                    # 长期记忆（本工具不导出）
  skills/**  plugins/**                     # 技能与插件（本工具不导出）
  connectors/<uid>/.credentials.v3.json     # 加密凭据（本工具**从不读取**）
  logs/  traces/<workerPid>/  app/          # 日志 / 遥测 / 应用缓存（不导出）
  storage/skeleton/account-snapshot.json    # 当前登录账号（primary.uid / nickname）
  settings.json                             # 含 claw.users.<uid> 渠道绑定
```

## 2. `workbuddy.db`

### 2.1 `sessions` 表

第三方实现记录（Danjack85/workbuddy-switch）：客户端 **5.6.x 把表从 30 列扩到 40 列**。

30 列版本：

```text
id, cwd, user_id, title, custom_title, status,
created_at, updated_at, last_activity_at, deleted_at,
is_playground, source_mode, is_background_automation, mode,
model, expert_id, expert_locale, expert_runtime_identity,
expert_marketplace, permission_mode, use_sandbox_cli,
project_id, plugin_context_json, addon_selection,
session_settings, last_user_prompt_expert_selection,
context_window, buddy_snapshot_id, buddy_binding_json,
thought_level
```

5.6.x 新增的 10 列：

```text
transport, conversation_origin, visibility, group_id, group_title,
agent_dirty, agent_dirty_at, agent_last_synced, verified_at, unread
```

语义（本工具用到的）：

| 列 | 含义 | 用法 |
|---|---|---|
| `id` | 会话 id | 等于 `projects/<slug>/<id>.jsonl` 的文件名主干 |
| `cwd` | 工作目录 | 对齐 `workspaces.path`，决定落在哪个空间 |
| `user_id` | 账号 id | 客户端**只显示当前账号的会话**；`--probe` 只统计不回显 |
| `title` / `custom_title` | 标题 / 用户改过的标题 | 文件标题（`custom_title` 优先） |
| `status` | `completed` / `active` / `running` / **`archived`** | 归档判定依据之一 |
| `deleted_at` | 软删除时间；**`-1` 表示未删除**（云侧行） | 默认跳过「有值且非 `-1`」的行 |
| `is_playground` | 无项目会话 | → 侧边栏「任务」板块 → `任务/` |
| `group_id` / `group_title` | 空间内的文件夹/分组 | 空间下的子目录 |
| `project_id` | 指向 `workspaces` 的候选列 | 空间对齐（按 id） |
| `last_activity_at` / `updated_at` / `created_at` | 时间（epoch 毫秒） | 排序与文件名日期 |

### 2.2 其他表

| 表 | 用途 | 本工具 |
|---|---|---|
| `workspaces` | 空间：`path` + `last_opened_at`（+ 可能的 `name`/`id`） | 分类来源 |
| `session_usage` | 用量：`session_id` / `used` / `size` / `updated_at` / `credit_json` | 不导出（会话 `transcript` 里有 token 统计） |
| `automations` / `automation_runs` / `automation_runtime_state` | 自动化 | 不导出 |
| `buddy_snapshots` | 快照 | 不导出 |

表名与列名可能与上表不同（版本演进 / 厂商改名），所以代码走的是
**PRAGMA 发现 + 名字模式匹配 + 全列原样保留**，`--probe` 可以打出实际读到的结构。

### 2.3 只读打开的姿势

1. `sqlite3.connect("file:<path>?mode=ro", uri=True)` + `PRAGMA query_only=ON`；
2. 打不开（客户端持锁 / 文件损坏）→ 把 `db` + `-wal` + `-shm` 拷到临时目录再读
   （**只拷主库会丢掉未 checkpoint 的写入**，这一点被上游实现明确警告过）；
3. 仍然读不出内容 → 抛出受控错误，导出器降级为「只读磁盘正文」模式。

## 3. 对话正文（`<id>.jsonl`）

一行一条 JSON。本工具已见过的类型与字段：

```jsonc
// 用户消息：真实输入通常包在信封里
{"type":"message","role":"user",
 "content":[{"type":"input_text","text":"<system-reminder>…</system-reminder><user_query>真实输入</user_query>"}],
 "sessionId":"…","cwd":"…","timestamp":1780000000000}

// 助手消息：status 为 completed 时该轮结束；rawUsage 里是 token 统计
{"type":"message","role":"assistant","status":"completed",
 "content":[{"type":"output_text","text":"…"}],
 "providerData":{"messageId":"msg-1","model":"auto",
                 "rawUsage":{"prompt_tokens":1200,"completion_tokens":300,
                             "prompt_cache_hit_tokens":800}}}

// 思考：content 常常是空数组，文本在 rawContent 里
{"type":"reasoning","content":[],"rawContent":[{"type":"text","text":"…"}]}

// 工具调用与结果：按 callId 配对
{"type":"function_call","callId":"call-1","name":"Glob","arguments":"{\"pattern\":\"*.md\"}"}
{"type":"function_call_result","callId":"call-1","status":"completed",
 "output":[{"type":"text","text":"…"}]}

// 标题 / 摘要、上下文压缩边界、元信息
{"type":"summary","summary":"整理周报"}
{"type":"ai-title","title":"整理周报"}
{"type":"system","subtype":"compact_boundary"}
{"type":"turn-metrics","durationMs":1234}
{"type":"file-history-snapshot", …}          // 客户端版本的元信息类型
{"type":"session-meta", …}
{"type":"goal-progress"|"goal-result"|"agent_started"|"agent_finished"|"run_started"|"run_finished"}
```

已记录的具体规则：

* **用户输入信封**：约 97% 的 user 记录把真实输入包在 `<system-reminder>` 里，
  真实输入在 `<user_query>…</user_query>`；无包裹时（子 Agent 转录）回退全文。
* **思考**：`reasoning` 记录的 `content` 常为空数组，文本在 `rawContent[].text`；
  助手记录的思考块也用 `type: thinking|reasoning`。
* **附件**：工具结果里的 `{blob_path, mime, size}` 指向 `<home>/blobs/**`（内容寻址）。
* **子 Agent**：主转录里 `function_call name="Agent"` 的结果末尾带
  `[Agent ID: agent-xxxx]`，对应 `<session-id>/subagents/agent-xxxx.jsonl`（同格式）。
* **中断**：`status: "incomplete"` 表示该轮被中止；`status != "completed"` 的助手记录
  常是流式中间态，token 统计只认 `completed`。
* **半截行**：客户端正在追加时最后一行可能不是完整 JSON —— 只丢那一行，不让整个文件失败。
* **兼容路径**：同源 CLI 也写过 Claude Code 形状（`type=user/assistant` +
  `message.content` 里的 `text` / `thinking` / `tool_use` / `tool_result` 块），
  两套都解析。

## 4. `projects/` 桶名（cwd → slug）

WorkBuddy 的 slug **不是** Claude Code 的规则。已记录的口径：

* 把路径分隔符与冒号换成连字符 → `-`；
* 去掉首尾连字符、把连续连字符合并；
* 绝对路径前导 `/` 被吃掉后结果相同（`/Users/me/proj` → `Users-me-proj`）。

因为不同版本/不同实现给出的规则略有出入（有的实现按 `[:/\\]+` 折叠，有的实现
按 `/ \ : -` 切分再合并），本工具**只把 slug 当快速命中提示**：真正的定位是扫描
`projects/*/` 里所有 `<id>.jsonl`（代价很小，且不会因为规则变动而找不到正文）。

## 5. 证据来源

| 来源 | 提供了什么 |
|---|---|
| [`Guyungy/wb-account-sync`](https://github.com/Guyungy/wb-account-sync) | 两个数据目录的实测路径；`projects/<slug>/<id>.jsonl` 及其 5 类配套文件；`workbuddy.db` 的 `sessions` / `session_usage` / `workspaces` / `automations`；「只 INSERT 缺失行、凭据不搬」的安全模型 |
| [WorkBuddy 官方文档：任务管理](https://www.workbuddy.cn/docs/workbuddy/Task-Management) | 侧边栏「任务 / 空间」两个板块、空间内的文件夹、`已归档` 状态与查看入口 |
| [WorkBuddy 更新日志](https://www.codebuddy.cn/docs/workbuddy/Changelog) | 4.24.2 起有「置顶/归档/筛选」交互；5.4.1 提到「未读状态、会话分类」 |
| `Danjack85/workbuddy-switch` | 5.6.x 的 30 → 40 列 schema 与完整列名；`user_id` 隔离导致「换号后旧会话不可见」 |
| `Tencent/teamai-cli` | 会话列表与空间分组来自 `sessions` / `workspaces` 表；`user_id` 的发现顺序（会话行 → `connectors/<uuid>` → `app/sessions.json`）；`BEGIN IMMEDIATE` + `busy_timeout` 的理由 |
| `kipkcl2016/remote-agent` | WorkBuddy 的 SQL（`deleted_at IS NULL OR deleted_at = -1`、`status != 'archived'`）、正文读取方式与 slug 规则 |
| `junhoyeo/tokscale` | `session_usage` 的列；正文记录的 `type/role/status/providerData/message.usage` 字段与 token 键名 |
| `Einsia/agent-git`（机制勘察文档 + adapter） | WorkBuddy AI 5.5.2 实测目录；`WORKBUDDY_CONFIG_DIR` 包装脚本；`function_call` / `function_call_result` / `compact_boundary` / `isCompactSummary` 的处理 |
| `tabtin-ai/TabTin` | `<user_query>` 提取（97% 的包裹率）、`rawContent[].text`、`blob_path` 附件、`[Agent ID: …]` 子 Agent 关联、WAL 快照读法 |
| `edison7009/EchoBird` | `user_id` 的 NOT NULL 语义、`VACUUM INTO` 备份、WAL 一致性 |
| `vshulcz/deja-vu`（issue #4681）、`yangxy112683/line-of-sight`（issue #11） | CodeBuddy CLI 的记录形状（OpenAI Responses 风格 item）、`ai-title` / 元信息 / 可丢弃类型的清单 |

**不要**把上面任何一方的代码当权威：它们的落点各不相同（迁移、用量统计、导入、
遥测），版本也不一样。本工具的做法是「口径取交集 + 全列原样保留 + 未识别留档」，
让版本差异最多影响分类与展示，不影响数据完整性。
