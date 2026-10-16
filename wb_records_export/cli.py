#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""命令行入口。

设计取向与参考项目一致：**默认动作是只读导出**，写操作（清空目录）必须显式
要求；参数一律可关可调，因为「导出」的粒度很个人化 —— 有人要完整留档，有人
只要可读的正文。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from . import __version__
from .exporter import ExportOptions, probe, run_export

DEFAULT_OUT = "workbuddy-export"
CLIENT_KEYS = ("wb", "wb_ai", "codebuddy")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="export-records",
        description="把 WorkBuddy / WorkBuddy AI 的全部对话历史导出成结构化 markdown 文档",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python3 export-records.py --out ./workbuddy-export\n"
            "  python3 export-records.py --client wb_ai --out ./ai-export --include-deleted\n"
            "  python3 export-records.py --home ~/.workbuddy --out ./export --copy-attachments\n"
            "  python3 export-records.py --probe          # 只看数据目录与数据库结构\n"
            "  python3 export-records.py --selftest       # 用合成数据自检，不碰你的数据\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"wb-records-export {__version__}")
    parser.add_argument("--out", default=DEFAULT_OUT,
                        help=f"导出目录（默认 ./{DEFAULT_OUT}）")
    parser.add_argument("--home", action="append", default=[],
                        help="显式指定客户端数据目录（可重复）；给了就不再猜候选路径")
    parser.add_argument("--client", action="append", default=[], choices=list(CLIENT_KEYS),
                        help="只导出指定客户端（可重复）：wb / wb_ai / codebuddy")
    parser.add_argument("--include-codebuddy", action="store_true",
                        help="同时导出 ~/.codebuddy（WorkBuddy 内置的同款 CLI）")

    group = parser.add_argument_group("目录结构")
    group.add_argument("--layout", choices=("category", "flat"), default="category",
                       help="category（默认）按空间/任务分类建文件夹；flat 全部平铺")
    group.add_argument("--archive-layout", choices=("nested", "top"), default="nested",
                       help="归档会话放法：nested（默认，留在原分类下的「已归档」）或 top（顶层「已归档」）")
    group.add_argument("--ascii-paths", action="store_true",
                       help="分类目录用英文（spaces/tasks/archived/unlisted/deleted）")
    group.add_argument("--no-group-folders", action="store_true",
                       help="不再为空间内的「分组/文件夹」建子目录（只写进文件头）")
    group.add_argument("--split-clients", action="store_true",
                       help="即使只有一个客户端，也在顶层加一层客户端目录")
    group.add_argument("--max-title-chars", type=int, default=60,
                       help="文件名里标题的最大字符数（默认 60）")

    scope = parser.add_argument_group("导出范围")
    scope.add_argument("--include-deleted", action="store_true",
                       help="连软删除的会话一起导出（默认跳过；deleted_at=-1 视为未删除）")
    scope.add_argument("--no-archived", action="store_true", help="跳过已归档会话")
    scope.add_argument("--limit", type=int, default=None, help="每个客户端最多导出多少条（调试用）")
    scope.add_argument("--copy-attachments", action="store_true",
                       help="把正文引用的附件（blobs/）复制到 _attachments/ 下")
    scope.add_argument("--max-attachment-mb", type=float, default=20.0,
                       help="单个附件的复制上限（MB，默认 20）")

    detail = parser.add_argument_group("内容详略")
    detail.add_argument("--no-thinking", action="store_true", help="不导出思考过程")
    detail.add_argument("--no-tools", action="store_true", help="不导出工具调用与结果")
    detail.add_argument("--no-tool-arguments", action="store_true", help="工具调用只留结果，不留参数")
    detail.add_argument("--no-appendix", action="store_true", help="不导出附录（任务清单/产物/元信息）")
    detail.add_argument("--no-unknown", action="store_true", help="不保留未识别记录类型的原文")
    detail.add_argument("--no-subagents", action="store_true", help="不导出子 Agent 转录")
    detail.add_argument("--with-reminder-context", action="store_true",
                        help="把被跳过的 system-reminder 上下文消息也导出（附录里）")
    detail.add_argument("--max-tool-output", type=int, default=4000,
                        help="单条工具结果的字符上限（默认 4000；0 = 不截断）")
    detail.add_argument("--max-thinking", type=int, default=8000,
                        help="单条思考的字符上限（默认 8000；0 = 不截断）")

    misc = parser.add_argument_group("运行")
    misc.add_argument("--dry-run", action="store_true", help="只显示会写哪些文件，不落盘")
    misc.add_argument("--clean", action="store_true", help="导出前清空输出目录（谨慎）")
    # --clean 的二次确认开关：刻意不出现在 --help 里，只能从错误提示里学到。
    misc.add_argument("--yes-i-mean-it", action="store_true", help=argparse.SUPPRESS)
    misc.add_argument("--quiet", action="store_true", help="只输出错误与最终摘要")
    misc.add_argument("--json", action="store_true",
                      help="把导出摘要以 JSON 打到 stdout（进度信息改走 stderr）")
    misc.add_argument("--probe", action="store_true",
                      help="只打印数据目录与数据库结构（含列名），不导出")
    misc.add_argument("--selftest", action="store_true",
                      help="用临时目录里的合成数据跑一遍完整导出并自检")
    return parser


def options_from_args(args: argparse.Namespace) -> ExportOptions:
    def emit(message: str) -> None:
        # --json 时进度一律走 stderr：stdout 只留那份 JSON，否则调用方
        # `... --json | jq` 会解析失败（参考项目里踩过同一个坑，它在自己的
        # 提交说明里把「--json 日志污染 stdout」列为必须避开的实现坑）。
        if args.quiet:
            return
        print(message, file=sys.stderr if args.json else sys.stdout)

    return ExportOptions(
        out=args.out,
        homes=list(args.home),
        clients=list(args.client),
        layout=args.layout,
        archive_layout=args.archive_layout,
        ascii_paths=args.ascii_paths,
        include_deleted=args.include_deleted,
        include_archived=not args.no_archived,
        include_codebuddy=args.include_codebuddy,
        group_folders=not args.no_group_folders,
        split_clients=args.split_clients,
        include_thinking=not args.no_thinking,
        include_tools=not args.no_tools,
        include_tool_arguments=not args.no_tool_arguments,
        include_appendix=not args.no_appendix,
        include_unknown_records=not args.no_unknown,
        include_reminder_context=args.with_reminder_context,
        include_subagents=not args.no_subagents,
        copy_attachments=args.copy_attachments,
        max_attachment_mb=args.max_attachment_mb,
        max_tool_output=args.max_tool_output,
        max_thinking=args.max_thinking,
        max_title_chars=args.max_title_chars,
        dry_run=args.dry_run,
        limit=args.limit,
        clean=args.clean,
        log=emit,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    options = options_from_args(args)

    if args.selftest:
        from .selftest import run_selftest

        passed, report = run_selftest()
        for line in report:
            print(line)
        return 0 if passed else 1

    if args.probe:
        print(probe(options))
        return 0

    # 导出本身是只读的；只有 --clean 会删东西，先要一次确认。
    if args.clean and not args.dry_run and os.path.isdir(args.out):
        print(f"--clean 会删除 {os.path.abspath(args.out)} 下的现有内容。"
              f"确认请重新执行并加上 --yes-i-mean-it。", file=sys.stderr)
        if "--yes-i-mean-it" not in (argv if argv is not None else sys.argv[1:]):
            return 2
        options.clean = True

    result = run_export(options)

    if args.json:
        summary: dict[str, Any] = {
            "out": result.out,
            "sessions": result.sessions,
            "files": result.files + result.subagent_files,
            "bytes": result.bytes,
            "archived": result.archived,
            "orphans": result.orphans,
            "missing_body": result.missing_body,
            "skipped_deleted": result.skipped_deleted,
            "attachments": result.attachments,
            "unknown_record_types": dict(sorted(result.unknown_types.items())),
            "warnings": result.warnings,
            "manifest": result.manifest_path,
            "clients": result.clients,
        }
        json.dump(summary, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
