#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""导出编排：发现客户端 → 读索引 → 配正文 → 渲染 → 落盘 → 生成索引与清单。

全程**只读**客户端数据：不写 `workbuddy.db`、不碰 `projects/`、不动 `connectors/`
（那里是加密凭据，导出工具永远不该碰）。因此导出时**不需要退出客户端**。
"""

from __future__ import annotations

import json
import os
import shutil
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable

from .database import DatabaseError, open_database, session_inventory
from .discover import detect_clients, platform_label
from .layout import LayoutPlan
from .render import RenderOptions, render_session, render_subagent
from .sessions import Session, collect_sessions, subagent_transcripts
from .util import (
    atomic_write_text,
    ensure_within,
    escape_table_cell,
    human_bytes,
    iso_local,
    iso_utc,
    markdown_table,
    now_ms,
    rel_posix,
)

VERSION = "0.1.0"


@dataclass
class ExportOptions:
    out: str
    homes: list[str] = field(default_factory=list)
    clients: list[str] = field(default_factory=list)
    layout: str = "category"
    archive_layout: str = "nested"
    ascii_paths: bool = False
    include_deleted: bool = False
    include_archived: bool = True
    include_codebuddy: bool = False
    group_folders: bool = True
    split_clients: bool = False
    include_thinking: bool = True
    include_tools: bool = True
    include_tool_arguments: bool = True
    include_appendix: bool = True
    include_unknown_records: bool = True
    include_reminder_context: bool = False
    include_subagents: bool = True
    copy_attachments: bool = False
    max_attachment_mb: float = 20.0
    max_tool_output: int = 4000
    max_thinking: int = 8000
    max_title_chars: int = 60
    dry_run: bool = False
    limit: int | None = None
    clean: bool = False
    log: Callable[[str], None] = print

    def render_options(self) -> RenderOptions:
        return RenderOptions(
            include_thinking=self.include_thinking,
            include_tools=self.include_tools,
            include_tool_arguments=self.include_tool_arguments,
            include_appendix=self.include_appendix,
            include_unknown_records=self.include_unknown_records,
            include_reminder_context=self.include_reminder_context,
            include_subagents=self.include_subagents,
            max_tool_output=self.max_tool_output,
            max_thinking=self.max_thinking,
        )


@dataclass
class ExportResult:
    out: str
    sessions: int = 0
    files: int = 0
    bytes: int = 0
    archived: int = 0
    orphans: int = 0
    missing_body: int = 0
    skipped_deleted: int = 0
    subagent_files: int = 0
    attachments: int = 0
    clients: list[dict[str, Any]] = field(default_factory=list)
    unknown_types: Counter = field(default_factory=Counter)
    schema: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    manifest_path: str = ""
    written: list[str] = field(default_factory=list)


def run_export(options: ExportOptions) -> ExportResult:
    result = ExportResult(out=os.path.abspath(options.out))
    log = options.log

    clients = detect_clients(
        include_codebuddy=options.include_codebuddy or "codebuddy" in options.clients,
        explicit=options.homes or None,
        keys=options.clients or None,
    )
    clients = [client for client in clients if client.has_db or client.has_projects]
    if not clients:
        raise SystemExit(
            "没有找到可导出的客户端数据目录。\n"
            "  - 默认会找 ~/.workbuddy（WorkBuddy）与 ~/.workbuddy-ai（WorkBuddy AI）；\n"
            "  - 用 --home /path/to/home 显式指定，或用 --client 选择客户端。"
        )

    log(f"平台：{platform_label()}")
    for client in clients:
        note = "" if client.confirmed else "（目录里没有 workbuddy.db，只导出正文文件）"
        log(f"客户端：{client.display} → {client.home} {note}")

    if options.clean and not options.dry_run and os.path.isdir(options.out):
        log(f"清空输出目录：{options.out}")
        shutil.rmtree(options.out)

    out_root = os.path.abspath(options.out)
    if not options.dry_run:
        # --dry-run 连目录都不建：试跑不该在磁盘上留下任何痕迹。
        os.makedirs(out_root, exist_ok=True)

    collected: list[Session] = []
    per_client: list[dict[str, Any]] = []
    for client in clients:
        database = None
        try:
            if client.has_db:
                database = open_database(client.db_path)
                result.schema.append(
                    {
                        "client": client.key,
                        "display": client.display,
                        "tables": list(database.tables),
                        "session_columns": [column.name for column in database.columns],
                        "workspaces": len(database.workspaces),
                    }
                )
            sessions, stats = collect_sessions(
                client,
                include_deleted=options.include_deleted,
                include_archived=options.include_archived,
                database=database,
            )
        except DatabaseError as exc:
            result.warnings.append(str(exc))
            log(f"  ! {exc}")
            sessions, stats = collect_sessions(
                client, include_deleted=options.include_deleted,
                include_archived=options.include_archived, database=None,
                use_db=False,
            )
        if options.limit is not None:
            sessions = sessions[: options.limit]
        stats["exported"] = len(sessions)
        per_client.append(stats)
        result.clients.append(stats)
        collected.extend(sessions)
        log(
            f"  {len(sessions)} 条会话"
            + (f"（磁盘有正文、数据库无行：{stats['orphans']}）" if stats["orphans"] else "")
            + (f"（索引有行、磁盘缺正文：{stats['missing_body']}）" if stats["missing_body"] else "")
            + (f"（跳过已删除：{stats['skipped_deleted']}）" if stats["skipped_deleted"] else "")
        )

    # 只要扫了不止一个数据目录（例如 WorkBuddy 与 WorkBuddy AI 同时导出），
    # 就在顶层加一层客户端目录 —— 否则两边同名空间会混在一起。
    split = options.split_clients or len(clients) > 1
    # 顶层客户端目录名也要去重：两个自定义目录同名时，否则会把两个数据源
    # 的会话合进同一个文件夹。
    folder_names: dict[str, str] = {}
    used_folders: set[str] = set()
    for client in clients:
        from .util import dedupe_name

        folder_names[client.key] = dedupe_name(client.folder_name, used_folders)
    registry: dict[str, LayoutPlan] = {}
    for session in collected:
        client = session.client
        if client.key not in registry:
            prefix = [folder_names.get(client.key, client.folder_name)] if split else []
            registry[client.key] = LayoutPlan(out_root, options, client_prefix=prefix)
        plan = registry[client.key]
        placement = plan.session_placement(session)
        session.category_label = placement.category
        session.parent_file_name = placement.filename

    if options.copy_attachments:
        _copy_attachments(collected, registry, out_root, options, result, log)

    render_defaults = options.render_options()
    for index, session in enumerate(collected, start=1):
        placement = registry[session.client.key].session_placement(session)
        path = ensure_within(out_root, placement.absolute(out_root))
        options_for_session = _render_options_for(session, render_defaults)
        text = render_session(session, options_for_session)
        if not options.dry_run:
            atomic_write_text(path, text)
        result.files += 1
        result.bytes += len(text.encode("utf-8"))
        result.written.append(rel_posix(out_root, path))
        if session.archived:
            result.archived += 1
        if session.row is None:
            result.orphans += 1
        if session.transcript and session.transcript.unknown_types:
            result.unknown_types.update(session.transcript.unknown_types)
        if options.include_subagents:
            result.subagent_files += _write_subagents(
                session, placement, registry[session.client.key], out_root, options, log
            )
        if index % 200 == 0:
            log(f"  … 已处理 {index}/{len(collected)}")

    result.sessions = len(collected)
    result.missing_body = sum(item.get("missing_body", 0) for item in result.clients)
    result.skipped_deleted = sum(item.get("skipped_deleted", 0) for item in result.clients)

    index_text = _render_index(collected, registry, options)
    readme_text = _render_readme(result, options)
    manifest = _render_manifest(result, collected, registry, options)

    if not options.dry_run:
        atomic_write_text(os.path.join(out_root, "index.md"), index_text)
        atomic_write_text(os.path.join(out_root, "README.md"), readme_text)
        manifest_path = os.path.join(out_root, "export-manifest.json")
        atomic_write_text(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        result.manifest_path = manifest_path

    log("")
    log(f"导出完成：{result.sessions} 个会话，{result.files + result.subagent_files} 个 markdown 文件"
        f"（{human_bytes(result.bytes)}）")
    if result.archived:
        log(f"  其中已归档 {result.archived} 个")
    if result.orphans:
        log(f"  磁盘有正文、数据库无行 {result.orphans} 个（放在「未收录」）")
    if result.missing_body:
        log(f"  数据库有行、磁盘缺正文 {result.missing_body} 个（只导出元数据）")
    if result.skipped_deleted:
        log(f"  跳过软删除会话 {result.skipped_deleted} 个（用 --include-deleted 一并导出）")
    if result.attachments:
        log(f"  复制附件 {result.attachments} 个")
    if result.unknown_types:
        summary = "、".join(f"{name}×{count}" for name, count in
                            sorted(result.unknown_types.items())[:6])
        log(f"  未识别的记录类型：{summary}（原文保留在各会话附录里）")
    for warning in result.warnings:
        log(f"  ! {warning}")
    if options.dry_run:
        log("（--dry-run：没有写入任何文件）")
    else:
        log(f"输出目录：{result.out}")
    return result


def _render_options_for(session: Session, base: RenderOptions) -> RenderOptions:
    subagents = _subagent_links(session)
    return RenderOptions(
        include_thinking=base.include_thinking,
        include_tools=base.include_tools,
        include_tool_arguments=base.include_tool_arguments,
        include_appendix=base.include_appendix,
        include_todo=base.include_todo,
        include_artifacts=base.include_artifacts,
        include_subagents=base.include_subagents,
        max_tool_output=base.max_tool_output,
        max_thinking=base.max_thinking,
        include_unknown_records=base.include_unknown_records,
        include_reminder_context=base.include_reminder_context,
        attachment_links=dict(session.attachment_links),
        subagent_files=subagents,
        subagent_count=len(session.subagent_paths),
    )


def _subagent_links(session: Session) -> dict[str, str]:
    """子 Agent 转录文件的相对链接（渲染在主文件里）。"""
    links: dict[str, str] = {}
    if not session.subagent_paths:
        return links
    stem = os.path.splitext(session.parent_file_name or "session")[0]
    for agent_id, _path in session.subagent_paths:
        links[agent_id] = f"{stem}/subagents/{agent_id}.md"
    return links


def _write_subagents(session: Session, placement, plan: LayoutPlan, out_root: str,
                     options: ExportOptions, log: Callable[[str], None]) -> int:
    if not session.subagent_paths:
        return 0
    stem = os.path.splitext(placement.filename)[0]
    written = 0
    base_options = _render_options_for(session, options.render_options())
    for agent_id, transcript in subagent_transcripts(session):
        target_dir = os.path.join(out_root, *placement.parts, stem, "subagents")
        path = ensure_within(out_root, os.path.join(target_dir, f"{agent_id}.md"))
        session.parent_link = placement.relative_path
        text = render_subagent(session, agent_id, transcript, base_options)
        if not options.dry_run:
            atomic_write_text(path, text)
        written += 1
    return written


def _copy_attachments(sessions: list[Session], registry: dict[str, LayoutPlan], out_root: str,
                      options: ExportOptions, result: ExportResult,
                      log: Callable[[str], None]) -> None:
    limit = int(options.max_attachment_mb * 1024 * 1024)
    for session in sessions:
        if not session.transcript or not session.transcript.blobs:
            continue
        placement = registry[session.client.key].session_placement(session)
        for blob in session.transcript.blobs:
            raw = str(blob.get("path") or "")
            if not raw:
                continue
            source = raw if os.path.isabs(raw) else os.path.join(session.client.home, raw)
            if not os.path.isfile(source):
                result.warnings.append(f"附件不存在：{raw}")
                continue
            size = os.path.getsize(source)
            if size > limit:
                result.warnings.append(
                    f"附件超过 {options.max_attachment_mb}MB 未复制：{raw}"
                    f"（{human_bytes(size)}）"
                )
                continue
            target_dir = os.path.join(out_root, *placement.parts,
                                      registry[session.client.key].names["attachments"],
                                      session.session_id)
            target = ensure_within(out_root, os.path.join(target_dir, os.path.basename(source)))
            if not options.dry_run:
                os.makedirs(target_dir, exist_ok=True)
                if not os.path.exists(target):
                    shutil.copy2(source, target)
            link = os.path.relpath(target, os.path.join(out_root, *placement.parts))
            session.attachment_links[raw] = link.replace(os.sep, "/")
            result.attachments += 1


# --------------------------------------------------------------------------
# index.md / README.md / 清单
# --------------------------------------------------------------------------

def _render_index(sessions: list[Session], registry: dict[str, LayoutPlan],
                  options: ExportOptions) -> str:
    rows: list[list[str]] = []
    for session in sessions:
        placement = registry[session.client.key].session_placement(session)
        transcript = session.transcript
        updated = session.sort_ms
        rows.append([
            iso_local(updated) if updated else "—",
            escape_table_cell(session.title)[:80],
            escape_table_cell(session.client.display),
            escape_table_cell(placement.category or "平铺"),
            "是" if session.archived else "",
            str(session.pinned and "是" or ""),
            str(transcript.counts.get("user", 0)) if transcript else "",
            str(transcript.counts.get("tool_call", 0)) if transcript else "",
            f"[打开]({_link(session, placement)})",
        ])
    headers = ["最近活动", "标题", "客户端", "分类", "已归档", "置顶", "用户轮次", "工具调用", "文件"]
    body = [f"# 会话索引（{len(sessions)} 条）", "",
            "按最近活动时间排序，路径相对于本文件所在目录。", "",
            markdown_table(headers, rows), ""]
    if not sessions:
        body.insert(3, "（没有可导出的会话）\n")
    return "\n".join(body)


def _link(session: Session, placement) -> str:
    from urllib.parse import quote

    return quote(placement.relative_path)


def _render_readme(result: ExportResult, options: ExportOptions) -> str:
    lines: list[str] = []
    lines.append("# WorkBuddy 对话历史导出")
    lines.append("")
    lines.append(f"导出时间：{iso_local(now_ms())} · 导出器：`wb-records-export {VERSION}` · 平台：{platform_label()}")
    lines.append("")
    lines.append("每个会话一个 markdown 文件，按客户端里的原有分类放进对应文件夹；"
                 f"全部会话见 [`index.md`](index.md)，机器可读的清单见 `export-manifest.json`。")
    lines.append("")
    lines.append("## 本次导出")
    lines.append("")
    lines.append(markdown_table(
        ["客户端", "数据目录", "会话数", "已归档", "未收录", "缺正文", "跳过已删除"],
        [
            [
                item.get("display", ""),
                f"`{item.get('home', '')}`",
                str(item.get("exported", 0)),
                str(item.get("archived", 0)),
                str(item.get("orphans", 0)),
                str(item.get("missing_body", 0)),
                str(item.get("skipped_deleted", 0)),
            ]
            for item in result.clients
        ],
    ))
    lines.append("")
    lines.append(f"合计：**{result.sessions}** 个会话 / **{result.files + result.subagent_files}** 个文件 / "
                 f"{human_bytes(result.bytes)}")
    lines.append("")
    lines.append("## 目录结构")
    lines.append("")
    lines.append("```text")
    lines.append("空间/<空间名>/[<分组>/][已归档/]<日期>_<标题>_<会话 id 前 8 位>.md")
    lines.append("任务/[<分组>/][已归档/]…          # 不属于任何空间的任务（侧边栏「任务」板块）")
    lines.append("未收录/…                          # 磁盘上有正文、数据库里没有索引行")
    lines.append("已删除/…                          # 仅在 --include-deleted 时出现")
    lines.append("_attachments/<会话 id>/…          # 仅在 --copy-attachments 时出现")
    lines.append("```")
    lines.append("")
    lines.append("分类来自客户端自己的数据：「空间」= `workbuddy.db` 的 `workspaces` 表"
                 "（按 `cwd` 对齐），空间内的「文件夹」= `group_id`/`group_title` 列，"
                 "「已归档」= `sessions.status = 'archived'`。")
    if options.archive_layout == "top":
        lines.append("")
        lines.append("本次使用了 `--archive-layout top`：归档会话统一放在顶层 `已归档/` 下。")
    if options.layout == "flat":
        lines.append("")
        lines.append("本次使用了 `--layout flat`：所有会话平铺在一个目录里，分类只写在文件头与索引里。")
    lines.append("")
    lines.append("## 每个文件里有什么")
    lines.append("")
    lines.append("- **YAML 文件头**：标题、会话 id、客户端与数据目录、分类、空间/分组、"
                 "归档与置顶状态、创建/更新时间、`cwd`、模型、记录数、tokens，"
                 "以及 `sessions` 表里的其它列（`db_` 前缀，原样保留 —— 客户端升版本多出来的列不会丢）")
    lines.append("- **对话正文**：按行序输出用户与助手的原话；思考过程、工具调用参数与结果"
                 "默认折叠在 `<details>` 里")
    lines.append("- **附录**：任务清单、产物索引、子 Agent 转录、`.meta.json` 原文、"
                 "以及本版未识别的记录原文")
    lines.append("")
    lines.append("## 数据缺口与边界")
    lines.append("")
    if result.missing_body:
        lines.append(f"- **{result.missing_body} 个会话只有数据库索引、磁盘上没有正文**"
                     "（客户端可能清理过 `projects/`）；这些文件仍然导出，内容处会写明原因。")
    if result.orphans:
        lines.append(f"- **{result.orphans} 个会话有正文但数据库里没有索引行**，放在「未收录」下。")
    if result.skipped_deleted:
        lines.append(f"- **{result.skipped_deleted} 个软删除会话被跳过**"
                     "（`deleted_at` 有值且不是 -1）；加 `--include-deleted` 可一并导出。")
    if result.unknown_types:
        summary = "、".join(f"`{name}`×{count}" for name, count in sorted(result.unknown_types.items()))
        lines.append(f"- 未识别的记录类型：{summary} —— 原文保留在对应会话的附录里，没有丢弃。")
    lines.append("- 正在写入的半截 JSON 行会被跳过（文件头里计数为 `transcript_dropped_lines`）。")
    lines.append("- 连接器凭据（`connectors/**`）与日志（`logs/`、`traces/`）从不读取。")
    lines.append("- 导出是只读的：不写客户端数据库、不改 `projects/`，因此不需要退出客户端。")
    if result.schema:
        columns = ", ".join(
            f"`{item['display']}` {len(item.get('session_columns', []))} 列"
            for item in result.schema
        )
        lines.append(f"- 读到的 `sessions` 表结构：{columns}"
                     "（多余列一律以 `db_` 前缀原样写进文件头）。")
    lines.append("")
    lines.append("## 重新导出")
    lines.append("")
    lines.append("```bash")
    lines.append("python3 export-records.py --out ./workbuddy-export")
    lines.append("# 只导出某个客户端 / 指定数据目录：")
    lines.append("python3 export-records.py --client wb_ai --out ./workbuddy-ai-export")
    lines.append("python3 export-records.py --home ~/.workbuddy --out ./export")
    lines.append("```")
    lines.append("")
    lines.append("> 非官方工具：只读取你本机上 WorkBuddy 客户端自己的数据目录。")
    lines.append("")
    return "\n".join(lines)


def _render_manifest(result: ExportResult, sessions: list[Session],
                     registry: dict[str, LayoutPlan], options: ExportOptions) -> dict[str, Any]:
    items = []
    for session in sessions:
        placement = registry[session.client.key].session_placement(session)
        transcript = session.transcript
        items.append({
            "session_id": session.session_id,
            "title": session.title,
            "client": session.client_key,
            "category": placement.category,
            "space": session.space_name,
            "group": session.group,
            "archived": session.archived,
            "archived_reason": session.archived_reason,
            "pinned": session.pinned,
            "deleted": session.deleted,
            "has_db_row": session.row is not None,
            "cwd": session.cwd,
            "created_at": iso_utc(session.created_ms),
            "updated_at": iso_utc(session.sort_ms),
            "records": len(transcript.items) if transcript else 0,
            "user_messages": transcript.counts.get("user", 0) if transcript else 0,
            "tool_calls": transcript.counts.get("tool_call", 0) if transcript else 0,
            "tokens_total": transcript.usage.total if transcript else 0,
            "models": list(transcript.models) if transcript else [],
            "subagents": len(session.subagent_paths),
            "transcript_path": rel_posix(session.client.home, session.transcript_path)
            if session.transcript_path else "",
            "file": placement.relative_path,
        })
    return {
        "exporter": f"wb-records-export {VERSION}",
        "exported_at": iso_utc(now_ms()),
        "platform": platform_label(),
        "out": result.out,
        "options": {
            "layout": options.layout,
            "archive_layout": options.archive_layout,
            "include_deleted": options.include_deleted,
            "include_archived": options.include_archived,
            "include_subagents": options.include_subagents,
            "copy_attachments": options.copy_attachments,
            "max_tool_output": options.max_tool_output,
            "ascii_paths": options.ascii_paths,
            "dry_run": options.dry_run,
        },
        "clients": [
            {key: value for key, value in item.items() if key != "session_columns"}
            | {"session_columns": item.get("session_columns", [])}
            for item in result.clients
        ],
        "schema": result.schema,
        "stats": {
            "sessions": result.sessions,
            "files": result.files + result.subagent_files,
            "bytes": result.bytes,
            "archived": result.archived,
            "orphans": result.orphans,
            "missing_body": result.missing_body,
            "skipped_deleted": result.skipped_deleted,
            "attachments": result.attachments,
        },
        "unknown_record_types": dict(sorted(result.unknown_types.items())),
        "warnings": result.warnings,
        "sessions": items,
    }


def probe(options: ExportOptions) -> str:
    """``--probe``：打印数据目录与数据库结构摘要（不含任何正文）。"""
    lines = [f"平台：{platform_label()}"]
    clients = detect_clients(
        include_codebuddy=options.include_codebuddy or "codebuddy" in options.clients,
        explicit=options.homes or None,
        keys=options.clients or None,
    )
    if not clients:
        return lines[0] + "\n没有找到客户端数据目录。用 --home 指定路径。"
    for client in clients:
        lines.append("")
        lines.append(f"[{client.display}] {client.home}（来源 {client.source}）")
        lines.append(f"  projects/ 目录：{'存在' if client.has_projects else '不存在'}")
        if client.has_db:
            try:
                database = open_database(client.db_path)
            except DatabaseError as exc:
                lines.append(f"  ! 数据库读取失败：{exc}")
                continue
            lines.append(session_inventory(database))
            database.close()
        else:
            lines.append(f"  数据库：未找到 {client.db_path}")
    return "\n".join(lines)
