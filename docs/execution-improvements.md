# v0.5 执行速度与可靠性

目标是 Windows 优先的本机桌面助手。模型仍可通过 Base URL、API Key 和模型名称配置；不提供云电脑。
这一轮改动主要降低执行开销与坐标错误，不以固定脚本测试宣称真实模型自主成功率，也不宣称超过商业产品。

## 公开项目中的取舍

2026-10-07 核对的公开资料如下。闭源产品只参考公开功能、接口与实践，未取得其内部实现。

| 参考 | 可借鉴的原则 | 当前落地 |
| --- | --- | --- |
| [Cua Driver 平台支持](https://cua.ai/docs/cua-driver/concepts/platform-support)、[源代码说明](https://github.com/trycua/cua/blob/main/libs/cua-driver/README.md) | 绑定具体窗口与元素，优先原生操作，以应用实际状态证明结果 | Windows 窗口/PID 与 UIA runtime identity 检查；Invoke/Toggle/Select/Focus；浏览器保留节点句柄；原生部分失败不重放 |
| [Claude 的电脑与浏览器实践](https://claude.com/resources/articles/best-practices-for-computer-and-browser-use-with-claude)、[Claude Code Desktop](https://code.claude.com/docs/en/desktop) | 结构化工具、正确图像变换、仅合并无需探索的动作、人工接管 | 指定字段的焦点加输入；合并动作内重新观察并分别过闸；1280 长边图片且保留坐标变换；暂停使剩余输入失效 |
| [Codex Computer Use](https://learn.chatgpt.com/docs/computer-use)、[OpenAI Computer Use](https://developers.openai.com/api/docs/guides/tools-computer-use) | 明确操作对象、可见执行与授权、工具执行反馈 | 每步观察与动作绑定、完成核验、耗时统计；继续使用现有确认与停止链路 |
| [ZCode Agent](https://zcode.z.ai/en/docs/agent-framework) | 统一任务与工具、保留状态、长任务预算与恢复 | 保留现有规划、里程碑、预算、分类恢复；增加失效目标的独立恢复规则与阶段计时 |
| [Grok Bot 电脑与应用](https://docs.x.ai/grok-bot/computer-and-apps) | 持续会话、接管后回到任务、电脑状态可见 | 保留本机会话复用与实时预览；接管后重新观察；未加入云端执行 |
| [Muse 的安全设计](https://research.meta.ai/blog/security-and-safety-for-ai-agents-our-approach-with-muse) | 凭据、界面与执行权限分工，授权贴近实际动作 | 保留主进程安全存储与私有管道；新原生动作、合并输入仍走统一检查，不绕过模型图片隐私限制 |

这里采用公开的设计原则，代码为本项目实现。没有捆绑 Cua Driver 二进制、Spaces 或可选 AGPL 感知扩展。
Cua 的原生后台操作能力不能自动推导成本项目的能力：目前 Windows 仍检查前台窗口，物理输入会占用键盘鼠标。

## 执行路径

- **Windows 控件**：观察时保存 runtime ID 和控件对象；执行前核对窗口句柄/PID、runtime ID、名称、角色、启用与密码属性、勾选状态。按钮优先 Invoke，复选框 Toggle，单选/列表/标签 Select，输入框 Focus。可用身份但无原生模式时重新计算坐标；异常发生在调用之后时，不追加一次鼠标点击。
- **浏览器元素**：保存观察中的实际节点 Map 句柄，不用新快照里的同一个候选编号替代旧目标。点击前检查名称、角色、链接、表单提交身份和勾选状态；鼠标移入后再检查一次。Playwright 保留可见性、稳定性与命中检查，不强制点击遮挡元素。
- **指定字段输入**：`{"type":"type","element_id":3,"text":"Alice","clear":true}` 表示先聚焦，再核对实际焦点的持久身份和安全属性，再检查输入/提交权限。页面刷新、被移焦或暂停过都中止剩余输入。探索导航和恢复动作仍按一步一观察执行。
- **等待界面**：浏览器先做便宜的 DOM/滚动/输入/有限动画探测；可读取 Canvas 用独立小图探测两次截图之间的变化，再检查像素稳定并生成新的完整观察。跨源污染/WebGL 等无法读取的 Canvas 依赖截图检查。Windows 以最多 120 ms 的间隔采样，仍保留像素与全量 UIA 收尾检查。
- **成功判定**：界面稳定不等于任务完成。原有忙碌、旧证据、子目标与整任务核验保留；无法证明完成仍显示不确定。
- **统计**：结果的 `performance.phases` 记录 observe/plan/decide/ground/execute/settle/verify，`execution_routes` 记录实际尝试路径；成功仍以核验结果为准。API 往返时间保留在 budget 中。统计不记录输入内容或截图。

变化探测是有限采样，不是对未来所有界面变化的证明。动画、目标不可读、未知焦点等仍可能超时或需要人工接管。

## 验证与复现

执行相关测试覆盖移动元素、同名替换、重载、换标签、遮挡、hover 改名、勾选状态改变、中文输入、移焦、提交拒绝、暂停中断、CSS/Canvas 动画，以及原生动作部分成功后抛异常。
其他既有安全、隐私、预算、恢复与完成核验测试继续运行。

```bash
python -m pytest -q
cd desktop
npm run build
npm test
# 在图形会话中，或 Linux 的 xvfb-run 下：
npm run test:e2e
```

Windows CI 还运行 `desktop/scripts/smoke-windows-native.py`。它创建真实 WinForms 窗口，由 UIA 执行焦点输入、勾选、单选和按钮调用，读取应用自己写出的结果确认操作生效；不能激活窗口或找不到控件就是失败。安装包另有冻结 Worker 与 Chromium 的任务检查。这个小型原生场景不代表所有 Windows 应用都已测过。

基准使用真实旧提交 `55638dbba439a73e358d5d355918f7fefdd2cfb4` 与当前代码，交替运行相同姓名、邮箱、套餐、订阅和提交结果的本地表单。
旧版分别点击与输入，新版使用指定字段输入；均保留验证、安全与恢复。计时排除浏览器启动与首次绘制，包含观察、操作、等待与完成核验。
模型决定由固定脚本产生，无真实 API 请求、无人工注入的模型延迟。调用数是脚本模型经过正常预算计数器的次数。

```bash
git worktree add --detach /tmp/gui-agent-before 55638dbba439a73e358d5d355918f7fefdd2cfb4
python scripts/benchmark_execution.py --compare-with /tmp/gui-agent-before --repeat 3 --output result.json
```

正式基准应与测试分开运行，以免竞争浏览器/CPU。布局漂移场景在观察后移动目标按钮，并在旧位置放另一按钮，检查实际点到了谁。
基准只代表这组本地 Chromium 执行任务；真实模型速度受供应商、模型与网络影响，复杂 Windows 工作流还需要单独任务集。

2026-10-07 本地 Linux、Python 3.12、Chromium、1280×800，交替运行 5 次的结果见 [原始基准数据](benchmarks/execution-2026-10-07.json)：

| 同一表单任务 | 修改前提交 | v0.5 |
| --- | --- | --- |
| 耗时中位数（排除启动） | 14.65 s | 5.58 s |
| 脚本模型调用中位数 | 12 | 8 |
| 截图观察次数中位数 | 52 | 36 |
| 实际表单结果正确 | 5/5 | 5/5 |
| 错误宣称完成 | 0/5 | 0/5 |
| 单独布局漂移场景：目标点击正确 | 0/5 | 5/5 |

这组任务执行约快 2.63 倍，调用减少约 33%。样本小且决定是固定脚本；速度、布局场景结果不外推为真实模型或所有 Windows 应用的成功率。

## 后续能力需要单独验证

原生 OpenAI Responses/Anthropic computer-use 协议、可选择的 Cua Driver 后台执行、持久浏览器登录、应用级权限、文档/文件结构化工具，以及真实 Windows 长任务成功率比较，仍是独立工作。
不靠增加并行鼠标或放宽检查来宣称更快；后续以任务正确率、错误完成率、耗时和调用预算作为接入/替换执行器的依据。
