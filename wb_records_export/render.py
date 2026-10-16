#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把会话渲染成结构化的 markdown。

每个会话一个文件，结构固定：

1. **YAML front matter** —— 会话的全部元数据（含数据库里我们没见过的列，原样带上）；
2. **标题 + 一行摘要** —— 客户端、分类、时间跨度、记录数、模型、正文大小；
3. **对话正文** —— 按行序：用户 / 助手 / 思考（折叠）/ 工具调用（折叠，含结果）；
4. **附录（折叠）** —— 任务清单、产物索引、子 Agent、元信息原文、未识别记录原文。

两个原则：

* **不静默丢数据。** 认不出的记录类型进附录，超长内容截断但标明原长与开关；
* **折叠而不删除。** 思考与工具输出默认折在 ``<details>`` 里：正文可读，
  需要时点开就有。工具输出里的围栏会自适应加长（见 ``util.fence_for``），
  不会把导出文件切碎。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from .sessions import Session
from .transcript import Item, Transcript, pair_tools
from .util import (
    collapse_blank_lines,
    escape_table_cell,
    fence_for,
    human_bytes,
    iso_local,
    iso_utc,
    markdown_table,
    shorten_path,
    truncate,
    yaml_front_matter,
)

VERSION = "0.1.0"


@dataclass
class RenderOptions:
    include_thinking: bool = True
    include_tools: bool = True
    include_tool_arguments: bool = True
    include_appendix: bool = True
    include_todo: bool = True
    include_artifacts: bool = True
    include_subagents: bool = True
    max_tool_output: int = 4000
    max_thinking: int = 8000
    include_unknown_records: bool = True
    include_reminder_context: bool = False
    attachment_links: dict[str, str] = field(default_factory=dict)
    subagent_files: dict[str, str] = field(default_factory=dict)
    subagent_count: int = 0


# --------------------------------------------------------------------------
# 会话文件
# --------------------------------------------------------------------------

def render_session(session: Session, options: RenderOptions) -> str:
    transcript = session.transcript or Transcript()
    parts: list[str] = []
    parts.append(_front_matter(session, transcript, options))
    parts.append("")
    parts.append(f"# {session.title}")
    parts.append("")
    parts.append(_summary_block(session, transcript, options))
    if session.notes:
        parts.append("")
        parts.append("> **导出备注**：" + "；".join(session.notes))
    parts.append("")
    parts.append("## 对话")
    parts.append("")
    parts.append(_conversation(session, transcript, options))
    appendix = _appendix(session, options)
    if appendix:
        parts.append("")
        parts.append(appendix)
    parts.append("")
    return collapse_blank_lines("\n".join(parts)) + "\n"


def _front_matter(session: Session, transcript: Transcript, options: RenderOptions) -> str:
    meta: dict[str, Any] = {
        "title": session.title,
        "session_id": session.session_id,
        "client": session.client.display,
        "client_key": session.client.key,
        "client_home": session.client.home,
        "category": getattr(session, "category_label", "") or "",
        "space": session.space_name,
        "group": session.group,
        "archived": session.archived,
        "archived_reason": session.archived_reason if session.archived else "",
        "pinned": session.pinned,
        "status": session.row.status if session.row else "",
        "created_at": iso_utc(session.created_ms),
        "updated_at": iso_utc(session.sort_ms),
        "cwd": session.cwd,
        "model": ", ".join(transcript.models),
        "records": len(transcript.items),
        "user_messages": transcript.counts.get("user", 0),
        "assistant_messages": transcript.counts.get("assistant", 0),
        "tool_calls": transcript.counts.get("tool_call", 0),
        "subagents": options.subagent_count or 0,
        "tokens_total": transcript.usage.total or None,
        "transcript": _rel(home=session.client.home, path=session.transcript_path),
        "transcript_bytes": _size(session.transcript_path),
        "transcript_dropped_lines": transcript.dropped_lines or None,
        "exported_at": iso_utc(_now()),
        "exporter": f"wb-records-export {VERSION}",
    }
    # 数据库里这一行的其余列原样带上（覆盖上面已算过的同名列除外）。
    if session.row is not None:
        reserved = {key.lower() for key in meta}
        for name, value in session.row.front_matter().items():
            if name.lower() in reserved or name in ("cwd", "title", "status"):
                continue
            meta[f"db_{name}"] = value
    return yaml_front_matter(meta)


def _summary_block(session: Session, transcript: Transcript, options: RenderOptions) -> str:
    bits = [f"**{session.client.display}**"]
    if session.archived:
        bits.append("**已归档**" + (f"（{session.archived_reason}）" if session.archived_reason else ""))
    if session.pinned:
        bits.append("已置顶")
    if session.category_label:
        bits.append(f"分类：`{session.category_label}`")
    span = _time_span(session, transcript)
    if span:
        bits.append(span)
    bits.append(f"{len(transcript.items)} 条记录 / "
                f"{transcript.counts.get('user', 0)} 轮用户输入 / "
                f"{transcript.counts.get('tool_call', 0)} 次工具调用")
    if transcript.models:
        bits.append("模型：" + "、".join(f"`{model}`" for model in transcript.models))
    if transcript.usage.total:
        bits.append(f"tokens 合计 {transcript.usage.total:,}")
    lines = [" · ".join(bits)]
    if session.cwd:
        lines.append(f"> 工作目录：`{shorten_path(session.cwd)}`")
    if session.transcript_path:
        lines.append(
            f"> 正文：`{_rel(home=session.client.home, path=session.transcript_path)}`"
            f"（{human_bytes(_size(session.transcript_path))}）"
        )
    if transcript.dropped_lines:
        lines.append(f"> 有 {transcript.dropped_lines} 行不是完整 JSON（客户端写入中），已跳过")
    return "\n".join(lines)


def _time_span(session: Session, transcript: Transcript) -> str:
    start = session.created_ms or transcript.first_ms
    end = session.sort_ms or transcript.last_ms
    if not start and not end:
        return ""
    if start and end and start != end:
        return f"{iso_local(start)} → {iso_local(end)}"
    return iso_local(start or end)


def _rel(*, home: str, path: str) -> str:
    if not path:
        return ""
    try:
        return os.path.relpath(path, home).replace(os.sep, "/")
    except ValueError:
        return path


def _size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _now() -> int:
    from .util import now_ms

    return now_ms()


# --------------------------------------------------------------------------
# 对话正文
# --------------------------------------------------------------------------

def _conversation(session: Session, transcript: Transcript, options: RenderOptions) -> str:
    ordered, results = pair_tools(transcript.items)
    lines: list[str] = []
    rendered_any = False

    for item in ordered:
        if item.kind == "user":
            if item.context_only and not options.include_reminder_context:
                continue
            lines.append(_message_heading("👤 用户", item, quoted=False))
            lines.append("")
            lines.append(_body(item.text))
            lines.append("")
            rendered_any = True
        elif item.kind == "assistant":
            lines.append(_message_heading("🤖 助手", item, quoted=False))
            lines.append("")
            lines.append(_body(item.text))
            lines.append("")
            rendered_any = True
        elif item.kind == "reasoning" and options.include_thinking and item.text.strip():
            lines.append(_details_block(
                f"💭 思考{_time_suffix(item)}",
                _capped(item.text, options.max_thinking, "思考"),
            ))
            lines.append("")
            rendered_any = True
        elif item.kind == "tool_call" and options.include_tools:
            lines.append(_tool_block(item, results.get(item.call_id), options))
            lines.append("")
            rendered_any = True
        elif item.kind == "tool_result" and options.include_tools:
            lines.append(_tool_block(None, item, options))
            lines.append("")
            rendered_any = True
        elif item.kind == "compact":
            lines.append("> ⓘ **上下文压缩边界** —— 客户端在此处压缩过历史上下文。")
            lines.append("")

    if not rendered_any:
        hint = "（这个文件里没有可渲染的对话内容）"
        if transcript.dropped_lines:
            hint = "（文件里只有不完整的 JSON 行）"
        elif not session.transcript_path:
            hint = "（数据库里有这条会话，但磁盘上没有对应正文文件）"
        return hint
    return "\n".join(lines)


def _message_heading(label: str, item: Item, *, quoted: bool) -> str:
    return f"### {label}{_time_suffix(item)}"


def _time_suffix(item: Item) -> str:
    return f" · {iso_local(item.timestamp_ms)}" if item.timestamp_ms else ""


def _body(text: str) -> str:
    return collapse_blank_lines(text.strip())


def _capped(text: str, limit: int, what: str) -> str:
    body, was_truncated = truncate(text.strip(), limit)
    if was_truncated:
        return body.replace("--max-tool-output", "--max-thinking").replace("工具结果", what)
    return body


def _details_block(summary: str, body: str, *, open_by_default: bool = False) -> str:
    head = f"<details{' open' if open_by_default else ''}>\n<summary>{summary}</summary>\n"
    return f"{head}\n{body.strip()}\n\n</details>"


def _tool_block(call: Item | None, result: Item | None, options: RenderOptions) -> str:
    name = (call.tool_name if call else "") or (result.tool_name if result else "") or "工具"
    if call is None:
        summary = f"🧾 工具结果（未匹配到调用）{_time_suffix(result)}"
    else:
        summary = f"🔧 工具调用 `{name}`{_time_suffix(call)}"
    chunks: list[str] = []

    if call is not None and options.include_tool_arguments:
        args = call.arguments
        if args not in (None, "", [], {}):
            rendered = args if isinstance(args, str) else _json_pretty(args)
            fence = fence_for(rendered)
            chunks.append("**参数**\n")
            chunks.append(f"{fence}json\n{rendered.strip()}\n{fence}")

    if result is not None:
        text, was_truncated = truncate(
            (result.text or "").strip(), int(options.max_tool_output)
        )
        if text:
            fence = fence_for(text)
            label = "**结果（失败）**" if result.is_error else "**结果**"
            chunks.append(label + "\n")
            chunks.append(f"{fence}text\n{text}\n{fence}")
            if result.is_error:
                chunks.append("> 该工具调用返回了失败状态（`status=failed/error`）。")
        refs = _blob_lines(result.blobs, options)
        if refs:
            chunks.append(refs)
        if result.subagents:
            for agent in result.subagents:
                link = options.subagent_files.get(agent)
                if link:
                    chunks.append(f"> 子 Agent 转录：`{agent}` → [单独文件]({link})")
                else:
                    chunks.append(f"> 子 Agent 转录：`{agent}`（未导出为单独文件）")
    elif call is not None:
        chunks.append("> 没有配到结果记录（调用与结果按 `callId` 配对；结果可能在其它文件里）。")

    if not chunks:
        chunks.append("（无参数与结果）")
    return _details_block(summary, "\n\n".join(chunks))


def _blob_lines(blobs: list[dict[str, Any]], options: RenderOptions) -> str:
    if not blobs:
        return ""
    lines = ["**引用附件**"]
    for blob in blobs:
        path = str(blob.get("path") or "")
        if not path:
            continue
        mime = str(blob.get("mime") or blob.get("mime_type") or "未知类型")
        size = blob.get("size")
        size_text = f"，{human_bytes(float(size))}" if isinstance(size, (int, float)) else ""
        link = options.attachment_links.get(path)
        if link:
            lines.append(f"- [{os.path.basename(path)}]({link}) —— {mime}{size_text}")
        else:
            lines.append(f"- `{path}` —— {mime}{size_text}")
    return "\n".join(lines) if len(lines) > 1 else ""


def _json_pretty(value: Any) -> str:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return value
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False)
    except (TypeError, ValueError):  # pragma: no cover - 防御性
        return str(value)


# --------------------------------------------------------------------------
# 附录
# --------------------------------------------------------------------------

def _appendix(session: Session, options: RenderOptions) -> str:
    if not options.include_appendix:
        return ""
    blocks: list[str] = []

    if options.include_todo and session.task_files:
        blocks.append(_todo_block(session))

    if options.include_artifacts and session.artifact_index:
        blocks.append(_artifact_block(session))

    if options.include_subagents and session.subagent_paths:
        blocks.append(_subagent_block(session, options))

    if session.meta:
        blocks.append(_meta_block(session))

    if options.include_unknown_records and session.transcript and session.transcript.unknown_types:
        blocks.append(_unknown_block(session))

    if options.include_reminder_context and session.transcript:
        blocks.append(_context_block(session))

    if not blocks:
        return ""
    return "\n\n".join(["## 附录（元信息与未展开内容）", *blocks])


def _todo_block(session: Session) -> str:
    lines = ["### 任务清单（`tasks/<id>/*.json`）"]
    for path in session.task_files:
        from .util import load_json

        data = load_json(path)
        name = os.path.basename(path)
        if isinstance(data, list):
            rows = []
            for entry in data:
                if isinstance(entry, dict):
                    text = entry.get("content") or entry.get("text") or entry.get("subject") or ""
                    state = entry.get("status") or entry.get("state") or ""
                    rows.append([escape_table_cell(str(text)), escape_table_cell(str(state))])
            if rows:
                lines.append("")
                lines.append(markdown_table(["内容", "状态"], rows))
                continue
        rendered = _json_pretty(data) if data is not None else "（无法解析）"
        fence = fence_for(rendered)
        lines.append(f"\n`{name}`\n\n{fence}json\n{rendered}\n{fence}")
    return "\n".join(lines)


def _artifact_block(session: Session) -> str:
    from .util import load_json

    data = load_json(session.artifact_index)
    lines = ["### 产物索引（`artifact-index/<id>.json`）"]
    rows: list[list[str]] = []
    if isinstance(data, list):
        for entry in data:
            if isinstance(entry, dict):
                rows.append([
                    escape_table_cell(str(entry.get("name") or entry.get("path") or "")),
                    escape_table_cell(str(entry.get("type") or entry.get("kind") or "")),
                    escape_table_cell(str(entry.get("size") or "")),
                ])
    if rows:
        lines.append("")
        lines.append(markdown_table(["名称", "类型", "大小"], rows))
        return "\n".join(lines)
    rendered = _json_pretty(data) if data is not None else "（无法解析）"
    fence = fence_for(rendered)
    lines.append(f"\n{fence}json\n{rendered}\n{fence}")
    return "\n".join(lines)


def _subagent_block(session: Session, options: RenderOptions) -> str:
    lines = ["### 子 Agent 转录"]
    for agent_id, _path in session.subagent_paths:
        link = options.subagent_files.get(agent_id)
        if link:
            lines.append(f"- `{agent_id}` → [单独文件]({link})")
        else:
            lines.append(f"- `{agent_id}`（未导出为单独文件）")
    return "\n".join(lines)


def _meta_block(session: Session) -> str:
    rendered = _json_pretty(session.meta)
    fence = fence_for(rendered)
    return _details_block(
        f"📎 元信息原文（`{os.path.basename(session.meta_path)}`）",
        f"{fence}json\n{rendered}\n{fence}",
    )


def _unknown_block(session: Session) -> str:
    transcript = session.transcript
    assert transcript is not None
    counts = "、".join(f"`{name}`×{count}" for name, count in
                       sorted(transcript.unknown_types.items()))
    lines = [f"这一版导出器没识别这些记录类型（原文保留，未丢弃）：{counts}", ""]
    for item in transcript.items:
        if item.kind != "unknown":
            continue
        rendered = _json_pretty(item.raw)
        fence = fence_for(rendered)
        lines.append(_details_block(
            f"未识别记录 · 第 {item.line_no} 行 · type={item.raw.get('type', '?')}",
            f"{fence}json\n{rendered}\n{fence}",
        ))
        lines.append("")
    return "\n".join(lines)


def _context_block(session: Session) -> str:
    transcript = session.transcript
    assert transcript is not None
    lines = ["### 原始用户消息与上下文（system-reminder / user_info）", ""]
    for item in transcript.items:
        if item.kind != "user":
            continue
        envelope = item.raw.get("__envelope__")
        if item.context_only:
            body = item.text
            label = f"上下文消息 · 第 {item.line_no} 行{_time_suffix(item)}"
        elif isinstance(envelope, str) and envelope.strip() and envelope.strip() != item.text.strip():
            body = envelope
            label = f"用户消息原文（含信封） · 第 {item.line_no} 行{_time_suffix(item)}"
        else:
            continue
        fence = fence_for(body)
        lines.append(_details_block(label, f"{fence}text\n{body.strip()}\n{fence}"))
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 子 Agent 文件
# --------------------------------------------------------------------------

def render_subagent(session: Session, agent_id: str, transcript: Transcript,
                    options: RenderOptions) -> str:
    meta = {
        "title": f"{session.title} · 子 Agent {agent_id}",
        "session_id": session.session_id,
        "subagent_id": agent_id,
        "client": session.client.display,
        "parent_category": getattr(session, "category_label", "") or "",
        "parent_file": getattr(session, "parent_link", "") or "",
        "records": len(transcript.items),
        "tool_calls": transcript.counts.get("tool_call", 0),
        "tokens_total": transcript.usage.total or None,
        "exported_at": iso_utc(_now()),
        "exporter": f"wb-records-export {VERSION}",
    }
    parts = [
        yaml_front_matter(meta),
        "",
        f"# {session.title} · 子 Agent `{agent_id}`",
        "",
        f"> 这是会话主文件的附带转录（`subagents/{agent_id}.jsonl`）。"
        f"主文件：`{getattr(session, 'parent_file_name', '')}`",
        "",
        "## 对话",
        "",
        _conversation(session, transcript, options),
        "",
    ]
    return collapse_blank_lines("\n".join(parts)) + "\n"
