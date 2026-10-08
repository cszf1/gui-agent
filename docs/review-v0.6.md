# v0.6 审查与验证（2026-10-08）

完整 `gui-agent-v0.6.zip` 与 8 个补丁在同一个 v0.5 基线上逐文件核对一致，保留补丁提交历史后进行修订。
产品方向仍是 Windows 优先的本机桌面 App：Electron / React / TypeScript 界面，Python 执行引擎，用户配置 Base URL、API Key 与模型。
Linux RemoteEnv、Docker 沙盒和 MCP 是可选的开发与评测能力，不提供云电脑托管服务。

## 主要发现与修订

| 问题与触发方式 | 修订后的行为 | 验证 |
|---|---|---|
| 后台 Toggle 延迟生效，短等待后改为点击会再翻转一次；GUI 点击结果不明也会被 recovery / actor 重放 | 已投递但结果不明的点击、Toggle、Invoke、提交记录在共享闸门，阻断换模态、fixed-retry、MCP 后续调用及 checkpoint 续跑；可等待和验证，不能盲目重放 | 延迟效果实际触发一次；故障基准另列幂等与非幂等场景 |
| 后台复查产生新快照后仍使用旧坐标 / ID 做前台兜底 | 重新观察，凭 DOM / UIA / AT-SPI 身份找同一个控件并重新过闸；同名替换控件不接受；赋值走已有目标输入路径 | 真实 Chromium：同一字段兜底可用，替换字段拒绝 |
| 无关像素变化覆盖明确失败的后置条件；同名控件取第一个；MCP verify 对忙碌画面签发完成回执 | 特定状态条件失败/未知不能被无关差分覆盖；歧义匹配为 unknown；不稳定或忙碌时不签发 verified_done | 条件歧义与 busy / stable 回归 |
| `type` 被拒后改成 `set_value`，或被拒的文件写入换成 `./path` | 输入意图共享拒绝签名，文件变更按规范化路径记忆拒绝；拒绝提交不误伤普通编辑 | 确认回调只调用一次，第二种模态 / 路径别名不执行 |
| UIA 获取 ValuePattern 时控件变成密码框 | 原生读写前后重新核验密码属性和身份，出现变化立即停止，不读取密码值 | 假 UIA 的前/后属性变化回归；不能代表所有真实应用 |
| MCP run_task 新建 agent 丢失先前拒绝、未确认激活和敏感状态 | 同一连接共享 Scrubber、安全记录、按住的键和注册工具；新观察后再 act | 跨 run_task / act 会话回归 |
| 续跑任务不匹配，或敏感 checkpoint 丢失清洗规则 | 绑定原任务、恢复拒绝与未确认激活；不保存明文秘密，敏感 checkpoint 跨进程续跑返回 privacy_blocked | 不执行错误任务，恢复后不重放旧激活 |
| RemoteEnv 截图和树分两次请求，接管 / 动作可与观察交错 | 原子观察图像与树；接管与动作/观察串行，确认接管前等待在途操作结束；交回后旧快照失效 | HTTP 并发回归与真实容器 |
| Remote daemon 仅比较 epoch、依赖树索引找控件；输入中途换焦点仍继续发字 | 比较完整快照，持有稳定 AT-SPI 控件对象；每次输入前核验当前字段、窗口、密码属性，移焦后停止后续字符和提交 | 旧快照、清空/逐字输入移焦回归；实际 GTK 原生动作 |
| noVNC 只用 URL 的 view_only 标记限制人类输入 | x11vnc 在服务端限制输入，接管/交回同步查询模式确认；默认回环发布 | 同一真实 WebSocket/RFB 连接在接管前、期间、交回后分别发送输入，只有期间生效 |
| shell 输出先全部读入，超时后派生进程继续运行 | 有界输出缓存，POSIX 子进程组清理与子进程 rlimit；文件读入也有上限 | 64 MiB 输出时父进程内存有界；超时派生进程未写入延迟结果 |
| Docker COPY 权限和 X socket 目录导致非 root 镜像启动失败；本机窗口管理器尚未准备好 | 镜像明确目录/文件权限与 X socket 权限；本机启动等待 openbox 就绪 | 实际构建、容器运行与双沙盒并行评测 |

`expand` / `collapse` 没有可靠的前台点击等价路径，不自动用点击模拟。Windows 的 `SetFocus` 需要前台，显式后台 focus 返回 `background_unavailable`。
脚本条款弹窗任务改为明确核验中间结果后进入下一步；没有用重放未确认的点击提高成功率。

## 实际运行范围

- Linux x86_64 / Python 3.12，真实 Chromium；Xvfb、openbox、GTK、AT-SPI。运行时 `NO_PROXY=127.0.0.1,localhost`。
- 完整 Python 回归：**562 passed**（391.04 s）。其中本轮新增 30 个边界用例和 2 个真实 Web 兜底用例。
- `GUA_TEST_NO_PLAYWRIGHT=1`：**485 passed, 8 skipped**（191.72 s），包含真实本机沙盒用例。
- 六个真实浏览器任务、双沙盒并行评测通过；目标判分来自页面或应用实际结果。
- 桌面 TypeScript / Electron 构建、12 项单元测试与 1 项真实 Electron → Python → Chromium E2E 通过。
  E2E 使用本地 HTTP 模型替身，覆盖任务、确认、暂停和停止，没有调用真实模型。
- Docker 镜像已构建，非 root、`--cap-drop=ALL`、`no-new-privileges`、内存/进程数限制及回环发布下运行。
  实际 GTK 结果为 `{"name":"Alice 中文","subscribe":true,"plan":"Free"}`；AT-SPI SetValue/Toggle/Invoke 路径、
  后台鼠标与前台不变、禁用 shell、文件越界拒绝、noVNC 服务端接管/交回、截图/动作阻断与重置均通过。
- Windows 在 Linux 上无法运行。新增 [真实 WinForms 后台检查](../desktop/scripts/smoke-windows-native.py)，
  CI 检查 SetValue 中文、Toggle、Select、Invoke 的应用结果与鼠标/前台不变，随后构建冻结引擎与 NSIS 安装包。
  运行结论以 [GitHub Actions](https://github.com/cszf1/gui-agent/actions) 对应提交日志为准。v0.5 的真实 Windows 原生与冻结引擎检查已通过。
- wheel 与 sdist 构建通过；已核对 wheel 包含独立 daemon、受限进程执行器、GTK 应用与 MCP 模块，sdist 包含 Docker 上下文和配置。

## 修订后基准

原始附件的 `modalities-2026-10-08.json` 保留为审查前数据。
修订后数据另存为 [modalities-review-2026-10-08.json](benchmarks/modalities-review-2026-10-08.json)，固定脚本、3 次重复，没有真实模型调用。
非幂等激活在无确认时应停止；新增可安全重复的 radio select 场景测量换模态恢复。
两种情况不可合并为一个“恢复成功率”。耗时只是本机小样本，不是对其他 agent 的速度比较。

| 场景（每种配置 3 次重复） | gui_only | hybrid + 模态恢复 | hybrid 关闭模态恢复 |
|---|---|---|---|
| 真实沙盒两个任务，各 3 次 | 6/6，前台 | 前台 6/6；后台 6/6，动作前后鼠标位置变化 0 次 | 此部分未测 |
| radio 的 GUI 点击被吞 | 0/3 | 3/3，改用 select | 0/3 |
| radio 的后台 select 静默丢失 | 3/3，走前台 | 3/3，执行器前台兜底 | 3/3，执行器前台兜底仍开启 |
| button 的 GUI 点击被吞 | 0/3 | 0/3，未确认激活不重放 | 0/3 |
| 后台 toggle 静默丢失 | 3/3，走前台 | 0/3，未确认激活不重放 | 0/3 |
| 后台 invoke 静默丢失 | 3/3，走前台 | 0/3，未确认激活不重放 | 0/3 |

共 18 次真实沙盒运行与 63 次 mock 运行，所有配置的虚假完成数为 0，mock 重复触发数为 0。
后台两任务的中位耗时分别为 15.42 s、11.08 s；基准与其他回归同时运行，耗时受宿主负载影响。
`modality_recovery` 控制恢复策略换模态，执行器的 `fallback_to_foreground` 是独立配置，不能将两者混淆。

重现命令：

```bash
export NO_PROXY=127.0.0.1,localhost
python -m pytest -q
GUA_TEST_NO_PLAYWRIGHT=1 python -m pytest -q
python scripts/benchmark_modalities.py --repeats 3 --output docs/benchmarks/modalities-review-2026-10-08.json
docker build -t gua-sandbox -f sandbox/Dockerfile .
python scripts/smoke-sandbox.py --image gua-sandbox
```

## 证据与剩余限制

- 完成回执记录界面条件、路由与摘要；摘要采用 SHA-256 前 32 个十六进制字符，不是签名或业务结果证明。
  无明确后置条件的像素 / 无障碍差分仍是启发式。未识别、未登记的秘密不具备全面清洗保证。
- 没有接入真实模型 API 进行自主任务实测，没有对 cua、UFO²、Claude 或 Grok 的同任务横向结果。
  因此不能宣称速度、失误率或能力已经超过它们。
- Windows CI 只覆盖受控 WinForms 窗口，不证明所有后台/最小化应用都支持 UIA pattern；复杂本机应用、macOS 与 Android 仍需实际设备验证。
- 本机 Xvfb 与当前用户同权限。Docker 功能测试不等于隔离安全审计。noVNC 无独立认证，仅供可信回环访问。
  快照只保存工作目录与应用启动列表，不保存内存或系统状态。
- shell 默认关闭；可执行文件白名单不是参数语义沙箱。POSIX 有进程组清理，Windows 尚无完整 Job Object 生命周期保证。
- Cua-Bench 是部分兼容层；OSWorld 示例为手写的同格式合成任务，不是官方任务集成绩。
- [Meta Muse 官方安全设计](https://research.meta.ai/blog/security-and-safety-for-ai-agents-our-approach-with-muse) 原链接有效（2026-09-08 发布，2026-10-08 核对）。
  README 已明确为 Meta Muse 设计参考，没有接入其代码，也不宣称同等隔离能力。
