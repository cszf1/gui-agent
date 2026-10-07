"""跨模块的终止类异常。

- UserAbort：人工紧急停止（pyautogui fail-safe：鼠标甩到屏幕角落；或 Ctrl+C 等）。
  继承 BaseException（与 KeyboardInterrupt 同级），这样任何 `except Exception` 都不会把它当作普通执行失败吞掉；
  GUIAgent.run 捕获后返回终止状态 "user_abort"，评测 runner 记录后停止整个任务集。
"""
from __future__ import annotations


class UserAbort(BaseException):
    """用户主动中止（紧急停止）。不是“执行失败”，不能被恢复策略重试。"""


class PrivacyBlocked(RuntimeError):
    """模型请求需要发送截图，但本次运行已进入严格隐私阻断（识别到秘密 / 敏感输入）。

    纯视觉路径（Claude computer-use / UI-TARS / 视觉 grounding / vision-only 策略）在没有截图时
    无法可靠执行，因此不做“无图也假装成功”的伪装，而是抛出本异常，让运行以明确的受限状态终止
    （GUIAgent.run → status "privacy_blocked"，绝不算成功）。普通文本路径不应抛它：它们改为
    “无图 + 明确提示”继续（见 gua.llm.base.EgressGate）。
    """
    def __init__(self, role: str = "llm", detail: str = ""):
        self.role = role
        self.detail = detail or "screenshots are blocked for this run (sensitive data detected)"
        super().__init__(f"privacy blocked for {role}: {self.detail}")
