#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""输出目录结构：把「原有分类」翻译成文件夹。

## 客户端里的分类长什么样

官方文档（workbuddy.cn/docs/workbuddy/Task-Management）写得很直白：左侧边栏

* **任务** —— 最近访问 / 最近更新的任务；
* **空间** —— 按工作空间分组的历史任务，空间里还可以有**文件夹**（分组）；
* **已归档** —— 任务归档后不再出现在列表里，去「系统设置 - 数据管理」查看。

落到数据库就是：

* 空间 = ``workspaces`` 表（``path`` + ``last_opened_at``），会话通过 ``cwd``
  与它对齐（新版另有 ``project_id`` 之类的指向列）；
* 空间内的文件夹 = ``group_id`` / ``group_title``（5.6.x 新增的 10 列之一）；
* 归档 = ``sessions.status = 'archived'``；
* 不在任何空间里的 = ``is_playground``、或 ``cwd`` 落在 ``~/WorkBuddy`` 的临时
  目录（``_temp`` / 时间戳目录）、或库里有行但压根没有 ``cwd`` —— 这些就是
  侧边栏「任务」板块的条目。

所以导出目录默认长这样（``--layout category``）::

    <导出目录>/
      WorkBuddy/                     # 多个客户端同时导出时才加这一层
        空间/<空间名>/[<分组>/]2026-07-01_标题_ab12cd34.md
        空间/<空间名>/[<分组>/]已归档/…
        任务/[<分组>/]…
        未收录/…                      # 磁盘有正文、数据库没行
        已删除/…                      # 需 --include-deleted
        _attachments/<会话 id>/…      # 需 --copy-attachments

归档的放法有两种（``--archive-layout``）：``nested``（默认，留在原分类下）与
``top``（统一放到顶层 ``已归档/``）。``--layout flat`` 则全部平铺到一个目录里。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from .sessions import Session
from .util import dedupe_name, safe_component, session_filename

#: 目录名（中文默认 / ASCII 可选）。用中文是因为界面本身就是中文，导出结果
#: 应该和用户看到的分类名对得上；ASCII 模式给跨平台脚本或不喜欢中文路径的人。
NAMES = {
    "zh": {
        "spaces": "空间",
        "tasks": "任务",
        "archived": "已归档",
        "unlisted": "未收录",
        "deleted": "已删除",
        "attachments": "_attachments",
    },
    "ascii": {
        "spaces": "spaces",
        "tasks": "tasks",
        "archived": "archived",
        "unlisted": "unlisted",
        "deleted": "deleted",
        "attachments": "_attachments",
    },
}


@dataclass
class Placement:
    """一个会话该写到哪里。"""

    parts: list[str]              # 相对导出目录的各级目录名
    filename: str
    category: str = ""            # 给人看的分类串，进 front matter 与索引
    archived: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def relative_path(self) -> str:
        return "/".join([*self.parts, self.filename])

    def absolute(self, root: str) -> str:
        return os.path.join(root, *self.parts, self.filename)


class LayoutPlan:
    """负责分配路径并**保证不重名**。

    重名要防两层：同一目录里两条会话的标题可能一模一样（「新建任务」在真实
    数据里成片出现），所以文件名带 id 短码 + 目录级去重；不同会话的**目录**也
    可能撞名（两个空间同 basename），所以目录名统一走 ``safe_component``。
    """

    def __init__(self, root: str, options: Any,
                 client_prefix: list[str] | None = None) -> None:
        self.root = root
        self.options = options
        #: 顶层客户端目录（多客户端导出时才有）。显式传入而不是让调用方去改
        #: options 上的临时属性 —— 那种写法在多个 LayoutPlan 并存时会互相干扰。
        self.client_prefix = list(client_prefix or [])
        self.names = NAMES["ascii" if getattr(options, "ascii_paths", False) else "zh"]
        self._used: dict[str, set[str]] = {}
        #: 同一个会话会被问很多次路径（算分类、复制附件、渲染正文、写索引）。
        #: 不缓存的话每次调用都会占用一个文件名配额，导出结果里就会出现
        #: ``标题.md-2`` ``标题.md-3`` 这种自相残杀的名字。
        self._placements: dict[str, Placement] = {}

    # ---- 目录名 ----------------------------------------------------------
    def _dir_key(self, parts: list[str]) -> str:
        return "/".join(parts)

    def session_placement(self, session: Session) -> Placement:
        cached = self._placements.get(session.session_id)
        if cached is not None:
            return cached
        parts, category, notes = self._category_parts(session)
        archive_note = session.archived_reason
        stem = session_filename(
            session.title,
            session.session_id,
            session.sort_ms,
            max_title=int(getattr(self.options, "max_title_chars", 60)),
        )
        key = self._dir_key(parts)
        used = self._used.setdefault(key, set())
        # 去重加在**扩展名之前**：``标题_ab12cd34-2.md`` 比 ``标题_ab12cd34.md-2``
        # 更像个人名，也能被 *.md 通配符正确匹配。
        filename = f"{dedupe_name(stem, used)}.md"
        placement = Placement(parts=parts, filename=filename, category=category,
                              archived=session.archived,
                              notes=notes + ([archive_note] if archive_note else []))
        self._placements[session.session_id] = placement
        return placement

    def _category_parts(self, session: Session) -> tuple[list[str], str, list[str]]:
        names = self.names
        notes: list[str] = []
        layout = str(getattr(self.options, "layout", "category") or "category")
        archive_layout = str(getattr(self.options, "archive_layout", "nested") or "nested")
        group_depth = bool(getattr(self.options, "group_folders", True))
        client_parts: list[str] = list(self.client_prefix)

        if layout == "flat":
            return client_parts, "平铺", notes

        if session.deleted:
            root = [names["deleted"], session.space_name or names["tasks"]]
            label = f"{names['deleted']}/{session.space_name or names['tasks']}"
        elif session.row is None:
            root = [names["unlisted"]]
            label = names["unlisted"]
        elif session.is_playground or session.is_ephemeral or not session.space_name:
            # 「任务」板块：客户端自动开的临时任务、以及库里有行但没有工作目录的会话。
            root = [names["tasks"]]
            label = names["tasks"]
        else:
            space = safe_component(session.space_name, fallback="未命名空间")
            root = [names["spaces"], space]
            label = f"{names['spaces']}/{space}"

        if session.group:
            # 分组名永远写进分类串（它在客户端里是真实存在的层级），
            # 只有 --no-group-folders 时才不为它单独建目录。
            group = safe_component(str(session.group), fallback="未命名分组")
            label = f"{label}/{group}"
            if group_depth:
                root = [*root, group]

        parts = [*client_parts, *root]
        if session.archived and archive_layout == "top" and not session.deleted:
            parts = [*client_parts, names["archived"], *root]
            label = f"{names['archived']}/{label}"
        elif session.archived and archive_layout == "nested" and not session.deleted:
            parts = [*parts, names["archived"]]
            label = f"{label}/{names['archived']}"
        return parts, label, notes
