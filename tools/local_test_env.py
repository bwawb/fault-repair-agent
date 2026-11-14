"""为 local pytest 构造受控的子进程环境。"""

import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path


_WINDOWS_VARIABLES = ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP")
_POSIX_VARIABLES = ("TMPDIR", "LANG", "LC_ALL", "LC_CTYPE")
_RUNNER_VARIABLES = {"PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
_VARIABLE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def build_local_test_env(extra_names: Sequence[str] = ()) -> dict[str, str]:
    """只读取系统所需变量和调用方显式选择的变量。"""
    windows = os.name == "nt"
    allowed = _WINDOWS_VARIABLES if windows else _POSIX_VARIABLES
    env = {name: os.environ[name] for name in allowed if name in os.environ}

    if windows:
        system_root = env.get("SYSTEMROOT")
        if not system_root or not Path(system_root).is_absolute():
            raise ValueError("Windows local pytest 需要有效的 SYSTEMROOT。")
        system_paths = [str(Path(system_root) / "System32"), system_root]
    else:
        system_paths = [path for path in os.defpath.split(os.pathsep) if Path(path).is_absolute()]
    # 保留虚拟环境的解释器目录，不解析其可能指向系统 Python 的符号链接。
    python_dir = str(Path(sys.executable).parent)
    env["PATH"] = os.pathsep.join(dict.fromkeys([python_dir, *system_paths]))

    for requested in extra_names:
        if not isinstance(requested, str) or not _VARIABLE_NAME.fullmatch(requested):
            raise ValueError("额外测试环境变量必须通过变量名指定，请勿传入 NAME=VALUE。")
        name = requested.upper() if windows else requested
        if name in _RUNNER_VARIABLES:
            raise ValueError(f"{name} 由测试执行器设置，不能覆盖。")
        if name not in os.environ:
            raise ValueError(f"指定的测试环境变量未设置：{name}")
        env[name] = os.environ[name]

    env.update(_RUNNER_VARIABLES)
    return env
