#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自检：用**合成数据**跑一遍完整导出，验证行为而不是验证你的隐私。

合成夹具刻意照着实测 schema 造（40 列 ``sessions``、``workspaces`` 表，
以及 WorkBuddy 的记录类型），覆盖这些分支：

* 空间内的普通会话、空间内带「分组」的会话；
* 已归档会话（``status='archived'``）；
* 侧边栏「任务」板块的 playground 会话（``is_playground=1``）；
* 有正文但数据库没有行的会话（「未收录」）；
* 软删除会话（``deleted_at`` 有值，默认跳过）；
* 工具调用 + 结果、思考、子 Agent 转录、附件引用、未知记录类型；
* 数据库里有行但没有正文的会话。

跑法：``python3 export-records.py --selftest``
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile

from .exporter import ExportOptions, run_export
from .util import now_ms

#: 客户端 5.6.x 的 sessions 表：30 列 + 后来新增的 10 列。
SESSION_COLUMNS: tuple[str, ...] = (
    "id", "cwd", "user_id", "title", "custom_title", "status",
    "created_at", "updated_at", "last_activity_at", "deleted_at",
    "is_playground", "source_mode", "is_background_automation", "mode",
    "model", "expert_id", "expert_locale", "expert_runtime_identity",
    "expert_marketplace", "permission_mode", "use_sandbox_cli",
    "project_id", "plugin_context_json", "addon_selection",
    "session_settings", "last_user_prompt_expert_selection",
    "context_window", "buddy_snapshot_id", "buddy_binding_json",
    "thought_level",
    "transport", "conversation_origin", "visibility", "group_id",
    "group_title", "agent_dirty", "agent_dirty_at", "agent_last_synced",
    "verified_at", "unread",
)

UID = "0f1e2d3c-aaaa-4bbb-8ccc-000000000001"
DAY = 86_400_000


def _session_row(session_id: str, cwd: str, title: str, **over) -> dict:
    row = {name: None for name in SESSION_COLUMNS}
    row.update({
        "id": session_id,
        "cwd": cwd,
        "user_id": UID,
        "title": title,
        "custom_title": "",
        "status": "completed",
        "created_at": now_ms() - 10 * DAY,
        "updated_at": now_ms() - 9 * DAY,
        "last_activity_at": now_ms() - 9 * DAY,
        "deleted_at": None,
        "is_playground": 0,
        "source_mode": None,
        "is_background_automation": 0,
        "mode": "code",
        "model": "auto",
        "permission_mode": "default",
        "use_sandbox_cli": 0,
        "context_window": 200000,
        "transport": "local",
        "conversation_origin": "desktop",
        "visibility": "private",
        "group_id": None,
        "group_title": None,
        "agent_dirty": 0,
        "unread": 0,
    })
    row.update(over)
    return row


def _jsonl(*records: dict) -> str:
    return "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n"


def _message(role: str, text: str, *, session_id: str, cwd: str, ms: int,
             status: str = "completed", **extra) -> dict:
    record = {
        "type": "message",
        "role": role,
        "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}],
        "sessionId": session_id,
        "cwd": cwd,
        "timestamp": ms,
        "status": status,
        "providerData": {"messageId": f"msg-{ms}", "model": "auto"},
    }
    record.update(extra)
    return record


def _usage_record(ms: int, session_id: str) -> dict:
    return {
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": "阶段完成。"}],
        "sessionId": session_id,
        "cwd": "/tmp",
        "timestamp": ms,
        "providerData": {
            "messageId": f"msg-usage-{ms}",
            "model": "auto",
            "rawUsage": {"prompt_tokens": 1200, "completion_tokens": 300,
                         "prompt_cache_hit_tokens": 800},
        },
    }


def build_fake_home(root: str, *, key: str = "wb") -> str:
    """在 ``root`` 下造一个客户端数据目录，返回路径。"""
    home = os.path.join(root, key)
    projects = os.path.join(home, "projects")
    os.makedirs(projects, exist_ok=True)

    space_a = os.path.join(root, "workspace", "房子")
    space_b = os.path.join(root, "workspace", "LensCheck")
    os.makedirs(space_a, exist_ok=True)
    os.makedirs(space_b, exist_ok=True)

    # ---- 数据库 ----------------------------------------------------------
    db_path = os.path.join(home, "workbuddy.db")
    conn = sqlite3.connect(db_path)
    columns_sql = ", ".join(f'"{name}"' for name in SESSION_COLUMNS)
    conn.execute(f"CREATE TABLE sessions ({columns_sql}, PRIMARY KEY (id))")
    conn.execute(
        "CREATE TABLE workspaces (id TEXT PRIMARY KEY, path TEXT, name TEXT, last_opened_at INTEGER)"
    )
    conn.execute(
        "CREATE TABLE session_usage (session_id TEXT PRIMARY KEY, used INTEGER, size INTEGER, "
        "updated_at INTEGER, credit_json TEXT)"
    )
    conn.execute("CREATE TABLE automations (id TEXT, title TEXT)")

    rows = [
        _session_row("s-normal", space_a, "整理周报",
                     custom_title="整理周报",
                     group_id="g-1", group_title="每周例事",
                     last_activity_at=now_ms() - 2 * DAY),
        _session_row("s-archived", space_a, "去年复盘", status="archived",
                     last_activity_at=now_ms() - 40 * DAY),
        _session_row("s-playground",
                     os.path.join(root, "WorkBuddy", "2026-09-16-09-08-41"),
                     "临时问题", is_playground=1, last_activity_at=now_ms() - 3 * DAY),
        _session_row("s-deleted", space_b, "删掉的会话", deleted_at=now_ms() - DAY),
        _session_row("s-nobody", space_b, "只有索引没有正文", status="active"),
        _session_row("s-cloud", space_b, "云端未删除标记", deleted_at=-1, status="running",
                     last_activity_at=now_ms() - 5 * DAY),
    ]
    placeholders = ", ".join("?" for _ in SESSION_COLUMNS)
    conn.executemany(
        f"INSERT INTO sessions ({columns_sql}) VALUES ({placeholders})",
        [tuple(row[name] for name in SESSION_COLUMNS) for row in rows],
    )
    conn.execute("INSERT INTO workspaces VALUES (?,?,?,?)",
                 ("ws-1", space_a, "房子", now_ms() - 6 * DAY))
    conn.execute("INSERT INTO workspaces VALUES (?,?,?,?)",
                 ("ws-2", space_b, "LensCheck", now_ms() - 5 * DAY))
    conn.execute("INSERT INTO session_usage VALUES (?,?,?,?,?)",
                 ("s-normal", 4321, 102400, now_ms() - 2 * DAY, "{}"))
    conn.commit()
    conn.close()

    # ---- 正文 ------------------------------------------------------------
    def write(slug: str, session_id: str, text: str) -> str:
        bucket = os.path.join(projects, slug)
        os.makedirs(bucket, exist_ok=True)
        path = os.path.join(bucket, f"{session_id}.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    slug_a = space_a.replace("/", "-").lstrip("-")
    slug_b = space_b.replace("/", "-").lstrip("-")
    slug_pg = os.path.join(root, "WorkBuddy", "2026-09-16-09-08-41").replace("/", "-").lstrip("-")

    normal = _jsonl(
        _message("user",
                 "<system-reminder>\n<user_info>name: 张三</user_info>\n"
                 "历史提示词索引见附录\n</system-reminder>\n"
                 "<user_query>把上周的周报汇总成一份 markdown</user_query>",
                 session_id="s-normal", cwd=space_a, ms=now_ms() - 9 * DAY),
        {"type": "reasoning",
         "content": [],
         "rawContent": [{"type": "text", "text": "先找到周报文件，再合并。"}],
         "sessionId": "s-normal", "cwd": space_a, "timestamp": now_ms() - 9 * DAY + 1000},
        {"type": "function_call", "callId": "call-1", "name": "Glob",
         "arguments": json.dumps({"pattern": "周报/*.md"}, ensure_ascii=False),
         "sessionId": "s-normal", "cwd": space_a, "timestamp": now_ms() - 9 * DAY + 2000},
        {"type": "function_call_result", "callId": "call-1",
         "output": [{"type": "text", "text": "周报/第 38 周.md"}], "status": "completed",
         "sessionId": "s-normal", "cwd": space_a, "timestamp": now_ms() - 9 * DAY + 3000},
        _message("assistant", "找到 1 份周报，正在合并。", session_id="s-normal",
                 cwd=space_a, ms=now_ms() - 9 * DAY + 4000),
        {"type": "summary", "summary": "整理周报",
         "sessionId": "s-normal", "cwd": space_a, "timestamp": now_ms() - 9 * DAY + 5000},
        # 引用了 blobs/ 下的图片附件
        {"type": "function_call", "callId": "call-2", "name": "Read",
         "arguments": json.dumps({"path": "blobs/ab/cdef.png"}),
         "sessionId": "s-normal", "cwd": space_a, "timestamp": now_ms() - 9 * DAY + 6000},
        {"type": "function_call_result", "callId": "call-2",
         "output": {"blob_path": "blobs/ab/cdef.png", "mime": "image/png", "size": 12},
         "status": "completed", "sessionId": "s-normal", "cwd": space_a,
         "timestamp": now_ms() - 9 * DAY + 7000},
        # 子 Agent：结果里带 [Agent ID: agent-xxxx]
        {"type": "function_call", "callId": "call-3", "name": "Agent",
         "arguments": json.dumps({"prompt": "读一下附带的历史报告"}, ensure_ascii=False),
         "sessionId": "s-normal", "cwd": space_a, "timestamp": now_ms() - 9 * DAY + 8000},
        {"type": "function_call_result", "callId": "call-3",
         "output": [{"type": "text",
                     "text": "子任务完成。\n[Agent ID: agent-abcd1234]"}],
         "status": "completed", "sessionId": "s-normal", "cwd": space_a,
         "timestamp": now_ms() - 9 * DAY + 9000},
        # 未知类型 + 元信息类型
        {"type": "brand-new-record-type", "sessionId": "s-normal", "cwd": space_a,
         "timestamp": now_ms() - 9 * DAY + 9500, "payload": {"keep": "me"}},
        {"type": "turn-metrics", "sessionId": "s-normal", "cwd": space_a,
         "timestamp": now_ms() - 9 * DAY + 9600, "durationMs": 1234},
        _usage_record(now_ms() - 9 * DAY + 9700, "s-normal"),
        _message("user", "顺便把附件里的图也放进周报", session_id="s-normal",
                 cwd=space_a, ms=now_ms() - 2 * DAY),
        _message("assistant", "好的，已插入。", session_id="s-normal",
                 cwd=space_a, ms=now_ms() - 2 * DAY + 1000),
    )
    write(slug_a, "s-normal", normal)

    archived = _jsonl(
        _message("user", "<user_query>复盘一下去年的项目</user_query>",
                 session_id="s-archived", cwd=space_a, ms=now_ms() - 40 * DAY),
        _message("assistant", "复盘要点如下 ……", session_id="s-archived",
                 cwd=space_a, ms=now_ms() - 40 * DAY + 1000),
    )
    write(slug_a, "s-archived", archived)

    playground = _jsonl(
        _message("user", "帮我写一封请假邮件", session_id="s-playground",
                 cwd=os.path.join(root, "WorkBuddy", "2026-09-16-09-08-41"),
                 ms=now_ms() - 3 * DAY),
        {"type": "message", "role": "user",
         "content": [{"type": "input_text", "text": "<system-reminder>自动注入的上下文</system-reminder>"}],
         "sessionId": "s-playground", "cwd": "/tmp",
         "timestamp": now_ms() - 3 * DAY + 1000, "status": "completed"},
        _message("assistant", "这是草稿：……", session_id="s-playground",
                 cwd="/tmp", ms=now_ms() - 3 * DAY + 2000),
    )
    write(slug_pg, "s-playground", playground)

    deleted = _jsonl(
        _message("user", "这条会话被软删除了", session_id="s-deleted", cwd=space_b,
                 ms=now_ms() - DAY),
    )
    write(slug_b, "s-deleted", deleted)

    cloud = _jsonl(
        _message("user", "云端同步过来的会话", session_id="s-cloud", cwd=space_b,
                 ms=now_ms() - 5 * DAY),
        # 半截行：模拟客户端正在写入
        '{"type": "message", "role": "assistant", "content": [{"type": "outp',
    )
    write(slug_b, "s-cloud", cloud)

    # 有正文、数据库没有行（例如 CLI 直接写的）
    orphan = _jsonl(
        _message("user", "CLI 里直接开的会话", session_id="s-orphan", cwd=space_b,
                 ms=now_ms() - 4 * DAY),
        _message("assistant", "收到。", session_id="s-orphan", cwd=space_b,
                 ms=now_ms() - 4 * DAY + 1000),
    )
    write(slug_b, "s-orphan", orphan)

    # ---- 配套资产 --------------------------------------------------------
    bucket_a = os.path.join(projects, slug_a)
    with open(os.path.join(bucket_a, "s-normal.meta.json"), "w", encoding="utf-8") as fh:
        json.dump({"sessionId": "s-normal", "title": "整理周报", "lastPrompt": "顺便把附件里的图也放进周报"},
                  fh, ensure_ascii=False)
    sub_dir = os.path.join(bucket_a, "s-normal", "subagents")
    os.makedirs(sub_dir, exist_ok=True)
    with open(os.path.join(sub_dir, "agent-abcd1234.jsonl"), "w", encoding="utf-8") as fh:
        fh.write(_jsonl(
            _message("user", "读一下附带的历史报告", session_id="agent-abcd1234",
                     cwd=space_a, ms=now_ms() - 9 * DAY + 8500),
            _message("assistant", "报告里提到三件事。", session_id="agent-abcd1234",
                     cwd=space_a, ms=now_ms() - 9 * DAY + 8600),
        ))
    os.makedirs(os.path.join(bucket_a, "s-normal", "tool-results"), exist_ok=True)
    with open(os.path.join(bucket_a, "s-normal", "tool-results", "call-1.txt"), "w",
              encoding="utf-8") as fh:
        fh.write("周报/第 38 周.md\n")
    tasks_dir = os.path.join(home, "tasks", "s-normal")
    os.makedirs(tasks_dir, exist_ok=True)
    with open(os.path.join(tasks_dir, "0.json"), "w", encoding="utf-8") as fh:
        json.dump([{"content": "收集周报", "status": "completed"},
                   {"content": "合并成一份", "status": "in_progress"}], fh, ensure_ascii=False)
    artifacts = os.path.join(home, "artifact-index")
    os.makedirs(artifacts, exist_ok=True)
    with open(os.path.join(artifacts, "s-normal.json"), "w", encoding="utf-8") as fh:
        json.dump([{"name": "周报汇总.md", "type": "markdown", "size": 2048}], fh, ensure_ascii=False)
    blobs = os.path.join(home, "blobs", "ab")
    os.makedirs(blobs, exist_ok=True)
    with open(os.path.join(blobs, "cdef.png"), "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n" + b"0" * 4)

    # 凭据兜底：即使有人手滑写了 connectors/，导出也不该碰它。
    connectors = os.path.join(home, "connectors", UID)
    os.makedirs(connectors, exist_ok=True)
    with open(os.path.join(connectors, ".master.key"), "w", encoding="utf-8") as fh:
        fh.write("DO-NOT-EXPORT-SECRET")
    return home


def run_selftest(*, keep: bool = False, out_dir: str | None = None) -> tuple[bool, list[str]]:
    """跑一遍合成导出并核查结果。返回 ``(是否全通过, 报告行)``。"""
    root = tempfile.mkdtemp(prefix="wb-export-selftest-")
    report: list[str] = []
    checks: list[tuple[str, bool]] = []
    try:
        build_fake_home(root)
        target = out_dir or os.path.join(root, "out")
        options = ExportOptions(
            out=target,
            homes=[os.path.join(root, "wb")],
            copy_attachments=True,
            log=lambda _message: None,
        )
        result = run_export(options)

        def expect(name: str, condition: bool, detail: str = "") -> None:
            checks.append((name, bool(condition)))
            report.append(f"{'✓' if condition else '✗'} {name}{('：' + detail) if detail and not condition else ''}")

        expect("导出了 6 个会话（跳过 1 个软删除）", result.sessions == 6, str(result.sessions))
        expect("识别到 1 个已归档会话", result.archived == 1, str(result.archived))
        expect("识别到 1 个「未收录」会话", result.orphans == 1, str(result.orphans))
        expect("识别到 1 个「只有索引没有正文」会话",
               result.missing_body == 1, str(result.missing_body))

        index = os.path.join(target, "index.md")
        expect("生成了 index.md", os.path.isfile(index))
        expect("生成了 export-manifest.json",
               os.path.isfile(os.path.join(target, "export-manifest.json")))

        def find(pattern: str) -> list[str]:
            hits: list[str] = []
            for current, _dirs, files in os.walk(target):
                for name in files:
                    path = os.path.join(current, name)
                    if pattern in path.replace(os.sep, "/"):
                        hits.append(path)
            return hits

        normal = [path for path in find("空间/房子/每周例事/")
                  if os.path.dirname(path).replace(os.sep, "/").endswith("空间/房子/每周例事")]
        expect("普通会话落在「空间/房子/每周例事」下", len(normal) == 1, str(normal))
        expect("归档会话落在「空间/房子/已归档」下", len(find("空间/房子/已归档/")) == 1)
        expect("playground 会话落在「任务」下", len(find("/任务/")) == 1)
        expect("未收录会话落在「未收录」下", len(find("未收录/")) == 1)
        expect("软删除会话默认不导出", not find("删掉的会话"))
        expect("子 Agent 转录导出为单独文件",
               len(find("subagents/agent-abcd1234.md")) == 1)
        expect("附件复制到 _attachments", len(find("_attachments/")) == 1)

        if normal:
            body = open(normal[0], encoding="utf-8").read()
            expect("用户输入从 <user_query> 里提取",
                   "把上周的周报汇总成一份 markdown" in body and "<user_query>" not in body)
            expect("system-reminder 上下文默认不展开", "历史提示词索引见附录" not in body)
            expect("思考内容保留", "先找到周报文件" in body)
            expect("工具调用与结果配对", "Glob" in body and "周报/第 38 周.md" in body)
            expect("未知记录类型原文留档", "brand-new-record-type" in body)
            expect("front matter 带归档/空间/分组字段",
                   "space: 房子" in body and "group: 每周例事" in body)
            expect("任务清单进附录", "收集周报" in body)
            expect("产物索引进附录", "周报汇总.md" in body)
            expect("数据库额外列原样保留", "db_transport:" in body)
            expect("半截行被计数", "transcript_dropped_lines" not in body or "1" in body)

        # 安全断言：凭据绝不能出现在任何导出文件里。
        leaked = False
        for current, _dirs, files in os.walk(target):
            for name in files:
                path = os.path.join(current, name)
                try:
                    with open(path, "rb") as fh:
                        if b"DO-NOT-EXPORT-SECRET" in fh.read():
                            leaked = True
                except OSError:
                    continue
        expect("凭据（.master.key）从未被读取/导出", not leaked)

        cloud = find("云端未删除标记")
        expect("deleted_at = -1 视为未删除，照常导出", len(cloud) == 1)

        passed = all(condition for _name, condition in checks)
        report.append("")
        report.append(f"自检{'通过' if passed else '未通过'}：{sum(1 for _n, c in checks if c)}/{len(checks)} 项")
        return passed, report
    finally:
        if not keep:
            shutil.rmtree(root, ignore_errors=True)
