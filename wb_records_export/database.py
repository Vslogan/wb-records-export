#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读读取 ``workbuddy.db``：会话索引、空间表、以及「分类」相关的列发现。

## 为什么列名要「发现」而不是写死

本机实测（2026-10-16 汇总多个第三方实现）：

* 客户端 5.6.x 时 ``sessions`` 表有 **40 列**，比 30 列版本多了
  ``transport / conversation_origin / visibility / group_id / group_title /
  agent_dirty / agent_dirty_at / agent_last_synced / verified_at / unread``。
* 表里带 ``group_id`` / ``group_title`` / ``project_id`` / ``visibility``，
  官方文档的侧边栏是「任务 + 空间（按工作空间分组，空间内还有文件夹）」，
  归档任务的 ``status`` 是 ``archived``。

列名在不同版本间变过（也有厂商改名的风险），所以这里**先读 PRAGMA 再按名字
模式匹配**，并且把每一列原样带进导出结果的 front matter —— 将来多出来的列
不会因为代码没更新就丢掉。

## 只读，且不干扰正在运行的客户端

优先级：``mode=ro`` 直连 → 失败时把 ``db`` + ``-wal`` + ``-shm`` 三件套拷到
临时目录再读（WAL 模式下只拷主库会丢最近的写入）。全程不写客户端任何文件，
因此**不需要退出客户端**。
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from typing import Any, Iterable

from .util import coerce_ms, is_truthy

#: 归档状态：客户端把归档任务的 ``status`` 写成 ``archived``（实测于第三方
#: 阅读器的 WHERE 子句：``lower(COALESCE(status,'')) != 'archived'``）。
ARCHIVED_STATUS = {"archived", "archive", "archiving"}

#: 软删除判定列。``deleted_at`` 用 ``-1`` 表示「未删除」是云侧行的写法，
#: 所以 ``<0`` 一律不算删除。
DELETED_COLUMNS = ("deleted_at", "deleted_time", "removed_at")
DELETED_FLAG_COLUMNS = ("deleted", "is_deleted", "removed")

#: 归档判定列（名单是保守的：只认明显表达「归档」的名字）。
ARCHIVE_COLUMNS = ("archived_at", "archive_at", "archived_time", "archive_time")
ARCHIVE_FLAG_COLUMNS = ("is_archived", "archived", "in_archive")

#: 置顶。客户端 4.24.2 起侧边栏有「置顶」，5.4.5 起置顶状态参与云同步。
PIN_COLUMNS = ("pinned_at", "pin_at", "pinned_time")
PIN_FLAG_COLUMNS = ("pinned", "is_pinned", "starred", "is_starred")

#: 空间（侧边栏「空间」板块）指向列。优先用指向 ``workspaces`` 主键的列，
#: 退而求其次用路径列直接比 cwd。
SPACE_ID_COLUMNS = ("space_id", "workspace_id", "project_id", "space")
SPACE_PATH_COLUMNS = ("workspace_path", "space_path", "project_path")

#: 空间内的「文件夹」/分组。5.6 新增了 group_id / group_title。
GROUP_TITLE_COLUMNS = ("group_title", "group_name", "folder_title", "folder_name",
                       "category_name", "collection_title")
GROUP_ID_COLUMNS = ("group_id", "folder_id", "category_id", "collection_id")

#: 标题候选，顺序即优先级：``custom_title`` 是用户改过的名字，最权威。
TITLE_COLUMNS = ("custom_title", "title", "name", "summary", "topic")

#: 这些列的原值已经在别处渲染过（正文、统计），front matter 里再放一份会重复。
_FRONT_MATTER_SKIP = {"user_id"}

_BLOB_LIMIT = 4096


class DatabaseError(RuntimeError):
    """受控失败：读不到数据库或 schema 不可用。"""


@dataclass(frozen=True)
class Column:
    name: str
    type: str = ""
    notnull: bool = False
    default: Any = None
    pk: bool = False


@dataclass
class SessionRow:
    """``sessions`` 表的一行，附带从列名模式里推导出来的语义字段。"""

    values: dict[str, Any]
    space_name: str = ""
    space_path: str = ""
    group: str = ""
    archived: bool = False
    archived_reason: str = ""
    deleted: bool = False
    pinned: bool = False

    def get(self, *names: str, default: Any = None) -> Any:
        for name in names:
            if name in self.values and self.values[name] not in (None, ""):
                return self.values[name]
        return default

    @property
    def id(self) -> str:
        return str(self.get("id", "session_id", "conversation_id", default="") or "")

    @property
    def cwd(self) -> str:
        return str(self.get("cwd", "work_dir", "workspace_path", default="") or "")

    @property
    def title(self) -> str:
        return str(self.get(*TITLE_COLUMNS, default="") or "")

    @property
    def status(self) -> str:
        return str(self.get("status", "state", default="") or "")

    @property
    def is_playground(self) -> bool:
        return is_truthy(self.get("is_playground", "playground"))

    @property
    def created_ms(self) -> int | None:
        return coerce_ms(self.get("created_at", "created_time", "createdAt"))

    @property
    def updated_ms(self) -> int | None:
        return coerce_ms(self.get("updated_at", "updated_time", "updatedAt"))

    @property
    def last_activity_ms(self) -> int | None:
        return coerce_ms(self.get("last_activity_at", "last_active_at"))

    @property
    def sort_ms(self) -> int | None:
        return self.last_activity_ms or self.updated_ms or self.created_ms

    def front_matter(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, value in self.values.items():
            if name in _FRONT_MATTER_SKIP:
                continue
            if isinstance(value, (bytes, bytearray, memoryview)):
                out[name] = f"<二进制 {len(bytes(value))} 字节>"
            elif isinstance(value, str) and len(value) > _BLOB_LIMIT:
                out[name] = value[:_BLOB_LIMIT] + f"…（共 {len(value)} 字符）"
            else:
                out[name] = value
        return out


@dataclass
class Database:
    """一个 ``workbuddy.db`` 的只读视图。"""

    path: str
    conn: sqlite3.Connection
    tables: list[str]
    columns: list[Column]
    sessions: list[SessionRow] = field(default_factory=list)
    workspaces: list[dict[str, Any]] = field(default_factory=list)
    workspace_id_column: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def close(self) -> None:
        try:
            self.conn.close()
        except sqlite3.Error:
            pass


def _connect(path: str) -> sqlite3.Connection:
    uri = f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _snapshot_copy(path: str, workdir: str) -> str:
    """把 ``db`` 与它的 ``-wal`` / ``-shm`` 拷到临时目录。"""
    os.makedirs(workdir, exist_ok=True)
    target = os.path.join(workdir, os.path.basename(path))
    shutil.copy2(path, target)
    for suffix in ("-wal", "-shm"):
        side = path + suffix
        if os.path.isfile(side):
            # ``-shm`` 拷不走时不影响读：SQLite 会自己重建。
            try:
                shutil.copy2(side, target + suffix)
            except OSError:
                pass
    return target


def open_database(path: str, *, workdir: str | None = None) -> Database:
    """打开数据库（只读）。直连失败就退到快照副本。"""
    if not os.path.isfile(path):
        raise DatabaseError(f"找不到数据库：{path}")
    temp_dir: str | None = None
    try:
        conn = _connect(path)
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
    except sqlite3.Error as exc:
        # 客户端持有排他锁、或者在只读介质上都可能落到这里。
        try:
            temp_dir = tempfile.mkdtemp(prefix="wb-export-db-", dir=workdir)
            snapshot = _snapshot_copy(path, temp_dir)
            conn = _connect(snapshot)
        except (OSError, sqlite3.Error) as inner:
            raise DatabaseError(
                f"无法读取数据库 {path}：{exc}；快照回退也失败：{inner}"
            ) from inner

    try:
        tables = [
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
        ]
        columns = table_columns(conn, "sessions")
        db = Database(path=path, conn=conn, tables=tables, columns=columns)
        if temp_dir:
            db.extra["snapshot_dir"] = temp_dir
        if "sessions" in tables:
            db.sessions = load_sessions(conn, columns)
        db.workspaces, db.workspace_id_column = load_workspaces(conn, tables)
        _resolve_spaces(db)
    except sqlite3.Error as exc:
        # 半截文件、「file is not a database」、损坏的页都落到这里。调用方会
        # 退到「只读磁盘正文」的模式，所以这里必须给出可读原因而不是抛栈。
        try:
            conn.close()
        except sqlite3.Error:
            pass
        raise DatabaseError(f"数据库内容不可读（{path}）：{exc}") from exc
    return db


def table_columns(conn: sqlite3.Connection, table: str) -> list[Column]:
    if not table:
        return []
    try:
        rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    except sqlite3.Error:
        return []
    return [
        Column(
            name=str(row[1]),
            type=str(row[2] or ""),
            notnull=bool(row[3]),
            default=row[4],
            pk=bool(row[5]),
        )
        for row in rows
    ]


def _row_to_dict(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in row.keys():
        value = row[key]
        if isinstance(value, sqlite3.Binary):  # pragma: no cover - 防御性
            value = bytes(value)
        out[str(key)] = value
    return out


def load_sessions(conn: sqlite3.Connection, columns: list[Column] | None = None) -> list[SessionRow]:
    columns = columns if columns is not None else table_columns(conn, "sessions")
    names = {column.name for column in columns}
    rows = conn.execute("SELECT * FROM sessions").fetchall()
    sessions: list[SessionRow] = []
    for row in rows:
        values = _row_to_dict(conn, row)
        sessions.append(_interpret_session(values, names))
    return sessions


def _interpret_session(values: dict[str, Any], names: set[str]) -> SessionRow:
    row = SessionRow(values=values)

    # ---- 删除 -------------------------------------------------------------
    for column in DELETED_COLUMNS:
        if column in names:
            raw = values.get(column)
            if raw in (None, "", 0, -1, "0", "-1"):
                continue
            if coerce_ms(raw) is None and not isinstance(raw, (int, float)):
                row.deleted = True
                break
            number = raw if isinstance(raw, (int, float)) else coerce_ms(raw)
            if number and number > 0:
                row.deleted = True
                break
    if not row.deleted:
        for column in DELETED_FLAG_COLUMNS:
            if column in names and is_truthy(values.get(column)):
                row.deleted = True
                break

    # ---- 归档 -------------------------------------------------------------
    for column in ARCHIVE_COLUMNS:
        if column in names and coerce_ms(values.get(column)):
            row.archived = True
            row.archived_reason = f"{column} 有值"
            break
    if not row.archived:
        for column in ARCHIVE_FLAG_COLUMNS:
            if column in names and is_truthy(values.get(column)):
                row.archived = True
                row.archived_reason = f"{column} 为真"
                break
    if not row.archived:
        status = str(values.get("status", "") or "").strip().lower()
        if status in ARCHIVED_STATUS:
            row.archived = True
            row.archived_reason = f"status={status}"
    if not row.archived:
        status = str(values.get("state", "") or "").strip().lower()
        if status in ARCHIVED_STATUS:
            row.archived = True
            row.archived_reason = f"state={status}"

    # ---- 置顶 -------------------------------------------------------------
    for column in PIN_COLUMNS:
        if column in names and coerce_ms(values.get(column)):
            row.pinned = True
            break
    if not row.pinned:
        for column in PIN_FLAG_COLUMNS:
            if column in names and is_truthy(values.get(column)):
                row.pinned = True
                break

    # ---- 分组名（空间内的文件夹） -----------------------------------------
    for column in GROUP_TITLE_COLUMNS:
        raw = values.get(column)
        if isinstance(raw, str) and raw.strip():
            row.group = raw.strip()
            break
    if not row.group:
        for column in GROUP_ID_COLUMNS:
            raw = values.get(column)
            if raw not in (None, "", 0, "0"):
                row.group = str(raw)
                break
    return row


def load_workspaces(
    conn: sqlite3.Connection, tables: Iterable[str]
) -> tuple[list[dict[str, Any]], str]:
    """读 ``workspaces`` 表（侧边栏「空间」板块的数据源）。

    表名在不同版本里可能叫 ``workspaces`` / ``workspace`` / ``spaces``，主键列
    可能是 ``id`` / ``path``。这里全部读出来，剩下的交给调用方按列名匹配。
    """
    table = next((t for t in tables if t in ("workspaces", "workspace", "spaces")), "")
    if not table:
        return [], ""
    try:
        rows = conn.execute(f'SELECT * FROM "{table}"').fetchall()
    except sqlite3.Error:
        return [], ""
    items = [_row_to_dict(conn, row) for row in rows]
    id_column = ""
    if items:
        keys = set(items[0].keys())
        for candidate in ("id", "workspace_id", "space_id", "path"):
            if candidate in keys:
                id_column = candidate
                break
    return items, id_column


def _resolve_spaces(db: Database) -> None:
    """把每个会话归到它所属的「空间」。

    三条路径，按可靠性排序：

    1. 会话行里有指向 workspaces 的 id 列（``space_id`` / ``workspace_id`` /
       ``project_id``）→ 用 id 对齐；
    2. 会话行的路径列或 ``cwd`` 与 workspaces 的路径列完全相等 → 用路径对齐；
    3. 都没有 → 归到「没有空间」（客户端侧表现为「任务」板块里的条目）。
    """
    if not db.workspaces:
        return
    by_id: dict[str, dict[str, Any]] = {}
    by_path: dict[str, dict[str, Any]] = {}
    path_keys = ("path", "cwd", "dir", "directory", "workspace_path", "root")
    for item in db.workspaces:
        for key in ("id", "workspace_id", "space_id", "path"):
            raw = item.get(key)
            if raw not in (None, ""):
                by_id.setdefault(str(raw), item)
        for key in path_keys:
            raw = item.get(key)
            if isinstance(raw, str) and raw.strip():
                by_path.setdefault(_norm_path(raw), item)

    for session in db.sessions:
        names = set(session.values.keys())
        matched: dict[str, Any] | None = None
        for column in SPACE_ID_COLUMNS:
            if column in names:
                raw = session.values.get(column)
                if raw not in (None, ""):
                    matched = by_id.get(str(raw))
                    if matched is not None:
                        break
        if matched is None:
            for column in SPACE_PATH_COLUMNS:
                raw = session.values.get(column)
                if isinstance(raw, str) and raw.strip():
                    matched = by_path.get(_norm_path(raw))
                    if matched is not None:
                        break
        if matched is None and session.cwd:
            matched = by_path.get(_norm_path(session.cwd))
        if matched is None:
            continue
        session.space_name = _workspace_label(matched)
        session.space_path = str(
            matched.get("path") or matched.get("cwd") or matched.get("dir") or ""
        )


def _norm_path(path: str) -> str:
    text = str(path).strip().replace("\\", "/")
    while text.endswith("/"):
        text = text[:-1]
    # macOS 的 /Users/x 与 /Users/x/ 等价；大小写不折叠（Linux 区分大小写）。
    return os.path.expanduser(text)


def _workspace_label(item: dict[str, Any]) -> str:
    for key in ("name", "title", "display_name", "label", "custom_title"):
        raw = item.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    path = str(item.get("path") or item.get("cwd") or item.get("dir") or "").strip()
    if path:
        base = os.path.basename(path.rstrip("/\\"))
        return base or path
    return ""


def session_inventory(db: Database) -> str:
    """给 ``--probe`` 用的、可直接贴进 issue 的 schema 摘要（不含任何正文）。"""
    lines = [f"数据库：{db.path}", f"表（{len(db.tables)}）：{'、'.join(db.tables) or '无'}"]
    lines.append(f"sessions 列（{len(db.columns)}）：")
    for column in db.columns:
        default = "" if column.default is None else f" 默认={column.default!r}"
        pk = " PK" if column.pk else ""
        lines.append(f"  - {column.name} {column.type}{pk}{default}")
    if db.workspaces:
        keys = sorted(db.workspaces[0].keys())
        lines.append(f"workspaces 列（{len(keys)}）：{'、'.join(keys)}")
    else:
        lines.append("workspaces：表不存在")
    # 只统计，不回显内容。
    counts: dict[str, int] = {}
    for session in db.sessions:
        key = "已删除" if session.deleted else ("已归档" if session.archived else "正常")
        counts[key] = counts.get(key, 0) + 1
    lines.append(
        "会话计数："
        + "、".join(f"{key} {value}" for key, value in sorted(counts.items()))
        or "会话计数：0"
    )
    return "\n".join(lines)
