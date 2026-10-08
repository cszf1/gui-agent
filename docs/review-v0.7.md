# v0.7 审查与改进（2026-10-08）

基线：`f195cdb`（v0.6 补丁 + 三个用户提交 `cbe6199`、`834ba44`、`f195cdb`）。环境：Linux aarch64 / Python 3.12，
真实 Chromium，Xvfb + openbox + GTK + AT-SPI，`NO_PROXY=127.0.0.1,localhost`。单条命令时限 120 s，测试分批运行。

## 测试数字

| | 默认模式 | `GUA_TEST_NO_PLAYWRIGHT=1` |
|---|---|---|
| 基线 `f195cdb` | 563 passed, **1 failed**（`test_shell_preserves_a_stricter_inherited_resource_limit`；另有一次 `test_parallel_sandboxes_are_isolated` 因 openbox 冷启动失败，重跑通过） | 485 passed, 8 skipped, **2 failed**（上面的 rlimit 用例 + `test_parallel_eval_with_sandbox_pool`；单独重跑 eval 文件又出现 3 个 openbox 启动 error） |
| v0.7 | **602 passed, 0 failed** | **525 passed, 8 skipped, 0 failed** |

新增用例：`tests/test_review_v07.py`（25 个回归，均先在 `f195cdb` 上运行确认失败，只有 1 个正向对照用例在旧代码上也通过）、
`tests/test_v07_cua.py`（9 个，本地 HTTP 替身上的端到端）、`tests/test_v07_grounding_som.py`（4 个）。

首次 GitHub CI 的 Windows 3.10/3.12 发现凭据继承用例硬编码 `/bin/sh`，导致 `FileNotFoundError`。
该用例改用当前 Python 启动真实应用子进程；核验普通标记变量被继承、两个凭据变量均未继承，保留跨平台的实际进程检查。

## 先看三个用户提交

整体方向正确：进程组清理与有界输出、继承更严格 rlimit、Darwin 不设 `RLIMIT_AS`、观察快照绑定、同控件身份兜底、
未确认激活不重放、MCP 会话共享安全状态、Electron E2E 绑定新任务。审查中发现与这三个提交直接相关的问题：

- `cbe6199` 的 openbox 就绪等待只解决了一半：X socket 文件早于 Xvfb 接受连接出现，openbox 在这段间隙启动会立刻退出
  （本机 3 次冷启动复现 1 次，见 #11）。
- `cbe6199` 新增的 `uncertain_activation` 对**只有坐标**的点击不解析控件，聚焦文本框“看不出变化”会被记为未确认激活，
  之后再点同一字段被闸门拒绝；纯视觉 / computer-use actor 因此卡住（#9）。
- `cbe6199` 保留了 MCP `handback`，同一个 agent 令牌可以交还控制权，人工接管不再是人工的决定（#1）。
- `834ba44` 新增的继承 rlimit 用例在子进程里 `import gua`，没装包、直接在源码目录运行时失败（#14）。

## 发现与修订

严重度：高 = 可能造成虚假完成或绕过人工 / 安全边界；中 = 错误执行或明显能力缺陷；低 = 健壮性 / 协议细节。

| # | 严重度 | 区域 | 位置（修订前） | 问题与复现 | 修订 | 回归测试 |
|---|---|---|---|---|---|---|
| 1 | 高 | 沙箱 / MCP | `gua/sandbox/daemon.py` `_post_locked` `/handback`；`gua/mcp_server.py` `TOOLS` | agent 令牌即可 `POST /handback`；MCP 把 `handback` 暴露给模型，模型可以在人工接管期间自行收回控制权 | 交还需要独立的 `X-Gua-Control-Token`（`GUA_SANDBOX_CONTROL_TOKEN`，未设置时守护进程生成并只写 stderr）；`RemoteEnv` 无控制令牌时拒绝 handback；MCP 去掉 handback；`gua sandbox up` 打印控制令牌；`LocalSandbox.operator_env()` 给人工端 | `test_agent_token_cannot_hand_control_back_to_itself`；`test_v06_remote.py` 接管用例改为人工端交还 |
| 2 | 高 | 完成核验 | `gua/verify/verifier.py:381, 423, 279`；`postconditions.py:165` | `expect_text` 用子串匹配：`"saved"` 命中 `"Unsaved changes"`、`"Not saved"`，`"保存"` 命中 `"未保存"` → 子目标 / 任务被判完成 | `text_evidence()`：ASCII 词边界 + 同一行紧邻否定词（not/no/failed to/未/没有/无法/不能/不…）排除；期望文本自身含否定词时照常匹配。用于 check_goal / check_final / rule_check / `text_appears` | `test_goal_text_evidence_rejects_negated_or_partial_words`（5 例）、`test_final_check_rejects_unsaved_as_saved` |
| 3 | 中 | 完成核验 | `verifier.py` `check_goal` 后置条件分支 | 子目标后置条件结论为 unknown（例如两个同名复选框）时，可见文字仍让 L1 判成功 | 后置条件未成立时不允许文字单独判成功：无 L2 → uncertain，有 L2 → 交给 L2 | `test_unknown_postcondition_is_not_overridden_by_visible_text` |
| 4 | 中 | 收尾核验 | `verifier.py` `check_final` | 同时带 `expect_text` 和后置条件的子目标，收尾时只看文字，不复查后置条件（后续步骤把复选框又取消也会判完成） | 每个子目标的后置条件都复查；失败 → FAILED，未成立 → 不计入规则证据 | `test_final_check_rechecks_postconditions_of_subgoals_with_expect_text` |
| 5 | 中 | 动作语义 | `gua/actions.py` `validate`；`llm/anthropic.py` `tool_input_to_action` | 指针动作上的 `keys` 没有任何后端执行，Claude 的 shift+click 被当作普通点击执行并验证“成功” | `keys` 只允许出现在 hotkey/key_down/key_up；旧版 Claude 适配器遇到修饰键点击返回可读错误；新适配器把修饰键展开为 key_down → 点击 → key_up | `test_modifier_keys_on_pointer_actions_are_refused_not_dropped`、`test_claude_modifier_click_is_expanded_and_key_released` |
| 6 | 中 | Claude 适配 | `llm/anthropic.py` | `triple_click` 执行成 double_click、`middle_click` 执行成左键、`hold_key` 执行成按一下 | 不能如实执行的动作返回错误反馈；新 toolset 适配器在工具定义里禁用这些成员 | `test_claude_legacy_adapter_refuses_actions_it_cannot_execute_faithfully`（4 例） |
| 7 | 中 | 沙箱 | `daemon.py` `launch` | 白名单按 basename 匹配：`./gua-form`、`/tmp/x/gua-form` 都能启动 | 精确匹配：白名单里的裸名只经 PATH 解析，绝对路径必须完全相同，以 `-` 开头拒绝 | `test_launch_allowlist_matches_exact_names_not_basenames` |
| 8 | 中 | 沙箱 | `local.py` 启动参数；`daemon.py` `State.env` | `--token` 放在 argv（本机其他用户 `ps` 可见）；守护进程把含 `GUA_SANDBOX_TOKEN` 的环境原样传给它启动的应用（Docker 也一样） | 令牌经守护进程环境传入；`State.env` 去掉两个令牌变量 | `test_launched_apps_do_not_inherit_sandbox_credentials`、`test_local_sandbox_passes_credentials_by_environment_not_argv` |
| 9 | 中 | 恢复 / 闸门 | `gua/hybrid.py` `uncertain_activation` | 坐标点击不解析控件 → 文本框聚焦被记为未确认激活 → 再次点击同一字段被拒绝 | 坐标点击先取点下控件再判断 | `test_coordinate_click_on_a_text_field_is_not_an_unrepeatable_activation` |
| 10 | 中 | Windows 输入 | `gua/env/desktop.py` `clipboard_type` | ASCII 用 `pyautogui.write`（虚拟键）；中文输入法处于中文模式时按键进入候选框，`abc` 变成拼音候选 | Windows 上 ASCII 逐字符 `SendInput(KEYEVENTF_UNICODE)`，换行 / Tab 发真实按键，BMP 外字符发代理对；SendInput 未全部送达即报错；每个字符前后仍做焦点核对 | `test_windows_ascii_typing_bypasses_the_ime`；`test_execution_routes.py` 焦点移走即停止用例改为新路径。**未在真实 Windows 上运行** |
| 11 | 中 | 沙箱启动 | `gua/sandbox/local.py` `start` | X socket 先于可连接出现；dbus / openbox 在间隙启动即退出；冷启动约 1/3 失败 | `start_display_and_wm()`：先用 `xdotool getdisplaygeometry` 确认可连接，再启动 dbus 与 openbox；openbox 最多重启 2 次 | `test_window_manager_waits_for_x_and_restarts_once_if_it_exited`、`test_window_manager_gives_up_after_bounded_restarts`；修订后 6/6 次冷启动成功 |
| 12 | 低 | 沙箱 | `daemon.py` `do_POST`/`do_GET`、`files`、`shell` | `x:"abc"`、写入目录、负数 / NaN 超时会抛异常，连接被直接断开 | 统一返回 JSON 400/500；超时必须是正有限数 | `test_malformed_requests_get_a_json_error_instead_of_a_dropped_connection` |
| 13 | 低 | MCP 协议 | `mcp_server.py` `handle`、`run_task` | 有 id 无 method 的请求不回复（客户端一直等）；未知工具未用 `-32602`；`protocolVersion` 照抄客户端；`run_task(max_steps)` 永久改写会话配置 | 分别返回 `-32600` / `-32602`；只回应支持的版本；按次复制配置 | `test_mcp_protocol_errors_are_answered`、`test_mcp_run_task_max_steps_does_not_leak_into_the_session` |
| 14 | 低 | 测试 | `tests/test_review_v06_boundaries.py:283` | 子进程 `import gua` 依赖已安装包 | 子进程显式设置 `PYTHONPATH` 指向当前检出 | 该用例本身（基线失败，修订后通过） |
| 15 | 低 | 进程 | `gua/sandbox/process.py:16` | 受限执行包装器 `python process.py` 受 `PYTHONPATH` 等变量影响 | 改为 `python -I` | 加固项，由现有 shell 用例覆盖 |

### 已确认但未修（写进限制）

| 严重度 | 区域 | 说明 |
|---|---|---|
| 中 | 沙箱 | 启用 `--shell` 时，agent 的命令与守护进程同 uid，可以读 `/proc/<pid>/environ` 或结束守护进程。控制令牌只保护 HTTP / MCP 通道；需要真正隔离请把守护进程与命令执行放在不同用户 / 容器。 |
| 低 | 进程 | 用 `setsid` 逃出进程组的子孙进程不会被 `killpg` 清理；需要 cgroup（Linux）或 Job Object（Windows）。 |
| 低 | MCP | 不处理 `notifications/cancelled`，长 `run_task` 不能中途取消；JSON-RPC 批量请求逐条回复（2025-06-18 协议已取消批量）。 |

## 能力升级

对照 Claude computer use、OpenAI 电脑工具、UFO²、Agent S、UI-TARS、browser-use、cua 的公开做法，选了收益最高的三项。

| 升级 | 内容 | 测试 | 是否端到端运行 |
|---|---|---|---|
| 原生 computer-use 适配器（`gua/llm/cua.py`） | Claude `computer_toolset_20260801`（GA，无 beta 头，成员工具 + `toolset_name`）、`computer_20251124`（beta + `enable_zoom`）、`computer_20250124`；OpenAI Responses `computer` 工具（`computer_call.actions[]`、`computer_call_output`、`previous_response_id`），可选旧版 `computer_use_preview`。多轮会话，按子目标重置。**批量动作逐个经过定位 → 闸门 → 执行 → 验证，首个失败后停止**，其余块回官方要求的停止文本。screenshot / zoom / cursor_position 在适配器内用当前观察回答；zoom 从原始分辨率截图裁剪。修饰键展开为 key_down / 点击 / key_up（失败也释放）。截图历史批量裁剪（保留最近 3 张，超出 5 张才裁）。OpenAI `pending_safety_checks` 挂到动作上由安全闸门要求人工确认，只有人工批准后才回 `acknowledged_safety_checks`，拒绝则重开会话。用量计入 Budget（调用数 / token / 成本 / 延迟）。配置：`configs/models/claude_toolset.yaml`、`configs/models/openai_computer.yaml` | `tests/test_v07_cua.py` 9 个 | **只在本地 HTTP 替身 + MockEnv 上端到端运行**；协议格式按 2026-10 官方文档实现，没有调用真实 API |
| Set-of-Mark 观察融合（`gua/som.py`） | 可交互、启用、在视口内的控件画编号框（编号 = element_id），密码框只画框；几何不变，坐标动作照常有效。`actor.som: true` 时 JSON actor 发送 SoM 图并提示优先用编号。没有 OCR | `tests/test_v07_grounding_som.py` 2 个 | 单元测试；截图经目视检查 |
| 置信度定位与放大复核（`gua/grounding.py`） | `grounding.refine: true`：VLM 全屏粗定位后围绕该点裁剪放大再定位；两点距离（占对角线比例）≤ `agree_tol` 为高置信（0.9），否则低置信；a11y 精确 1.0、模糊 0.8。`grounding.min_confidence` 以下 agent 不点击，当作定位失败反馈给模型 | 同上 2 个 | 单元测试 + MockEnv 上的 agent 循环 |

没有做的候选：MCP 取消、OCR 融合、文件上传 / 下载与剪贴板动作、等待空闲检测的独立 API、Docker 镜像重新构建（本轮没有改镜像内容，只改了入口脚本注释与运行示例）。

## 复现

```bash
export NO_PROXY=127.0.0.1,localhost
python -B -m pytest -p no:cacheprovider -q tests/test_review_v07.py tests/test_v07_cua.py tests/test_v07_grounding_som.py
# 全量（分批，避免单条命令超时）
python -B -m pytest -p no:cacheprovider -q --ignore=tests/test_v06_remote.py --ignore=tests/test_v06_eval_adapters.py
python -B -m pytest -p no:cacheprovider -q tests/test_v06_remote.py tests/test_v06_eval_adapters.py
GUA_TEST_NO_PLAYWRIGHT=1 python -B -m pytest -p no:cacheprovider -q
```

## 仍然不能宣称的

- 没有真实模型 API 的任务数据；新适配器只在替身上运行过，不能宣称成功率或速度超过其他 agent。
- Windows 中文输入法修订按 SendInput 文档实现，未在真实 Windows + 微软拼音上验证；建议在 Windows CI 的 WinForms 检查里加一项“中文输入法开启时输入 ASCII”。
- 文字证据的否定词规则是启发式，覆盖常见英文 / 中文写法，不是语义理解。
