#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通用工具：时间、路径、文件名、文本处理。

只依赖标准库。这里放的都是「口径」类的东西 —— 时间戳怎么认、文件名怎么净化、
代码块围栏怎么选 —— 它们会决定导出结果的形态，所以单独成模块，便于测试。
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Iterable

# --------------------------------------------------------------------------
# 时间
# --------------------------------------------------------------------------

#: 大于这个数就认为是毫秒（1970 年起的秒数到 2026 年约 1.8e9，毫秒约 1.8e12）。
_MS_THRESHOLD = 100_000_000_000


def coerce_ms(value: Any) -> int | None:
    """把各种时间写法统一成 Unix 毫秒。

    客户端在不同表、不同记录里混用秒、毫秒和 ISO 字符串（``created_at`` 是
    epoch 毫秒，日志里的 ``lastHeartbeat`` 也是毫秒，但早期字段出现过 ISO）。
    认不出来返回 ``None``，**不猜**：猜错会让会话排到 1970 年去。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number <= 0:
            return None
        if number >= _MS_THRESHOLD:
            return int(number)
        # 早期客户端写过 10 位秒级时间戳；小于 1e11 且大于 1e8 视为秒。
        if number >= 1e8:
            return int(number * 1000)
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit():
            return coerce_ms(int(text))
        iso = text.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(iso)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp() * 1000)
    return None


def iso_local(ms: int | None) -> str:
    """转成本地时区的 ``YYYY-MM-DD HH:MM:SS``，给正文里的人看。"""
    if ms is None:
        return "—"
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")


def iso_date(ms: int | None) -> str:
    if ms is None:
        return "unknown"
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d")


def iso_utc(ms: int | None) -> str:
    """给 front matter 用的、带时区偏移的 ISO 8601。"""
    if ms is None:
        return ""
    return datetime.fromtimestamp(ms / 1000).astimezone().isoformat(timespec="seconds")


def now_ms() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)


# --------------------------------------------------------------------------
# 路径与文件名
# --------------------------------------------------------------------------

#: Windows 上不能做文件名的保留字。导出目录可能被拷到 Windows 上，所以一律避开。
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

#: 除路径分隔符与控制字符外，``#`` 与 ``?`` 也一并避开：它们会截断 markdown
#: 链接（``#`` 是片段、``?`` 是查询串），而导出结果就是给 markdown 阅读器看的。
_ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*#\x00-\x1f\x7f]')
_WHITESPACE = re.compile(r"\s+")


def safe_component(name: str, *, fallback: str = "未命名", max_len: int = 80) -> str:
    """把一个字符串净化成安全的**单层**路径片段（目录名或文件名主干）。

    - 去掉路径分隔符与控制字符，避免 ``../`` 之类的目录穿越；
    - 折叠空白为下划线，首尾去点和空格（Windows 不允许结尾空格/点）；
    - 超长截断（按字符数，不切坏中文）；
    - 命中 Windows 保留字时加后缀。
    """
    text = unicodedata.normalize("NFC", str(name or ""))
    text = _ILLEGAL_CHARS.sub("_", text)
    text = _WHITESPACE.sub("_", text).strip(" ._")
    text = text.strip(".")
    if len(text) > max_len:
        text = text[:max_len].rstrip(" ._")
    if not text:
        text = fallback
    if text.split(".")[0].upper() in _WINDOWS_RESERVED:
        text = f"{text}_"
    return text


def session_filename(title: str, session_id: str, date_ms: int | None,
                     *, max_title: int = 60) -> str:
    """``2026-07-01_标题_ab12cd34.md`` —— 按日期可排序、人可读、id 可回溯。"""
    stem = safe_component(title, fallback="无标题", max_len=max_title)
    short = re.sub(r"[^0-9A-Za-z_-]", "", session_id)[:8] or "session"
    return f"{iso_date(date_ms)}_{stem}_{short}"


def dedupe_name(name: str, used: set[str], *, suffix: int = 2) -> str:
    """同名时追加 ``-2``、``-3`` …… 保持字母序稳定。

    标题重复在真实数据里很常见（「新建任务」出现过几十次），不加这一步会静默
    互相覆盖 —— 那是导出工具最不可原谅的一种失败。
    """
    if name not in used:
        used.add(name)
        return name
    candidate = f"{name}-{suffix}"
    while candidate in used:
        suffix += 1
        candidate = f"{name}-{suffix}"
    used.add(candidate)
    return candidate


def shorten_path(path: str, *, limit: int = 240) -> str:
    """把过长的**绝对路径**中段省略，避免 markdown 表格被撑爆。"""
    if len(path) <= limit:
        return path
    keep = max(10, (limit - 3) // 2)
    return f"{path[:keep]}...{path[-keep:]}"


def ensure_within(root: str, path: str) -> str:
    """确认 ``path`` 落在 ``root`` 内，返回绝对路径；否则抛 ``ValueError``。

    导出目标是用户目录，任何拼错的分段都可能把文件写到别处，所以在写盘前
    统一过一遍这个断言。
    """
    root_abs = os.path.abspath(root)
    target = os.path.abspath(path)
    if target != root_abs and not target.startswith(root_abs + os.sep):
        raise ValueError(f"拒绝写出导出目录之外：{target}")
    return target


def atomic_write_text(path: str, text: str) -> None:
    """先写临时文件再 ``os.replace``：中断时不会留下半截 markdown。"""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, path)


def rel_posix(root: str, path: str) -> str:
    return os.path.relpath(path, root).replace(os.sep, "/")


# --------------------------------------------------------------------------
# 文本
# --------------------------------------------------------------------------

def human_bytes(size: float) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)}B" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024.0
    return f"{value:.1f}GB"


def fence_for(text: str, *, preferred: str = "```") -> str:
    """选一个不会与正文冲突的围栏。

    工具输出里带 ``` 是常态（模型经常被要求输出 markdown），用固定三反引号
    会把导出文件切碎。按正文里最长的一段反引号加一，符合 CommonMark 的规则。
    """
    longest = 0
    for match in re.finditer(r"`{3,}", text or ""):
        longest = max(longest, len(match.group(0)))
    needed = max(len(preferred), longest + 1)
    return "`" * needed


def truncate(text: str, limit: int) -> tuple[str, bool]:
    """截断长文本（默认用于工具结果），返回 ``(文本, 是否被截断)``。"""
    if limit <= 0 or len(text) <= limit:
        return text, False
    head = text[: limit].rstrip()
    return f"{head}\n\n…（已截断，原长 {len(text)} 字符；用 --max-tool-output 0 导出全文）", True


def collapse_blank_lines(text: str, *, max_blank: int = 1) -> str:
    out: list[str] = []
    blanks = 0
    for line in text.split("\n"):
        if line.strip():
            blanks = 0
            out.append(line)
        else:
            blanks += 1
            if blanks <= max_blank:
                out.append("")
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out)


def yaml_scalar(value: Any) -> str:
    """把任意值写成一个安全的 YAML 标量（只使用会被任何解析器接受的形式）。"""
    if value is None:
        return '""'
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if text == "" or text.strip() != text or re.search(r'[:#\n"\'\[\]{}&*!|>%@`]', text) \
            or text.lower() in {"true", "false", "null", "yes", "no", "~"} \
            or re.fullmatch(r"-?\d+(\.\d+)?", text):
        return json.dumps(text, ensure_ascii=False)
    return text


def yaml_front_matter(mapping: dict[str, Any]) -> str:
    lines = ["---"]
    for key, value in mapping.items():
        if value is None or value == "" or value == [] or value == {}:
            continue
        if isinstance(value, (list, tuple)):
            rendered = ", ".join(yaml_scalar(item) for item in value)
            lines.append(f"{key}: [{rendered}]")
        else:
            lines.append(f"{key}: {yaml_scalar(value)}")
    lines.append("---")
    return "\n".join(lines)


def markdown_table(headers: Iterable[str], rows: Iterable[Iterable[Any]]) -> str:
    head = list(headers)
    body = [[("" if cell is None else str(cell)) for cell in row] for row in rows]
    widths = [len(h) for h in head]
    for row in body:
        for index, cell in enumerate(row):
            if index < len(widths):
                widths[index] = max(widths[index], len(cell))
    def render(cells: list[str]) -> str:
        padded = [
            cell + " " * (widths[index] - len(cell)) if index < len(widths) else cell
            for index, cell in enumerate(cells)
        ]
        return "| " + " | ".join(padded) + " |"
    out = [render(head), "| " + " | ".join("-" * width for width in widths) + " |"]
    out.extend(render(row) for row in body)
    return "\n".join(out)


def escape_table_cell(text: str) -> str:
    return (text or "").replace("|", "\\|").replace("\n", " ").strip()


def is_truthy(value: Any) -> bool:
    """SQLite 里布尔是 0/1，也可能被写成 'true'/'yes'/'1'。"""
    if value is None or value is False:
        return False
    if value is True:
        return True
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    return text not in {"", "0", "false", "no", "none", "null", "-1"}


def load_json(path: str) -> Any | None:
    """宽容地读一个 JSON 文件；坏文件返回 ``None`` 而不是抛异常。

    客户端写文件时被强杀会留下半截 JSON。一个坏文件不该让整个导出失败。
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


