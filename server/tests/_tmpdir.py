# -*- coding: utf-8 -*-
"""测试用例的临时目录工具（`server/tests/_tmpdir.py`）。

## ⚠ 为什么不能用 `tempfile.mkdtemp()` / `TemporaryDirectory`

两者都会把目录按 **0700** 建（POSIX 语义），Windows 上落成一条**只授权建目录者**的
访问控制项。DSH 的沙箱进程以受限身份运行，需要工作区那条**授权项**才写得进去 ——
新目录上没有它，于是：

```
tempfile.mkdtemp()  ->  PermissionError: [Errno 13] Permission denied: '<dir>/player.json'
TemporaryDirectory  ->  PermissionError: [WinError 5]（在 cleanup 的 chmod 里，把结果盖成 ERROR）
```

实测对照（同一工作区、同一进程）：

| 建目录方式 | 往里写文件 |
|---|---|
| `os.makedirs(path)` | ✅ 可以（目录**继承工作区 ACL**） |
| `tempfile.mkdtemp(dir=...)` | ❌ `PermissionError [Errno 13]` |

所以本仓库的测试统一用 `make_case_dir()`：`os.makedirs` + 唯一后缀。
（这也是为什么不能把测试临时目录放到系统 `%TEMP%`：既同样中招，又属于工作区之外。）
"""
from __future__ import annotations

import os
import shutil
import uuid

#: 工作区内的测试临时根目录（**不要**改成系统临时目录）
TMP_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "scratch", "_unittest_tmp")


def make_case_dir(prefix: str = "case") -> str:
    """在 `TMP_ROOT` 下建一个用例专用目录并返回其路径（可读写）。"""
    os.makedirs(TMP_ROOT, exist_ok=True)
    path = os.path.join(TMP_ROOT, "%s_%s" % (prefix, uuid.uuid4().hex[:8]))
    os.makedirs(path, exist_ok=True)
    return path


def cleanup(path: str) -> None:
    """尽最大努力删除；删不掉也不抛（沙箱里残留目录是已知现象）。"""
    shutil.rmtree(path, ignore_errors=True)
