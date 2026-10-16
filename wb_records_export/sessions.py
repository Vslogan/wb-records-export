#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把「数据库索引」与「磁盘正文」拼成一个个可导出的会话。

会话有两个来源，必须都认：

* ``workbuddy.db`` 的 ``sessions`` 表 —— 标题、时间、空间、归档状态在这里；
* ``projects/**/<id>.jsonl`` —— 正文在这里。

两者**都可能缺**：数据库里删过的行可能还在磁盘上；反过来，客户端早期版本或
CLI 直接写出来的正文可能根本没有数据库行。第三方实现（如 agent-git）明确写过
「Transcript discovery must include JSONL even when a desktop metadata row is
absent」。所以这里做并集，缺失的一方留空并标注，而不是任选一头当权威。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

from .database import Database, SessionRow, open_database
from .discover import ClientHome
from .transcript import Transcript, parse_file
from .util import load_json

#: WorkBuddy 默认的「工作台」根目录：里面的临时目录（时间戳目录、_temp）是
#: 无项目会话的落脚点，客户端把它们放在侧边栏「任务」板块而不是空间里。
WORKBUDDY_HOME_DIRNAME = "WorkBuddy"
_DATED_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}$")
TEMP_DIRNAME = "_temp"

#: 正文配套文件的后缀（与 wb-account-sync / 上游实现一致）。
META_SUFFIX = ".meta.json"
ROLLBACK_SUFFIX = ".file-rollback.ndjson"


def is_ephemeral_cwd(cwd: str, home: str | None = None) -> bool:
    """``cwd`` 是不是「没有项目」的临时目录。

    口径与第三方实现一致：
    ``~/WorkBuddy`` 本身、它下面的 ``_temp``、以及形如
    ``2026-09-16-09-08-41`` 的时间戳目录都算临时；``~/WorkBuddy/房子`` 这种
    有名字的目录**不算**（那是真项目）。
    """
    if not cwd:
        return False
    import os as _os

    workbuddy_home = _os.path.join(home or _os.path.expanduser("~"), WORKBUDDY_HOME_DIRNAME)
    resolved = _os.path.abspath(_os.path.expanduser(cwd)).replace("\\", "/")
    root = _os.path.abspath(workbuddy_home).replace("\\", "/").rstrip("/")
    if resolved == root:
        return True
    if not resolved.startswith(root + "/"):
        return False
    rest = resolved[len(root) + 1:]
    parts = [part for part in rest.split("/") if part]
    if not parts:
        return True
    if parts[0] == TEMP_DIRNAME:
        return True
    return len(parts) == 1 and bool(_DATED_DIR_RE.match(parts[0]))


def project_slug(cwd: str) -> str:
    """cwd → ``projects/`` 下的桶名。

    上游实测口径（agent-git 的机制勘察文档）：把路径分隔符与冒号换成连字符、
    去掉首尾连字符、把连续连字符合并 —— **不是** Claude Code 的 slug 规则。
    这里实现同一口径，但只把它当成「快速命中」的提示：真正的定位永远是全桶
    扫描（见 ``TranscriptIndex``），因为规则随版本变过，扫一遍代价很小。
    """
    text = (cwd or "").strip().replace("\\", "/")
    text = re.sub(r"[:/\\]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")


@dataclass
class TranscriptIndex:
    """``projects/`` 下一次扫描建立起来的「会话 id → 正文路径」索引。"""

    by_id: dict[str, list[str]] = field(default_factory=dict)
    by_bucket: dict[str, list[str]] = field(default_factory=dict)
    buckets_by_id: dict[str, list[str]] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)

    def add(self, path: str, bucket: str, session_id: str) -> None:
        self.by_id.setdefault(session_id, []).append(path)
        self.by_bucket.setdefault(bucket, []).append(path)
        self.buckets_by_id.setdefault(session_id, []).append(bucket)

    def preferred(self, session_id: str, cwd: str = "") -> str:
        """优先返回与 ``cwd`` 对得上的那个桶里的正文。"""
        candidates = self.by_id.get(session_id)
        if not candidates:
            return ""
        if cwd:
            want = project_slug(cwd)
            for path in candidates:
                bucket = os.path.basename(os.path.dirname(path))
                if bucket == want:
                    return path
        return sorted(candidates)[0]


def build_transcript_index(client: ClientHome) -> TranscriptIndex:
    index = TranscriptIndex()
    root = client.projects_dir
    if not os.path.isdir(root):
        return index
    for entry in sorted(os.listdir(root)):
        bucket_dir = os.path.join(root, entry)
        if os.path.isdir(bucket_dir):
            _collect_bucket(bucket_dir, entry, index)
        elif entry.endswith(".jsonl"):
            # 直接躺在 projects/ 下的正文（客户端两种放法都见过）。
            stem = entry[: -len(".jsonl")]
            if stem:
                index.add(os.path.join(root, entry), "", stem)
    return index


def _collect_bucket(bucket_dir: str, bucket: str, index: TranscriptIndex) -> None:
    for name in sorted(os.listdir(bucket_dir)):
        full = os.path.join(bucket_dir, name)
        if os.path.isdir(full):
            # ``<id>/`` 目录里是 tool-results/ 与 subagents/，正文另算。
            continue
        if not name.endswith(".jsonl"):
            continue
        stem = name[: -len(".jsonl")]
        if not stem or stem.endswith(".file-rollback"):
            continue
        index.add(full, bucket, stem)


@dataclass
class Session:
    """一个待导出会话（数据库行 + 正文 + 配套资产）。"""

    client: ClientHome
    session_id: str
    row: SessionRow | None = None
    bucket: str = ""
    transcript_path: str = ""
    transcript: Transcript | None = None
    subagent_paths: list[tuple[str, str]] = field(default_factory=list)  # (agent_id, path)
    meta_path: str = ""
    meta: dict[str, Any] | None = None
    rollback_path: str = ""
    tool_result_dir: str = ""
    task_files: list[str] = field(default_factory=list)
    artifact_index: str = ""
    notes: list[str] = field(default_factory=list)
    #: 由布局层回填，渲染 front matter 与索引时用
    category_label: str = ""
    parent_link: str = ""
    parent_file_name: str = ""
    #: 正文里引用的 blob 在本文件里的相对链接（--copy-attachments 时才会填）
    attachment_links: dict[str, str] = field(default_factory=dict)

    # ---- 语义字段 --------------------------------------------------------
    @property
    def cwd(self) -> str:
        if self.row is not None and self.row.cwd:
            return self.row.cwd
        if self.transcript and self.transcript.cwds:
            return self.transcript.cwds[0]
        meta_cwd = (self.meta or {}).get("cwd") if isinstance(self.meta, dict) else None
        return str(meta_cwd or "")

    @property
    def db_title(self) -> str:
        return self.row.title if self.row is not None else ""

    @property
    def transcript_title(self) -> str:
        for title in (self.transcript.titles if self.transcript else []):
            cleaned = title.strip()
            if cleaned and cleaned.lower() not in {"new session", "untitled", "无标题"}:
                return cleaned
        if isinstance(self.meta, dict):
            for key in ("title", "summary", "name"):
                raw = self.meta.get(key)
                if isinstance(raw, str) and raw.strip():
                    return raw.strip()
        return ""

    @property
    def title(self) -> str:
        """标题优先级：用户改过的名字 → 库里的标题 → 正文里的摘要 → cwd 末段。"""
        for candidate in (self.db_title, self.transcript_title):
            if candidate.strip():
                return candidate.strip()
        if self.cwd:
            base = os.path.basename(self.cwd.rstrip("/\\"))
            if base:
                return base
        return "无标题会话"

    @property
    def archived(self) -> bool:
        return bool(self.row and self.row.archived)

    @property
    def deleted(self) -> bool:
        return bool(self.row and self.row.deleted)

    @property
    def pinned(self) -> bool:
        return bool(self.row and self.row.pinned)

    @property
    def space_name(self) -> str:
        if self.row and self.row.space_name:
            return self.row.space_name
        if self.cwd:
            base = os.path.basename(self.cwd.rstrip("/\\"))
            return base or self.cwd
        return ""

    @property
    def group(self) -> str:
        return self.row.group if self.row else ""

    @property
    def is_playground(self) -> bool:
        return bool(self.row and self.row.is_playground)

    @property
    def is_ephemeral(self) -> bool:
        """临时目录里的会话：属于侧边栏「任务」板块，不属于任何空间。"""
        return is_ephemeral_cwd(self.cwd)

    @property
    def created_ms(self) -> int | None:
        if self.row and self.row.created_ms:
            return self.row.created_ms
        return self.transcript.first_ms if self.transcript else None

    @property
    def sort_ms(self) -> int | None:
        if self.row and self.row.sort_ms:
            return self.row.sort_ms
        if self.transcript:
            return self.transcript.last_ms or self.transcript.first_ms
        return _file_mtime_ms(self.transcript_path)

    @property
    def archived_reason(self) -> str:
        return self.row.archived_reason if self.row else ""

    @property
    def client_key(self) -> str:
        return self.client.key


def _file_mtime_ms(path: str) -> int | None:
    try:
        return int(os.stat(path).st_mtime * 1000)
    except OSError:
        return None


def load_session_assets(session: Session) -> None:
    """补齐配套文件路径（meta / 回滚 / 工具结果 / 子 Agent / 任务 / 产物索引）。"""
    client = session.client
    bucket_dir = os.path.dirname(session.transcript_path) if session.transcript_path else ""
    if bucket_dir:
        meta = os.path.join(bucket_dir, session.session_id + META_SUFFIX)
        if os.path.isfile(meta):
            session.meta_path = meta
            loaded = load_json(meta)
            session.meta = loaded if isinstance(loaded, dict) else None
        rollback = os.path.join(bucket_dir, session.session_id + ROLLBACK_SUFFIX)
        if os.path.isfile(rollback):
            session.rollback_path = rollback
    session_dir = os.path.join(bucket_dir, session.session_id) if bucket_dir else ""
    if session_dir and os.path.isdir(session_dir):
        tool_dir = os.path.join(session_dir, "tool-results")
        if os.path.isdir(tool_dir):
            session.tool_result_dir = tool_dir
        subagents_dir = os.path.join(session_dir, "subagents")
        if os.path.isdir(subagents_dir):
            for name in sorted(os.listdir(subagents_dir)):
                if name.startswith("agent-") and name.endswith(".jsonl"):
                    session.subagent_paths.append(
                        (name[: -len(".jsonl")], os.path.join(subagents_dir, name))
                    )
    tasks_dir = os.path.join(client.home, "tasks", session.session_id)
    if os.path.isdir(tasks_dir):
        session.task_files = [
            os.path.join(tasks_dir, name)
            for name in sorted(os.listdir(tasks_dir))
            if name.endswith(".json")
        ]
    artifact = os.path.join(client.home, "artifact-index", session.session_id + ".json")
    if os.path.isfile(artifact):
        session.artifact_index = artifact


def collect_sessions(
    client: ClientHome,
    *,
    include_deleted: bool = False,
    include_archived: bool = True,
    database: Database | None = None,
    parse_bodies: bool = True,
    use_db: bool = True,
) -> tuple[list[Session], dict[str, Any]]:
    """汇总一个客户端里的全部会话。

    返回 ``(会话列表, 统计)``。统计里带上「有索引没正文」「有正文没索引」的条数，
    让用户知道数据里本来就有缺口，而不是以为导出漏了东西。
    """
    index = build_transcript_index(client)
    rows: list[SessionRow] = []
    db = database
    if client.has_db and use_db:
        # ``use_db=False`` 是给「数据库坏了」的降级路径用的：上层已经知道了，
        # 这里不能再试一次（否则会把同一个异常再抛一遍，降级就等于没做）。
        db = db or open_database(client.db_path)
        rows = db.sessions

    sessions: list[Session] = []
    seen_ids: set[str] = set()
    #: 数据库里出现过的**全部** id（含被跳过的已删除行）。判定「未收录」时必须
    #: 用它：否则被跳过的软删除会话会因为「没被导出」而被当成孤儿再导出一次 ——
    #: 那就等于 --include-deleted 被绕过了。
    row_ids: set[str] = set()
    missing_body: list[str] = []

    for row in rows:
        if not row.id:
            continue
        row_ids.add(row.id)
        if row.deleted and not include_deleted:
            continue
        if row.archived and not include_archived:
            continue
        seen_ids.add(row.id)
        path = index.preferred(row.id, row.cwd)
        if not path:
            missing_body.append(row.id)
        session = Session(client=client, session_id=row.id, row=row, transcript_path=path)
        if path:
            session.bucket = (index.buckets_by_id.get(row.id) or [""])[0]
            if os.path.dirname(path) == client.projects_dir:
                session.bucket = ""
        load_session_assets(session)
        sessions.append(session)

    orphans = 0
    for session_id, paths in index.by_id.items():
        if session_id in seen_ids or session_id in row_ids:
            continue
        for path in sorted(paths):
            orphan = Session(client=client, session_id=session_id, transcript_path=path)
            orphan.bucket = os.path.basename(os.path.dirname(path))
            if os.path.dirname(path) == client.projects_dir:
                orphan.bucket = ""
            orphan.notes.append("磁盘上有正文，但数据库里没有对应行（可能是 CLI 直接写的，或索引被清理过）")
            load_session_assets(orphan)
            sessions.append(orphan)
            orphans += 1

    if parse_bodies:
        for session in sessions:
            if session.transcript_path:
                try:
                    session.transcript = parse_file(session.transcript_path)
                except OSError as exc:
                    session.notes.append(f"正文读取失败：{exc}")

    sessions.sort(key=lambda item: (item.sort_ms or 0, item.session_id), reverse=True)
    stats = {
        "client": client.key,
        "display": client.display,
        "home": client.home,
        "db": client.db_path if client.has_db else "",
        "db_sessions": len(rows),
        "exported": len(sessions),
        "orphans": orphans,
        "archived": sum(1 for item in sessions if item.archived),
        "missing_body": len(missing_body),
        "skipped_deleted": sum(1 for row in rows if row.deleted),
        "skipped_archived": sum(1 for row in rows if row.archived) if not include_archived else 0,
        "tables": list(db.tables) if db else [],
        "session_columns": [column.name for column in db.columns] if db else [],
    }
    return sessions, stats


def subagent_transcripts(session: Session) -> list[tuple[str, Transcript]]:
    """读该会话的子 Agent 转录。

    主转录里 ``function_call name="Agent"`` 的结果末尾会带 ``[Agent ID: agent-xxxx]``
    （上游实现据此关联）。磁盘上即使没有引用也会列出来，宁多勿少。
    """
    out: list[tuple[str, Transcript]] = []
    known = list(session.subagent_paths)
    referenced = set(session.transcript.subagent_ids) if session.transcript else set()
    ordered = sorted(known, key=lambda item: (item[0] not in referenced, item[0]))
    for agent_id, path in ordered:
        try:
            out.append((agent_id, parse_file(path)))
        except OSError:
            continue
    return out


