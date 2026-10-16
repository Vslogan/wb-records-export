#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""命令行边界：默认值、--json 的 stdout 纯净、--clean 的二次确认。"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wb_records_export.cli import build_parser, main  # noqa: E402
from wb_records_export.selftest import build_fake_home  # noqa: E402


def run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = main(argv)
        except SystemExit as exc:  # argparse 的 --help / 参数错误
            code = int(exc.code or 0)
    return code, out.getvalue(), err.getvalue()


class ParserTest(unittest.TestCase):
    def test_defaults_are_read_only_and_inclusive(self) -> None:
        args = build_parser().parse_args([])
        self.assertFalse(args.include_deleted)
        self.assertFalse(args.no_archived)
        self.assertFalse(args.clean)
        self.assertFalse(args.dry_run)
        self.assertEqual(args.layout, "category")
        self.assertEqual(args.archive_layout, "nested")
        self.assertEqual(args.max_tool_output, 4000)
        self.assertEqual(args.out, "workbuddy-export")

    def test_unknown_client_key_is_rejected(self) -> None:
        code, _out, err = run(["--client", "nope"])
        self.assertEqual(code, 2)
        self.assertIn("invalid choice", err)


class SelftestTest(unittest.TestCase):
    def test_selftest_passes(self) -> None:
        code, out, _err = run(["--selftest"])
        self.assertEqual(code, 0)
        self.assertIn("自检通过", out)


class JsonStdoutTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = tempfile.mkdtemp(prefix="wb-cli-")
        cls.home = build_fake_home(cls.root, key="wb")

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_json_output_is_clean(self) -> None:
        out_dir = os.path.join(self.root, "out-json")
        code, out, err = run(["--home", self.home, "--out", out_dir, "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)  # 进度信息不能混进 stdout
        self.assertEqual(payload["sessions"], 6)
        self.assertTrue(payload["manifest"])
        self.assertIn("导出完成", err)

    def test_dry_run_writes_nothing(self) -> None:
        out_dir = os.path.join(self.root, "out-dry")
        code, _out, _err = run(["--home", self.home, "--out", out_dir, "--dry-run",
                                "--quiet"])
        self.assertEqual(code, 0)
        self.assertFalse(os.path.isdir(out_dir))

    def test_clean_requires_explicit_confirmation(self) -> None:
        out_dir = os.path.join(self.root, "out-clean")
        os.makedirs(out_dir, exist_ok=True)
        keep = os.path.join(out_dir, "keep.txt")
        with open(keep, "w", encoding="utf-8") as fh:
            fh.write("不要删我")
        code, _out, err = run(["--home", self.home, "--out", out_dir, "--clean", "--quiet"])
        self.assertEqual(code, 2)
        self.assertTrue(os.path.exists(keep))
        self.assertIn("--yes-i-mean-it", err)

    def test_clean_with_confirmation_removes_old_content(self) -> None:
        out_dir = os.path.join(self.root, "out-clean2")
        os.makedirs(out_dir, exist_ok=True)
        stale = os.path.join(out_dir, "stale.txt")
        with open(stale, "w", encoding="utf-8") as fh:
            fh.write("旧数据")
        code, _out, _err = run(["--home", self.home, "--out", out_dir, "--clean",
                                "--yes-i-mean-it", "--quiet"])
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(stale))
        self.assertTrue(os.path.exists(os.path.join(out_dir, "index.md")))

    def test_probe_lists_schema_only(self) -> None:
        code, out, _err = run(["--home", self.home, "--probe"])
        self.assertEqual(code, 0)
        self.assertIn("sessions 列", out)
        self.assertIn("workspaces", out)
        self.assertNotIn("整理周报", out)


if __name__ == "__main__":
    unittest.main()
