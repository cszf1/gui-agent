# 设计说明（v0.2 跨平台版；v0.3 审查修复见 [review-fixes.md](review-fixes.md)）

> v0.3 对本文描述的主循环有 4 处行为变化：①所有执行动作（含恢复动作）统一经过安全闸门，拒绝是终止性的；
> ②任务收尾核验总是运行，只有明确 success 才算完成，uncertain 不算；③预算在模型调用边界硬性执行（`budget_exhausted`）；
> ④各组件读取同一个 `CapabilityPolicy`（`gua/policy.py`）。坐标换算改为 `ImageTransform` 变换链。

> v0.1（win-gui-agent / `wga`）只支持 Windows。v0.2 把同一套验证-恢复核心推广到 6 个后端；调研与取舍见 [research.md](research.md)。

## 1. 研究问题

> 在相同模型与调用预算下，任务相关的状态验证 + 按失败类型的局部恢复，能否比原始执行循环、固定重试和每步泛泛验证，更好地提升受干扰 GUI 任务（Windows / macOS / Linux / Android / Web）的最终成功率，并降低错误宣告完成率？

可进一步收窄的切口（见调研报告 4.4 / 4.5）：
- **A1 等待还是恢复**：Windows 异步界面里，`in_progress` 与 `no_effect` 的区分（`wait_until_stable` + 局部像素差 + VLM）
- **A2 低成本收尾确认**：`check_goal` 只看当前屏幕，配合 UIA 值读取，减少错误宣告完成

## 2. 三级证据

| 级别 | 证据 | 成本 | 能定论的情况 |
|---|---|---|---|
| L0 执行层 | `ExecResult.ok/error` | 0 | 越界、窗口/应用不存在、平台不支持、被安全策略拦截 |
| L1 规则层 | 前台窗口、**新对话框**、**目标被遮挡**、是否稳定、**忙碌指示**（Loading…/progressbar）、**输入框值 / 复选框状态**、期望文本、URL/前台变化、全局/局部像素差 | 极低 | 失焦、弹窗、加载中、无变化、已输入、已勾选、期望文本出现 |
| L2 模型层 | 前后截图拼图 + 预期结果 → verdict | 1 次 VLM 调用 | 规则层 `uncertain` 时 |

`trigger=on_event` 只在 L1 给出 `uncertain` 时才调用 L2，这是相对 `every_step` 节省预算的来源，需要在实验里报告节省了多少调用、牺牲了多少准确率。

L1 的无障碍证据来自统一元素模型（`env/a11y.py`），所以同一条规则在 UIA / AX / AT-SPI / uiautomator / DOM 上都成立。
另外新增 **动作前状态核对**（`agent._precheck_focus`）：观察到前台已经不是任务窗口时，不调用 actor，先恢复焦点。

## 3. 失败类型 → 恢复策略（平台化）

| Verdict / 信号 | 恢复 | 说明 |
|---|---|---|
| `in_progress` | wait（递增，最多 `max_waits` 次） | 不重复点击 |
| exec `out_of_bounds` | 按目标点到屏幕中心的距离 / `env.scroll_unit_px` 计算方向与格数后滚动 | QQ 实测中屏幕外元素；Web 长页面 |
| `focus_lost` | 桌面/Web：focus_window（含还原最小化、切回任务标签页）；Android：open_app(任务包名) | QQ 实测中最小化窗口；新标签页 / Home 键干扰 |
| exec `blocked_by_safety` / `unsupported` | replan | 换一条不危险 / 平台支持的路径 |
| `blocked`（新窗口/弹窗） | 交给 Actor 读弹窗内容处理 | 不盲目 Esc，弹窗可能是保存确认 |
| `no_effect` 首次 | 围绕原点击点放大重定位 | RegionFocus 思路 |
| `no_effect` 再次 | replan | 换路径（如改用快捷键） |
| `failed`（点错） | 桌面/Web Esc、Android back；`allow_undo` 时 Ctrl/Cmd+Z | 撤销默认关闭，只在能确认误操作时开 |
| `uncertain` | 重新观察，反馈给 Actor | 禁止盲目重复 |
| 恢复预算用尽 / 循环 | replan → 超过 `max_replans` 则失败 | |

## 4. 模块与数据流

```
Planner ──subgoals(expected, evidence, expect_text)──▶ 每个子目标循环：
  before = env.observe()  →  precheck(焦点)  →  Actor(截图+元素列表+历史+里程碑+反思笔记)
  → coords.to_pixel_action → Grounder(a11y → VLM → zoom) → SafetyGuard.gate
  → env.execute → wait_until_stable → Verifier.check_step(L0/L1/L2)
  → [非 success] RecoveryPolicy.decide → 执行最小恢复动作 → Reflector → feedback
  → [actor 说 done] Verifier.check_goal(expect_text 规则优先, 否则 L2) → Milestone
任务结束：多子目标时再做一次 final_check；全程 Budget / 步数 / 墙钟限制
```

## 5. 平台后端

| 后端 | 截图 | 元素 | 输入 | 焦点 / 前台 | 状态 |
|---|---|---|---|---|---|
| windows | mss | uiautomation（UIA） | pyautogui + 剪贴板中文 | UIA 前台窗口，SetActive + 还原最小化 | v0.1 真机代码迁移，**v0.2 未在真机复测** |
| macos | Quartz / screencapture | pyobjc AX API | pyautogui（point = 像素 / Retina 倍率） | NSWorkspace + osascript activate | **未在真机验证**；AX→元素转换有 fixture 测试 |
| linux | mss（X11） | AT-SPI（pyatspi，可选） | pyautogui | xdotool getactivewindow / windowactivate | **未在真机验证**；AT-SPI→元素转换有 fixture 测试 |
| android | adb screencap | uiautomator dump | input tap/swipe/text/keyevent、monkey | dumpsys mCurrentFocus | **未连真机/模拟器**；命令构造与 XML 解析用假 adb 测试 |
| web | Playwright | 注入 JS 的 DOM 快照（编号、offscreen、covered、dialog） | page.mouse / keyboard / goto | 多标签页：新页面视为抢焦点 | **沙箱内真实 Chromium 端到端测试通过** |
| mock | PIL 绘制 | 内置 | 内置 | 内置 | 单元测试 |

## 6. 与 14 篇论文的对应

| 论文 | 在本项目中的位置 |
|---|---|
| Mind2Web, WebArena | 基于最终状态的判分；任务初始化与复位（`eval/runner.py`） |
| OSWorld | 真实桌面任务与执行验证；干扰设计参考 |
| SeeClick, UGround, OS-ATLAS | “做什么”与“点哪里”分离；坐标约定 |
| ShowUI | 截图缩放与 token 成本（`image_max_side`） |
| ScreenSpot-Pro, RegionFocus | 无效点击后的局部放大重定位 |
| GUI-Cursor | 提醒：定位闭环 ≠ 真实软件执行闭环 |
| Agent S | Planner / Actor / Grounder 分层；ACI 式动作空间 |
| OS-Symphony | 里程碑记忆、反思与重规划 |
| OpenCUA, VideoAgentTrek | 轨迹日志格式可直接作为后续示范学习数据 |

## 7. 12 周计划（2–3 人，参考）

| 周 | 交付 |
|---|---|
| 1–2 | 真机跑通 `env/windows.py` 与至少一个其他平台（推荐 web + android 模拟器），`raw_loop` 跑 10 个任务 |
| 3–4 | 扩到 30 个任务（文件管理 / 记事本+Office 或 WPS / 一个复杂软件），按 `steps.jsonl` 做失败分类，确定 A1 或 A2 |
| 5–7 | 打磨主方法：规则阈值、验证 prompt、恢复策略 |
| 8–10 | 6 组配置 × 每任务 3 次 × 有/无干扰；同预算对比与消融 |
| 11–12 | 复现实验、演示视频、报告与失败案例分析 |

## 8. 已知限制 / TODO

- `wait_until_stable` 对持续动画（光标闪烁、视频）会超时；小号 spinner 像素变化低于阈值时依赖“忙碌指示”文本规则
- `focus_lost` 判断依赖窗口标题片段 / 包名 / 页面标题，多窗口同名时需要进程/句柄级身份
- 脚本策略（`--policy scripted`）只验证非模型部分；L2 是乐观桩，不能代表真实模型表现
- Claude computer-use actor 为每步无状态调用（不保留 tool_result 链），与官方参考实现不同
- Android 非 ASCII 输入需要 ADBKeyboard；Linux 仅支持 X11；macOS 需要“辅助功能 + 屏幕录制”权限
- 未实现：MCP 工具通道、代码动作、推测式多动作、OmniParser 视觉检测（见 research.md 第 4 节）
- 视觉验证器本身需要单独标注和评测（Microsoft 关于 CUA verifier 的研究提醒）
