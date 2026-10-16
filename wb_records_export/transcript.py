#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对话正文（``projects/<slug>/<id>.jsonl``）的解析与归一化。

## 记录长什么样

WorkBuddy 的正文是**一行一条 JSON**，形状与 Claude Code 的 ``projects/**``
不同（它更像 OpenAI Responses 的 item 流）。本机与公开实现印证到的类型：

    {"type":"message","role":"user","content":[{"type":"input_text","text":"…"}],
     "sessionId":"…","cwd":"…","timestamp":1780000000000,"providerData":{…}}
    {"type":"message","role":"assistant","status":"completed",
     "content":[{"type":"output_text","text":"…"}],
     "providerData":{"messageId":"…","model":"…","rawUsage":{…}}}
    {"type":"reasoning","rawContent":[{"type":"text","text":"…"}]}
    {"type":"function_call","callId":"…","name":"Bash","arguments":"{…}"}
    {"type":"function_call_result","callId":"…","output":[…],"status":"completed"}
    {"type":"summary"…} / {"type":"ai-title"…}         # 标题
    {"type":"system","subtype":"compact_boundary"}     # 上下文压缩边界

同时**保留 Claude Code 形状的兼容路径**（``type":"user"/"assistant"`` +
``message.content`` 里的 ``text/thinking/tool_use/tool_result`` 块）：WorkBuddy
内置的 CLI 与 CodeBuddy CLI 同源，格式在版本间演进过，遇到就按块渲染比丢数据好。

## 三条容易踩的坑

1. **用户输入被包了两层。** 约 97% 的 user 记录把真实输入包在
   ``<system-reminder>`` 里，真正的输入在 ``<user_query>…</user_query>``；
   没有包裹时（子 Agent 转录）回退全文。
2. **思考文本不在 content 里。** ``reasoning`` 记录的 ``content`` 常常是空数组，
   文本在 ``rawContent[].text``。
3. **最后一行可能是半截。** 客户端正在写盘时读到的是不完整 JSON —— 只丢那一行，
   不让整个文件失败（与「不能导出半截内容」是两回事：我们导出的是已落盘的行）。
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable

USER_QUERY_RE = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.S | re.I)
REPLY_QUERY_RE = re.compile(r"<reply_query>\s*(.*?)\s*</reply_query>", re.S | re.I)
REMINDER_RE = re.compile(r"<system-reminder\b[^>]*>.*?</system-reminder>", re.S | re.I)
USER_INFO_RE = re.compile(r"<user_info\b[^>]*>.*?</user_info>", re.S | re.I)
AGENT_ID_RE = re.compile(r"\[Agent ID:\s*(agent-[A-Za-z0-9_-]+)\]", re.I)

#: 只当元信息处理的记录类型（不进对话流，但**保留原文**到附录）。
META_TYPES = {
    "file-history-snapshot",
    "turn-metrics",
    "session-meta",
    "goal-progress",
    "goal-result",
    "agent_started",
    "agent_finished",
    "run_started",
    "run_finished",
    "session_start",
    "session_end",
    "tool-metrics",
    "usage",
}
#: 标题类记录。
TITLE_TYPES = {"summary", "ai-title", "title", "session-title"}

TEXT_BLOCK_TYPES = {"input_text", "output_text", "text", "message", "markdown"}
THINKING_BLOCK_TYPES = {"thinking", "reasoning", "analysis"}

_USAGE_INPUT_KEYS = ("input_tokens", "inputTokens", "prompt_tokens", "cachedMissTokens",
                     "cacheMissTokens")
_USAGE_OUTPUT_KEYS = ("output_tokens", "outputTokens", "completion_tokens")
_USAGE_CACHE_READ_KEYS = ("cache_read_input_tokens", "cacheReadInputTokens", "cacheTokens",
                          "prompt_cache_hit_tokens", "cached_tokens")
_USAGE_CACHE_WRITE_KEYS = ("cache_creation_input_tokens", "cacheCreationInputTokens",
                           "cachedWriteTokens", "prompt_cache_write_tokens")
_USAGE_REASONING_KEYS = ("completion_thinking_tokens", "completionThinkingTokens",
                         "reasoning_tokens")


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------

@dataclass
class Item:
    """归一化后的一条记录。"""

    kind: str                      # user / assistant / reasoning / tool_call /
                                   # tool_result / compact / title / meta / unknown
    line_no: int = 0
    timestamp_ms: int | None = None
    text: str = ""
    role: str = ""
    #: 工具调用相关
    call_id: str = ""
    tool_name: str = ""
    arguments: Any = None
    output: Any = None
    is_error: bool = False
    output_truncated_hint: bool = False
    #: 该条工具结果引用的子 Agent 转录
    subagents: list[str] = field(default_factory=list)
    #: 原始记录，用于附录里的「原文」和未知类型的兜底
    raw: dict[str, Any] = field(default_factory=dict)
    #: 用户消息被识别为纯上下文（system-reminder 包裹但没有真实输入）
    context_only: bool = False
    #: 引用的附件（blob）
    blobs: list[dict[str, Any]] = field(default_factory=list)

    @property
    def kind_label(self) -> str:
        return {
            "user": "用户",
            "assistant": "助手",
            "reasoning": "思考",
            "tool_call": "工具调用",
            "tool_result": "工具结果",
            "compact": "上下文压缩",
            "title": "标题",
            "meta": "元信息",
            "unknown": "未识别记录",
        }.get(self.kind, self.kind)


@dataclass
class Usage:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    reasoning: int = 0

    @property
    def total(self) -> int:
        return self.input + self.output + self.cache_read + self.cache_write + self.reasoning

    def add(self, other: "Usage") -> None:
        self.input += other.input
        self.output += other.output
        self.cache_read += other.cache_read
        self.cache_write += other.cache_write
        self.reasoning += other.reasoning

    def as_dict(self) -> dict[str, int]:
        return {
            "input": self.input,
            "output": self.output,
            "cache_read": self.cache_read,
            "cache_write": self.cache_write,
            "reasoning": self.reasoning,
            "total": self.total,
        }


@dataclass
class Transcript:
    items: list[Item] = field(default_factory=list)
    titles: list[str] = field(default_factory=list)
    session_ids: list[str] = field(default_factory=list)
    cwds: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    counts: Counter = field(default_factory=Counter)
    unknown_types: Counter = field(default_factory=Counter)
    dropped_lines: int = 0
    total_lines: int = 0
    source_path: str = ""
    subagent_ids: list[str] = field(default_factory=list)
    blobs: list[dict[str, Any]] = field(default_factory=list)
    first_ms: int | None = None
    last_ms: int | None = None

    def add(self, item: Item) -> None:
        self.items.append(item)
        self.counts[item.kind] += 1
        if item.timestamp_ms:
            self.first_ms = item.timestamp_ms if self.first_ms is None else min(self.first_ms, item.timestamp_ms)
            self.last_ms = item.timestamp_ms if self.last_ms is None else max(self.last_ms, item.timestamp_ms)

    @property
    def conversation_items(self) -> list[Item]:
        """真正的对话内容（用户 / 助手 / 思考 / 工具）。"""
        return [item for item in self.items if item.kind in
                {"user", "assistant", "reasoning", "tool_call", "compact"}]

    @property
    def visible_items(self) -> list[Item]:
        """默认渲染的条目：对话内容 + 未识别的记录不进正文。"""
        return [item for item in self.items if item.kind in
                {"user", "assistant", "reasoning", "tool_call", "tool_result", "compact"}]


# --------------------------------------------------------------------------
# 单行记录 → Item
# --------------------------------------------------------------------------

def _iter_text_blocks(content: Any) -> Iterable[tuple[str, str, dict]]:
    """把 ``content`` 展开成 ``(种类, 文本, 原始块)``。

    种类：``text`` / ``thinking`` / ``tool_use`` / ``tool_result`` / ``image`` /
    ``other``。
    """
    if content is None:
        return
    if isinstance(content, str):
        if content.strip():
            yield "text", content, {"type": "text", "text": content}
        return
    if isinstance(content, dict):
        nested = content.get("content")
        if nested is not None and "text" not in content:
            yield from _iter_text_blocks(nested)
            return
        text = content.get("text") or content.get("thinking") or content.get("output")
        block_type = str(content.get("type") or "")
        if isinstance(text, str) and text.strip():
            kind = "thinking" if block_type in THINKING_BLOCK_TYPES else "text"
            yield kind, text, content
        return
    if isinstance(content, (list, tuple)):
        for block in content:
            yield from _iter_text_blocks(block)
        return


def _block_kind(block: dict) -> str:
    block_type = str(block.get("type") or "").lower()
    if block_type in TEXT_BLOCK_TYPES:
        return "text"
    if block_type in THINKING_BLOCK_TYPES:
        return "thinking"
    if block_type in ("tool_use", "function_call", "tool_call"):
        return "tool_use"
    if block_type in ("tool_result", "function_call_result"):
        return "tool_result"
    if block_type in ("image", "image_url", "input_image"):
        return "image"
    return "other"


def _blocks_of(content: Any) -> Iterable[dict[str, Any]]:
    """把 ``content`` 展开成一串**内容块字典**（字符串会被包成 text 块）。"""
    if content is None:
        return
    if isinstance(content, str):
        if content.strip():
            yield {"type": "text", "text": content}
        return
    if isinstance(content, dict):
        nested = content.get("content")
        if nested is not None and "text" not in content and "type" not in content:
            yield from _blocks_of(nested)
            return
        yield content
        return
    if isinstance(content, (list, tuple)):
        for entry in content:
            yield from _blocks_of(entry)


def _image_ref(block: dict) -> dict[str, Any] | None:
    ref = _blob_ref(block)
    if ref:
        return ref
    source = block.get("source")
    if isinstance(source, dict):
        return _blob_ref(source)
    url = block.get("image_url") or block.get("url")
    if isinstance(url, str) and url:
        return {"path": url, "mime": "image"}
    return None


def _blob_ref(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    path = value.get("blob_path") or value.get("blobPath")
    if isinstance(path, str) and path:
        ref = {"path": path}
        for key in ("mime", "mime_type", "size"):
            if value.get(key) is not None:
                ref[key] = value[key]
        return ref
    return None


def _collect_blobs(value: Any, out: list[dict[str, Any]], depth: int = 0) -> None:
    if depth > 6 or value is None:
        return
    if isinstance(value, dict):
        ref = _blob_ref(value)
        if ref:
            out.append(ref)
            return
        for nested in value.values():
            _collect_blobs(nested, out, depth + 1)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _collect_blobs(nested, out, depth + 1)


def split_user_text(text: str) -> tuple[str, str]:
    """拆用户消息的「信封」。返回 ``(可读文本, 类别)``。

    类别：``query``（从 ``<user_query>`` 里取到了真实输入）、``plain``（没有信封，
    原文可用）、``context``（整条都是 system-reminder / user_info 之类的上下文，
    没有用户输入）。
    """
    raw = text or ""
    queries = [match.strip() for match in USER_QUERY_RE.findall(raw) if match.strip()]
    if not queries:
        replies = [match.strip() for match in REPLY_QUERY_RE.findall(raw) if match.strip()]
        queries = replies
    if queries:
        return "\n\n".join(queries), "query"
    stripped = REMINDER_RE.sub("", raw)
    stripped = USER_INFO_RE.sub("", stripped).strip()
    if stripped:
        return stripped, "plain"
    return "", "context"


def _usage_from(value: Any) -> Usage | None:
    if not isinstance(value, dict):
        return None
    def pick(keys: tuple[str, ...]) -> int:
        for key in keys:
            raw = value.get(key)
            if isinstance(raw, (int, float)) and raw > 0:
                return int(raw)
        return 0
    usage = Usage(
        input=pick(_USAGE_INPUT_KEYS),
        output=pick(_USAGE_OUTPUT_KEYS),
        cache_read=pick(_USAGE_CACHE_READ_KEYS),
        cache_write=pick(_USAGE_CACHE_WRITE_KEYS),
        reasoning=pick(_USAGE_REASONING_KEYS),
    )
    return usage if usage.total else None


def extract_usage(record: dict) -> Usage | None:
    """从记录的常见位置里取 token 用量。"""
    candidates: list[Any] = []
    provider = record.get("providerData") or record.get("provider_data")
    if isinstance(provider, dict):
        candidates.extend([provider.get("rawUsage"), provider.get("usage")])
    message = record.get("message")
    if isinstance(message, dict):
        candidates.append(message.get("usage"))
    candidates.append(record.get("usage"))
    merged = Usage()
    found = False
    for candidate in candidates:
        usage = _usage_from(candidate)
        if usage:
            merged.add(usage)
            found = True
    return merged if found else None


def _timestamp(record: dict) -> int | None:
    from .util import coerce_ms

    for key in ("timestamp", "created_at", "createdAt", "time", "ts"):
        if key in record:
            value = coerce_ms(record[key])
            if value is not None:
                return value
    return None


def _model_of(record: dict) -> str:
    provider = record.get("providerData")
    if isinstance(provider, dict):
        for key in ("model", "requestModelId", "modelId"):
            raw = provider.get(key)
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
    message = record.get("message")
    if isinstance(message, dict):
        raw = message.get("model")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return ""


def items_from_record(record: dict, line_no: int = 0) -> list[Item]:
    """把一条 JSONL 记录展开成 0..n 个 ``Item``。"""
    if not isinstance(record, dict):
        return [Item(kind="unknown", line_no=line_no, raw={"value": record})]

    line_type = str(record.get("type") or "").strip()
    lowered = line_type.lower()
    timestamp = _timestamp(record)
    out: list[Item] = []

    # ---- 标题 / 摘要 ------------------------------------------------------
    if lowered in TITLE_TYPES:
        text = record.get("summary") or record.get("title") or record.get("text") or ""
        if isinstance(text, str) and text.strip():
            out.append(Item(kind="title", line_no=line_no, timestamp_ms=timestamp,
                            text=text.strip(), raw=record))
            return out
        return [Item(kind="meta", line_no=line_no, timestamp_ms=timestamp, raw=record)]

    # ---- 用户 / 助手消息 --------------------------------------------------
    if lowered == "message" or lowered in ("user", "assistant", "system") and (
        "content" in record or "message" in record
    ):
        role = str(record.get("role") or "").lower()
        message = record.get("message")
        if isinstance(message, dict) and not role:
            role = str(message.get("role") or "").lower()
        if not role:
            role = {"user": "user", "assistant": "assistant"}.get(lowered, "")
        content = record.get("content")
        if content is None and isinstance(message, dict):
            content = message.get("content")

        if role in ("user", "assistant"):
            compact = bool(record.get("isCompactSummary"))
            if compact:
                out.append(Item(kind="compact", line_no=line_no, timestamp_ms=timestamp,
                                text=str(record.get("text") or ""), raw=record))
                return out
            blobs: list[dict[str, Any]] = []
            texts: list[str] = []
            thinking: list[str] = []
            for block in _blocks_of(content):
                block_kind = _block_kind(block)
                if block_kind == "text":
                    text = block.get("text")
                    if isinstance(text, str) and text.strip():
                        texts.append(text)
                elif block_kind == "thinking":
                    text = block.get("thinking") or block.get("text")
                    if isinstance(text, str) and text.strip():
                        thinking.append(text)
                elif block_kind == "tool_use":
                    out.append(_item_from_tool_use(block, line_no, timestamp, record))
                elif block_kind == "tool_result":
                    out.append(_item_from_tool_result(block, line_no, timestamp, record))
                elif block_kind == "image":
                    ref = _image_ref(block)
                    if ref:
                        blobs.append(ref)
                else:
                    # 认不出的内容块：不静默丢，落到附录里的「未识别记录」。
                    out.append(Item(kind="unknown", line_no=line_no, timestamp_ms=timestamp,
                                    raw={"type": f"content_block:{block.get('type', '?')}",
                                         "block": block}))
            _collect_blobs(content, blobs)
            if thinking:
                out.insert(0, Item(kind="reasoning", line_no=line_no, timestamp_ms=timestamp,
                                   text="\n\n".join(thinking).strip(), raw=record))
            text = "\n\n".join(part.strip() for part in texts if part.strip()).strip()
            if not text:
                if out:
                    return out
                return [Item(kind="meta", line_no=line_no, timestamp_ms=timestamp,
                             role=role, raw=record)]
            item = Item(kind=role, line_no=line_no, timestamp_ms=timestamp, role=role,
                        text=text, raw=record, blobs=blobs)
            if role == "user":
                display, mode = split_user_text(text)
                if mode == "context":
                    item.context_only = True
                    item.text = text
                else:
                    item.text = display
                    if mode == "query":
                        # 真实输入是从信封里抽出来的；原文留着，
                        # --with-reminder-context 时会在附录里还原。
                        item.raw["__envelope__"] = text
            usage = extract_usage(record)
            if usage:
                item.raw["__usage__"] = usage.as_dict()
            model = _model_of(record)
            if model:
                item.raw["__model__"] = model
            item.raw["__status__"] = str(record.get("status") or "")
            out.append(item)
            return out

        # system / 其它角色
        subtype = str(record.get("subtype") or "")
        if lowered == "system" and subtype == "compact_boundary":
            return [Item(kind="compact", line_no=line_no, timestamp_ms=timestamp, raw=record)]
        return [Item(kind="meta", line_no=line_no, timestamp_ms=timestamp, raw=record)]

    # ---- 思考 -------------------------------------------------------------
    if lowered in ("reasoning", "thinking"):
        raw_content = record.get("rawContent")
        source = raw_content if isinstance(raw_content, list) and raw_content else record.get("content")
        parts = [text for kind, text, _raw in _iter_text_blocks(source) if kind in ("text", "thinking")]
        text = "\n\n".join(part.strip() for part in parts if part.strip()).strip()
        return [Item(kind="reasoning", line_no=line_no, timestamp_ms=timestamp, text=text,
                     raw=record)]

    # ---- 工具调用 / 结果 --------------------------------------------------
    if lowered in ("function_call", "tool_call", "tool_use"):
        return [_item_from_tool_use(record, line_no, timestamp, record)]
    if lowered in ("function_call_result", "tool_result", "function_result"):
        return [_item_from_tool_result(record, line_no, timestamp, record)]

    # ---- 上下文压缩边界 ---------------------------------------------------
    if lowered == "system" and str(record.get("subtype") or "") == "compact_boundary":
        return [Item(kind="compact", line_no=line_no, timestamp_ms=timestamp, raw=record)]

    # ---- 已知元信息 -------------------------------------------------------
    if lowered in META_TYPES:
        return [Item(kind="meta", line_no=line_no, timestamp_ms=timestamp, raw=record)]

    # ---- 其它：原文留档，不静默丢弃 ---------------------------------------
    return [Item(kind="unknown", line_no=line_no, timestamp_ms=timestamp, raw=record)]


def _tool_output_payload(value: Any) -> Any:
    """工具结果的 ``output`` 可能是字符串、块数组或对象；统一成可渲染的东西。"""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        for key in ("output", "content", "text", "result", "stdout"):
            if key in value:
                return value[key]
    return value


def _item_from_tool_use(block: dict, line_no: int, timestamp: int | None,
                        record: dict) -> Item:
    arguments = block.get("arguments", block.get("input", block.get("args")))
    return Item(
        kind="tool_call",
        line_no=line_no,
        timestamp_ms=timestamp,
        call_id=str(block.get("callId") or block.get("id") or block.get("tool_use_id") or ""),
        tool_name=str(block.get("name") or block.get("tool") or block.get("tool_name") or ""),
        arguments=arguments,
        raw=record,
    )


def _item_from_tool_result(block: dict, line_no: int, timestamp: int | None,
                           record: dict) -> Item:
    payload = block.get("output", block.get("content", block.get("result")))
    payload = _tool_output_payload(payload)
    status = str(block.get("status") or "").lower()
    is_error = bool(block.get("isError") or block.get("is_error")
                    or status in ("failed", "error"))
    blobs: list[dict[str, Any]] = []
    _collect_blobs(payload, blobs)
    text = render_value_text(payload)
    subagents = sorted(set(AGENT_ID_RE.findall(text)))
    return Item(
        kind="tool_result",
        line_no=line_no,
        timestamp_ms=timestamp,
        call_id=str(block.get("callId") or block.get("tool_use_id") or block.get("id") or ""),
        output=payload,
        text=text,
        is_error=is_error,
        subagents=subagents,
        blobs=blobs,
        raw=record,
    )


def render_value_text(value: Any) -> str:
    """把任意 JSON 值渲染成给人看的文本（不丢结构，但优先用 ``text`` 字段）。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    collected: list[str] = []

    def visit(node: Any, depth: int = 0) -> None:
        if depth > 8 or node is None:
            return
        if isinstance(node, str):
            if node.strip():
                collected.append(node)
            return
        if isinstance(node, (int, float, bool)):
            collected.append(str(node))
            return
        if isinstance(node, list):
            for child in node:
                visit(child, depth + 1)
            return
        if isinstance(node, dict):
            ref = _blob_ref(node)
            if ref:
                collected.append(f"（附件 {ref.get('mime', '未知类型')}"
                                 f"{' ' + str(ref['size']) + ' 字节' if ref.get('size') else ''}"
                                 f"：{ref['path']}）")
                return
            for key in ("text", "output", "content", "result", "message", "stdout",
                        "stderr", "error", "value", "data"):
                if key in node:
                    visit(node[key], depth + 1)
                    return
            if depth == 0:
                collected.append(_json_dump(node))
            else:
                for child in node.values():
                    visit(child, depth + 1)

    visit(value)
    text = "\n".join(part for part in collected if part is not None)
    return text.strip()


def _json_dump(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    except (TypeError, ValueError):  # pragma: no cover - 防御性
        return str(value)


# --------------------------------------------------------------------------
# 解析入口
# --------------------------------------------------------------------------

def parse_text(text: str, *, source_path: str = "", keep_unknown: bool = True) -> Transcript:
    transcript = Transcript(source_path=source_path)
    for index, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        transcript.total_lines += 1
        try:
            record = json.loads(line)
        except ValueError:
            # 只丢坏行，不丢文件：客户端正在追加时最后一行天然是半截。
            transcript.dropped_lines += 1
            continue
        if not isinstance(record, dict):
            transcript.dropped_lines += 1
            continue
        line_type = str(record.get("type") or "")
        for item in items_from_record(record, index):
            if item.kind == "unknown":
                transcript.unknown_types[line_type or "<无 type>"] += 1
                if not keep_unknown:
                    continue
            transcript.add(item)
            if item.kind == "title" and item.text:
                transcript.titles.append(item.text)
            if item.kind in ("user", "assistant"):
                usage_raw = item.raw.get("__usage__")
                if isinstance(usage_raw, dict):
                    transcript.usage.add(Usage(
                        input=int(usage_raw.get("input", 0)),
                        output=int(usage_raw.get("output", 0)),
                        cache_read=int(usage_raw.get("cache_read", 0)),
                        cache_write=int(usage_raw.get("cache_write", 0)),
                        reasoning=int(usage_raw.get("reasoning", 0)),
                    ))
                model = str(item.raw.get("__model__") or "")
                if model and model not in transcript.models:
                    transcript.models.append(model)
            for blob in item.blobs:
                if blob not in transcript.blobs:
                    transcript.blobs.append(blob)
        for key in ("sessionId", "session_id", "conversationId", "conversation_id"):
            value = record.get(key)
            if isinstance(value, str) and value and value not in transcript.session_ids:
                transcript.session_ids.append(value)
        cwd = record.get("cwd")
        if isinstance(cwd, str) and cwd and cwd not in transcript.cwds:
            transcript.cwds.append(cwd)
        usage = extract_usage(record) if line_type.lower() == "function_call" else None
        if usage:
            transcript.usage.add(usage)

    for item in transcript.items:
        for agent in item.subagents:
            if agent not in transcript.subagent_ids:
                transcript.subagent_ids.append(agent)
    _dedupe_titles(transcript)
    return transcript


def _dedupe_titles(transcript: Transcript) -> None:
    seen: set[str] = set()
    out: list[str] = []
    for title in transcript.titles:
        key = title.strip()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    transcript.titles = out


def parse_file(path: str, *, tolerate_partial: bool = True) -> Transcript:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        if tolerate_partial:
            text = fh.read()
        else:
            text = fh.read()
    return parse_text(text, source_path=path)


def pair_tools(items: list[Item]) -> tuple[list[Item], dict[str, Item]]:
    """把 ``function_call_result`` 配上它的 ``function_call``。

    返回 ``(顺序条目, {call_id: 结果条目})``：渲染器按顺序输出调用，结果就地
    折叠在调用下面；孤立的结果（没有对应调用）仍会出现在正文里，不丢。
    """
    results: dict[str, Item] = {}
    orphans: list[Item] = []
    for item in items:
        if item.kind == "tool_result" and item.call_id:
            results[item.call_id] = item
        elif item.kind == "tool_result":
            orphans.append(item)
    ordered = [item for item in items if item.kind != "tool_result"] + orphans
    return ordered, results
