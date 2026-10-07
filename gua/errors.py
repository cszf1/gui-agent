"""跨模块的终止类异常。

- UserAbort：人工紧急停止（pyautogui fail-safe：鼠标甩到屏幕角落；或 Ctrl+C 等）。
  继承 BaseException（与 KeyboardInterrupt 同级），这样任何 `except Exception` 都不会把它当作普通执行失败吞掉；
  GUIAgent.run 捕获后返回终止状态 "user_abort"，评测 runner 记录后停止整个任务集。
"""
from __future__ import annotations


class UserAbort(BaseException):
    """用户主动中止（紧急停止）。不是“执行失败”，不能被恢复策略重试。"""
