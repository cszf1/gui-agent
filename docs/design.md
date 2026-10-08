# 设计说明（v0.2 跨平台版起；v0.3 审查修复见 [review-fixes.md](review-fixes.md)；v0.6 新模块见 §9）

> v0.3 对本文描述的主循环有 4 处行为变化：①所有执行动作（含恢复动作）统一经过安全闸门，拒绝是终止性的；
> ②任务收尾核验总是运行，只有明确 success 才算完成，uncertain 不算；③预算在模型调用边界硬性执行（`budget_exhausted`）；
> ④各组件读取同一个 `CapabilityPolicy`（`gua/policy.py`）。坐标换算改为 `ImageTransform` 变换链。

> 当前工作树进一步收紧：秘密先展开再过闸；模型请求由统一出口清洗；发现敏感数据后不再发送/保存截图，
> 必须依赖视觉的路径返回 `privacy_blocked`。复合移焦/激活不能塞入单个 hotkey；完成证据不再以全局画面变化刷新。
> Web 输入核对安全观察中的实际元素身份；白名单开启时保留非 GET/HEAD 方法的跨源导航重定向在下一跳前拒绝，
> 避免改变文档同源隔离。步骤级 L2 提示词也遵守 `a11y_in_prompts`。

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
- 未实现：推测式多动作、OmniParser 视觉检测（见 research.md 第 4 节）；MCP 与代码 / 文件 / API 工具通道 v0.6 已以受限形式实现（见 §9）
- 视觉验证器本身需要单独标注和评测（Microsoft 关于 CUA verifier 的研究提醒）
- 敏感输入分类与已知秘密清洗共同保护输出；桌面与 Android 焦点仍依赖无障碍树。`AgentConfig.secrets` 从首次请求前登记，
  运行中的未知秘密不能靠字符串替换自动识别；敏感状态后的截图一律不发送/保存，纯视觉能力因此可能被终止。详见 README“已知限制”与 review-fixes.md 第三轮。

## 9. v0.6 新模块

v0.6 把“执行验证与失败恢复”从像素 GUI 扩展到多种执行模态，并让完成结论可复查。所有新路径共用同一个 SafetyGuard、
Verifier 与 Scrubber；对标资料与差异点见 [research.md §5](research.md#5-v06-对标)。

| 模块 | 作用 | 验证情况 |
|---|---|---|
| `gua/hybrid.py` | 混合执行器：语义 / 工具动作与像素 GUI。后台尝试前检查模态遮挡；之后做侵入检测（指针 / 前台）与生效检测。缺少 pattern 可走前台；已投递但无证据时，仅 `set_value` / `select` / `focus` 等存在等价路径的幂等动作允许恢复。点击、`toggle`、`invoke`、提交结果不明时不重放；侵入也不触发重放 | mock + 真实 Chromium + 本机 Xvfb + Docker/GTK |
| `gua/tools/registry.py` + `gua/sandbox/process.py` | shell / 文件 / 注册 API 通道。默认全部关闭；shell 限定可执行文件、argv 直接 exec、最小环境、超时及有界输出。POSIX 子进程组清理、CPU/文件大小上限；地址空间上限仅 Linux 支持，尊重更严格的继承上限。Windows 普通完成后的脱离进程清理尚无 Job Object 保证。文件限定根目录、读入有上限 | 单元测试，含大输出内存、继承资源上限与超时子进程检查 |
| `gua/verify/postconditions.py` | 规则后置条件（`text_appears` / `text_disappears` / `element_state` / `window_title` / `url` / `pixel_change` / `output_contains`），结论只有 pass / fail / unknown；控件匹配歧义为 unknown，隐含条件绑定控件身份；密码元素的值不读取。特定状态核验失败不能用无关像素变化覆盖 | 单元测试 + 端到端 |
| 子目标级后置条件（`planner.py` / `verifier.py`） | 规划器可为子目标给出后置条件（校验后最多 8 条）。`check_goal`：任一明确不成立 → 失败；全部成立且无 `expect_text` → 成功；有 `expect_text` 时仍需文本证据。`check_final` 对没有 `expect_text` 的子目标重新核验其后置条件 | `tests/test_v06_subgoal_postconditions.py` + 沙箱任务 |
| `gua/env/web.py` / `gua/env/windows.py` + `uia_execution.py` | 后台语义执行：Web 绑定观察到的 DOM 节点；Windows 用 UIA Invoke/Toggle/SelectionItem/Value/ExpandCollapse/ScrollItem 模式，缺模式返回 `background_unavailable`，`SetFocus` 需要前台。原生读写前后复核身份与密码属性 | Web：真实 Chromium；Windows 新增真实 WinForms 后台路径 CI，运行状态见审查记录 |
| `gua/recovery.py`（`SWITCH_MODALITY`） | 幂等 GUI 选择 / 聚焦与语义路径之间恢复；结果不明的非幂等激活记录在共享闸门，阻断换模态、fixed-retry 和 actor 重放 | mock 故障注入 + 延迟执行回归 |
| `gua/verify/receipts.py` + `agent.py` + `logger.py` | 完成凭据、清洗后的事件流、里程碑 checkpoint；续跑绑定任务、恢复拒绝与未确认激活、重新验证已完成子目标。敏感 checkpoint 不保存明文清洗规则，跨进程恢复返回 `privacy_blocked` | MockEnv、MCP 会话与续跑回归 |
| `gua/env/remote.py` + `gua/sandbox/` + `sandbox/Dockerfile` | HTTP 观察原子返回图像与树；动作绑定确切快照和稳定 AT-SPI 控件身份；接管与观察/动作串行，交回后旧观察失效。noVNC 服务端输入限制等待确认；快照还原工作目录与应用启动列表，不含内存 | 本机 Xvfb + AT-SPI、真实 Docker/GTK/noVNC；本机模式与当前用户同权限，容器未作隔离安全审计 |
| `gua/mcp_server.py`（`gua mcp`） | MCP stdio 服务器：observe / act / verify / run_task / takeover / handback / snapshot / reset / live_view；`act` 返回规则验证结论而非 “OK”；需确认的动作默认拒绝 | stdio 子进程 + 真实 Chromium / 沙箱 |
| `gua/eval/runner.py`（`run_suite_parallel`、`gua eval --workers N`） | 每个 worker 独立环境（remote 任务各起一台本机沙箱电脑），结果合并汇总；行内新增模态 / 回退 / 侵入 / 已验证凭据计数 | 沙箱端到端（2 workers） |
| `gua/eval/cuabench_compat.py` / `gua/eval/osworld.py` | Cua-Bench 任务目录子集兼容层（oracle 或 gua agent 运行，判分由任务自己的 evaluate 决定）；OSWorld 形状任务 JSON 子集转换，不支持的配置 / 判分函数直接拒绝 | 沙箱端到端；合成任务，非官方任务集 |
| `scripts/benchmark_modalities.py` | gui_only / hybrid 前台 / hybrid 后台 + mock 故障注入对比，输出 `docs/benchmarks/*.json` | 仅脚本策略，**不是模型证据** |

数据流增量（在 §4 的基础上）：

```
Actor 动作 → SafetyGuard.gate(意图) → HybridExecutor
   ├─ shell / file / api → ToolRegistry（默认关闭）→ 输出经 Scrubber → 后置条件 output_contains
   ├─ invoke（语义）→ 遮挡检查 → 后台投递 → 侵入检测 → 重新观察 → 生效检测
   │      └─ 无生效证据：有等价路径的幂等 → 重新观察同一控件 → 前台动作（重新过闸）
   │                      非幂等 → background_no_effect → 记录未确认激活，等待/重新规划，不重放
   └─ 像素 GUI → env.execute
→ Verifier.check_step（含动作级后置条件）→ 子目标 check_goal（含子目标级后置条件）→ receipt
```
