#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端：合成客户端数据 → 导出 → 核查目录、内容与安全边界。"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wb_records_export import exporter  # noqa: E402
from wb_records_export.exporter import ExportOptions, run_export  # noqa: E402
from wb_records_export.selftest import build_fake_home  # noqa: E402


def quiet(*_args) -> None:
    return None


class ExportFixture(unittest.TestCase):
    """一次搭好合成 home，多个用例各自导出到不同目录。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.root = tempfile.mkdtemp(prefix="wb-export-test-")
        cls.home = build_fake_home(cls.root, key="wb")

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.root, ignore_errors=True)

    # ---- 辅助 ------------------------------------------------------------
    def export(self, name: str, **over):
        out = os.path.join(self.root, "out-" + name)
        options = ExportOptions(out=out, homes=[self.home], log=quiet, **over)
        result = run_export(options)
        return result, out

    def walk(self, root: str) -> list[str]:
        found: list[str] = []
        for current, _dirs, files in os.walk(root):
            for name in files:
                found.append(os.path.relpath(os.path.join(current, name), root)
                             .replace(os.sep, "/"))
        return sorted(found)


class StructureTest(ExportFixture):
    def test_default_layout(self) -> None:
        result, out = self.export("default")
        files = self.walk(out)
        self.assertEqual(result.sessions, 6)
        self.assertIn("README.md", files)
        self.assertIn("index.md", files)
        self.assertIn("export-manifest.json", files)
        self.assertTrue(any(path.startswith("空间/房子/每周例事/") and path.endswith(".md")
                            for path in files))
        self.assertTrue(any(path.startswith("空间/房子/已归档/") for path in files))
        self.assertTrue(any(path.startswith("任务/") for path in files))
        self.assertTrue(any(path.startswith("未收录/") for path in files))
        self.assertFalse(any("删掉的会话" in path for path in files))

    def test_client_folder_only_when_multiple_clients(self) -> None:
        _result, out = self.export("single")
        self.assertFalse(os.path.isdir(os.path.join(out, "wb")))
        _result, out2 = self.export("split", split_clients=True)
        self.assertTrue(os.path.isdir(os.path.join(out2, "wb")))

    def test_flat_layout(self) -> None:
        _result, out = self.export("flat", layout="flat")
        files = [path for path in self.walk(out) if path.endswith(".md")]
        session_files = [path for path in files
                         if path not in ("README.md", "index.md")
                         and "/subagents/" not in path]
        self.assertTrue(all("/" not in path for path in session_files), session_files)

    def test_archive_top_layout(self) -> None:
        _result, out = self.export("archive-top", archive_layout="top")
        files = self.walk(out)
        self.assertTrue(any(path.startswith("已归档/空间/房子/") for path in files))
        self.assertFalse(any(path.startswith("空间/房子/已归档/") for path in files))

    def test_ascii_paths(self) -> None:
        _result, out = self.export("ascii", ascii_paths=True)
        files = self.walk(out)
        self.assertTrue(any(path.startswith("spaces/") for path in files))
        self.assertTrue(any(path.startswith("tasks/") for path in files))
        self.assertTrue(any(path.startswith("unlisted/") for path in files))

    def test_include_deleted(self) -> None:
        result, out = self.export("deleted", include_deleted=True)
        self.assertEqual(result.sessions, 7)
        self.assertTrue(any(path.startswith("已删除/") for path in self.walk(out)))

    def test_limit(self) -> None:
        result, _out = self.export("limit", limit=2)
        self.assertEqual(result.sessions, 2)

    def test_no_archived(self) -> None:
        result, out = self.export("no-archived", include_archived=False)
        self.assertEqual(result.archived, 0)
        self.assertFalse(any("已归档" in path for path in self.walk(out)))

    def test_dry_run_writes_nothing(self) -> None:
        out = os.path.join(self.root, "out-dry")
        result = run_export(ExportOptions(out=out, homes=[self.home], log=quiet,
                                          dry_run=True))
        self.assertEqual(result.sessions, 6)
        self.assertEqual(os.listdir(out) if os.path.isdir(out) else [], [])

    def test_no_database_still_exports_bodies(self) -> None:
        """数据库丢了也要能从 projects/ 里把正文导出来。"""
        home = build_fake_home(os.path.join(self.root, "nodbe"), key="wb")
        os.remove(os.path.join(home, "workbuddy.db"))
        out = os.path.join(self.root, "out-nodb")
        result = run_export(ExportOptions(out=out, homes=[home], log=quiet))
        self.assertEqual(result.sessions, 6)  # 5 条有正文 + 1 条被删的正文也在磁盘上
        self.assertTrue(all(path.startswith("未收录/") for path in self.walk(out)
                            if path.endswith(".md")
                            and path not in ("README.md", "index.md")))


class ContentTest(ExportFixture):
    def session_file(self, out: str, needle: str) -> str:
        for current, _dirs, files in os.walk(out):
            for name in files:
                path = os.path.join(current, name)
                if needle in name and name.endswith(".md"):
                    return path
        raise AssertionError(f"没有找到含 {needle} 的会话文件")

    def test_front_matter_and_labels(self) -> None:
        _result, out = self.export("content")
        path = self.session_file(out, "整理周报")
        text = open(path, encoding="utf-8").read()
        self.assertIn("space: 房子", text)
        self.assertIn("group: 每周例事", text)
        self.assertIn("category: 空间/房子/每周例事", text)
        self.assertRegex(text, r"client_key: \S+")
        self.assertIn("db_transport: local", text)
        self.assertIn("transcript: projects/", text)

    def test_user_query_extraction_and_noise_removal(self) -> None:
        _result, out = self.export("content2")
        text = open(self.session_file(out, "整理周报"), encoding="utf-8").read()
        self.assertIn("把上周的周报汇总成一份 markdown", text)
        self.assertNotIn("<user_query>", text)
        self.assertNotIn("历史提示词索引见附录", text)

    def test_reminder_context_can_be_included(self) -> None:
        """--with-reminder-context 时，原始用户消息（含 system-reminder）要能还原。"""
        _result, out = self.export("content3", include_reminder_context=True)
        text = open(self.session_file(out, "整理周报"), encoding="utf-8").read()
        self.assertIn("历史提示词索引见附录", text)
        self.assertNotIn("v", "")  # 占位，保持断言数量
        plain = open(self.session_file(out, "临时问题"), encoding="utf-8").read()
        self.assertIn("自动注入的上下文", plain)

    def test_tools_and_thinking_switches(self) -> None:
        _result, out = self.export("content4", include_thinking=False,
                                   include_tools=False)
        text = open(self.session_file(out, "整理周报"), encoding="utf-8").read()
        self.assertNotIn("💭", text)
        self.assertNotIn("🔧", text)
        self.assertIn("把上周的周报汇总成一份 markdown", text)

    def test_tool_output_truncation(self) -> None:
        _result, out = self.export("content5", max_tool_output=10)
        text = open(self.session_file(out, "整理周报"), encoding="utf-8").read()
        self.assertIn("已截断", text)

    def test_unknown_records_are_kept(self) -> None:
        _result, out = self.export("content6")
        text = open(self.session_file(out, "整理周报"), encoding="utf-8").read()
        self.assertIn("brand-new-record-type", text)
        self.assertIn("未识别记录", text)

    def test_index_and_manifest(self) -> None:
        result, out = self.export("content7")
        index = open(os.path.join(out, "index.md"), encoding="utf-8").read()
        self.assertIn("整理周报", index)
        self.assertIn("空间/房子/已归档", index)
        manifest = json.load(open(os.path.join(out, "export-manifest.json"),
                                  encoding="utf-8"))
        self.assertEqual(manifest["stats"]["sessions"], 6)
        self.assertEqual(manifest["stats"]["archived"], 1)
        self.assertEqual(manifest["stats"]["orphans"], 1)
        self.assertEqual(len(manifest["sessions"]), 6)
        self.assertIn("sessions", manifest["schema"][0]["tables"])
        self.assertEqual(manifest["unknown_record_types"], {"brand-new-record-type": 1})
        readme = open(os.path.join(out, "README.md"), encoding="utf-8").read()
        self.assertIn("WorkBuddy 对话历史导出", readme)
        self.assertIn("只读", readme)
        self.assertEqual(result.manifest_path, os.path.join(out, "export-manifest.json"))

    def test_subagents_written_and_linked(self) -> None:
        _result, out = self.export("content8")
        files = self.walk(out)
        self.assertTrue(any(path.endswith("subagents/agent-abcd1234.md") for path in files))
        text = open(self.session_file(out, "整理周报"), encoding="utf-8").read()
        self.assertIn("agent-abcd1234.md", text)
        self.assertIn("子 Agent", text)

    def test_subagents_can_be_skipped(self) -> None:
        result, out = self.export("content9", include_subagents=False)
        self.assertFalse(any("subagents/" in path for path in self.walk(out)))
        self.assertEqual(result.subagent_files, 0)

    def test_attachments_copying(self) -> None:
        result, out = self.export("content10", copy_attachments=True)
        self.assertEqual(result.attachments, 1)
        copied = [path for path in self.walk(out) if path.endswith("cdef.png")]
        self.assertEqual(len(copied), 1)
        text = open(self.session_file(out, "整理周报"), encoding="utf-8").read()
        self.assertIn("_attachments/s-normal/cdef.png", text)

    def test_attachments_not_copied_by_default(self) -> None:
        _result, out = self.export("content11")
        self.assertFalse(any(path.endswith("cdef.png") for path in self.walk(out)))

    def test_half_written_line_is_reported(self) -> None:
        _result, out = self.export("content12")
        path = self.session_file(out, "云端未删除标记")
        text = open(path, encoding="utf-8").read()
        self.assertIn("transcript_dropped_lines: 1", text)
        self.assertIn("不是完整 JSON", text)

    def test_missing_body_is_explained(self) -> None:
        _result, out = self.export("content13")
        path = self.session_file(out, "只有索引没有正文")
        text = open(path, encoding="utf-8").read()
        self.assertIn("磁盘上没有对应正文文件", text)


class SafetyTest(ExportFixture):
    def test_client_home_is_untouched(self) -> None:
        before = {}
        for current, _dirs, files in os.walk(self.home):
            for name in files:
                path = os.path.join(current, name)
                before[path] = (os.path.getsize(path), os.path.getmtime(path))
        self.export("safety")
        after = {}
        for current, _dirs, files in os.walk(self.home):
            for name in files:
                path = os.path.join(current, name)
                after[path] = (os.path.getsize(path), os.path.getmtime(path))
        self.assertEqual(before, after, "导出过程改动了客户端数据目录")
        self.assertFalse(os.path.exists(os.path.join(self.home, "exports")))

    def test_credentials_never_appear_in_output(self) -> None:
        _result, out = self.export("safety2", copy_attachments=True)
        for current, _dirs, files in os.walk(out):
            for name in files:
                with open(os.path.join(current, name), "rb") as fh:
                    self.assertNotIn(b"DO-NOT-EXPORT-SECRET", fh.read(),
                                     f"{name} 里出现了凭据内容")


class MultiClientTest(unittest.TestCase):
    def test_two_homes_get_client_folders(self) -> None:
        root = tempfile.mkdtemp(prefix="wb-export-multi-")
        try:
            a = build_fake_home(root, key="wb")
            b = build_fake_home(root, key="wb_ai")
            out = os.path.join(root, "out")
            result = run_export(ExportOptions(out=out, homes=[a, b], log=quiet))
            self.assertEqual(result.sessions, 12)
            self.assertTrue(os.path.isdir(os.path.join(out, "wb")))
            self.assertTrue(os.path.isdir(os.path.join(out, "wb_ai")))
        finally:
            shutil.rmtree(root, ignore_errors=True)


class ErrorHandlingTest(unittest.TestCase):
    def test_no_clients_found_exits_with_message(self) -> None:
        root = tempfile.mkdtemp(prefix="wb-export-none-")
        try:
            with self.assertRaises(SystemExit):
                run_export(ExportOptions(out=os.path.join(root, "out"),
                                         homes=[os.path.join(root, "missing")], log=quiet))
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_broken_database_falls_back_to_filesystem(self) -> None:
        root = tempfile.mkdtemp(prefix="wb-export-broken-")
        try:
            home = build_fake_home(root, key="wb")
            with open(os.path.join(home, "workbuddy.db"), "wb") as fh:
                fh.write(b"not a database at all")
            out = os.path.join(root, "out")
            result = run_export(ExportOptions(out=out, homes=[home], log=quiet))
            self.assertGreaterEqual(result.sessions, 5)
            self.assertTrue(result.warnings, "应给出可读的告警")
        finally:
            shutil.rmtree(root, ignore_errors=True)


class ProbeTest(unittest.TestCase):
    def test_probe_lists_schema_without_content(self) -> None:
        root = tempfile.mkdtemp(prefix="wb-export-probe-")
        try:
            home = build_fake_home(root, key="wb")
            text = exporter.probe(ExportOptions(out=os.path.join(root, "out"),
                                                homes=[home], log=quiet))
            self.assertIn("sessions 列（40）", text)
            self.assertIn("workspaces", text)
            self.assertIn("group_title", text)
            self.assertNotIn("整理周报", text)
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
