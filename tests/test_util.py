#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工具层：时间口径、文件名净化、围栏选择、截断。"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wb_records_export.util import (  # noqa: E402
    coerce_ms,
    dedupe_name,
    fence_for,
    iso_local,
    safe_component,
    session_filename,
    truncate,
    yaml_front_matter,
    yaml_scalar,
)


class CoerceMsTest(unittest.TestCase):
    def test_milliseconds_pass_through(self) -> None:
        self.assertEqual(coerce_ms(1_780_000_000_000), 1_780_000_000_000)

    def test_seconds_are_scaled(self) -> None:
        self.assertEqual(coerce_ms(1_780_000_000), 1_780_000_000_000)

    def test_iso_string_is_parsed(self) -> None:
        self.assertEqual(coerce_ms("2026-07-01T00:00:00Z"), 1782864000000)

    def test_numeric_string_is_accepted(self) -> None:
        self.assertEqual(coerce_ms("1780000000000"), 1_780_000_000_000)

    def test_garbage_is_none_not_guess(self) -> None:
        for value in (None, "", "hello", -5, 0, True, {}):
            self.assertIsNone(coerce_ms(value), f"{value!r} 不该被当成时间")

    def test_local_formatting(self) -> None:
        self.assertEqual(iso_local(None), "—")
        self.assertRegex(iso_local(1_780_000_000_000), r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


class SafeComponentTest(unittest.TestCase):
    def test_path_separators_are_removed(self) -> None:
        self.assertNotIn("/", safe_component("../../etc/passwd"))
        self.assertNotIn("\\", safe_component("..\\..\\windows"))

    def test_windows_reserved_names_get_suffix(self) -> None:
        self.assertTrue(safe_component("CON").endswith("_"))
        self.assertTrue(safe_component("com1").endswith("_"))

    def test_empty_becomes_fallback(self) -> None:
        self.assertEqual(safe_component("   ", fallback="未命名"), "未命名")

    def test_long_names_are_truncated(self) -> None:
        self.assertLessEqual(len(safe_component("标题" * 100, max_len=20)), 20)

    def test_chinese_is_preserved(self) -> None:
        self.assertEqual(safe_component("整理周报"), "整理周报")


class FilenameTest(unittest.TestCase):
    def test_filename_shape(self) -> None:
        name = session_filename("整理周报", "0f1e2d3c-aaaa", 1782864000000)
        self.assertRegex(name, r"^\d{4}-\d{2}-\d{2}_整理周报_0f1e2d3c$")

    def test_id_with_illegal_chars_is_cleaned(self) -> None:
        name = session_filename("t", "a/b:c", 1782864000000)
        self.assertNotIn("/", name)
        self.assertNotIn(":", name)

    def test_dedupe_adds_suffix(self) -> None:
        used: set[str] = set()
        self.assertEqual(dedupe_name("a.md", used), "a.md")
        self.assertEqual(dedupe_name("a.md", used), "a.md-2")
        self.assertEqual(dedupe_name("a.md", used), "a.md-3")


class TextHelperTest(unittest.TestCase):
    def test_fence_grows_past_content(self) -> None:
        self.assertEqual(fence_for("no fences"), "```")
        self.assertEqual(fence_for("```\ncode\n```"), "````")

    def test_truncate_reports_original_length(self) -> None:
        text, cut = truncate("x" * 100, 10)
        self.assertTrue(cut)
        self.assertIn("100", text)

    def test_truncate_keeps_short_text(self) -> None:
        text, cut = truncate("short", 100)
        self.assertEqual(text, "short")
        self.assertFalse(cut)

    def test_front_matter_quotes_unsafe_scalars(self) -> None:
        self.assertEqual(yaml_scalar("a: b"), '"a: b"')
        self.assertEqual(yaml_scalar(True), "true")
        rendered = yaml_front_matter({"title": "含: 冒号", "n": 1})
        self.assertIn('title: "含: 冒号"', rendered)
        self.assertIn("n: 1", rendered)
        self.assertNotIn("empty", yaml_front_matter({"empty": "", "none": None}))


if __name__ == "__main__":
    unittest.main()
