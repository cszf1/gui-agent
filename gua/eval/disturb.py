"""可重复注入的干扰（调研报告 10.2 干扰设计），用于测“恢复能力”。

两种触发方式：
- at_step（推荐，跨平台、可复现）：在第 N 步观察之前同步注入，通过 GUIAgent.before_step 钩子
- delay（v0.1 方式，仅 Windows）：从任务开始计时 delay 秒后在后台线程注入

各平台支持的干扰：
  windows : steal_focus（打开记事本）/ popup（模态消息框）/ minimize / move_window
  macos   : steal_focus（打开 TextEdit）/ minimize（Cmd+M 当前窗口）
  linux   : steal_focus（xterm）/ minimize（xdotool windowminimize）
  android : home（按 Home 把任务 App 切到后台）/ notification（下拉通知栏）/ rotate
  web     : new_tab / popup（注入 role=dialog 遮罩）/ reload / scroll_away
  mock    : popup / steal_focus / scroll_away
"""
from __future__ import annotations

import subprocess
import sys
import threading
import time


def _ps(cmd: str) -> None:
    subprocess.Popen(["powershell", "-NoProfile", "-Command", cmd],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def inject(env, kind: str, task_window: str = "") -> None:
    p = getattr(env, "platform", "")
    if p == "web":
        env.disturb(kind)
    elif p == "mock":
        if kind == "popup":
            env.popup = "Disturbance: unexpected dialog"
        elif kind == "steal_focus":
            env.focused = False
        elif kind == "scroll_away":
            env.scroll_y += 2000
        else:
            raise ValueError(kind)
    elif p == "android":
        cmd = {"home": "input keyevent 3", "notification": "cmd statusbar expand-notifications",
               "rotate": "settings put system user_rotation 1"}[kind]
        env.shell(cmd)
    elif p == "windows" and sys.platform == "win32":
        if kind == "steal_focus":
            subprocess.Popen(["notepad.exe"])
        elif kind == "popup":
            _ps("Add-Type -AssemblyName PresentationFramework;"
                "[System.Windows.MessageBox]::Show('Disturbance: please close me','GUA Popup')")
        elif kind in {"minimize", "move_window"}:
            import uiautomation as auto
            for w in auto.GetRootControl().GetChildren():
                if task_window and task_window.lower() in (w.Name or "").lower():
                    if kind == "minimize":
                        w.GetWindowPattern().SetWindowVisualState(auto.WindowVisualState.Minimized)
                    else:
                        w.GetTransformPattern().Move(200, 150)
                    break
        else:
            raise ValueError(kind)
    elif p == "macos":
        if kind == "steal_focus":
            subprocess.Popen(["open", "-a", "TextEdit"])
        elif kind == "minimize":
            subprocess.run(["osascript", "-e", 'tell application "System Events" to keystroke "m" using command down'])
        else:
            raise ValueError(kind)
    elif p == "linux":
        if kind == "steal_focus":
            subprocess.Popen(["xterm"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif kind == "minimize":
            subprocess.run(["xdotool", "getactivewindow", "windowminimize"])
        else:
            raise ValueError(kind)


class DisturbanceScheduler:
    def __init__(self, env, kind: str, at_step: int | None = None, delay: float | None = None,
                 task_window: str = ""):
        self.env, self.kind, self.at_step, self.delay, self.task_window = env, kind, at_step, delay, task_window
        self.fired_at = None
        self._t = None

    def attach(self, agent) -> None:
        if self.at_step is not None:
            def hook(step_no: int, ag) -> None:
                if self.fired_at is None and step_no >= self.at_step:
                    inject(self.env, self.kind, self.task_window)
                    self.fired_at = time.time()
                    if ag.log:
                        ag.log.step(kind="disturbance", step=step_no, disturbance=self.kind)
            agent.before_step.append(hook)
        elif self.delay is not None:
            def go():
                time.sleep(self.delay)
                inject(self.env, self.kind, self.task_window)
                self.fired_at = time.time()
            self._t = threading.Thread(target=go, daemon=True)
            self._t.start()
