#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""目录结构：分类映射、归档放法、平铺、重名与路径安全。"""

from __future__ import annotations

import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wb_records_export.database import SessionRow  # noqa: E402
from wb_records_export.discover import ClientHome, ClientSpec  # noqa: E402
from wb_records_export.layout import LayoutPlan  # noqa: E402
from wb_records_export.sessions import Session  # noqa: E402

HOME = ClientHome(spec=ClientSpec("wb", "WorkBuddy", ("~/.workbuddy",)),
                  home="/tmp/home", has_db=True, has_projects=True)


def options(**over) -> SimpleNamespace:
    base = dict(layout="category", archive_layout="nested", ascii_paths=False,
                group_folders=True, max_title_chars=60)
    base.update(over)
    return SimpleNamespace(**base)


def session(session_id: str = "s1", *, title: str = "标题", cwd: str = "/home/me/房子",
            space: str = "", group: str = "", archived: bool = False,
            deleted: bool = False, playground: bool = False, has_row: bool = True,
            sort_ms: int = 1_782_864_000_000) -> Session:
    row = None
    if has_row:
        row = SessionRow(values={"id": session_id, "title": title, "cwd": cwd,
                                 "group_title": group or None,
                                 "last_activity_at": sort_ms})
        row.space_name = space
        row.group = group
        row.archived = archived
        row.archived_reason = "status=archived" if archived else ""
        row.deleted = deleted
        row.values["is_playground"] = 1 if playground else 0
    item = Session(client=HOME, session_id=session_id, row=row)
    if not has_row and cwd:
        item.transcript = None
    return item


class CategoryTest(unittest.TestCase):
    def place(self, item, **over):
        plan = LayoutPlan("/out", options(**over))
        return plan.session_placement(item)

    def test_space_session_goes_under_space(self) -> None:
        placement = self.place(session(space="房子"))
        self.assertEqual(placement.parts, ["空间", "房子"])
        self.assertEqual(placement.category, "空间/房子")

    def test_group_creates_subfolder(self) -> None:
        placement = self.place(session(space="房子", group="每周例事"))
        self.assertEqual(placement.parts, ["空间", "房子", "每周例事"])

    def test_group_folder_can_be_disabled(self) -> None:
        placement = self.place(session(space="房子", group="每周例事"),
                               group_folders=False)
        self.assertEqual(placement.parts, ["空间", "房子"])
        self.assertIn("每周例事", placement.category)

    def test_archived_nested_by_default(self) -> None:
        placement = self.place(session(space="房子", archived=True))
        self.assertEqual(placement.parts, ["空间", "房子", "已归档"])
        self.assertTrue(placement.archived)

    def test_archived_top_level_layout(self) -> None:
        placement = self.place(session(space="房子", archived=True),
                               archive_layout="top")
        self.assertEqual(placement.parts, ["已归档", "空间", "房子"])

    def test_playground_goes_to_tasks(self) -> None:
        placement = self.place(session(playground=True, cwd="/home/me/WorkBuddy/_temp"))
        self.assertEqual(placement.parts, ["任务"])

    def test_session_without_space_uses_cwd_folder(self) -> None:
        """库里的 workspaces 行缺失时，用 cwd 目录名兜底 —— 客户端自己也是这么分组的。"""
        placement = self.place(session(cwd="/home/me/proj"))
        self.assertEqual(placement.parts, ["空间", "proj"])

    def test_session_without_cwd_goes_to_tasks(self) -> None:
        placement = self.place(session(cwd=""))
        self.assertEqual(placement.parts, ["任务"])

    def test_ephemeral_workbuddy_dirs_go_to_tasks(self) -> None:
        home = os.path.expanduser("~")
        for cwd in (os.path.join(home, "WorkBuddy"),
                    os.path.join(home, "WorkBuddy", "_temp"),
                    os.path.join(home, "WorkBuddy", "2026-09-16-09-08-41")):
            placement = self.place(session(cwd=cwd))
            self.assertEqual(placement.parts, ["任务"], cwd)

    def test_named_workbuddy_project_is_not_ephemeral(self) -> None:
        placement = self.place(session(cwd=os.path.join(os.path.expanduser("~"),
                                                        "WorkBuddy", "房子")))
        self.assertEqual(placement.parts, ["空间", "房子"])

    def test_orphan_transcript_goes_to_unlisted(self) -> None:
        placement = self.place(session(has_row=False))
        self.assertEqual(placement.parts, ["未收录"])

    def test_deleted_goes_to_own_folder(self) -> None:
        placement = self.place(session(deleted=True, space="房子"))
        self.assertEqual(placement.parts, ["已删除", "房子"])

    def test_flat_layout_has_no_folders(self) -> None:
        placement = self.place(session(space="房子", group="g"), layout="flat")
        self.assertEqual(placement.parts, [])

    def test_ascii_paths(self) -> None:
        placement = self.place(session(space="房子"), ascii_paths=True)
        self.assertEqual(placement.parts, ["spaces", "房子"])
        archived = self.place(session(space="房子", archived=True), ascii_paths=True)
        self.assertEqual(archived.parts, ["spaces", "房子", "archived"])


class FilenameTest(unittest.TestCase):
    def test_filename_contains_date_title_and_short_id(self) -> None:
        placement = LayoutPlan("/out", options()).session_placement(session("abcdef123456"))
        self.assertRegex(placement.filename, r"^\d{4}-\d{2}-\d{2}_标题_abcdef12\.md$")

    def test_duplicate_titles_get_distinct_names(self) -> None:
        """同名标题靠 id 短码区分；万一还撞名，再由去重加后缀。"""
        plan = LayoutPlan("/out", options())
        first = plan.session_placement(session("aaaaaaaa"))
        second = plan.session_placement(session("bbbbbbbb"))
        self.assertNotEqual(first.filename, second.filename)
        self.assertIn("aaaaaaaa", first.filename)
        self.assertIn("bbbbbbbb", second.filename)

    def test_same_id_and_title_are_deduped(self) -> None:
        plan = LayoutPlan("/out", options())
        first = session("aaaaaaaa")
        plan.session_placement(first)
        # 模拟「同一个 id 又出现一次」：清掉缓存后重新分配。
        plan._placements.clear()
        second = plan.session_placement(session("aaaaaaaa"))
        self.assertTrue(second.filename.endswith("-2.md"), second.filename)

    def test_placement_is_stable_across_calls(self) -> None:
        plan = LayoutPlan("/out", options())
        item = session("aaaaaaaa")
        self.assertEqual(plan.session_placement(item).filename,
                         plan.session_placement(item).filename)

    def test_path_separators_in_title_are_sanitised(self) -> None:
        placement = LayoutPlan("/out", options()).session_placement(
            session(title="../../etc/passwd"))
        self.assertNotIn("..", placement.filename)
        self.assertNotIn("/", placement.filename)

    def test_path_separators_in_space_name_are_sanitised(self) -> None:
        placement = LayoutPlan("/out", options()).session_placement(
            session(space="a/../../b"))
        self.assertTrue(all("/" not in part and "\\" not in part for part in placement.parts))
        self.assertNotIn("..", placement.parts)

    def test_absolute_paths_stay_inside_output_root(self) -> None:
        placement = LayoutPlan("/out", options()).session_placement(session(space="房子"))
        self.assertTrue(placement.absolute("/out").startswith("/out/"))

    def test_missing_timestamp_still_produces_name(self) -> None:
        placement = LayoutPlan("/out", options()).session_placement(
            session(sort_ms=None))
        self.assertTrue(placement.filename.startswith("unknown_"))


if __name__ == "__main__":
    unittest.main()
