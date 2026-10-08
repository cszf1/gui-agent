# 开源 Computer-Use Agent 调研：设计模式对比与本项目的取舍

> 调研时间：2026-10。资料来源以各项目 GitHub README / 源码和 arXiv 摘要为准（链接见每节）；
> 表中“我们是否采用”一栏对应 `gui-agent` v0.2 的实际代码位置。凡是未从一手资料确认的细节，正文会标“（据论文/社区描述）”。

## 1. 总览对比表

| 项目 | 平台 | 观察 | 动作空间 / 输出格式 | 坐标约定 | 定位方式 | 反思 / 验证 | 记忆 | 安全 / 人工介入 | 扩展（代码 / MCP） |
|---|---|---|---|---|---|---|---|---|---|
| **Agent S2 / S3**（simular-ai） | Win / macOS / Linux（+OSWorld、WAA、AndroidWorld） | 截图 | 高层 ACI 动作，grounding agent 翻译成 pyautogui 代码 | grounding 模型输出分辨率（UI-TARS-1.5-7B 用 1920×1080，72B 用 1000×1000） | **生成器与 grounder 分离**；S2 提出 Mixture-of-Grounding | S3 默认开启 reflection agent；bBoN/BJudge 多轮次挑选 | `max_trajectory_length` 只保留最近 N 张图 | README 警告会执行 Python 代码 | S3 `call_code_agent` 本地代码环境（bash/python，30s 超时） |
| **UFO² / UFO³**（microsoft） | Windows（UFO³ Galaxy 扩到 Linux / Android） | UIA 控件树 + 截图，**UIA 与视觉解析混合检测** | GUI 动作 + 原生 API（Win32 / COM）统一动作层 | 控件中心 | 控件候选编号 | 推测式多动作（speculative multi-action，官方称少 51% LLM 调用） | 知识库 RAG（文档、演示、执行轨迹） | Picture-in-Picture 隔离桌面，人机并行不干扰 | MCP 赋能的设备 agent；UFO³ 用 DAG 编排多设备 |
| **UI-TARS / UI-TARS-desktop**（bytedance） | Win / macOS / Browser / Android | 纯截图（原生端到端模型） | `Thought: … Action: click(start_box=…)`；COMPUTER_USE / MOBILE_USE / GROUNDING 三套提示 | 1.5（Qwen2.5-VL 底座）输出 **smart_resize 后图像的绝对像素**，需按 README_coordinates 换算 | 模型自己出坐标 | `wait()` 动作；`call_user()` 交给用户 | 历史截图 | README 承认可被滥用于过 CAPTCHA | Agent TARS 内核基于 MCP |
| **OpenCUA / AgentNet**（xlang-ai） | Win / macOS / Ubuntu 数据；OSWorld 评测 | 截图（默认 3 张历史图） | pyautogui 风格低层动作（click / moveTo / write / press / scroll / terminate） | `--coordinate_type qwen25` | 端到端 | **反思式长 CoT**：反思上一步、解释选择、预测下一状态 | L2 CoT 作为“内心独白” | – | AgentNetTool 录制屏幕+键鼠+**无障碍树**，用于数据 |
| **browser-use** | 浏览器（Playwright/CDP） | DOM → 带编号的可交互元素 + 截图 | click(index) / input / scroll / navigate / go_back / extract / done | 元素编号（无坐标） | DOM 索引 | `is_done` 只代表 agent 自称完成，文档明确要求**独立验证外部动作** | 步骤历史 | sensitive data、allowed_domains、secret bindings | 自定义 Tools、MCP Server |
| **Mobile-Agent-v3 / GUI-Owl**（X-PLUG） | Android / PC / Web | 截图 | GUI-Owl 原生动作 | 模型坐标 | 端到端 + 多 agent | 框架提供规划、**进度管理、反思、记忆**；GUI-Critic-R1 做“动作前”错误诊断 | Notetaker 式关键信息记录（据论文描述） | – | GUI-Owl-1.5 支持 tool/MCP 调用；ToolCUA 学习 GUI/工具切换 |
| **AppAgent**（Tencent） | Android（adb） | 截图 + 带数字标签的元素（XML）；网格覆盖层兜底 | tap / text / long_press / swipe / grid | 元素编号或网格 | SoM 编号 | 探索阶段对上一步做反思 | **探索/演示生成的元素文档库** | – | – |
| **OS-Copilot / FRIDAY** | Linux / macOS | 终端 + 文件 + 应用（+vision 版） | 以**代码/工具**为主 | – | – | 自我改进（self-learning 工具库） | 工具库 | 免责声明：可能导致数据丢失 | 工具注册与 API 服务 |
| **Cradle**（BAAI） | 游戏 + 通用软件 | 截图（+OCR、目标检测、SAM） | 键鼠；atomic / composite skills | – | 检测模型辅助 | 信息收集 → 自我反思 → 任务推理 → 技能整理 → 动作规划 | 技能库 + 情节记忆 | 实时游戏需暂停等待模型 | 技能注册表 |
| **Anthropic computer-use 参考实现** | Linux（Docker + Xvfb + xdotool） | 截图 | `computer` 工具：left_click / double_click / key / type / scroll / wait / left_click_drag / zoom… | **截图缩放到 XGA/WXGA/FWXGA**，执行时按比例放大 | 模型出坐标 | 每次动作后延迟 2s 再截图 | 对话历史（tool_result 链） | 建议在隔离 VM 运行 | bash / edit 工具 |
| **OpenAI CUA sample app** | 浏览器（Playwright）/ 桌面（PyAutoGUI） | 截图 + 代码运行时 | **模型写代码**（JS/Python）在持久运行时里操作，可在一次调用里循环并自检 | – | Playwright locator / 坐标 | 代码里“检查每个改动是否生效”；README：**最终答复不代表任务成功** | 持久运行时状态 | 无 OS 沙箱、真实键鼠，需专用桌面；崩溃可能残留按键 | – |
| **Open Interpreter** | macOS / Linux / Windows | 终端为主，computer use 走 QA skill（agent-browser / trycua） | 代码执行 | – | – | – | – | 原生沙箱 + 审批（sandbox & approvals） | MCP、ACP、skills |
| **OSWorld** | Ubuntu / Windows VM | screenshot / a11y_tree / SoM 多种观察类型 | pyautogui 或 computer_13 | – | – | **基于执行结果的判分脚本** | – | VM 快照复位 | – |
| **AndroidWorld** | Android 模拟器 | 截图 + UI 元素 | JSONAction：click / long_press / input_text / navigate_back / navigate_home / open_app / scroll / swipe / wait / **status / answer** | index 或 (x,y) | 元素 index | 每任务步数上限≈人类 2×；**持久化奖励信号** | – | – | 参数化任务（百万级变体） |
| **WindowsAgentArena** | Windows 11 VM（Docker） | `--som-origin`：a11y / omni / oss / **mixed-omni（a11y+OmniParser，最佳）** | Navi agent | SoM 编号 | 混合检测 | – | – | VM 黄金镜像复位 | Azure 并行评测 |

链接：
[Agent-S](https://github.com/simular-ai/Agent-S) ·
[S2 论文](https://arxiv.org/abs/2504.00906) · [S3 论文](https://arxiv.org/abs/2510.02250) ·
[UFO](https://github.com/microsoft/UFO) · [UFO² 论文](https://arxiv.org/abs/2504.14603) ·
[UI-TARS](https://github.com/bytedance/UI-TARS)（[prompt.py](https://github.com/bytedance/UI-TARS/blob/main/codes/ui_tars/prompt.py)、[坐标说明](https://github.com/bytedance/UI-TARS/blob/main/README_coordinates.md)）·
[UI-TARS-desktop](https://github.com/bytedance/UI-TARS-desktop) ·
[OpenCUA](https://github.com/xlang-ai/OpenCUA) ·
[browser-use](https://github.com/browser-use/browser-use)（[docs](https://docs.browser-use.com/llms.txt)）·
[MobileAgent](https://github.com/X-PLUG/MobileAgent) · [Mobile-Agent-v3 论文](https://arxiv.org/abs/2508.15144) ·
[AppAgent](https://github.com/TencentQQGYLab/AppAgent) ·
[OS-Copilot](https://github.com/OS-Copilot/OS-Copilot) ·
[Cradle](https://github.com/BAAI-Agents/Cradle) ·
[Anthropic computer-use demo](https://github.com/anthropics/claude-quickstarts/blob/main/computer-use-demo/computer_use_demo/tools/computer.py) ·
[OpenAI CUA sample app](https://github.com/openai/openai-cua-sample-app) ·
[Open Interpreter](https://github.com/openinterpreter/open-interpreter) ·
[OSWorld](https://github.com/xlang-ai/OSWorld) ·
[AndroidWorld](https://github.com/google-research/android_world)（[json_action.py](https://github.com/google-research/android_world/blob/main/android_world/env/json_action.py)）·
[WindowsAgentArena](https://github.com/microsoft/WindowsAgentArena)

## 2. 横向提炼的设计模式

1. **动作空间收敛到同一组原语**。UI-TARS、AndroidWorld、Anthropic 工具、browser-use 的动作基本可以一一映射：
   click / double / right / long_press / drag / scroll(direction) / type / hotkey / wait / open_app / back / home / navigate / done / fail / ask_user。
   差异只在“谁来给坐标”和“平台是否支持”（Web 没有 home，Android 没有右键）。
2. **坐标约定是最容易出错的一环**。至少有四种：归一化 0–1000（UI-TARS-1.0、SeeClick、OS-Atlas）、
   smart_resize 后绝对像素（Qwen2.5-VL、UI-TARS-1.5、OpenCUA qwen25）、缩放截图像素（Anthropic，缩到 XGA/WXGA）、
   原始物理像素。macOS Retina / Windows DPI 又在“截图像素 ↔ 输入坐标”之间加一层。
3. **a11y + 视觉融合优于任一单独来源**：UFO² 的 UIA+视觉混合检测、WAA 的 mixed-omni 最佳、Agent S2 的 Mixture-of-Grounding、
   AppAgent 的 XML 编号 + 网格兜底、browser-use 的 DOM 编号。规律：**能从结构化树拿到的不要让模型猜坐标**。
4. **规划与定位分离**（Agent S 系列、UGround 思路）vs. **端到端原生模型**（UI-TARS、OpenCUA、GUI-Owl）。
   前者便于替换模型、调试和做消融；后者延迟低、对没有 a11y 的界面更稳。
5. **反思/批评模块**普遍存在（Agent S reflection、Mobile-Agent Reflector、Cradle self-reflection、OpenCUA 反思 CoT、
   GUI-Critic-R1 动作前诊断），但**很少有项目把“动作是否真的生效”做成可计量的、分级的验证**——
   browser-use 和 OpenAI sample app 都在文档里明确提醒“agent 自称完成 ≠ 真完成”。这正是方向 A 的空间。
6. **代码即动作**（OpenAI CUA sample、Agent S3 code agent、OS-Copilot、Open Interpreter）：一次调用内循环+自检，减少往返；
   代价是安全面变大。
7. **记忆**：短期轨迹窗口（Agent S `max_trajectory_length`）、里程碑/笔记（OS-Symphony、Mobile-Agent Notetaker）、
   长期知识库（UFO² RAG、AppAgent 元素文档、Cradle 技能库）。
8. **安全**：隔离环境（Anthropic Docker、UFO² PiP 虚拟桌面、OSWorld/WAA VM）、人工确认（UI-TARS `call_user`、
   OpenAI 安全检查）、域名白名单与敏感数据隔离（browser-use）。
9. **评测**：基于执行结果的判分（OSWorld、AndroidWorld 持久奖励、WebArena），步数上限按人类耗时设定，
   多轮次 + 挑选（Agent S3 bBoN）显著提分但成本倍增。
10. **MCP**：UFO³、Agent TARS、GUI-Owl-1.5、browser-use 都把 MCP 当作“GUI 之外的工具通道”。

## 3. 本项目采用了什么、为什么

| 设计 | 来源 | 在 gui-agent 中的实现 | 理由 |
|---|---|---|---|
| 统一动作空间 + 别名解析 | UI-TARS、AndroidWorld、Anthropic、browser-use | `gua/actions.py`（`parse_action` 接受 left_click / input_text / navigate_back / finished / call_user 等别名）；`UNSUPPORTED` 按平台声明 | 同一套验证/恢复逻辑要跨 6 个后端复用；接入新模型只需写解析器 |
| 坐标约定显式化 | UI-TARS README_coordinates、Agent S grounding_width/height、Anthropic scale_coordinates | `gua/coords.py`（norm1000 / norm1 / resized / pixel 双向换算）；`llm/anthropic.py::scaling_target` 复刻参考实现的缩放规则；macOS 后端按 Retina 倍率换算 | 坐标错位是真机失败的首要来源（v0.1 QQ 实测的 DPI 问题） |
| 统一无障碍元素模型 + a11y 优先定位 | UFO²、WAA mixed、AppAgent、browser-use、Agent S2 MoG | `env/a11y.py` 把 UIA / AX / AT-SPI / uiautomator XML / DOM 统一成 `UIElement(role,name,rect,state,offscreen)`；`grounding.py` 先 element_id/唯一同名，再 VLM，再局部放大 | 零模型调用、可解释；同名控件不猜，交给视觉（避免“身份失配”） |
| planner–grounder 分离为默认，端到端可选 | Agent S 系列 / UI-TARS、Claude computer-use | `actor.kind: json`（默认）/ `uitars` / `claude_computer_use`；`actor.coord_space` 让 json actor 也能直接给坐标 | 研究上要做“定位来源”消融；工程上要能直接跑 UI-TARS / Claude |
| **分级执行验证 L0/L1/L2 + 事件触发** | 方向 A 自有设计；browser-use / OpenAI sample 的“自称完成≠完成”提醒 | `verify/verifier.py`：L0 执行错误，L1 规则（失焦、新对话框、遮挡、忙碌指示、输入框值、复选框翻转、URL/前台变化、像素差），L2 VLM；`trigger=on_event` 只在 L1 不确定时花模型调用 | 这是本项目的研究贡献；L1 的 a11y 证据在 6 个平台通用 |
| 动作前状态核对（pre-action check） | 时间失配研究（stale observation）；Anthropic 参考实现“动作后延迟再截图”的反面 | `agent.py::_precheck_focus`：观察到前台已不是任务窗口时，不调用 actor，直接恢复焦点 | 避免基于错误窗口做决策；Web 集成测试里“新标签页抢焦点”靠它恢复 |
| 分类恢复 + 平台化最小动作 | v0.1 + 各平台惯例 | `recovery.py`：REFOCUS（focus_window / open_app）、DISMISS（Esc / back）、按距离计算滚动格数、UNDO（ctrl/cmd+z） | 同一种失败在不同平台的“最小修复”不同 |
| 反思器写入记忆笔记 | Agent S reflection、Mobile-Agent Reflector/Notetaker、OpenCUA 反思 CoT | `reflection.py` + `Memory.notes`，可在配置中关闭做消融 | 与验证器分工：verdict 说“发生了什么”，反思说“下一步改什么” |
| 里程碑记忆 + 重规划后失效 | OS-Symphony、Agent S2 proactive hierarchical planning | `memory.py::invalidate_after`、`planner.replan(..., lessons)` | 防止用旧证据宣告完成（任务状态失配） |
| 安全闸门、ask_user、域名白名单 | OpenAI CUA 安全检查、UI-TARS call_user、browser-use allowed_domains、Anthropic 隔离建议 | `safety.py`：allow / confirm / deny；危险关键词（中英）、破坏性命令模式、危险快捷键、密码框；`ask_user` 动作 | 跨平台真机运行前的最低限度保护；被拒动作走 L0 → 重规划 |
| 预算与限制 | OSWorld / AndroidWorld 步数上限、同预算对比的实验要求 | `Budget(max_calls,max_tokens)` + 全局/子目标步数 + 墙钟时间 | 公平对比；防失控 |
| 轨迹 JSONL + 截图 + HTML 回放 | OSWorld 结果目录、OpenAI sample app replay、OpenCUA 数据格式 | `logger.py`、`report.py`、`gua replay` | 失败分类和人工复核；轨迹可直接当示范数据 |
| 基于执行结果的判分 + 可复现干扰 | OSWorld、AndroidWorld、WebArena | `eval/checkers.py`（文件 / DOM / uiautomator），`eval/disturb.py` 按步注入（new_tab / popup / home / minimize …） | v0.1 的时间触发干扰不可复现，改为 at_step |
| 本地静态页面任务 + 脚本策略 | OpenAI sample app 的 labs 思路 | `tasks/web_assets/*.html` + `gua/scripted.py` | 无 API key 也能在 CI 中端到端跑真实浏览器 |

## 4. 暂不采用（及原因）

> v0.6 更新：下面的“代码即动作 / 本地代码执行”和“MCP 工具接入”已以受限形式实现（默认关闭的 shell / 文件 / API 工具通道，
> 结果仍经同一闸门与验证器；`gua mcp` 服务器），见第 5 节与 [design.md](design.md) §9。其余条目仍未采用。

- **代码即动作 / 本地代码执行**（OpenAI CUA sample、Agent S3 code agent）：安全面大，且会绕过 GUI 执行验证，与方向 A 的研究对象冲突。保留为未来扩展（可作为独立“工具通道”，验证器仍只看 GUI 结果）。
- **推测式多动作**（UFO²）：会让“每步验证”的语义变复杂；可在 L1 能定论时批量执行，列为 TODO。
- **多轮次 + Behavior Judge 挑选**（Agent S3 bBoN）：成本 N 倍，超出本科 SRTP 预算；但其“行为叙述”思路可用于我们的轨迹报告。
- **MCP 工具接入**：GUI 之外的工具通道（UFO³、Agent TARS、GUI-Owl-1.5）。接口上留了位置（Action 可扩展），本版本未实现，避免在 GUI 验证之外引入新的成功判定来源。
- **OmniParser / SoM 视觉检测**（WAA）：需要 GPU 与额外模型；当前以 a11y + grounding VLM 为主，没有 a11y 的界面（游戏、Canvas）退化为纯视觉。
- **知识库 RAG / 技能库**（UFO²、AppAgent、Cradle）：需要先积累轨迹；本版本的 JSONL 轨迹即为后续数据来源。

## 5. v0.6 对标

> 资料抓取时间：2026-10-08。下表每一格只写**所引页面上能看到的内容**；页面没写的标“未见”（不代表没有该能力），
> 来自第 1 节旧调研、本次没有重新核对的内容标“（第 1 节，未复核）”。产品名与功能状态以页面为准，会随时间变化。

来源（本节编号引用）：

- [C1] Claude computer use tool：https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool
- [X1] Codex / ChatGPT Computer Use：https://developers.openai.com/codex/app/computer-use
- [O1] ChatGPT agent（含 Operator 说明）：https://help.openai.com/en/articles/11752874-chatgpt-agent/
- [G1] Grok Bot，Use the computer and apps：https://docs.x.ai/grok-bot/computer-and-apps
- [D1] Cua Driver 文档：https://cua.ai/docs/cua-driver ；[D2] Cua 博客 Inside Windows computer-use：https://cua.ai/blog/inside-windows-computer-use
- [B1] Cua-Bench 任务定义：https://cua.ai/docs/cua-bench/reference/task-definition
- [U1] UFO² Hybrid GUI–API Actions：https://microsoft.github.io/UFO/ufo2/core_features/hybrid_actions/
- [BU] browser-use README：https://github.com/browser-use/browser-use
- [AS] Agent S README：https://github.com/simular-ai/Agent-S
- [TD] UI-TARS-desktop README：https://github.com/bytedance/UI-TARS-desktop

### 5.1 能力 × 系统对照表

| 能力 | Claude computer use 工具 [C1] | Codex / ChatGPT Computer Use [X1] | ChatGPT agent / Operator [O1] | Grok Bot 云电脑 [G1] | Cua Driver [D1][D2] / Cua-Bench [B1] | UFO² [U1] | browser-use [BU] | Agent S [AS] | UI-TARS-desktop [TD] | **gua v0.6** |
|---|---|---|---|---|---|---|---|---|---|---|
| 当前可用性 | 可用（Claude API 等，`computer_toolset_20260801`） | macOS / Windows 桌面 App 插件 | **页面写明“ChatGPT agent is no longer available”**；Operator 已并入 agent，Operator 网站不可访问 | 可用 | 开源，macOS / Windows / Linux | 开源，Windows | 开源库 + 云服务 | 开源框架 | 开源桌面 App | 开源研究代码，未发布 Release |
| 观察 | 截图 + `zoom` 局部放大；不含无障碍树 | 屏幕画面（macOS 需屏幕录制 + 辅助功能权限） | 虚拟浏览器窗口截图 | 页面未细述 | 窗口像素 + UIA/MSAA、AX、AT-SPI 无障碍树 | UIA 控件 + 视觉检测（第 1 节，未复核） | 浏览器（DOM，第 1 节，未复核） | 截图 + grounding 模型 | VLM 截图识别 | 截图 + 统一无障碍元素（UIA / AX / AT-SPI / uiautomator / DOM） |
| 混合动作（代码 / API + GUI） | 与 bash、文本编辑工具同用 | 有插件 / MCP 时建议优先结构化集成；shell 与文件编辑走沙箱和审批设置 | apps（connectors）作为数据源 | 浏览器 + 命令行 + 文件 + connectors；有 connector 时优先 | 动作层优先调用 UIA Invoke/Toggle/Value 等模式，失败再像素输入 | **API 优先、GUI 兜底**（MCP 服务器包装 Excel/Word/PowerPoint COM 等），由模型选择 | 自定义 Tools、MCP（README 指向文档） | 可选本地 coding 环境（Python / Bash） | Agent TARS：GUI / DOM / 混合浏览器策略、MCP 工具 | 语义动作（invoke）+ 受白名单约束的 shell / 文件 / 注册 API，GUI 兜底；默认全部工具关闭 |
| 后台执行（不抢用户指针 / 前台） | 由集成方的环境决定（参考实现在 Docker + Xvfb 中） | **Windows 只能前台**（会移动指针、占用前台）；macOS 可在后台跑限定任务 | 云端虚拟浏览器 | 云端电脑，每个 Bot 一块屏幕 | **默认 background**；不会静默抢前台，必要时返回 `background_unavailable` 由调用方选择 `dispatch:"foreground"`；独立合成光标 | 未见（本页） | 未见（本页） | 未见 | 本地 / 远程 operator | Web DOM 与 Windows UIA 语义路径不移动指针；沙箱电脑内 AT-SPI 语义动作；侵入检测记录指针 / 前台变化 |
| 动作后“真的生效了吗”的核验 | 文档建议提示模型每步截图并自评；承认 Claude 有时不检查结果就假定成功 | 未见专门机制（示例提示“每次修改后重跑同一 UI 流程”） | 未见 | 未见 | 博客把 verification 列为动作层能力之一，并说明遮挡截图会被标记；细节未见 | 常见陷阱中提醒“API 执行要配合截图验证” | 未见（本页） | 反思 agent（README） | 未见 | **规则验证每一步**：后置条件（勾选翻转、值相等、选中、焦点、文本出现/消失、URL、像素区域）+ 无障碍树差分；后台动作无生效证据时不算成功 |
| 失败后换模态恢复 | 未见 | 未见 | 未见 | 未见 | 把 `background_unavailable` 等作为显式错误返回给调用方 | API 不可用时回退 GUI | 未见 | 未见 | 未见 | GUI 点击无效 → 语义动作；后台语义无效 → 前台（各一次）；非幂等 invoke 不自动重放 |
| 完成结论的证据 | 模型最终文本回复 | 未见 | 输出含来源链接或截图 | 结果文件 / 预览 | 轨迹录制（文档目录有 trajectories 页，未细读） | 评测 agent / 日志（第 1 节，未复核） | 未见（本页） | 未见 | 事件流（Agent TARS） | **完成凭据**（receipts.json）：结论、证据等级、逐步模态 / 路由 / 后置条件、判定时刻截图与无障碍树 SHA-256 |
| 安全闸门 / 人工确认 | 截图提示注入分类器；建议对有实际后果的操作请人确认；SDK 提供审批回调 | 按 App 授权（可“Always allow”）；敏感或干扰性操作前请求许可；不能自动操作终端和管理员授权 | 高影响操作需确认、提示注入监控、部分网站“watch mode”、网站黑名单 | 敏感步骤请用户接管；本地电脑命令需开启并审批 | 未见（本页） | 未见（本页） | 云浏览器的隐身 / 验证码相关能力（不同取向） | README 警告会执行任意代码 | 未见 | 同一闸门覆盖 GUI / 语义 / shell / 文件 / API；换模态生成的前台动作重新过闸，拒绝记忆共享 |
| 人工接管 | 未见 | 可随时停止任务或接管电脑 | **Take over browser**：用户控制期间不截图，交还后尝试接续 | 可打开电脑接管密码 / 2FA / CAPTCHA / 支付等步骤 | 未见 | 未见（本页） | 未见 | 未见 | 未见 | 沙箱 `takeover` 期间拒绝 agent 的动作与截图；`handback` 后 epoch+1，旧观察全部失效 |
| 隔离环境 / 快照重置 | 建议专用 VM / 容器；参考实现为 Docker | Windows 可在虚拟机里运行以免占用主桌面 | 云端虚拟浏览器 | 持久云电脑；Reset 从上次保存的快照重建（Bot 之间的屏幕不是安全边界） | Cua Sandbox；Cua-Bench 每个任务变体启动沙箱，`setup` → agent 或 `solve` oracle → `evaluate` | 未见（本页） | 本地或云浏览器 | 未见 | 远程 computer operator | 本机 Xvfb 沙箱电脑（快照 / 重置 / noVNC）；Dockerfile 已写但**镜像未构建** |
| MCP 暴露 | 未见（本页） | 插件包含 MCP 服务器 | 未见 | 未见 | `cua-driver mcp` | 内部用 MCP 服务器组织动作 | 文档提及 MCP | 未见 | MCP 集成 | `gua mcp`：observe / act / verify / run_task / takeover / handback / snapshot / reset |
| 基准 / 评测接入 | 未见（本页） | 未见 | 未见 | 未见 | Cua-Bench：`@cb.tasks_config` / `setup_task` / `solve_task` / `evaluate_task`，奖励为 `list[float]` 均值 | WAA / OSWorld（第 1 节，未复核） | 自有 benchmark | OSWorld / WAA / AndroidWorld 成绩（README 自报） | 未见 | 自有任务集 + `gua eval --workers N`；Cua-Bench 子集兼容层、OSWorld 形状 JSON 子集转换（见 5.3） |

### 5.2 gua 的差异点（不是“更强”的证据）

下面四点是 gua 的设计取舍，在上表所引页面里没有看到同样的组合；**这只说明设计不同，不能证明效果更好**——
gua 还没有任何真实模型实验数据（见 README“哪些验证过”）。

1. **已验证完成凭据**：完成结论附带可复查证据（证据等级、后置条件逐条结果、判定时刻画面摘要），续跑时先在当前画面重新验证。
   凭据证明的是“界面上可观察到的状态”，不是服务器端业务事实。
2. **换模态恢复**：GUI 点击无效时改用语义动作，后台语义动作没有生效证据时改走前台；非幂等的 invoke 不自动重放，避免执行两次。
3. **后台动作的效果核验**：Cua Driver 的取向是“诚实地报告后台不可用”；gua 在此之上要求后台动作**证明自己生效**
   （勾选翻转、值写入、无障碍树或像素变化），否则不算成功。
4. **跨模态的同一安全闸门**：GUI、语义动作、shell、文件、API 走同一个 SafetyGuard；换模态生成的动作重新过闸，拒绝记忆共享，
   不能靠换一种模态绕过拒绝。

### 5.3 这一版的评测证据到哪一步

- `docs/benchmarks/modalities-2026-10-08.json`：**脚本策略（固定 demo 步骤）**在本机沙箱电脑与 mock 故障注入上的模态对比，
  标签即写明“NOT model evidence”。它说明执行 / 验证 / 恢复管线按设计工作（例如后台投递被丢弃、指针失效时换模态恢复能完成任务，
  去掉换模态恢复则失败），不说明任何模型能完成任务。
- Cua-Bench 兼容层（`gua/eval/cuabench_compat.py`）是**子集 shim**，不是官方 `cua-bench` 包：官方文档示例 `hello_file_env` 代码不改即可在
  gua 沙箱（`--shell`）里用 oracle 跑通；bench_ui / pywebview 窗口相关方法未实现，调用会抛 `NotImplementedError`。
- OSWorld 适配（`gua/eval/osworld.py`）只转换能忠实复现的子集（launch / execute / command / sleep；`exact_match`、`check_include_exclude`）。
  `tasks/osworld_subset/gua_form_pro.json` 是**按 OSWorld 任务形状手写的合成任务，不是 OSWorld 官方任务**，不能当作 OSWorld 成绩。
