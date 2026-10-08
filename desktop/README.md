# GUI Agent 桌面 App

Windows 优先的本机电脑操作助手：打开 App，配置模型，在聊天框输入任务。
桌面壳采用 Electron + React + TypeScript，执行引擎复用仓库中的 Python GUI Agent。

## 开发模式启动（Windows PowerShell）

需要 Python 3.10+、Node.js 22.12+（推荐 24）。从仓库根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[windows,web]"
.\.venv\Scripts\python.exe -m playwright install chromium
cd desktop
npm ci
npm run dev
```

App 会自动找到项目根目录的 `.venv\Scripts\python.exe`。
也可以在设置中的“运行参数”填写已有 Python 环境的可执行文件路径，或设置 `GUI_AGENT_PYTHON` 环境变量。
macOS/Linux 的桌面开发分别安装 `.[macos,web]` / `.[linux,web]`；Linux 桌面需要 X11，当前不支持 Wayland。

## 使用

1. 点击左下角“连接你的模型”，填写接口协议、Base URL、模型名称和 API Key。
2. 点击“测试连接”。测试向你填写的模型服务发送一张生成的图片，用来检查 API 和图片输入；按服务商规则计费。
3. 选择“本机桌面”或“独立浏览器”，保存设置，在聊天框输入任务，按 Enter 执行。
4. 对需要确认的动作，在 App 中允许或拒绝。暂停后可以手动接管，继续时 Agent 重新观察界面。
5. 点击“停止”，或使用全局快捷键 **Ctrl + Alt + Shift + Esc**（macOS 为 Cmd + Alt + Shift + Esc）。

无需模型的“试运行本地表单”使用预设动作操作真实 Chromium，用来验证安装与执行链路；它不代表模型自主任务的成功率。

### 模型接入

- **OpenAI 兼容**：填写 API 根地址，例如 `https://api.openai.com/v1`、服务商提供的 `/v1` 地址或 `http://localhost:11434/v1`。地址不要以 `/chat/completions` 结尾。
- **Anthropic Messages**：填写服务根地址，例如 `https://api.anthropic.com`；也接受末尾 `/v1`。地址不要以 `/messages` 结尾。
- 需要填写服务商提供的准确模型 ID，并使用支持图片输入的模型。无认证本地服务可以留空 API Key。
- 同一个配置供规划、动作、定位和核验使用，默认不会连接额外的本地 UI-TARS 服务。桌面版采用通用 JSON 动作协议。

Windows 的 API Key 使用 Electron `safeStorage`（操作系统保护）加密后保存；没有可用安全存储时仅保留在内存。
界面不会读回已保存 Key 的明文；更换接口协议或服务地址后需重新填写该服务的 Key。
模型服务会收到执行所需的界面图片和文字；密码/敏感焦点沿用引擎的严格截图阻断。
同一执行环境进入截图阻断后，后续任务也保持阻断，重新创建环境才能解除。

### 会话与接管

同一对话里的连续任务复用当前浏览器/桌面环境，并向模型提供之前任务及结果的摘要。
浏览器会话保留到切换对话、修改设置、停止任务或退出 App；目前不跨 App 重启保存浏览器登录状态。
桌面任务开始时 App 最小化，避免操作到自己的聊天界面；确认、暂停和结束时恢复窗口。
暂停在操作边界生效，正在进行的网络请求会先返回；停止会终止执行进程及其子进程。

会话记录和运行报告位于系统的 GUI Agent 用户数据目录（Windows 通常为 `%APPDATA%\GUI Agent`）。
可从每条任务的“查看运行报告”打开 HTML 回放。截图只在允许保存时写入运行目录，不写入聊天历史文件。

## Windows 安装包

在 **Windows** 上打包（从上述 `desktop` 目录继续）：

```powershell
..\.venv\Scripts\python.exe -m pip install pyinstaller
..\.venv\Scripts\python.exe scripts/build-worker.py
..\.venv\Scripts\python.exe scripts/smoke-worker.py
npm run dist:win
```

输出为 `desktop/release/GUI-Agent-0.6.0-x64-Setup.exe`。
安装包包含独立 Python 执行引擎和 Chromium，最终用户无需另外安装 Python、Node.js 或修改 YAML。
安装包尚未配置代码签名。

仓库的 [desktop app 工作流](../.github/workflows/desktop.yml) 先运行桌面测试，再在 Windows 构建并实际运行冻结后的引擎和 Chromium。
通过后在工作流的 Artifacts 中提供 **GUI-Agent-Windows-Installer**；工作流失败时不会上传不完整安装包。

## 检查

```powershell
npm run build
npm test
npm run test:e2e
```

Electron 端到端测试会启动真实 App 与 Python 执行引擎，验证真实浏览器表单、配置后的模型 API 请求、拒绝与允许、暂停和停止。
模型回复由本地 HTTP 测试服务提供，不调用付费 API。Linux 无显示器环境使用 `xvfb-run -a npm run test:e2e`。

产品范围、技术栈取舍与官方参考见 [桌面产品设计](../docs/desktop-app.md)。
