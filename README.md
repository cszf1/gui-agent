# gui-agent（`gua`）：跨平台、可验证执行与失败恢复的 GUI Agent

SRTP 研究代码 **v0.3**（修复一轮代码审查发现的 12 个问题，见下方更新日志与 [docs/review-fixes.md](docs/review-fixes.md)）。
前身是只支持 Windows 的 `win-gui-agent`（`wga`）。研究方向 A：**执行验证与失败恢复**
（时间失配：页面没刷新就判断、窗口被最小化/抢焦点、目标在屏幕外……）。

一套核心（规划 → 定位 → 安全闸门 → 执行 → 分级验证 → 分类恢复 → 反思 → 里程碑）跑在 6 个后端上：
**Windows / macOS / Linux(X11) / Android(adb) / Web(Playwright) / Mock**。

- 设计说明：[docs/design.md](docs/design.md)
- v0.3 审查修复对照（审查条目 → 改动 → 回归测试）：[docs/review-fixes.md](docs/review-fixes.md)
- 开源 computer-use agent 调研与取舍：[docs/research.md](docs/research.md)

## 更新日志

### v0.3（代码审查修复）

每一条都是先写回归测试、确认它在 v0.2 上失败，再修复（新增测试在 v0.2 上 73 个失败，5 个对照用例本来就该通过）。

**高优先级**
1. **安全拒绝不再被重试绕过**：所有真正执行的动作（actor 动作、恢复 / 重试 / 撤销 / 滚动 / 切回窗口）都走同一个安全闸门出口；
   被人工拒绝或 deny 的动作记下签名，之后同一动作直接拒绝、不再询问、不再执行（`fixed_retry` 也一样）。
2. **任务收尾核验不再误报完成**：只有明确的 `success` 才算完成；`uncertain` / 无法解析默认先重规划一次，仍不确定则返回
   `uncertain`（不算成功）。收尾核验 = 重新规则核验**每个**子目标的 `expect_text` + 需要时用“整个任务 + 全部子目标预期”做一次聚合 L2；
   单子目标任务也运行（`verification.final_check: false` 才关闭）。
3. **预算是硬上限**：在模型调用边界检查调用数 / token / 成本（`agent.max_cost_usd` + `models.*.price`），触顶时请求不发出，
   运行以终止状态 `budget_exhausted` 结束（绝不算成功），评测记录 `budget_exhausted`、汇总 `budget_exhausted_runs`。
4. **坐标变换链统一**：新增 `ImageTransform`（原始物理像素 → 实际发送尺寸 → 模型坐标约定，另有 DPI → 输入坐标），
   在缩放截图处产生、随模型回复返回；grounder / actor / UI-TARS / Claude 都用它换算。

**中优先级**

5. **畸形动作不再崩溃**：严格 schema 校验，返回结构化 `ActionParseError`（code / field / message），作为反馈交还模型；有模糊测试。

**研究有效性**

6. **消融与名字一致**：新增 `CapabilityPolicy`（`gua/policy.py`），由配置一次推导，所有组件只读它；`vision_only` 的提示词里
   不再有元素 id / 名字 / 值 / 可见文本，`fixed_retry` 保证不调用任何 L2 验证或反思（见下方“消融”表）。

**安全 / 平台**

7. **命令注入**：不再用 `cmd /c start`、不拼接 shell 字符串；Windows / macOS / Linux 启动应用改为 argv + 应用名校验，
   AppleScript 应用名通过 `on run argv` 传入，adb 每个参数 `shlex.quote`、包名 / 键名白名单；可选 `safety.allowed_apps`。
8. **快捷键别名**：键名先规范化（del/delete、ctrl/control、cmd/win/super/meta…），危险组合按子集匹配，`key_down` 按住的键会累计；
   输入文本里含控制字符也要确认。
9. **Web 白名单在浏览器层执行**：Playwright 路由拦截点击、JS 跳转、服务器重定向、新标签页 / 弹窗，越界页面被关闭、主页面回到最后一个合法 URL。
10. **紧急停止**：pyautogui fail-safe（鼠标甩到屏幕角落）在三个桌面平台都作为 `user_abort` 终止运行，评测随即停止。
11. **密码框**：统一元素模型新增 `is_password`（UIA IsPassword、AXSecureTextField、Android password、`input[type=password]`、
    AT-SPI password text），安全闸门据此判断；密码框的值不进入提示词，日志里写成 `***`。
12. **Claude 拖拽**：只给终点时从当前光标开始（按 Anthropic computer-use 语义），不再是零长度拖拽；GPT-5 / o 系列请求改用
    `max_completion_tokens`、不发 `temperature`（按模型名检测，**未用真实 API 验证**）。

新的终止状态：`done | fail | uncertain | step_limit | budget_exhausted | time_limit | user_abort`，只有 `done` 计为“宣称完成”。

### v0.2

跨平台重构：一套验证-恢复核心跑在 Windows / macOS / Linux / Android / Web / Mock 六个后端上。

## 架构

```mermaid
flowchart TB
    T[任务 / tasks/*.json] --> P[Planner<br/>子目标 + expected + expect_text]
    P --> L{子目标循环}
    L --> O[env.observe<br/>截图 + 统一 UIElement + 可见文本]
    O --> PC{动作前核对<br/>前台仍是任务窗口?}
    PC -- 否 --> RC
    PC -- 是 --> A[Actor<br/>json / UI-TARS / Claude computer-use]
    A --> CM[coords 换算<br/>norm1000 / resized / pixel]
    CM --> G[Grounder<br/>a11y → VLM → 局部放大]
    G --> S[SafetyGuard<br/>allow / confirm / deny]
    S --> X[env.execute]
    X --> W[wait_until_stable]
    W --> V[Verifier<br/>L0 执行 / L1 规则+无障碍树 / L2 VLM]
    V -- success --> L
    V -- 其他 --> RC[RecoveryPolicy<br/>wait / refocus / dismiss / scroll / zoom / undo / replan]
    RC --> RF[Reflector → Memory.notes]
    RF --> L
    A -- done --> GC[check_goal<br/>expect_text 规则优先, 否则 L2]
    GC -- 通过 --> M[Milestone] --> L
    GC -- 不通过 --> L
    L -- 子目标失败 --> RP[Planner.replan]
    subgraph Env[平台后端（统一 Env 接口）]
      WIN[windows: mss + UIA + pyautogui]
      MAC[macos: Quartz + AX + pyautogui]
      LIN[linux: mss + AT-SPI + xdotool]
      AND[android: adb screencap + uiautomator + input]
      WEB[web: Playwright + DOM 快照]
      MOCK[mock]
    end
    O -.-> Env
    X -.-> Env
    LOG[(runs/&lt;id&gt;/steps.jsonl + shots + report.html)]
    V -.-> LOG
```

目录：

```
gua/
  actions.py      统一动作空间（click/double/right/long_press/drag/scroll/type/hotkey/wait/open_app/navigate/back/home/focus_window/ask_user/done/fail）
  parsing.py      JSON 动作、坐标点、UI-TARS 原生输出解析
  coords.py       坐标约定换算 + ImageTransform 变换链（原始像素 → 发送尺寸 → 模型坐标；DPI → 输入坐标）
  policy.py       CapabilityPolicy：由配置推导的信息/能力策略（消融的唯一来源）
  keys.py         按键名规范化（安全判定与各平台映射共用）
  urlpolicy.py    域名白名单判定（按主机名）
  errors.py       UserAbort（紧急停止）
  env/            base.py（Env / UIElement / Observation）、a11y.py（各平台无障碍树→统一元素）、
                  windows.py macos.py linux.py android.py web.py mock.py desktop.py（pyautogui 输入层）、
                  commands.py（启动应用 / 激活窗口的 argv 构造与校验）
  llm/            openai_compat.py（GPT / Qwen-VL@DashScope / UI-TARS@vLLM）、anthropic.py（Messages API + computer-use 工具映射）
  planner.py      Planner / Actor / UITarsActor
  grounding.py    a11y 匹配 → VLM → RegionFocus 局部放大
  verify/         L0/L1/L2 分级验证、像素差
  recovery.py     失败分类 → 平台化最小恢复动作
  reflection.py   反思器
  memory.py       步骤历史 / 里程碑 / 反思笔记 / 循环检测
  safety.py       危险动作确认闸门、域名白名单、ask_user
  agent.py        主循环 + 预算/步数/时间限制
  logger.py report.py   轨迹 JSONL + 截图 + HTML 回放
  scripted.py     脚本策略（离线“假模型”，测试 / CI / 演示用）
  eval/           runner.py（指标）checkers.py（判分）disturb.py（按步注入干扰）
configs/          default.yaml、models/*.yaml、ablations/*.yaml、web_local.yaml、mock.yaml
tasks/            windows/ macos/ linux/ android/ web/ + web_assets/*.html（自包含的本地网页任务）
tests/            mock 单元测试、fixture 解析测试、真实 Chromium 集成测试
```

## 安装

```bash
pip install -e ".[dev]"                 # 核心 + 测试（mock 平台即可跑）
pip install -e ".[web]" && playwright install chromium     # Web
pip install -e ".[windows]"             # Windows：mss + uiautomation + pyautogui
pip install -e ".[macos]"               # macOS：pyobjc（Quartz / ApplicationServices / Cocoa）+ pyautogui
pip install -e ".[linux]"               # Linux X11：mss + pyautogui；AT-SPI: sudo apt install python3-pyatspi gir1.2-atspi-2.0 xdotool wmctrl
# Android：只需要 PATH 里有 adb（platform-tools），手机开启 USB 调试或使用模拟器
gua doctor                              # 检查各后端依赖
```

## 快速开始

**0. 无需 API key 的离线演示（真实浏览器 + 脚本策略）**

```bash
gua demo                    # = gua eval tasks/web --policy scripted
gua replay runs/<run_id> --open
```

**1. 配置模型**（任选其一，叠加到默认配置上 `-m`）

```bash
export OPENAI_API_KEY=...      ; M=configs/models/openai_gpt4o.yaml    # GPT-4o 规划 + 本地 UI-TARS 定位
export DASHSCOPE_API_KEY=...   ; M=configs/models/qwen_dashscope.yaml  # 全部 Qwen2.5-VL（百炼，无需 GPU）
export GROUNDER_API_KEY=EMPTY  ; M=configs/models/uitars_vllm.yaml     # UI-TARS-1.5-7B 端到端（vLLM 部署）
export ANTHROPIC_API_KEY=...   ; M=configs/models/claude.yaml          # Claude computer-use 工具（端到端）
```

**2. 各平台运行**

```bash
# Web（本地页面或任意 URL；--headed 显示浏览器）
gua run --platform web -m $M --url tasks/web_assets/form.html --window "Contact form" \
        --task "填写姓名 Alice、邮箱 alice@example.com，选 Pro 套餐并提交"

# Windows（v0.1 的任务照常可用）
gua run --platform windows -m $M --window 记事本 --task "在记事本末尾输入 SRTP 测试完成 并保存"

# macOS（先给终端授予“辅助功能”和“屏幕录制”权限）
gua run --platform macos -m $M --window TextEdit --task "在 TextEdit 里输入 hello 并保存"

# Linux（X11 会话；Wayland 下不可用）
gua run --platform linux -m $M --window gedit --task "在 gedit 里输入 hello 并保存"

# Android（adb devices 能看到设备；--window 写包名用于“App 被切走”检测）
gua run --platform android -m $M --app com.android.settings --window com.android.settings \
        --task "打开 网络和互联网，告诉我 Wi-Fi 是否开启"
```

危险动作（删除/支付/发送、`rm -rf`、Alt+F4、往密码框输入……）默认在终端询问；`--deny` 一律拒绝，`--yes` 一律放行（只在虚拟机里用）。
模型可以输出 `ask_user` 向你提问。

## 模型配置

`configs/default.yaml` 中 `models.{planner,actor,grounder,verifier,reflector}` 各自独立，`null` 表示复用上一个：

| 字段 | 说明 |
|---|---|
| `provider` | `openai`（任何 OpenAI 兼容接口：OpenAI / DashScope / vLLM / Ollama）或 `anthropic` |
| `model` / `base_url` / `api_key_env` | 模型名、接口地址、存 key 的环境变量名 |
| `image_max_side` | 发送前缩放截图（控制 token） |
| `actor.kind` | `json`（默认，planner–grounder 分离）/ `uitars`（端到端）/ `claude_computer_use`（端到端） |
| `actor.coord_space` | `null` 时 actor 只给 element_id/描述；设为 `norm1000`/`resized`/`pixel` 时 actor 直接给坐标 |
| `grounding.coord` / `max_pixels` | grounder 的坐标约定：Qwen2.5-VL / UI-TARS-1.5 用 `resized` 且 `max_pixels` 要与部署一致；UI-TARS-1.0 / SeeClick 用 `norm1000` |

## 评测

```bash
gua eval tasks/web --policy scripted                       # 离线
gua eval tasks/web -m $M --repeats 3 --tag main             # 真实模型
gua eval tasks/windows -m $M -c configs/ablations/raw_loop.yaml --tag raw
python scripts/compare.py runs/summary-*.json               # 汇总成 Markdown 表
```

任务 JSON 字段见 `gua/eval/runner.py` 顶部注释：`platform`、`instruction`、`task_window`、`start_url`、`setup`、`checks`、
`teardown`、`disturbance`（`{"kind": "new_tab", "at_step": 3}`，按步注入、可复现）、`demo`（脚本策略）。

指标：`success_rate`、**`false_done_rate`（宣称完成但判分失败 / 宣称完成数）**、`recovery_rate`（有干扰且通过 / 有干扰运行数）、
`budget_exhausted_runs` / `uncertain_runs` / `user_abort_runs`（v0.3）、平均步数 / 模型调用 / token / 耗时。只有终止状态 `done` 计为“宣称完成”。

自带任务：

| 平台 | 任务 | 测什么 |
|---|---|---|
| web | form_submit / todo_add | 基本表单、输入框值与复选框的 L1 核验 |
| web | delayed_report | 加载中等待（旧报告是陈旧证据，`raw_loop` 会错误宣告完成） |
| web | modal_export | 点击被意外弹窗吞掉 → BLOCKED → 处理弹窗 → 重做 |
| web | long_page_save | 目标在视口外 → 越界 → 按距离滚动 → 重做 |
| web | form_focus_steal | 第 3 步前新标签页抢焦点 → 动作前核对 → 恢复焦点 |
| windows | notepad_append_save / calc_result_to_notepad / explorer_make_folder | v0.1 任务（抢焦点 / 弹窗干扰） |
| android | settings_wifi / contacts_add | Home 键干扰、App 切回 |
| linux / macos | gedit_hello / textedit_hello | 文本编辑 + 保存 |

## 消融

v0.3 起每个配置都先被推导成一个 `CapabilityPolicy`（`gua/policy.py`），planner / actor 提示词 / grounder / verifier / 反思 / 恢复 /
收尾核验都只读这一个对象；`tests/test_review_v03.py::test_i06_every_ablation_maps_to_documented_policy` 断言下表与代码一致。

| 配置 | 无障碍树：定位 / L1 规则 / 提示词 | 步骤验证 | L2 模型验证 | 子目标核验 | 任务收尾核验 | 恢复 | 反思 |
|---|---|---|---|---|---|---|---|
| `configs/default.yaml`（主方法） | ✅ / ✅ / ✅ | on_event（规则定不了才调 L2） | ✅ | ✅ | ✅（规则全证实时不调 L2） | 分类恢复 | ✅ |
| `ablations/raw_loop.yaml`（基线 1） | ✅ / — / ✅ | none（只看执行层报错） | ❌ | ❌ | ❌ | 无 | ❌ |
| `ablations/fixed_retry.yaml`（基线 2） | ✅ / ✅ / ✅ | on_event，**纯规则** | ❌（`verification.llm: false`） | ❌ | ❌ | 原样重试 | ❌ |
| `ablations/every_step_verify.yaml`（基线 3） | ✅ / ✅ / ✅ | every_step（每步都调 L2，成本上界） | ✅ | ✅ | ✅ | 分类恢复 | ✅ |
| `ablations/no_goal_check.yaml` | ✅ / ✅ / ✅ | on_event | ✅ | ❌ | ❌ | 分类恢复 | ✅ |
| `ablations/vision_only.yaml` | ❌ / ❌ / ❌ | on_event（只有像素 / 窗口规则） | ✅（只给截图，不给可见文本） | ✅（只能靠 L2） | ✅（只能靠 L2） | 分类恢复 | ✅ |
| `ablations/no_reflection.yaml` | ✅ / ✅ / ✅ | on_event | ✅ | ✅ | ✅ | 分类恢复 | ❌ |

所有配置都保留、且**不受消融影响**的部分：安全闸门（始终使用元素名与 `is_password` 判断危险动作）、窗口标题 / URL
（窗口管理器元数据，`vision_only` 下仍提供给模型，焦点检查需要它）、预算硬上限。
所有配置共享 `agent.max_budget_calls`，保证同预算对比。

## 测试

```bash
pytest -q                 # 全部（没有 playwright/chromium 时 web 集成测试自动跳过）
pytest -q -m "not web"    # 只跑 mock / fixture / 离线后端测试（CI 三平台矩阵）
```

v0.3 在沙箱（Linux aarch64，Python 3.12，Playwright + Chromium headless shell）中的结果：**130 passed**
（v0.2 的 52 个 + v0.3 新增的 78 个审查回归测试，含参数化用例；其中 `tests/test_web_allowlist.py` 用真实 Chromium + 本地 HTTP 服务器）。
GitHub Actions：`core` 任务在 ubuntu / windows / macos × Python 3.10 / 3.12 上跑 `-m "not web"`；`web` 任务在 ubuntu 上装 Chromium 跑全部测试 + `gua demo`。

## 哪些验证过、哪些没有（诚实说明）

| 部分 | 状态（v0.3） |
|---|---|
| 核心逻辑（动作解析与校验、坐标变换链、验证规则、收尾核验、恢复决策、记忆、反思、安全闸门、预算硬上限、能力策略、日志/报告、CLI） | ✅ mock 单元测试 + 审查回归测试（含随机往返 / 模糊测试） |
| **Web 后端** | ✅ 沙箱内真实 Chromium 端到端：6 个本地任务在更严格的收尾核验下仍全部通过，等待 / 弹窗 / 滚动 / 抢焦点 4 条恢复路径都真实触发；`raw_loop` 复现“错误宣告完成”；安全闸门拦下“Delete account”；**域名白名单在浏览器层拦截链接 / 新标签页 / 302 重定向 / JS 跳转 / window.open**（本地 HTTP 服务器验证白名单外主机收不到请求） |
| Windows 后端 | 🟡 命令构造（启动应用不再经过 cmd）、UIA → 元素转换（含 IsPassword）、fail-safe 传播用假模块在 Linux 上测过；**v0.2 / v0.3 都未在真机复测** |
| macOS 后端 | 🟡 AX 树转换（含 AXSecureTextField 子角色）、AppleScript argv 传参、fail-safe 传播用假模块测过；**未在 Mac 上运行** |
| Linux 后端 | 🟡 AT-SPI 树转换、open_app 校验、fail-safe 传播测过；沙箱无 X11 桌面，**未端到端运行** |
| Android 后端 | 🟡 uiautomator XML 解析、adb 命令构造与注入防护（假 adb）；**未连接真机/模拟器** |
| 模型后端 | 🟡 只测了请求构造与响应映射（OpenAI 兼容含 GPT-5/o 系列参数、Anthropic Messages、computer-use 工具与拖拽、UI-TARS 解析、坐标变换）；**没有用真实 API 跑过任务**，GPT-5/o 系列参数兼容是按文档写的、未经真实调用验证 |
| 脚本策略 | ⚠️ 只证明非模型部分可用；步骤级 L2 在脚本模式下是乐观桩，子目标 / 任务收尾的 L2 桩一律返回 uncertain（所以脚本任务必须靠可规则核验的 expect_text 才能完成），不代表真实模型能力 |
| 研究结论 | ❌ 还没有任何真实模型实验数据；目前的数字（成功率 1.0 等）只来自脚本策略，**不能**当作方法有效的证据 |

## 已知限制

见 [docs/design.md §8](docs/design.md)。主要是：Wayland 不支持、Android 中文输入需 ADBKeyboard、Claude computer-use 采用无状态逐步调用、
未实现 MCP / 代码动作 / OmniParser。v0.3 新增的限制说明：

- token / 成本上限只能在调用返回后才知道用量，所以语义是“累计达到上限后拒绝之后所有调用”，最后一次调用可能让累计值略超上限（不超过单次用量）。
- 拒绝的“同一动作”按签名判断（指针动作 = 类型 + 目标元素名；快捷键 = 规范键集合；输入 = 文本）。同一个危险按钮换一种描述
  但反查不到元素名时可能被视为新动作，会再次询问（不会自动执行）。
- Web 白名单拦截主页面导航时，Chromium 会先显示拦截错误页，agent 随后回到最后一个合法 URL（页面会重新加载，未提交的表单内容会丢失）。
- Claude computer-use 的光标位置：Web 与桌面后端在观察里提供真实光标；其他情况用 actor 自己跟踪的上一次指针落点。

## 下一步

建议把精力**集中在 Web + Windows 两个后端**，先拿到真实模型数据，而不是继续扩平台：

1. **Web（已最完整）**：用 1–2 个真实模型（例如 Qwen2.5-VL@DashScope 做规划 + UI-TARS-1.5-7B 做定位，或 GPT-4o / Claude）在
   `tasks/web` 上跑主方法与全部消融，每个配置 ≥3 次重复，**分别在有干扰 / 无干扰**（`--no-disturb` 对照）条件下统计
   `success_rate`、`false_done_rate`、`recovery_rate`、`budget_exhausted_runs`、调用数与成本。重点看：收尾核验能把 false done 压低多少、
   代价是多少次额外调用。
2. **Windows（v0.1 有真机经验）**：在真机或虚拟机上复测 v0.3（DPI、UIA IsPassword、open_app 改为 argv / ShellExecute 后的行为、
   fail-safe 中止），再跑 `tasks/windows` 的抢焦点 / 弹窗干扰任务，同样做有 / 无干扰对照。
3. 扩充任务：每个平台 20–30 个带客观判分的任务，干扰按步注入（`disturbance.at_step`）保证可复现；把真实失败轨迹按失败类型归档，
   用来校准 L1 规则与恢复策略。
4. macOS / Linux / Android 暂时只维护 fixture 测试，等 Web + Windows 的结论稳定后再做真机验证。

## 致谢与参考

动作空间、坐标约定、a11y+视觉融合、反思、安全设计参考了 Agent S2/S3、UFO²/UFO³、UI-TARS、OpenCUA、browser-use、
Mobile-Agent-v3、AppAgent、OS-Copilot、Cradle、Anthropic computer-use 参考实现、OpenAI CUA sample app、Open Interpreter，
以及 OSWorld / AndroidWorld / WindowsAgentArena 评测框架。详见 [docs/research.md](docs/research.md)。
