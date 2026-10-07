"""AndroidEnv：用假 adb runner 测试命令构造与观察解析（无需设备）。"""
import io

from PIL import Image

from conftest import FIX
from gua.actions import Action
from gua.env.android import AndroidEnv, escape_input_text


class FakeADB:
    def __init__(self):
        self.calls = []
        img = Image.new("RGB", (1080, 2400), (250, 250, 250))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        self.png = buf.getvalue()

    def __call__(self, args, binary=False):
        self.calls.append(args)
        if args[:2] == ["exec-out", "screencap"]:
            return self.png
        cmd = args[1] if len(args) > 1 else ""
        if cmd.startswith("wm size"):
            return "Physical size: 1080x2400\n"
        if cmd.startswith("uiautomator dump"):
            return (FIX / "android_settings.xml").read_text(encoding="utf-8")
        if cmd.startswith("dumpsys window"):
            return "  mCurrentFocus=Window{1a2b3c u0 com.android.settings/com.android.settings.Settings}\n"
        if cmd.startswith("monkey") and "does.not.exist" in cmd:
            return "** No activities found to run, monkey aborted."
        return ""


def test_observe_parses_screen_tree_and_foreground():
    adb = FakeADB()
    env = AndroidEnv(runner=adb)
    o = env.observe()
    assert o.screen_size == (1080, 2400) and o.platform == "android"
    assert o.active_process == "com.android.settings"
    assert any(e.name == "Wi‑Fi" for e in o.elements)


def test_commands():
    env = AndroidEnv(runner=FakeADB())
    assert env.command_for(Action("click", x=10, y=20)) == "input tap 10 20"
    assert env.command_for(Action("long_press", x=5, y=6, seconds=1)) == "input swipe 5 6 5 6 1000"
    assert env.command_for(Action("back")) == "input keyevent 4"
    assert env.command_for(Action("home")) == "input keyevent 3"
    # v0.3：参数 shlex.quote（不再手写反斜杠转义），多条命令逐条发送（这里仅为显示用 && 连接）
    assert env.command_for(Action("type", text="a b&c", submit=True)) == "input text 'a%sb&c' && input keyevent 66"
    assert env.command_for(Action("open_app", app="com.android.settings")).startswith("monkey -p com.android.settings")
    assert env.command_for(Action("open_app", app="com.x/.Main")) == "am start -n com.x/.Main"
    # 内容向下滚 = 手指向上滑
    sw = env.command_for(Action("scroll", direction="down", amount=2))
    _, _, x1, y1, x2, y2, _ = sw.split()
    assert int(y2) < int(y1)
    assert env.command_for(Action("hotkey", keys=["enter"])) == "input keyevent 66"
    assert escape_input_text("it's") == "it\\'s"


def test_execute_errors():
    adb = FakeADB()
    env = AndroidEnv(runner=adb)
    assert not env.execute(Action("click", x=5000, y=10)).ok                      # 越界
    r = env.execute(Action("type", text="中文"))
    assert not r.ok and "ADBKeyboard" in r.error                                  # 非 ASCII 需要 ADBKeyboard
    assert AndroidEnv(runner=adb, adb_keyboard=True).command_for(Action("type", text="中文")).startswith("am broadcast")
    assert not env.execute(Action("right_click", x=1, y=1)).ok                   # 平台不支持
    assert not env.execute(Action("open_app", app="does.not.exist")).ok
    assert env.execute(Action("click", x=10, y=10)).ok
    assert ["shell", "input tap 10 10"] in adb.calls
