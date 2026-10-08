# GUI Agent 桌面 App v0.8

Windows x64 本机电脑操作助手。界面采用 React + TypeScript，由系统 Microsoft Edge 的独立 App 窗口打开；引擎使用安装包内的嵌入式 Python。浏览器任务也使用系统 Edge，不再捆绑 Electron 或 Chromium。

## 安装与使用

需要 Windows 10/11 x64 和已安装的 Microsoft Edge。无需另装 Python、Node.js 或浏览器，也无需管理员权限。

1. 从 [desktop app 工作流](https://github.com/cszf1/gui-agent/actions/workflows/desktop.yml) 的成功构建下载 **GUI-Agent-Windows-Installer**，解压并运行 `GUI-Agent-0.8.0-x64-Setup.exe`。
2. 打开桌面或开始菜单的 GUI Agent，点击左下角模型设置，填写协议、Base URL、模型 ID 和 API Key。
3. 点击“测试连接”，选择“本机桌面”或“独立浏览器”，保存后输入任务并按 Enter。连接测试会向所填模型服务发送一张生成的测试图片，按服务商规则计费。
4. 在界面处理确认请求；可以暂停接管、继续或停止。Windows 紧急停止快捷键为 **Ctrl + Alt + Shift + Esc**；界面显示该快捷键是否注册成功。

不使用 API Key 的“试运行本地表单”会实际操作系统 Edge，但动作来自脚本，不能作为真实模型自主成功率的证据。
安装包尚未配置代码签名。v0.7 及更早的 Electron 安装包请先卸载再安装 v0.8，以免同时保留两套程序；旧 Key 需重新填写。v0.8 安装器支持覆盖升级，升级保留配置和历史。

### 模型配置

- OpenAI 兼容接口填写 API 根地址，例如 `https://api.openai.com/v1` 或服务商的 `/v1`，不要附加 `/chat/completions`。
- Anthropic Messages 填写 `https://api.anthropic.com` 或服务根地址，也接受末尾 `/v1`，不要附加 `/messages`。
- 使用支持图片输入的模型，填写准确模型 ID。本地无认证服务可留空 Key。
- Windows Key 用当前用户的 DPAPI 加密，界面不读回明文。更换服务需重新填 Key；不能加密时仅在内存保留，不降级保存明文。

模型会收到操作所需的界面文字和图片；敏感输入继续遵守引擎的截图阻断规则。界面采用通用 JSON 动作协议，CLI 的原生 computer-use 适配器继续保留。

### 会话与窗口

同一对话的连续任务复用执行环境及此前任务的结果摘要。切换对话、修改设置、停止或关闭 App 会结束当前执行环境；浏览器登录状态不跨 App 重启保存。

系统 Edge 界面使用本软件专属的临时 profile，不复用个人 Edge profile。浏览器任务同样使用独立的临时 profile。本机桌面任务开始时聊天窗口最小化，确认、暂停和结束时恢复。

关闭 App 会结束自己的后台引擎、执行器和 Edge 进程；Windows Job Object 在后台异常退出时结束其所属子进程。异常断电留下的临时文件在下一次启动清理。任务通过 UIA 或鼠标键盘操作用户应用的效果仍然存在。

## 文件与清理

| 内容 | 位置 / 策略 |
| --- | --- |
| 程序、嵌入式 Python、前端 | `%LOCALAPPDATA%\Programs\GUI Agent` |
| 配置、加密 Key、历史 | `%APPDATA%\GUI Agent\state.json`，历史文件限制为 8 MiB |
| 运行报告 | 数据目录下 `runs`，最多 30 天、50 份、200 MiB；启动和任务结束时清理 |
| Edge profile、临时文件、UIA 生成缓存 | 数据目录下 `cache` / `tmp`，正常关闭后清理 |
| 截图 | 默认只实时预览，不写盘；可在设置中主动启用保存 |
| 安装登记 | 当前用户 HKCU 下两项应用键、当前用户桌面和开始菜单快捷方式 |

设置中的“清理历史和任务缓存”删除本软件的历史、报告与任务临时文件，保留模型配置和 Key；正在使用的界面 Edge profile 在关闭窗口后清理。清理按钮不会删除任务产生的外部文档。

从 Windows 设置 → 已安装的应用 → GUI Agent → 卸载，或者运行程序目录中的 `Uninstall.exe`。**卸载会删除本软件全部用户数据，包括 Key、设置、历史与主动保存的截图**，并移除程序目录、快捷方式和本软件注册表项。目录归属标记及 junction/symlink 检查防止清理跨越到外部文档目录。卸载清理未完成时会提示重试，不报告虚假的成功。

安装器不写系统 PATH，不设置开机启动、服务或计划任务，不写 HKLM。软件清理自己的文件；Windows 的事件记录、Prefetch、下载的安装包等由系统或用户管理。

## 从源码构建

在 Windows x64 使用 Python 3.13、Node.js 24 与 NSIS；这些是构建依赖，最终用户不需要安装。

```powershell
python -m pip install -e ".[windows,web]"
cd desktop
npm ci
npm run build
npm test
npm run dist:win
```

生成 `desktop/release/GUI-Agent-0.8.0-x64-Setup.exe` 和 `build-manifest.json`。构建下载官方 Python 3.13.9 embedded ZIP 并校验固定 SHA-256，依赖安装到内置 runtime。不执行 `playwright install chromium`。Playwright 自己的私有 Node 驱动随依赖保留，体积计入清单；它不提供 Electron 桌面壳。

体积闸门为安装包小于 150 MiB，且负载内禁止浏览器/Electron 可执行文件。最终体积以成功 Windows 构建的清单为准。

## 开发与验证

Windows 从根目录执行 `python -B -m gua.windows_app`（先构建前端），会打开真实系统 Edge 窗口。`npm run dev` 只提供前端资源开发服务；完整应用使用 Python 服务，不依赖 Vite dev server。

CI 在 Linux 验证真实 React → 认证本机 HTTP → Python → Playwright 链路，以及模型接口、拒绝/确认、暂停/停止和清理。Linux 的开发测试使用 Chromium，与 Windows 安装负载无关。

Windows CI 构建后，先用嵌入式 Python 检查真实 WinForms/UIA 控件，再实际运行安装器和系统 Edge 窗口，检查任务结果、DPAPI、关闭与崩溃、升级、卸载、快捷方式/注册表以及外部文档和无关 Edge 的保留。全部通过才上传安装包。范围与证据见 [v0.8 验证记录](../docs/review-v0.8.md)。
