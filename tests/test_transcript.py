#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""正文解析：两种记录方言、用户输入信封、工具配对、坏行容错。"""

from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wb_records_export.transcript import (  # noqa: E402
    pair_tools,
    parse_text,
    render_value_text,
    split_user_text,
)


def line(record: dict) -> str:
    return json.dumps(record, ensure_ascii=False)


class WorkBuddyDialectTest(unittest.TestCase):
    def test_user_message_and_user_query_extraction(self) -> None:
        text = line({
            "type": "message", "role": "user",
            "content": [{"type": "input_text",
                         "text": "<system-reminder>噪音</system-reminder>"
                                 "<user_query>真正的问题</user_query>"}],
            "sessionId": "s1", "cwd": "/tmp", "timestamp": 1_780_000_000_000,
        })
        transcript = parse_text(text)
        users = [item for item in transcript.items if item.kind == "user"]
        self.assertEqual(len(users), 1)
        self.assertEqual(users[0].text, "真正的问题")
        self.assertFalse(users[0].context_only)
        self.assertEqual(transcript.session_ids, ["s1"])
        self.assertEqual(transcript.cwds, ["/tmp"])

    def test_context_only_message_is_flagged(self) -> None:
        text = line({
            "type": "message", "role": "user",
            "content": [{"type": "input_text",
                         "text": "<system-reminder>只有上下文</system-reminder>"}],
            "timestamp": 1_780_000_000_000,
        })
        transcript = parse_text(text)
        self.assertTrue(transcript.items[0].context_only)

    def test_plain_user_message_without_envelope(self) -> None:
        text = line({"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "直接输入"}],
                     "timestamp": 1})
        transcript = parse_text(text)
        self.assertEqual(transcript.items[0].text, "直接输入")
        self.assertEqual(transcript.items[0].kind, "user")

    def test_string_content_is_accepted(self) -> None:
        transcript = parse_text(line({"type": "message", "role": "assistant",
                                      "content": "字符串正文", "timestamp": 1}))
        self.assertEqual(transcript.items[0].text, "字符串正文")

    def test_reasoning_prefers_raw_content(self) -> None:
        text = line({"type": "reasoning", "content": [],
                     "rawContent": [{"type": "text", "text": "思考文本"}],
                     "timestamp": 2})
        transcript = parse_text(text)
        self.assertEqual(transcript.counts["reasoning"], 1)
        self.assertEqual(transcript.items[0].text, "思考文本")

    def test_tool_call_and_result_pairing(self) -> None:
        records = [
            {"type": "function_call", "callId": "c1", "name": "Bash",
             "arguments": json.dumps({"command": "ls"}), "timestamp": 1},
            {"type": "function_call_result", "callId": "c1",
             "output": [{"type": "text", "text": "ok"}], "status": "completed",
             "timestamp": 2},
        ]
        transcript = parse_text("\n".join(line(record) for record in records))
        ordered, results = pair_tools(transcript.items)
        self.assertEqual([item.kind for item in ordered], ["tool_call"])
        self.assertIn("c1", results)
        self.assertEqual(results["c1"].text, "ok")
        self.assertFalse(results["c1"].is_error)

    def test_failed_tool_result_is_marked(self) -> None:
        transcript = parse_text(line({
            "type": "function_call_result", "callId": "c1", "status": "failed",
            "output": "boom", "timestamp": 3}))
        self.assertTrue(transcript.items[0].is_error)

    def test_is_error_flag_is_marked(self) -> None:
        transcript = parse_text(line({
            "type": "function_call_result", "callId": "c1", "isError": True,
            "output": {"content": "boom"}, "timestamp": 3}))
        self.assertTrue(transcript.items[0].is_error)

    def test_unknown_type_is_preserved(self) -> None:
        transcript = parse_text(line({"type": "future-record", "payload": 1}))
        self.assertEqual(transcript.items[0].kind, "unknown")
        self.assertEqual(transcript.unknown_types["future-record"], 1)
        self.assertEqual(transcript.items[0].raw["payload"], 1)

    def test_meta_types_do_not_pollute_conversation(self) -> None:
        transcript = parse_text(line({"type": "turn-metrics", "durationMs": 5}))
        self.assertEqual(transcript.items[0].kind, "meta")
        self.assertEqual(transcript.conversation_items, [])

    def test_titles_are_collected_and_deduped(self) -> None:
        transcript = parse_text("\n".join([
            line({"type": "summary", "summary": "标题"}),
            line({"type": "ai-title", "title": "标题"}),
            line({"type": "ai-title", "title": "更细的标题"}),
        ]))
        self.assertEqual(transcript.titles, ["标题", "更细的标题"])

    def test_usage_is_aggregated(self) -> None:
        transcript = parse_text("\n".join([
            line({"type": "message", "role": "assistant", "status": "completed",
                  "content": [{"type": "output_text", "text": "a"}],
                  "providerData": {"model": "auto",
                                   "rawUsage": {"prompt_tokens": 100,
                                                "completion_tokens": 20,
                                                "prompt_cache_hit_tokens": 50}},
                  "timestamp": 1}),
            line({"type": "message", "role": "assistant", "status": "completed",
                  "content": [{"type": "output_text", "text": "b"}],
                  "message": {"usage": {"input_tokens": 10, "output_tokens": 5}},
                  "timestamp": 2}),
        ]))
        usage = transcript.usage.as_dict()
        self.assertEqual(usage["input"], 110)
        self.assertEqual(usage["output"], 25)
        self.assertEqual(usage["cache_read"], 50)
        self.assertEqual(transcript.models, ["auto"])

    def test_blob_reference_is_collected(self) -> None:
        transcript = parse_text(line({
            "type": "function_call_result", "callId": "c1", "status": "completed",
            "output": {"blob_path": "blobs/ab/cdef.png", "mime": "image/png", "size": 12},
            "timestamp": 1}))
        self.assertEqual(transcript.blobs[0]["path"], "blobs/ab/cdef.png")
        self.assertIn("附件", transcript.items[0].text)

    def test_subagent_reference_is_collected(self) -> None:
        transcript = parse_text(line({
            "type": "function_call_result", "callId": "c1", "status": "completed",
            "output": [{"type": "text", "text": "完成\n[Agent ID: agent-abcd1234]"}],
            "timestamp": 1}))
        self.assertEqual(transcript.subagent_ids, ["agent-abcd1234"])

    def test_compact_boundary_and_flag(self) -> None:
        transcript = parse_text("\n".join([
            line({"type": "system", "subtype": "compact_boundary", "timestamp": 1}),
            line({"type": "message", "role": "user", "isCompactSummary": True,
                  "text": "summary", "timestamp": 2}),
        ]))
        self.assertEqual(transcript.counts["compact"], 2)

    def test_incomplete_trailing_line_is_dropped_only(self) -> None:
        text = "\n".join([
            line({"type": "message", "role": "user", "content": "完整", "timestamp": 1}),
            '{"type": "message", "role": "assistant", "content": [{"type": "outp',
        ])
        transcript = parse_text(text)
        self.assertEqual(transcript.dropped_lines, 1)
        self.assertEqual(transcript.total_lines, 2)
        self.assertEqual(len(transcript.items), 1)

    def test_empty_file_yields_empty_transcript(self) -> None:
        transcript = parse_text("")
        self.assertEqual(transcript.items, [])
        self.assertEqual(transcript.dropped_lines, 0)


class ClaudeDialectTest(unittest.TestCase):
    """同源 CLI 的另一种记录形状：type=user/assistant + message.content 块。"""

    def test_message_blocks_are_rendered(self) -> None:
        record = {
            "type": "assistant",
            "message": {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "先看看"},
                {"type": "text", "text": "答案是 42"},
                {"type": "tool_use", "id": "t1", "name": "Read",
                 "input": {"file_path": "/etc/hosts"}},
            ]},
            "timestamp": 1_780_000_000_000,
        }
        transcript = parse_text(line(record))
        kinds = [item.kind for item in transcript.items]
        self.assertIn("reasoning", kinds)
        self.assertIn("assistant", kinds)
        self.assertIn("tool_call", kinds)
        call = next(item for item in transcript.items if item.kind == "tool_call")
        self.assertEqual(call.call_id, "t1")
        self.assertEqual(call.tool_name, "Read")

    def test_tool_result_block_is_paired(self) -> None:
        records = [
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]},
             "timestamp": 1},
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "file.txt"}]},
             "timestamp": 2},
        ]
        transcript = parse_text("\n".join(line(record) for record in records))
        ordered, results = pair_tools(transcript.items)
        self.assertIn("t1", results)
        self.assertEqual(results["t1"].text, "file.txt")
        self.assertEqual([item.kind for item in ordered], ["tool_call"])


class UserEnvelopeTest(unittest.TestCase):
    def test_multiple_queries_are_joined(self) -> None:
        text, mode = split_user_text("<user_query>a</user_query>中间<user_query>b</user_query>")
        self.assertEqual(mode, "query")
        self.assertEqual(text, "a\n\nb")

    def test_reply_query_is_supported(self) -> None:
        text, mode = split_user_text("<reply_query>回复</reply_query>")
        self.assertEqual((text, mode), ("回复", "query"))

    def test_context_only(self) -> None:
        text, mode = split_user_text("<system-reminder>x</system-reminder>")
        self.assertEqual((text, mode), ("", "context"))

    def test_leftover_text_survives_stripping(self) -> None:
        text, mode = split_user_text(
            "<system-reminder>x</system-reminder>真正的输入保留")
        self.assertEqual(mode, "plain")
        self.assertEqual(text, "真正的输入保留")


class RenderValueTest(unittest.TestCase):
    def test_nested_text_is_flattened(self) -> None:
        value = [{"type": "text", "text": "第一行"},
                 {"type": "text", "text": "第二行"}]
        self.assertEqual(render_value_text(value), "第一行\n第二行")

    def test_unknown_object_is_dumped_as_json(self) -> None:
        self.assertIn('"a"', render_value_text({"a": 1}))

    def test_scalar_and_none(self) -> None:
        self.assertEqual(render_value_text(None), "")
        self.assertEqual(render_value_text(12), "12")
        self.assertEqual(render_value_text(True), "true")


if __name__ == "__main__":
    unittest.main()
