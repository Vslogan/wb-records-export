#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""``python3 -m wb_records_export`` 入口。"""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
