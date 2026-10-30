"""把进程的标准输出/标准错误钉成 UTF-8（Windows 中文区域的必修项）。

## 为什么需要

Windows 上 Python 的 stdout/stderr 默认按 **ANSI 代码页**编码（中文区域就是
cp936）。只要往屏幕上/管道里打一句带非 GBK 字符的话——比如日志里带上了
emoji 或生僻昵称——那次 `print` 就会抛 `UnicodeEncodeError`，整个进程直接崩。

所以凡是「可能打印用户数据」的进程入口都要在最早的时刻调一次
`force_utf8_stdio()`。`errors='replace'` 是兜底：万一某台机器上连 UTF-8 都
写不出去（极罕见），也宁可把那个字符打成 `?`，不能让整个进程死在一句日志上。
"""
from __future__ import annotations

import sys


def force_utf8_stdio(streams: tuple[object, ...] | None = None) -> None:
    """把 stdout / stderr 重新配置为 UTF-8（不可用时静默跳过）。

    `streams` 只为测试注入用：传进来的对象只要有 `reconfigure` 就会被调用。
    """
    targets = streams if streams is not None else (sys.stdout, sys.stderr)
    for stream in targets:
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')  # type: ignore[attr-defined]
        except (AttributeError, ValueError, OSError):
            # 没有 reconfigure（被替换成 StringIO 等）或流已关闭：都不是错误，
            # 继续处理下一个；编码兜底不该成为新的崩溃点。
            continue
