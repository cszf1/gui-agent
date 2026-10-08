# v0.8 Windows 瘦身与清理验证

本版按产品要求移除 Electron 和内置 Chromium。React 界面由系统 Edge 的独立 App 窗口打开，浏览器任务使用 Playwright 的 `msedge` channel，引擎使用官方嵌入式 Python。

## 实现与范围

- 官方 Python 3.13.9 x64 embedded ZIP，SHA-256 固定校验；依赖安装至私有 `Lib/site-packages`。启动器不依赖系统 Python/Node。
- 仅绑定 `127.0.0.1` 随机端口的受限 API，控制令牌、Host/Origin 校验与 CSP；模型 Key 经私有管道传给执行器，DPAPI 当前用户加密。
- 软件界面使用自己的 Edge profile，避免个人浏览器数据混用。Windows Job Object 管理自建的执行器和 Edge 子进程；进程删除同时核对 PID 的创建时间。
- 程序和数据分别集中在 `%LOCALAPPDATA%\Programs\GUI Agent`、`%APPDATA%\GUI Agent`。
- 默认实时预览不保存截图；报告限 30 天/50 份/200 MiB，历史文件限 8 MiB；临时目录包含 UIA 生成的 COM 包装缓存，退出时清理。
- NSIS `RequestExecutionLevel user`，仅写 HKCU、当前用户快捷方式与目录，不修改系统 PATH、自启、服务或任务计划。升级保留数据，卸载删除软件用户数据。
- 软件目录归属标记与 junction/symlink 检查防止清理跟随到外部文档；无法完成清理时卸载返回失败并保留安装登记。

## 已完成的本地检查

2026-10-08：

| 检查 | 结果 |
| --- | --- |
| Python 全量回归，包括真实 Web 和 Linux sandbox | 614 passed，306.90 秒 |
| 前端 TypeScript / Vite 构建 | 通过；主 JS 252.22 KB，gzip 79.62 KB |
| 前端事件路由与预览不写历史 | 2 passed |
| 完整 React → 认证 HTTP → Python → Playwright E2E | 1 passed；表单、模型协议、拒绝/确认、暂停/停止、清理与配置保留 |
| 新增存储与 HTTP 边界 + 既有桌面 worker | 33 passed |

Linux 开发验证使用 Chromium，不计入 Windows 安装负载。模型响应由本地测试服务确定性提供，不能证明真实模型自主任务的成功率。

## Windows 构建检查

[desktop app 工作流](../.github/workflows/desktop.yml) 对本版运行以下真实检查，全部通过后才上传安装包和 `build-manifest.json`：

1. 禁止负载出现 Electron/Chromium/Edge 浏览器可执行文件，安装包小于 150 MiB。
2. 使用负载内嵌入式 Python 检查真实 WinForms/UIA 输入、后台模式与实际控件状态。
3. 实际运行 NSIS 安装器、小型启动器、系统 Edge 界面并确认 React 已连接；执行内置脚本表单任务并验证结果。
4. 检查默认无截图、DPAPI 加密、程序目录不被运行时写入、无 Playwright 浏览器下载。
5. 关闭真实 UI 窗口，确认引擎/Edge 结束且缓存和临时文件为空；覆盖升级和重启保留配置、Key 与历史。
6. 强制终止后台引擎，确认所属子进程结束，重启清理崩溃遗留临时文件。
7. 实际卸载，确认程序目录、用户数据、快捷方式与两个应用注册表键消失。
8. 在程序与用户数据目录中放入指向外部文档的 junction，确认卸载后文档保留；同时确认无关 Edge 仍可使用。

最终安装包字节数以成功构建清单为准。v0.7 的旧安装包构建产物 ZIP 为 405,294,618 字节，包含 Electron 与 Chromium；比较时需注明 ZIP 与内部 EXE 的区别。

## 实测的边界

真实 UIA 检查仅覆盖有限 WinForms 控件。复杂第三方应用、长期运行、多显示器、系统 Edge 的企业管理策略和实际付费模型任务需要后续实机验证。

软件自身的文件、注册表和进程属于清理范围。Windows Prefetch、事件日志、下载的安装包与任务生成的外部文档不由卸载器删除。用户主动启用截图保存时仍受已有隐私阻断约束。
