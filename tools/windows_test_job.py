"""Windows 本地测试的进程分组。"""

import ctypes
import subprocess
from ctypes import wintypes


# 启动器先等待父进程放行。加入 Job 之前不会运行 pytest 或导入项目。
PYTEST_START_FAILED = 125  # 启动器保留的退出码，位于 pytest 的标准退出码范围之外。
PYTEST_LAUNCHER = f"""\
import sys
if sys.stdin.buffer.read(1) != b'1':
    sys.exit({PYTEST_START_FAILED})
import subprocess
try:
    process = subprocess.Popen(
        [sys.executable, '-m', 'pytest', *sys.argv[1:]],
        stdin=subprocess.DEVNULL, shell=False,
    )
except OSError as exc:
    print(f'无法创建 pytest 进程：{{exc}}', file=sys.stderr)
    sys.exit({PYTEST_START_FAILED})
sys.exit(process.wait())
"""


class WindowsTestJob:
    def __init__(self):
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        self.kernel.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        self.kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self.process_attached = False

    def attach(self, process: subprocess.Popen) -> None:
        # CPython 的 Popen 在 Windows 上保留 CreateProcess 返回的原始句柄。
        if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise ctypes.WinError(ctypes.get_last_error())
        self.process_attached = True

    def terminate(self) -> None:
        if not self.kernel.TerminateJobObject(self.handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        self.kernel.CloseHandle(self.handle)
