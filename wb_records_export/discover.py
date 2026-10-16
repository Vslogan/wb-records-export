#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""客户端数据目录（home）探测。

WorkBuddy 家族的数据目录都是「一个目录装全部」：

    <home>/workbuddy.db                     会话索引（标题、时间、空间、归档状态）
    <home>/projects/<cwd-slug>/<id>.jsonl   对话正文（一行一条记录）
    <home>/projects/<cwd-slug>/<id>/        tool-results/ 与 subagents/
    <home>/tasks/<id>/<n>.json              任务清单
    <home>/artifact-index/<id>.json         产物索引

三个客户端（WorkBuddy / WorkBuddy AI / CodeBuddy CLI）目录布局同构，只是根不同。
本模块只做**发现**，不做解析；路径候选顺序参考 wb-account-sync 的
``tools/wb_platform.py``（macOS 侧 ``~/.workbuddy``、``~/.workbuddy-ai`` 已实测）。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field

DB_NAME = "workbuddy.db"

#: 允许用户用环境变量显式指定数据根。WorkBuddy 客户端自带的 CLI 认
#: ``WORKBUDDY_CONFIG_DIR`` / ``CODEBUDDY_CONFIG_DIR``，这里沿用同一组变量名，
#: 免得同一个目录在两套工具里要用两种方式指。
ENV_HOME_VARS = ("WORKBUDDY_CONFIG_DIR", "CODEBUDDY_CONFIG_DIR", "WORKBUDDY_HOME")


@dataclass(frozen=True)
class ClientSpec:
    key: str
    display: str
    home_candidates: tuple[str, ...]
    note: str = ""


#: 候选顺序 = 探测顺序。``~/.workbuddy`` 与 ``~/.workbuddy-ai`` 是 macOS 实测值；
#: Windows 侧是推断值（未在真机验证），所以候选里同时给 ``%APPDATA%`` 形式。
CLIENTS: tuple[ClientSpec, ...] = (
    ClientSpec(
        key="wb",
        display="WorkBuddy",
        home_candidates=("~/.workbuddy", "%APPDATA%/WorkBuddy", "%APPDATA%/workbuddy"),
        note="WorkBuddy 桌面客户端",
    ),
    ClientSpec(
        key="wb_ai",
        display="WorkBuddy AI",
        home_candidates=("~/.workbuddy-ai", "%APPDATA%/WorkBuddy AI", "%APPDATA%/workbuddy-ai"),
        note="WorkBuddy AI 客户端（与 WorkBuddy 数据互相隔离）",
    ),
    ClientSpec(
        key="codebuddy",
        display="CodeBuddy CLI",
        home_candidates=("~/.codebuddy", "%APPDATA%/codebuddy"),
        note="WorkBuddy 内置的同款 CLI；只有你直接用过它才会有内容",
    ),
)

CLIENTS_BY_KEY = {spec.key: spec for spec in CLIENTS}


def expand(path: str) -> str:
    return os.path.abspath(os.path.expanduser(os.path.expandvars(path)))


@dataclass
class ClientHome:
    """一个可用的客户端数据目录。"""

    spec: ClientSpec
    home: str
    has_db: bool
    has_projects: bool
    #: 目录里出现了 workbuddy.db 才算「确认」；只有目录存在时留个记号，让
    #: 调用方能提示「这个目录看起来不像客户端数据目录」而不是静默导出空结果。
    confirmed: bool = False
    source: str = "candidate"
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return self.spec.key

    @property
    def display(self) -> str:
        return self.spec.display

    @property
    def folder_name(self) -> str:
        """在导出目录里代表这个客户端的文件夹名。

        已知客户端用 key（``wb`` / ``wb_ai`` / ``codebuddy``，跨平台安全且稳定）；
        用户用 ``--home`` 指过来的自定义目录则用目录名 —— 那时候「custom」
        对用户没有任何信息量，目录名才能让他对上号。
        """
        from .util import safe_component

        known = {spec.key for spec in CLIENTS}
        if self.spec.key in known:
            return self.spec.key
        return safe_component(self.display or self.spec.key, fallback="client",
                              max_len=40)

    @property
    def db_path(self) -> str:
        return os.path.join(self.home, DB_NAME)

    @property
    def projects_dir(self) -> str:
        return os.path.join(self.home, "projects")


def _looks_like_home(path: str) -> tuple[bool, bool]:
    return (
        os.path.isfile(os.path.join(path, DB_NAME)),
        os.path.isdir(os.path.join(path, "projects")),
    )


def _spec_for_path(path: str) -> ClientSpec:
    """给显式指定的目录认领身份：能对上候选路径就沿用客户端的显示名与 key。

    认不出来就退成「自定义」，显示名取目录名 —— 用户显式指了一个目录时，
    界面里出现这个目录的名字比出现一个笼统的 "custom" 更容易核对。
    """
    for spec in CLIENTS:
        for candidate in spec.home_candidates:
            if expand(candidate) == path:
                return spec
    return ClientSpec(
        key="custom",
        display=os.path.basename(path.rstrip(os.sep)) or path,
        home_candidates=(path,),
        note="--home 显式指定",
    )


def _env_home() -> str | None:
    for name in ENV_HOME_VARS:
        value = os.environ.get(name, "").strip()
        if value:
            return expand(value)
    return None


def detect_clients(
    *,
    include_codebuddy: bool = False,
    explicit: list[str] | None = None,
    keys: list[str] | None = None,
) -> list[ClientHome]:
    """找出所有存在内容的客户端数据目录。

    ``explicit`` 是用户直接给的目录（``--home``），此时不再猜候选路径：
    用户给了就是权威，即使目录结构不像客户端也照样用它（`require` 检查会在
    读数据库时给出明确报错）。
    """
    homes: list[ClientHome] = []

    if explicit:
        used_keys: dict[str, int] = {}
        for raw in explicit:
            path = expand(raw)
            has_db, has_projects = _looks_like_home(path)
            spec = _spec_for_path(path)
            # 同一个 key 出现两次时（例如两次 --home 都指向自定义目录），
            # 必须给出互不相同的 key：导出层用 key 区分目录，撞了就会把两个
            # 客户端的会话混进同一个文件夹。
            count = used_keys.get(spec.key, 0) + 1
            used_keys[spec.key] = count
            if count > 1:
                # 只改 key（内部主键），显示名保持目录名 —— 导出目录名的去重
                # 由导出层统一处理，这里加 "(2)" 只会让用户对不上号。
                spec = ClientSpec(f"{spec.key}-{count}", spec.display,
                                  spec.home_candidates, spec.note)
            homes.append(
                ClientHome(
                    spec=spec,
                    home=path,
                    has_db=has_db,
                    has_projects=has_projects,
                    confirmed=has_db,
                    source="--home",
                )
            )
        return homes

    env_home = _env_home()
    wanted = set(keys) if keys else None

    for spec in CLIENTS:
        if wanted is not None and spec.key not in wanted:
            continue
        if spec.key == "codebuddy" and not include_codebuddy and wanted is None:
            # 默认不导出 CodeBuddy CLI 的会话：它和 WorkBuddy 是同一套格式但
            # 目录不同，对大多数用户是重复数据。想一起导就用 --client codebuddy。
            continue
        candidates = list(spec.home_candidates)
        if env_home and spec.key == "wb":
            candidates.insert(0, env_home)
        picked: ClientHome | None = None
        for index, candidate in enumerate(candidates):
            path = expand(candidate)
            has_db, has_projects = _looks_like_home(path)
            if not has_db and not has_projects:
                continue
            picked = ClientHome(
                spec=spec,
                home=path,
                has_db=has_db,
                has_projects=has_projects,
                confirmed=has_db,
                source="env" if (env_home and index == 0 and spec.key == "wb") else "candidate",
            )
            break
        if picked is not None:
            homes.append(picked)

    return homes


def platform_label() -> str:
    import platform

    return f"{platform.system()} {platform.release()} · Python {sys.version.split()[0]}"
