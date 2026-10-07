# v0.3 代码审查修复对照表

流程：每一条审查意见都**先写回归测试并确认它在 v0.2 上失败**，再修复，再确认测试通过。
（本仓库当前没有 git 历史，所以“提交级改动”一栏写的是每条修复对应的一组文件改动。）

- 回归测试：`tests/test_review_v03.py`（条目 1–8、10–12）、`tests/test_web_allowlist.py`（条目 9，真实 Chromium + 本地 HTTP 服务器）
- 在 v0.2 代码上跑这批新测试：**73 个失败，5 个通过**。通过的 5 个是对照用例，本来就该通过：
  `test_i02_final_check_can_be_disabled_explicitly`（关闭开关的行为）、`test_i04_grounder_uses_actually_sent_image_size[norm1000]`
  （归一化坐标与图像尺寸无关）、`test_i08_dangerous_hotkey_aliases[keys1]`（`Shift+Delete`）/`[keys7]`（`Alt+F4`，v0.2 已覆盖）、
  `test_i09_allowed_redirect_still_works`（白名单内的重定向不能被误拦）。
- 在 v0.3 上：全部测试 **130 passed**（v0.2 原有 52 个 + 新增 78 个，含参数化用例）。

| # | 审查意见 | 改动（文件 → 做了什么） | 回归测试 |
|---|---|---|---|
| 1 | fixed_retry 绕过安全拒绝；恢复动作不过安全闸门 | `agent.py`：新增唯一执行出口 `_execute_gated`，actor 动作、恢复 / 重试 / 撤销 / 滚动、动作前焦点恢复全部经过它；`safety.py`：拒绝记录动作签名（`denied`），同一动作再次出现直接拒绝、不再询问；`recovery.py`：`blocked_by_safety` 在 fixed_retry 分支**之前**判定，只交回规划器 | `test_i01_fixed_retry_never_executes_denied_action`、`test_i01_recovery_actions_pass_safety_gate` |
| 2 | 收尾核验把 UNCERTAIN / 无法解析当成完成；只用最后一个子目标的 expect_text；只在 >1 个子目标时运行 | `verify/verifier.py`：新增 `check_final`：先重新规则核验所有 persistent 子目标的 expect_text，再（需要时）用“整任务 + 全部子目标预期”的聚合 L2；`_parse_check` 对缺 verdict / 非法标签返回 UNCERTAIN。`agent.py`：只有明确 SUCCESS 才 `done`；FAILED / UNCERTAIN 先重规划一次（`on_uncertain: replan`，可配 `fail`），再分别返回 `fail` / `uncertain`；默认总是运行（`final_check: false` 才关闭）。`planner.py`：子目标新增 `persistent` | `test_i02_final_check_rechecks_every_subgoal`、`test_i02_uncertain_or_unparseable_final_is_not_done`、`test_i02_final_check_runs_for_single_subgoal_and_sees_whole_task`、`test_i02_final_check_can_be_disabled_explicitly` |
| 3 | 预算只记账，不是硬上限 | `llm/base.py`：`Budget.before_call()` 在请求发出前检查调用数 / token / 成本（`max_cost_usd` + `models.*.price`），触顶抛 `BudgetExceeded`；`BudgetGate` 包住所有模型（包括注入的）；OpenAI 兼容 / Anthropic / Scripted 后端都在请求前调用。`agent.py`：转为终止状态 `budget_exhausted`（`claimed_done` 恒为 False）；`eval/runner.py`：记录 `budget_exhausted`、`budget_refused_calls`、`cost_usd`，汇总 `budget_exhausted_runs` | `test_i03_budget_raises_before_call`、`test_i03_token_and_cost_caps`、`test_i03_agent_budget_is_terminal_and_never_exceeded`、`test_i03_eval_records_budget_exhausted`、（更新）`test_budget_limit_stops_run` |
| 4 | 截图缩放与坐标换算不一致 | `coords.py`：新增 `ImageTransform`（原始物理像素 → 实际发送尺寸 → 模型坐标约定；另有 dpi_scale → 输入坐标）；`llm/base.py` 的 `prepare_image` 在缩放处产生它，回复以 `LLMReply`（str 子类）携带 `transforms` 返回；`grounding.py`、`planner.py`（Actor / UITarsActor 把 transform 挂到 Action）、`agent._resolve`、`llm/anthropic.py`（Claude 缩放）统一用它；覆盖 resized(qwen) / norm1000 / norm1 / pixel / Claude 缩放 / Retina | `test_i04_grounder_uses_actually_sent_image_size[resized/pixel/norm1000]`、`test_i04_actor_coordinates_use_sent_image_size`、`test_i04_transform_roundtrip_property`（400 组随机尺寸 / 约定 / DPI 往返）、`test_i04_claude_scaling_through_transform` |
| 5 | 畸形动作导致 TypeError 崩溃 | `actions.py`：`ActionParseError(code, field, message).feedback()`；`parse_action` 严格校验类型 / 必填 / 范围 / 未知动作；`Action.validate()`；`parsing.py`：所有失败统一抛 `ActionParseError`，`{"action": null}`、列表等不再崩溃。`agent.py`：主循环捕获解析 / 校验错误并把 `feedback()` 交还模型，同时对任何 actor 返回的 Action 再 `validate()` | `test_i05_malformed_actions_raise_structured_parse_error`（11 种畸形输入）、`test_i05_agent_survives_malformed_actions`、`test_i05_fuzz_parse_never_crashes`（600 个随机动作） |
| 6 | 消融配置与名字不符 | 新增 `policy.py`：`CapabilityPolicy.from_config()` 推导唯一策略；`config.build_agent`、`planner.py`（提示词）、`grounding.py`、`verify/verifier.py`（L1 规则 / L2 是否允许 / 提示词文本）、反思、恢复都只读它；策略禁止的模型角色根本不会被构造。重写 `ablations/*.yaml` 并在 README 逐条写清楚 | `test_i06_vision_only_prompts_contain_no_a11y_text`、`test_i06_rules_only_never_calls_llm_verifier`、`test_i06_every_ablation_maps_to_documented_policy` |
| 7 | 命令注入（Windows 启动 / adb shell / AppleScript） | 新增 `env/commands.py`（应用名校验 + argv 构造）；`env/windows.py`：去掉 `cmd /c start`，改为 `[exe]` 或 `os.startfile`；`env/macos.py`：`open -a` argv + AppleScript `on run argv` 传参；`env/linux.py`：只接受单个可执行文件；`env/android.py`：`commands_for` 生成 argv，逐个 `shlex.quote`，不再用 `&&` 串联，包名 / 键名白名单；`safety.py`：可选 `allowed_apps` | `test_i07_android_type_text_is_one_argv[8 种恶意字符串]`、`test_i07_android_open_app_and_keys_validated[...]`、`test_i07_macos_activate_passes_app_via_argv`、`test_i07_windows_open_app_no_cmd_shell`、`test_i07_linux_open_app_validated`（只检查命令构造，不执行） |
| 8 | 危险快捷键别名漏判；key_down 不检查 | 新增 `keys.py`（规范键名）；`safety.py`：危险组合按“子集”匹配，累计 key_down 按住的键（作用于之后的 hotkey / key_down / 按住修饰键时的 type），typed text 含控制字符需确认；`env/desktop.py`、`env/web.py`、`env/android.py` 的键名映射先规范化（Android：delete=112 向前删除，backspace=67） | `test_i08_dangerous_hotkey_aliases[10 组]`、`test_i08_key_down_sequences_are_checked`、`test_i08_control_sequences_in_typed_text`、`test_i08_canonical_key_names_and_android_delete` |
| 9 | Web 白名单只检查显式 navigate | 新增 `urlpolicy.py`（按主机名匹配，SafetyGuard 与 WebEnv 共用）；`env/web.py`：`context.route` 拦截所有导航（链接、JS 跳转、iframe、新标签页、window.open）；白名单内导航用 `route.fetch(max_redirects=0)` 检查 3xx 的 Location；关闭被拦 / 越界的新页面；每个动作后复核 URL 并回到最后一个合法 URL，动作返回 `blocked_by_safety`（终止性）；`config.build_env`：`safety.allowed_domains` 也下发到浏览器层 | `test_i09_off_allowlist_navigation_is_blocked[链接 / target=_blank / 302 重定向 / JS location / window.open]`（同时断言白名单外主机**没有收到请求**）、`test_i09_allowed_redirect_still_works` |
| 10 | macOS / Linux 吞掉 pyautogui FailSafeException | 新增 `errors.py`：`UserAbort(BaseException)`；`env/desktop.py`：输入层把 FailSafeException 转成 UserAbort；`env/windows.py` 兜底同样转换；`agent.py`：终止状态 `user_abort`；`eval/runner.py`：记录后停止整个任务集 | `test_i10_failsafe_propagates_as_user_abort[linux/macos/windows]`、`test_i10_agent_reports_user_abort` |
| 11 | Windows 丢掉 UIA IsPassword | `env/base.py`：`UIElement.is_password`；`env/a11y.py`：新增纯函数 `uia_raw()`（读 `IsPassword`，且不读取密码框的值），AX 的 `AXSecureTextField` 角色或子角色、Android `password`、Web `type=password`、AT-SPI `password text` 都写入；Web / AT-SPI 密码框的值不再进入元素列表；`safety.py` 按标志判断；日志里密码明文写成 `***` | `test_i11_is_password_from_every_platform`、`test_i11_safety_uses_flag_not_name` |
| 12 | Claude 拖拽只有终点时变成零长度拖拽；GPT-5 / o 系列参数 | `llm/anthropic.py`：`left_click_drag` 优先用 `start_coordinate`，否则用当前光标（Observation.cursor 或 actor 跟踪的上一次指针落点），都未知 → `ActionParseError` 反馈；desktop / web 观察里提供 `cursor`。`llm/openai_compat.py`：`is_reasoning_model()` 按模型名检测，推理模型用 `max_completion_tokens`、不发 temperature、system 改为 developer 角色（**未用真实 API 验证**） | `test_i12_claude_drag_starts_at_cursor`、`test_i12_gpt5_and_o_series_request_params` |

## 其他随修复一起改动的旧测试

- `tests/test_agent_mock.py::test_budget_limit_stops_run`：状态名 `budget_limit` → `budget_exhausted`，并断言调用数**正好**等于上限。
- `tests/test_android_env.py::test_commands`：`input text` 的期望字符串从手写反斜杠转义改为 `shlex.quote` 形式。

---

# 第二轮（v0.3.1）代码审查修复对照表

审查报告：`gui-agent-v0.3-review-20261007.md`（Windows / Python 3.12，无 Playwright：120 passed, 2 skipped）。

流程同第一轮：每一条都**先写回归测试并确认它在 v0.3.0 上失败**，再修复，再确认通过。

- 回归测试：`tests/test_review_v031.py`（不依赖 Playwright，可在 Windows 上跑；白名单 fail-closed 用假 route）、
  `tests/test_web_review_v031.py`（真实 Chromium + 本地 HTTP 服务器，127.0.0.1 = 白名单内 / 同源，localhost = 白名单外 / 跨源）。
- 在 v0.3.0 代码上跑这 59 个新测试：**56 个失败，3 个通过**。通过的 3 个是对照用例，本来就该通过：
  `test_r07_agent_waits_for_busy_to_clear_then_succeeds`（加载结束后仍应能完成，防止修过头）、
  `test_r05_post_navigation_sent_exactly_once`（白名单内的 POST 只发送一次）、
  `test_rx_no_playwright_switch_hides_module`（测试工具本身）。
- 在 v0.3.1 上：**189 passed**；`GUA_TEST_NO_PLAYWRIGHT=1 python -B -m pytest -q -p no:cacheprovider`：**166 passed, 3 skipped**。

| # | 审查意见 | 改动（文件 → 做了什么） | 回归测试 |
|---|---|---|---|
| r01 | 高：聚焦的危险按钮可用 Enter / Space / `type(submit)` 激活，绕过确认 | `safety.py`：新增“激活目标”语义——`_activation()` / `activation_target()` 对指针动作取目标元素，对 Enter / Space / DPAD_CENTER（hotkey、key_down，含按住的键）、`type(submit=True)`、文字含换行、往按钮里打字取焦点元素（文本框取 `attrs.form_submit`）；同一条 RISKY_WORDS 规则；`Decision.sigs` + `signatures()`：拒绝按 `activate|<目标名>` 记录，所有激活方式共享；焦点 `unknown` / 未报告时按激活键 → 确认；同一观察上执行过可能移焦的动作（Tab、点击…）后焦点视为 unknown。`keys.py`：`ACTIVATION_KEYS` 与 Android keycode 别名；`env/android.py`：`dpad_center` keycode；`env/mock.py`：`focus_state="none"` | `test_r01_keyboard_activation_of_focused_dangerous_button_denied[9 种激活方式]`、`test_r01_click_and_keyboard_share_one_denial`、`test_r01_unknown_focus_activation_needs_confirmation`、`test_r01_enter_in_textbox_checks_form_submit_target`、`test_r01_focus_after_tab_on_same_observation_is_unknown`、`test_r01_android_dpad_center_is_activation`；Web：`test_r01_keyboard_cannot_activate_focused_delete_button[enter/space/submit]`（含对照：不经闸门时按键确实会删除） |
| r02 | 高：重复拒绝日志、控制字符规则先命中、`cli_confirm()` 出现明文 | 新增 `sensitive.py`：`focus_target()`（password / normal / none / unknown / unreported）、`is_sensitive_type()`、`safe_view()` / `safe_short()` / `redacted_action()`；`actions.py`：`Action.safe_short()`；`safety.py`：所有日志条目（含 repeat）用安全摘要，脱敏只看输入目标；reason 不再包含敏感字符；确认回调只收到脱敏副本，`cli_confirm` 无观察时一律脱敏；拒绝签名中 typed text 只存 sha256 前缀 | `test_r02_repeated_denial_log_redacted`、`test_r02_redaction_independent_of_which_rule_fired`、`test_r02_confirm_terminal_and_callbacks_never_see_plaintext`（非交互 stderr、交互 input 提示、自定义回调）、`test_r02_safe_summary_api` |
| r03 | 高：密码进入 L2 请求、步骤记忆，进而进入 Actor / 反思提示词 | `agent.py`：`_view()` 在闸门前生成安全摘要，用于 StepRecord、`_log_step`、恢复动作说明、L2 `action_desc`；`_execute_gated` 把敏感原文登记到 `Scrubber`，秘密占位符 `<secret>名字</secret>` 只在 `env.execute` 前替换（`AgentConfig.secrets`，Actor 只看到名字）；执行层报错 / 输出、证据、反思笔记、反馈、重规划说明、`RunResult`（message / answer / safety_events）都过清洗；`logger.py`：`TrajectoryLogger.scrubber`，`steps.jsonl` / `meta.json` 落盘前清洗（`report.html` 由它们生成）；`eval/runner.py`：结果行清洗；`verify/verifier.py`：`model_check(action_desc=...)`；类型规则不再读密码框的值 | `test_r03_secret_never_appears_in_any_output_channel`（截获全部假模型请求：planner / actor / verifier / reflector，以及记忆、笔记、RunResult、确认回调、steps.jsonl、meta.json、report.html、stdout、stderr）、`test_r03_exec_error_echo_is_scrubbed`、`test_r03_secret_placeholder_resolved_only_by_executor`、`test_r03_verifier_l2_request_uses_safe_summary`、`test_rx_secrets_come_from_environment_not_config`（`config.py`：`agent.secrets_env`） |
| r04 | 高：Web 的 iframe / shadow DOM 里的密码框不可见，焦点只查顶层 | `env/web.py`：`SNAPSHOT_JS` 遍历所有 open shadow root（`elementFromPoint` 用所在 root，避免误标 covered），`_snapshot()` 逐个 frame（同源 / 跨源）调用并加 `_frame_offset()`（bounding_box + 边框 / 内边距）；独立的 `FOCUS_JS` + `_probe_focus()`：activeElement 穿透 shadow root，落在 iframe 上时打标记、在子 frame 里继续，任何异常 → `focus_state="unknown"`；`_apply_focus()` 把焦点标到元素上或追加，不受 `max_elements` 限制；`env/base.py`：`Observation.focus_state`；`safety.py`：unknown 时输入需确认（且脱敏） | `test_r04_shadow_dom_password_focus_detected`、`test_r04_same_origin_iframe_password_focus_detected`、`test_r04_cross_origin_iframe_password_focus_detected`（矩形换算到主视口）、`test_r04_focus_probe_independent_of_candidate_limit`（`max_elements=5`、密码框在第 41 个之后）、`test_r04_undeterminable_focus_is_unknown_and_conservative`、`test_r04_nested_elements_collected_and_clickable` |
| r05 | 高风险：白名单预取异常 → `route.continue_()`（fail-open，可能重发） | `env/web.py`：`_route()` 外层兜底，任何异常 abort + `safety_failures`；预取异常 abort（不 continue、不重发），并通过 `_unreported` 让动作返回 `blocked_by_safety`；`is_navigation_request()` 异常按导航处理；**多跳重定向**：v0.3.0 用 `fulfill(3xx)` 交给浏览器跟随，第二跳不再经过路由（测试复现：白名单外主机真的收到了请求），现改为 200 + `location.replace()` 客户端跳转（保留 Set-Cookie 等响应头），下一跳重新进入路由检查；307/308 非 GET 与子资源由处理器逐跳 `fetch(max_redirects=0)`；`max_redirect_hops` 防环；`fetch_timeout` 可配 | 单元（假 route，无需 Playwright）：`test_r05_prefetch_exception_fails_closed`、`test_r05_navigation_flag_exception_is_conservative`、`test_r05_unexpected_handler_error_aborts`、`test_r05_subresource_redirects_checked_when_blocking_subresources`；Chromium：`test_r05_prefetch_timeout_fails_closed_then_offlist_redirect_blocked`（第一次超时 → 拦截且服务器只收到 1 次；第二次越界 302 → 拦截，白名单外主机 0 次请求）、`test_r05_every_redirect_hop_is_checked`、`test_r05_post_navigation_sent_exactly_once` |
| r06 | 中：Android / AX 密码值未清除，Android 名字回退到密码 text | `env/a11y.py`：Android 密码节点 `text` 置空（名字用 content-desc / resource-id / “password field”），父容器 `desc_text` 不用密码子节点的 text；AX 安全输入框（角色或子角色）不读 `AXValue`；AT-SPI 也认 `password` 状态；Web 认 `secure`（`-webkit-text-security`）/ autocomplete；`finalize()` 对 `is_password` 清空 value、不把名字放进可见文本；`env/base.py`：`UIElement.brief()`、`Observation.all_text()` 再兜底 | `test_r06_android_password_text_never_kept`、`test_r06_ax_secure_value_cleared`、`test_r06_web_and_atspi_password_values_cleared`、`test_r06_common_serialization_layer_defense` |
| r07 | 中：收尾核验把忙碌 / 未稳定当完成（丢弃 `_settle()` 的布尔值；规则不看 busy） | `agent.py`：`_settled_for_check()` 未稳定 / 忙碌时再等待复查 `busy_rechecks` 次；`_confirm_goal` 与任务收尾把 `stable` 和基线（子目标第一帧 / 任务开始时）传给验证器；`verify/verifier.py`：`check_goal` / `check_final` 新增 `stable`、`baseline`——未稳定或忙碌 → UNCERTAIN；期望文本在基线中已存在且屏幕未变化 → 旧证据，不单独定论；`busy()`：元素信号绝对判断、文字信号相对基线（页面说明里的 “Loading takes…” 不算）；`is_busy()` 不把满进度条（aria-valuenow == valuemax）当忙碌；步骤级规则忽略动作前已存在的期望文本 | `test_r07_goal_check_busy_is_not_success`（审查复现：Loading progressbar + 旧 “Report ready”）、`test_r07_final_check_busy_is_not_success`、`test_r07_unstable_screen_is_not_success`、`test_r07_stale_evidence_is_not_success`、`test_r07_step_rule_ignores_text_already_present_before`、`test_r07_static_full_progressbar_is_not_busy`、`test_r07_agent_never_reports_done_while_busy`、`test_r07_agent_waits_for_busy_to_clear_then_succeeds`（对照） |

## 第二轮自查发现的同类问题（审查范围外）

| 问题 | 改动 | 回归测试 |
|---|---|---|
| `SafetyGuard.assess()` 自身抛异常会直接让运行崩溃（没有定义的失败语义） | `safety.py`：判定 / 签名计算异常 → 按“需要确认”处理（deny 模式即拒绝），日志完全脱敏 | `test_rx_safety_assess_exception_fails_closed` |
| 拖拽到废纸篓 / 回收站（= 删除）不检查 | `safety.py`：drag 终点元素名 / target2 按 RISKY_WORDS + TRASH_WORDS 检查 | `test_rx_drag_onto_trash_needs_confirmation` |
| 在密码框里用单字符 hotkey / key_down 逐键输入，日志记录按键 | `sensitive.py`：只含可打印字符的按键动作在密码 / 未知目标上脱敏；`safety.py`：需要确认 | `test_rx_single_char_keys_into_password_redacted` |
| CSS 掩码输入框（`-webkit-text-security`）、`autocomplete=current-password/new-password/one-time-code` 不算密码框 | `env/web.py` JS `secureOf()`；`env/a11y.py` `web_raws` | `test_rx_obscured_web_inputs_are_password` |
| `input[type=submit] value="Delete account"` 没有名字（危险提交按钮对规则不可见）；回车在文本框里提交危险表单 | `env/web.py` `nameOf()` 对 submit/button/reset 取 value，`submitOf()` 给出表单提交按钮名 | `test_rx_enter_in_text_field_submitting_dangerous_form`（Chromium） |
| 非 ASCII 文本经剪贴板粘贴时，读不到旧剪贴板就把输入文字（可能是密码）留在系统剪贴板上 | `env/desktop.py` `clipboard_type()`：finally 中恢复旧内容，读不到则清空 | `test_rx_clipboard_typing_never_leaves_text_on_clipboard` |
| 各平台的进度条在元素转换时被丢掉（Web role=progressbar、Android ProgressBar、AX ProgressIndicator 都映射为 other 被过滤；UIA ProgressBar 被跳过），忙碌规则只能靠文字 | `env/a11y.py`：`PROGRESS_ROLES` / `is_busy_raw()`，`finalize` 保留忙碌指示器，`uia_raw` 不跳过 ProgressBar；Web 快照包含 `<progress>` 与 `aria-busy` | `test_rx_busy_indicators_survive_element_conversion` |
| `block_subresources=True` 时子资源的白名单内 302 → 白名单外不检查 | `env/web.py`：子资源也预取并逐跳检查 | `test_r05_subresource_redirects_checked_when_blocking_subresources` |
| 主页面离开白名单、回不到最后一个合法 URL 时停在越界页面 | `env/web.py` `_enforce()`：转到 `about:blank`，失败记录 `safety_failures` | （代码路径；未单独构造浏览器失败场景） |
| 焦点元素若排在候选上限之后或角色被过滤，所有平台都看不到焦点 | `env/a11y.py` `finalize()`：焦点元素无论角色都保留，超过 `max_elements` 时继续查找并追加 | `test_r04_focus_probe_independent_of_candidate_limit`（Web）；fixture 测试覆盖 Android 焦点密码框 |
| Web 文字只取主 frame 的 `innerText`（iframe / shadow DOM 里的期望文本对 L1 不可见） | `env/web.py`：每个 frame 的 innerText 与 shadow root 文字都并入 `Observation.text` | （未单独测试） |
| 测试套件无法方便地模拟“没有 Playwright” | `tests/conftest.py`：`GUA_TEST_NO_PLAYWRIGHT=1` → `hide_playwright()`；CI core 任务改为 `python -B -m pytest -q -p no:cacheprovider -m "not web"` | `test_rx_no_playwright_switch_hides_module` |

## 第二轮随修复改动的旧测试

- `tests/test_review_v03.py::test_i08_control_sequences_in_typed_text`：`"hello\nworld\t!"` 原来在**没有观察**时断言放行。
  v0.3.1 中换行等同回车激活，无观察 = 焦点未知 → 需要确认（这正是条目 r01 要求的保守行为）。改为在“焦点是普通文本框”的观察下断言放行；
  控制字符部分的断言不变。

## 仍未完全解决（详见 README“已知限制”）

- 桌面 / Android 没有独立焦点探测，只依赖无障碍树的 focused 状态；未报告焦点时普通输入放行（但脱敏）、激活键需要确认。
- 页面 JS 自己监听键盘触发的操作、非 Web 平台文本框所属表单的提交按钮，安全闸门看不到。
- Scrubber 只能清洗已知秘密（≥ 4 字符）；任务描述里直接写出的密码、`ask_user` 的回答、截图中的明文不在保护范围内。
- Android 的 `input text` / ADBKeyboard 广播通道本身会短暂暴露明文。
- closed shadow root 无法穿透；Web 客户端跳转改变了“后退”历史；307/308 非 GET 时地址栏停在第一跳。
- 旧证据判定是近似；常驻的不定进度条会让收尾核验一直 uncertain。


---

# 第三轮（当前工作树安全修订）对照表

基于第二轮审查（v0.3.1）提出的剩余风险与执行语义缺口进行收口修复。

- 回归测试文件：
  - `tests/test_review_url_policy.py`（离线 URL 规范化与重定向状态机）
  - `tests/test_web_review_security.py`（真实 headless Chromium + 本地 HTTP：无脚本跳转、CSP、Cookie 隔离、移焦阻断）
  - `tests/test_review_input_safety.py`（复合快捷键、按键签名哈希、输入错误脱敏、表单提交去重）
  - `tests/test_review_observations.py`（密码名称收口、autocomplete token 列表、closed shadow 保守焦点）
  - `tests/test_review_model_privacy.py`（模型出口统一清洗、严格截图阻断、短秘密结构化脱敏、真实文本过闸）
  - `tests/test_review_completion.py`（片段级新鲜证据、现实进度忙碌行、窗口标题旧证据）
- 实测结果：**343 passed**（在 Windows 11 / Python 3.12.10 + Chromium 153 上全部通过；`GUA_TEST_NO_PLAYWRIGHT=1` 时 304 passed, 4 skipped）。

| # | 审查意见 | 改动（文件 → 做了什么） | 回归测试 |
|---|---|---|---|
| r08 | 高：URL 解析歧义与重定向安全（反斜杠/userinfo绕过、脚本注入、CSP卡死、POST恢复、跨源Cookie注入） | `urlpolicy.py`：新增 `normalize()` 与 `UrlRejected`，严格拒绝反斜杠、控制字符、userinfo、百分号 authority、数字/十六进制 IP 别名；`domain_allowed` fail-closed。<br>`env/web.py`：重定向改为无脚本 `meta refresh`（目标正确编码），丢弃卡死跳转的原 3xx CSP 并注入严格无脚本 CSP，多 Set-Cookie 保留；手动逐跳 fetch 维护当前方法与正文，POST 转 GET 后清空正文且后续 307 不恢复，跨源跳转剥离 Set-Cookie 等响应头；navigate 亦走规范化 URL | `test_r08_*`（test_review_url_policy.py，42 项）、`test_web_review_security.py`（无脚本跳转不执行脚本、3xx CSP 穿透、多 Cookie 保留、跨源 Cookie 隔离、POST→303→GET→307→GET 状态机） |
| r09 | 高：复合按键先移焦再激活、按键脱敏失效、拒绝签名留原文、错误消息泄露密码 | `keys.py`：`validate_sequence()` 严格限制 `hotkey` 为零或多修饰键 + 恰好一个非修饰键，非法序列直接抛 `ActionParseError` / 闸门 deny；错误消息不回显非法键名；支持 Android 数字键码。<br>`sensitive.py`：`carries_text()` 判定有字符即携带字符，不因混入 Enter/Tab/Backspace 取消脱敏；unreported / unknown 与 password 同等脱敏。<br>`safety.py`：按键拒绝签名一律哈希（`keys|<hash>`），denied 映射无明文；移焦键与激活键混入同一动作时按未知目标保守确认 | `test_review_input_safety.py`（36 项：非法序列直接拦截、错误消息无明文、按键签名哈希、unreported 字符脱敏、移焦激活拆分要求） |
| r10 | 高：密码识别三个盲区（autocomplete token、closed shadow、掩码元素文本泄露）及输入移焦 | `env/a11y.py`：`web_raws` 按 ASCII 空白拆分 autocomplete token 列表；密码节点名称与描述统一收口为 `password field`，排除 Android content-desc、AXTitle/Description/Help 等不可信字段；父容器命名跳过密码子树。<br>`env/base.py`：`UIElement.brief()` 与 `Observation.all_text()` 公共层彻底排除密码 name 与 value。<br>`env/web.py`：`textOf()` 排除掩码子树，`FOCUS_JS` 对非原生控件判 `unknown`；`_do_type()` 绑定输入目标并逐字检查焦点变化，移焦立即中止输入；检验执行时焦点与观察时安全属性一致 | `test_review_observations.py`（9 项：token 拆分、密码名称规范、父容器递归排除、公共序列化保护）、`test_web_review_security.py`（掩码 contenteditable 不泄露、closed shadow 判 unknown、逐字输入移焦 0 字符泄漏） |
| r11 | 高：已知秘密回流模型出口、截图泄密 | `agent.py`：统一使用 `EgressGate` 包装所有模型出口（chat 与 post），递归清洗 system / prompt / 结构化 body 中的已知秘密（含 dict key 与额外字段）；配置秘密以 `explicit=True` 登记到 Scrubber，短 PIN 亦受保护。<br>`llm/base.py`：`EgressGate` 增加**严格截图阻断**（单调生效）：一旦识别密码框、不透明焦点或配置秘密，该次运行后续所有请求剥离图片；纯视觉角色以 `PrivacyBlocked` 终止任务并报 `privacy_blocked`，绝不无图伪装成功；`BudgetGate` 增加 `__setattr__` 透传。<br>`logger.py`：在敏感运行中停止保存截图（`shot()` 返回 None）；日志写入前使用 `scrub_obj` 保护 JSON 结构 | `test_review_model_privacy.py`（27 项：全角色出口文本清洗、单调截图阻断、短 PIN 保护、JSON 结构完整、纯视觉受限终止、日志无截图落盘、预算属性透传） |
| r12 | 中：秘密替换在闸门之后发生、表单提交重新询问 | `agent.py`：`_execute_gated()` 先展开秘密占位符得到真实动作 `exec_a`，再以 `exec_a` 通过 `SafetyGuard.gate()`；检查真实文本的控制字符、危险命令与提交语义；未配置占位符安全拦截并报错。<br>`safety.py`：表单提交目标统一使用真实提交按钮名与 `form_submit_id`，不再拼接文本框名；表单点击与回车提交共享同一拒绝签名，消除重复询问 | `test_review_model_privacy.py`（危险秘密按真实内容拦截、换行密码按真实内容过闸、未知占位符拦截）、`test_review_input_safety.py`（表单点击与回车提交共享签名） |
| r13 | 中：无关界面变化洗白旧证据、持续 Loading 信号被忽略 | `verify/verifier.py`：删除全局 `changed()` 与 `busy_count()`；改为片段级新鲜度比较（`_evidence_fragments`，涵盖文本行、元素 name/value 及窗口/标签标题）；只有承载证据的片段自身发生状态转换才算新鲜，全局像素/无关文本变化不再洗白旧证据。<br>`BUSY_LINE_RE` 扩展覆盖现实带进度百分比/计数的忙碌行（如 `Loading 45%`、`Processing 3/10`、`正在加载 45%` 等）；当前屏幕有忙碌状态始终 fail-closed；步骤级 L2 补充文本上下文兜底 | `test_review_completion.py`（22 项：无关像素/banner/重复行不洗白、进度数忙碌行拦截、窗口标题旧证据拦截、正向状态转换成功、L2 旧证据约束上下文） |

## 第三轮随修复改动的旧测试

- `tests/test_a11y_parsers.py::test_ax_tree_retina_scale`：旧断言通过 `by_name(els, "Password")` 选取密码框。由于条目 r10 确立了“所有平台的密码元素统一使用固定安全名称 `password field`，不再信任不可信标签”的明确安全契约，该断言调整为通过 `next(e for e in els if e.is_password)` 选取，并断言其 `name == SAFE_PASSWORD_NAME`、`value is None`，其余 Retina 坐标与属性断言保持不变。

---

# 第四轮审查修复

基于 `318fa56` 的审查复现，修复 2 项高优先级安全问题和 2 项中优先级执行 / 实验问题。

| 问题 | 修复行为 | 回归测试 |
|---|---|---|
| 跨源 POST 导航经 307/308 后，目标文档在来源 origin 中执行并能读取来源 localStorage | `env/web.py`：保留非 GET/HEAD 方法的跨源导航在请求下一跳前 abort，禁止用来源请求 fulfill 跨源文档；GET 导航、POST 转 GET 和同源 POST 跳转保留 | `test_web_review_execution.py`：真实 Chromium 检查目标未收到 POST、目标脚本未执行；原生导航 / 302 / 303 对照检查目标 origin 和 storage 隔离。`test_review_url_policy.py`：主机、端口、协议改变以及多跳导航均在跨源下一跳前拒绝 |
| 确认期间换到另一个密码框仍输入完整秘密；页面刷新可复用 DOM 编号 | `env/web.py`：保存安全观察中的实际元素句柄、页面与 frame；输入前校验同一元素仍连接、属性仍一致，变化时返回 blocked_by_safety。clear 的两次按键之间及每次输入前后复核焦点 | `test_web_review_execution.py`：确认期间换框不输入、不清空、不提交；刷新后同编号不能继承授权；普通输入正常；同一元素变成密码框时要求重新观察 |
| Android 指定 x/y 的 scroll 用字符串参与端点计算，抛未捕获 TypeError | `env/android.py`：滑动坐标用整数计算，最终 argv 才转字符串 | `test_review_execution.py`：四个方向、指定坐标 / 屏幕中心共 8 项，核对实际执行的 adb argv |
| vision_only 的步骤级 L2 验证仍附带 DOM 文本 | `verify/verifier.py`：仅在 a11y_in_prompts 允许时附带动作前后文本 | `test_review_execution.py`：on_event / every_step 下分别检查纯视觉、仅关闭提示词文字、保留文字对照，共 6 项 |

新增 **32 项回归用例**（14 项执行 / 提示词、11 项浏览器、7 项离线导航边界）。没有改写原有测试断言。

验证环境：Linux / Python 3.12.14 / Playwright 1.63.0 / Chromium 153。

- `python -B -m pytest -q -p no:cacheprovider`：**375 passed**，含真实浏览器测试，无跳过。
- `gua demo`：**6/6 任务通过**，全部返回 done，false_done_rate=0。
- `git diff --check`：通过。

行为限制：启用域名白名单时，保留非 GET/HEAD 方法的跨源导航被明确拒绝。发现重定向时来源请求已经发出，
拒绝不会撤销来源站点的业务副作用。跨源子资源 / fetch 的既有逐跳检查与去认证头行为保持原状。
本轮未调用真实付费模型 API，未在 Windows / macOS 桌面或 Android 真机上做端到端验证。

## 桌面版后续回归：被拦导航的异步恢复

原第四轮回归在 GitHub CI 发现导航拦截后的竞态：`route.abort` 返回后，Chromium 的错误页可能尚未提交。
只等待 60 ms 即恢复会被迟到的错误页打断；导航预取仍在进行时也可能提前返回成功的点击结果。

`WebEnv` 现在跟踪正在进行的导航路由检查，等待已触发的预取和重定向检查结束；
记录被拒绝的主导航，在有限时间内等待其提交，再恢复上一合法页面。中间重定向页不作为恢复目标。
请求异常仍只 abort，不重发原始请求，也不重复提交 POST。

既有浏览器回归保留原断言；307/308 拒绝用例增加 150 ms 的真实 HTTP 响应延迟对照，
检查点击报告拒绝、目标没有收到请求、目标脚本没有执行且恢复后可以读取页面。

## Windows 冻结引擎：中文通信编码

Windows 安装包验证发现 PyInstaller 启动器没有沿用 `PYTHONIOENCODING`，中文事件在默认系统代码页中写出失败。
桌面执行入口现在明确将 stdin、stdout 与 stderr 配置为 UTF-8，确保中文任务和返回事件使用同一管道协议。
新增回归用实际 Worker 入口读取中文请求并输出中文错误，将三个输入输出流的初始编码设为 Windows `cp1252`。
