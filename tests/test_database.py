#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据库层：只读保证、列发现、「分类」语义的推导。"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wb_records_export.database import (  # noqa: E402
    DatabaseError,
    open_database,
    session_inventory,
)
from wb_records_export.discover import ClientHome, ClientSpec  # noqa: E402
from wb_records_export.selftest import SESSION_COLUMNS  # noqa: E402
from wb_records_export.util import now_ms  # noqa: E402


def make_db(path: str, rows: list[dict], workspaces: list[dict] | None = None,
            columns: tuple[str, ...] = SESSION_COLUMNS) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        'CREATE TABLE sessions (' + ", ".join(f'"{name}"' for name in columns)
        + ", PRIMARY KEY (id))"
    )
    for row in rows:
        names = [name for name in columns if name in row]
        conn.execute(
            f"INSERT INTO sessions ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)})",
            [row[name] for name in names],
        )
    if workspaces is not None:
        conn.execute(
            "CREATE TABLE workspaces (id TEXT PRIMARY KEY, path TEXT, name TEXT, last_opened_at INTEGER)"
        )
        for item in workspaces:
            conn.execute("INSERT INTO workspaces VALUES (?,?,?,?)",
                         (item.get("id"), item["path"], item.get("name"), item.get("last_opened_at")))
    conn.commit()
    conn.close()


class ReadOnlyTest(unittest.TestCase):
    def test_connection_rejects_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "workbuddy.db")
            make_db(path, [{"id": "a", "cwd": "/tmp", "title": "t"}])
            db = open_database(path)
            with self.assertRaises(sqlite3.Error):
                db.conn.execute("UPDATE sessions SET title='x'")
            db.close()

    def test_missing_database_reports_path(self) -> None:
        with self.assertRaises(DatabaseError):
            open_database("/nonexistent/workbuddy.db")


class ColumnDiscoveryTest(unittest.TestCase):
    def test_unknown_columns_are_kept_verbatim(self) -> None:
        columns = tuple(SESSION_COLUMNS) + ("brand_new_column",)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "workbuddy.db")
            row = {name: None for name in columns}
            row.update({"id": "a", "cwd": "/tmp/proj", "title": "t",
                        "brand_new_column": "留着"})
            make_db(path, [row], columns=columns)
            db = open_database(path)
            self.assertIn("brand_new_column", [column.name for column in db.columns])
            self.assertEqual(db.sessions[0].front_matter()["brand_new_column"], "留着")
            db.close()

    def test_minimal_schema_still_works(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "workbuddy.db")
            make_db(path, [{"id": "a", "cwd": "/tmp/proj", "title": "t"}],
                    columns=("id", "cwd", "title"))
            db = open_database(path)
            self.assertEqual(db.sessions[0].id, "a")
            self.assertEqual(db.sessions[0].title, "t")
            self.assertFalse(db.sessions[0].archived)
            db.close()


class ArchiveAndDeleteTest(unittest.TestCase):
    def test_status_archived(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "workbuddy.db")
            make_db(path, [{"id": "a", "cwd": "/tmp", "status": "archived"},
                           {"id": "b", "cwd": "/tmp", "status": "completed"}])
            db = open_database(path)
            by_id = {row.id: row for row in db.sessions}
            self.assertTrue(by_id["a"].archived)
            self.assertFalse(by_id["b"].archived)
            db.close()

    def test_archive_columns_of_other_spellings(self) -> None:
        columns = tuple(SESSION_COLUMNS) + ("archived_at", "is_pinned")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "workbuddy.db")
            row = {name: None for name in columns}
            row.update({"id": "a", "cwd": "/tmp", "archived_at": now_ms(),
                        "is_pinned": 1})
            make_db(path, [row], columns=columns)
            db = open_database(path)
            self.assertTrue(db.sessions[0].archived)
            self.assertTrue(db.sessions[0].pinned)
            db.close()

    def test_deleted_at_minus_one_means_not_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "workbuddy.db")
            make_db(path, [
                {"id": "cloud", "cwd": "/tmp", "deleted_at": -1},
                {"id": "zero", "cwd": "/tmp", "deleted_at": 0},
                {"id": "gone", "cwd": "/tmp", "deleted_at": now_ms()},
            ])
            db = open_database(path)
            flags = {row.id: row.deleted for row in db.sessions}
            self.assertFalse(flags["cloud"])
            self.assertFalse(flags["zero"])
            self.assertTrue(flags["gone"])
            db.close()


class WorkspaceAlignmentTest(unittest.TestCase):
    def test_space_matched_by_cwd_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "workbuddy.db")
            make_db(path, [{"id": "a", "cwd": "/home/me/proj",
                            "title": "t"}],
                    workspaces=[{"id": "w1", "path": "/home/me/proj", "name": "我的项目"}])
            db = open_database(path)
            self.assertEqual(db.sessions[0].space_name, "我的项目")
            self.assertEqual(db.sessions[0].space_path, "/home/me/proj")
            db.close()

    def test_space_matched_by_id_column(self) -> None:
        columns = tuple(SESSION_COLUMNS)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "workbuddy.db")
            make_db(path, [{"id": "a", "cwd": "/elsewhere", "title": "t"}],
                    workspaces=[{"id": "ws-7", "path": "/home/me/other", "name": "另一个空间"}])
            conn = sqlite3.connect(path)
            conn.execute("UPDATE sessions SET project_id='ws-7' WHERE id='a'")
            conn.commit()
            conn.close()
            db = open_database(path)
            self.assertEqual(db.sessions[0].space_name, "另一个空间")
            db.close()

    def test_space_label_falls_back_to_basename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "workbuddy.db")
            make_db(path, [{"id": "a", "cwd": "/home/me/房子", "title": "t"}],
                    workspaces=[{"id": "w1", "path": "/home/me/房子"}])
            db = open_database(path)
            self.assertEqual(db.sessions[0].space_name, "房子")
            db.close()

    def test_no_workspaces_table_is_fine(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "workbuddy.db")
            make_db(path, [{"id": "a", "cwd": "/home/me/proj", "title": "t"}])
            db = open_database(path)
            self.assertEqual(db.workspaces, [])
            self.assertEqual(db.sessions[0].space_name, "")
            db.close()

    def test_group_title_prefers_human_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "workbuddy.db")
            make_db(path, [{"id": "a", "cwd": "/tmp", "group_id": "g1",
                            "group_title": "每周例事"}])
            db = open_database(path)
            self.assertEqual(db.sessions[0].group, "每周例事")
            db.close()

    def test_group_falls_back_to_id(self) -> None:
        """没有 group_title 时退回 group_id，总比丢掉分组信息好。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "workbuddy.db")
            make_db(path, [{"id": "b", "cwd": "/tmp", "group_id": "g1"}])
            db = open_database(path)
            self.assertEqual(db.sessions[0].group, "g1")
            db.close()


class InventoryTest(unittest.TestCase):
    def test_inventory_lists_columns_and_counts_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "workbuddy.db")
            make_db(path, [{"id": "a", "cwd": "/tmp", "title": "机密标题"},
                           {"id": "b", "cwd": "/tmp", "status": "archived"}])
            db = open_database(path)
            text = session_inventory(db)
            self.assertIn("sessions 列", text)
            self.assertIn("已归档", text)
            self.assertNotIn("机密标题", text, "结构摘要不该回显会话内容")
            db.close()


class ClientHomeHelperTest(unittest.TestCase):
    def test_client_home_paths(self) -> None:
        spec = ClientSpec("wb", "WorkBuddy", ("~/.workbuddy",))
        home = ClientHome(spec=spec, home="/tmp/home", has_db=True, has_projects=True)
        self.assertEqual(home.db_path, "/tmp/home/workbuddy.db")
        self.assertEqual(home.projects_dir, "/tmp/home/projects")


if __name__ == "__main__":
    unittest.main()
