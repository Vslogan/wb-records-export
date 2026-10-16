#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""仓库根目录的启动脚本：``python3 export-records.py [参数]``。

之所以留一个根目录脚本，是因为大多数用户是 ``git clone`` 之后直接在仓库里跑的，
不需要先 ``pip install``。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from wb_records_export.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
