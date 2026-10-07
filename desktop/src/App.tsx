import { useEffect, useRef, useState, type FormEvent } from 'react'
import { ArrowUp, Check, CheckCircle2, ChevronDown, ChevronRight, CircleHelp, Cpu, Eye, EyeOff,
  Globe, Loader2, MessageSquare, Monitor, MoreHorizontal, Pause, Play, Plus, Settings2,
  ShieldCheck, Square, Workflow, X, XCircle } from 'lucide-react'
import { activeStatuses, applyEvent, sameService, statusText, type AgentEvent, type AppState, type DesktopEvent,
  type PublicSettings, type Run, type Session, type SettingsInput } from './shared'

function message(error: unknown) {
  return (error instanceof Error ? error.message : '操作失败').replace(/^Error invoking remote method '[^']+': Error: /, '')
}

function actionText(record: Record<string, unknown>) {
  const action = record.action as Record<string, unknown> | undefined
  if (!action) return String(record.kind || '观察界面')
  const names: Record<string, string> = { click: '点击', double_click: '双击', type: '输入', hotkey: '按键',
    scroll: '滚动', navigate: '打开网页', open_app: '打开应用', wait: '等待', focus_window: '切换窗口', drag: '拖动' }
  const target = record.displayTarget || action.target || action.url || action.app || ''
  if (action.type === 'type') return `输入 ${action.redacted ? '••••（已保护）' : String(action.text || '').slice(0, 70)}`
  if (action.type === 'hotkey') return `按键 ${(action.keys as string[] || []).join(' + ')}`
  return `${names[String(action.type)] || String(action.type)} ${target}`.trim()
}

function SettingsDialog({ settings, bundled, onClose, onSave }: {
  settings: PublicSettings; bundled: boolean; onClose(): void; onSave(settings: PublicSettings): void
}) {
  const [draft, setDraft] = useState<SettingsInput>(() => {
    const { hasApiKey: _has, keyPersisted: _persisted, ...value } = settings
    return { ...value, apiKey: '' }
  })
  const [showKey, setShowKey] = useState(false)
  const [busy, setBusy] = useState('')
  const [notice, setNotice] = useState<{ ok: boolean; text: string }>()
  const retainsKey = settings.hasApiKey && sameService(draft, settings) && !draft.clearApiKey
  const update = (value: Partial<SettingsInput>) => { setDraft((old) => ({ ...old, ...value })); setNotice(undefined) }
  async function save(event: FormEvent) {
    event.preventDefault()
    setBusy('save')
    try { onSave(await window.desktop.saveSettings(draft)); onClose() }
    catch (error) { setNotice({ ok: false, text: message(error) }) }
    finally { setBusy('') }
  }
  async function test() {
    setBusy('test'); setNotice(undefined)
    try {
      const result = await window.desktop.testConnection(draft)
      setNotice({ ok: result.ok, text: result.message })
    } catch (error) { setNotice({ ok: false, text: message(error) }) }
    finally { setBusy('') }
  }
  return <div className="modal-backdrop" onClick={() => { if (!busy) onClose() }}>
    <section className="settings-dialog" role="dialog" aria-modal="true" aria-label="模型与执行设置" onClick={(e) => e.stopPropagation()}>
      <div className="dialog-heading"><div><div className="eyebrow">PREFERENCES</div><h2>模型与执行设置</h2></div>
        <button className="icon-button" aria-label="关闭设置" onClick={onClose} disabled={!!busy}><X size={20} /></button></div>
      <form onSubmit={save}>
        <div className="form-section-label"><Cpu size={16} /> 接入你的模型</div>
        <label>接口协议<select aria-label="接口协议" value={draft.provider} onChange={(e) => update({ provider: e.target.value as SettingsInput['provider'] })}>
          <option value="openai">OpenAI 兼容 · Chat Completions</option><option value="anthropic">Anthropic · Messages</option>
        </select></label>
        <label>Base URL<input aria-label="Base URL" value={draft.baseUrl} placeholder="https://your-provider.com/v1" onChange={(e) => update({ baseUrl: e.target.value })} required /></label>
        <label>模型名称<input aria-label="模型名称" value={draft.model} placeholder="服务商提供的多模态模型 ID" onChange={(e) => update({ model: e.target.value })} /></label>
        <label>API Key<div className="key-input"><input aria-label="API Key" autoComplete="off" spellCheck={false} type={showKey ? 'text' : 'password'}
          value={draft.apiKey || ''} placeholder={retainsKey ? '已配置，留空保留现有 Key' : '输入此服务的 API Key；本地无认证服务可留空'}
          onChange={(e) => update({ apiKey: e.target.value, clearApiKey: false })} />
          <button type="button" className="icon-button" aria-label={showKey ? '隐藏 API Key' : '显示 API Key'} onClick={() => setShowKey(!showKey)}>{showKey ? <EyeOff size={16} /> : <Eye size={16} />}</button></div></label>
        <div className="field-hint">使用支持图片输入的模型。连接测试会向该地址发送一张生成的测试图片。</div>
        {settings.hasApiKey && !sameService(draft, settings) && <div className="field-hint">服务已变更，请填写新服务的 API Key。</div>}
        {settings.hasApiKey && <button type="button" className="text-button danger-text" onClick={() => update({ apiKey: '', clearApiKey: true })}>
          {draft.clearApiKey ? '保存后清除 API Key' : '清除已配置的 API Key'}</button>}
        <div className="form-section-label second"><Monitor size={16} /> 执行环境</div>
        <div className="two-fields"><label>操作对象<select aria-label="操作对象" value={draft.target} onChange={(e) => update({ target: e.target.value as SettingsInput['target'] })}>
          <option value="desktop">本机桌面</option><option value="browser">独立浏览器</option></select></label>
          <label>敏感操作<select aria-label="敏感操作" value={draft.safetyMode} onChange={(e) => update({ safetyMode: e.target.value as SettingsInput['safetyMode'] })}>
            <option value="confirm">询问我后执行</option><option value="deny">直接拒绝</option></select></label></div>
        {draft.target === 'browser' && <label>起始网页<input aria-label="起始网页" value={draft.startUrl} placeholder="https://example.com" onChange={(e) => update({ startUrl: e.target.value })} /></label>}
        <label>目标窗口标题 <span className="optional">可选</span><input value={draft.taskWindow} placeholder="例如：记事本；留空由 Agent 决定" onChange={(e) => update({ taskWindow: e.target.value })} /></label>
        <details className="advanced"><summary>运行参数</summary>
          <label>最多执行步骤<input type="number" min="1" max="200" value={draft.maxSteps} onChange={(e) => update({ maxSteps: Number(e.target.value) })} /></label>
          {!bundled && <label>Python 可执行文件 <span className="optional">可选</span><input value={draft.pythonPath} placeholder="自动使用项目 .venv 或系统 Python" onChange={(e) => update({ pythonPath: e.target.value })} /></label>}
        </details>
        {notice && <div className={`notice ${notice.ok ? 'good' : 'bad'}`} role="status">{notice.ok ? <CheckCircle2 size={16} /> : <XCircle size={16} />}{notice.text}</div>}
        <div className="dialog-footer"><button type="button" className="secondary-button" disabled={!!busy || !draft.model} onClick={() => { void test() }}>
          {busy === 'test' ? <Loader2 size={15} className="spin" /> : <Workflow size={15} />} 测试连接</button>
          <button className="primary-button" type="submit" disabled={!!busy}>{busy === 'save' ? '保存中…' : '保存设置'}</button></div>
      </form>
    </section>
  </div>
}

function RunCard({ run, onError }: { run: Run; onError(text: string): void }) {
  const [expanded, setExpanded] = useState(false)
  const records = run.events.filter((event) => event.type === 'log' && ['step', 'safety', 'replan', 'parse_error', 'milestone'].includes(String(event.record?.kind)))
  const plan = run.events.find((event) => event.type === 'log' && event.record?.kind === 'plan')?.record?.subgoals as Array<{ goal: string }> | undefined
  const lastError = run.events.findLast((event) => event.type === 'error')?.message
  const isActive = activeStatuses.has(run.status)
  const visible = expanded ? records : records.slice(-4)
  return <div className="conversation-pair">
    <div className="user-message">{run.task}</div>
    <div className="agent-message"><div className="agent-avatar"><Workflow size={17} /></div><div className="agent-content">
      <div className="agent-line"><strong>GUI Agent</strong><span className={`status-badge ${run.status}`}>{isActive && run.status !== 'paused' && run.status !== 'waiting' && <Loader2 size={12} className="spin" />}{statusText[run.status]}</span></div>
      {run.demo && <div className="demo-label">离线演示 · 预设动作 · 不调用模型 API</div>}
      {plan && <div className="plan"><div className="mini-label">执行计划</div>{plan.map((sg, index) => <div key={index}><span>{index + 1}</span>{sg.goal}</div>)}</div>}
      {!!visible.length && <div className="steps">{visible.map((event, index) => {
        const record = event.record!
        const okay = record.verdict === 'success' || record.kind === 'milestone'
        return <div className="step-row" key={`${event.time}-${index}`}>
          {okay ? <CheckCircle2 size={14} /> : <ChevronRight size={14} />}
          <span>{record.kind === 'replan' ? '调整执行计划' : record.kind === 'safety' ? `${record.approved ? '已允许' : '已阻止'}：${actionText(record)}` : record.kind === 'milestone' ? '子目标已验证' : record.kind === 'parse_error' ? '重新请求有效动作' : actionText(record)}</span>
          {record.step ? <small>#{String(record.step)}</small> : null}
        </div>
      })}</div>}
      {records.length > 4 && <button className="text-button" onClick={() => setExpanded(!expanded)}><MoreHorizontal size={14} />{expanded ? '收起步骤' : `查看全部 ${records.length} 条记录`}</button>}
      {run.status === 'paused' && <p>已暂停执行，你可以接管电脑。继续时会重新观察界面。</p>}
      {run.status === 'done' && <p className="result-copy">{run.result?.answer || '任务已执行，并通过完成核验。'}</p>}
      {run.status === 'stopped' && <p>执行已停止。</p>}
      {run.status === 'interrupted' && <p>上次运行已中断，请重新发送任务。</p>}
      {(run.status === 'fail' || run.status === 'uncertain') && <p className="result-copy">{run.result?.status === 'privacy_blocked' ? '检测到敏感界面，视觉操作已停止。请接管并检查当前结果。' : run.result?.message || '当前结果尚未通过核验，请检查界面后继续。'}</p>}
      {lastError && <p className="error-copy">{lastError}</p>}
      {run.result?.seconds !== undefined && <div className="field-hint" aria-label="运行耗时">
        用时 {run.result.seconds.toFixed(1)} 秒 · 模型调用 {run.result.budget?.calls || 0} 次
        {run.result.performance?.phases?.settle && ` · 等待界面 ${run.result.performance.phases.settle.seconds.toFixed(1)} 秒`}
      </div>}
      {run.reportReady && <button className="text-button" onClick={() => { void window.desktop.openReport(run.id).catch((error) => onError(message(error))) }}><Eye size={14} /> 查看运行报告</button>}
    </div></div>
  </div>
}

function HumanRequest({ event, onError, onAnswered }: { event: AgentEvent; onError(text: string): void; onAnswered(): void }) {
  const [answer, setAnswer] = useState('')
  const [busy, setBusy] = useState(false)
  async function respond(approved?: boolean) {
    setBusy(true)
    try {
      await window.desktop.respond({ runId: event.runId, requestId: event.requestId!,
        ...(event.kind === 'confirm' ? { approved } : { answer }) })
      onAnswered()
    } catch (error) { onError(message(error)) }
    finally { setBusy(false) }
  }
  return <div className="human-request" role="alert"><div className="request-heading"><ShieldCheck size={19} /><strong>{event.kind === 'confirm' ? '需要你的确认' : 'Agent 需要补充信息'}</strong></div>
    <p>{event.question}</p>
    {event.action && <code>{actionText({ action: event.action })}</code>}
    {event.kind === 'ask' && <textarea aria-label="回答 Agent" value={answer} placeholder="输入你的回答" onChange={(e) => setAnswer(e.target.value)} />}
    <div className="request-buttons">{event.kind === 'confirm' ? <><button className="secondary-button" disabled={busy} onClick={() => { void respond(false) }}>拒绝</button>
      <button className="primary-button" disabled={busy} onClick={() => { void respond(true) }}><Check size={15} /> 允许这次操作</button></> :
      <button className="primary-button" disabled={busy || !answer.trim()} onClick={() => { void respond() }}>发送回答</button>}</div>
  </div>
}

export default function App() {
  const [app, setApp] = useState<AppState>()
  const [sessionId, setSessionId] = useState('')
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [input, setInput] = useState('')
  const [error, setError] = useState('')
  const [pending, setPending] = useState<AgentEvent>()
  const [preview, setPreview] = useState<{ runId: string; image: string; title: string; time: number }>()
  const [privacy, setPrivacy] = useState(false)
  const [sending, setSending] = useState(false)
  const bottom = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!window.desktop) { setError('请从桌面 App 启动 GUI Agent。'); return }
    let disposed = false
    const unsubscribe = window.desktop.subscribe((event: DesktopEvent) => {
      if (event.type === 'run-created') {
        setPreview(undefined); setPrivacy(false); setPending(undefined)
        setApp((old) => old ? { ...old, sessions: old.sessions.map((s) => s.id === event.run.sessionId ? {
          ...s, title: s.runs.length ? s.title : event.run.task.slice(0, 24),
          runs: s.runs.some((r) => r.id === event.run.id) ? s.runs : [...s.runs, event.run],
        } : s) } : old)
        return
      }
      if (event.type === 'preview' && event.image) setPreview({ runId: event.runId, image: event.image, title: event.title || '', time: Date.now() })
      if (event.type === 'privacy') { setPreview(undefined); setPrivacy(true) }
      if (event.type === 'request') setPending(event)
      if (['result', 'error', 'state'].includes(event.type)) setPending(undefined)
      if (event.type !== 'preview') setApp((old) => old ? { ...old, sessions: old.sessions.map((s) => ({ ...s, runs: s.runs.map((r) => applyEvent(r, event)) })) } : old)
    })
    void window.desktop.load().then(async (value) => {
      if (!value.sessions.length) value.sessions = [await window.desktop.newSession()]
      if (disposed) return
      setApp(value); setSessionId(value.sessions[value.sessions.length - 1].id)
      const running = value.sessions.flatMap((s) => s.runs).find((r) => r.status === 'waiting')
      if (running) setPending(running.events.findLast((e) => e.type === 'request'))
    }).catch((e) => { if (!disposed) setError(message(e)) })
    return () => { disposed = true; unsubscribe() }
  }, [])

  const session = app?.sessions.find((s) => s.id === sessionId)
  const active = app?.sessions.flatMap((s) => s.runs).find((r) => activeStatuses.has(r.status))
  const last = session?.runs[session.runs.length - 1]
  useEffect(() => { bottom.current?.scrollIntoView({ behavior: 'smooth', block: 'end' }) }, [session?.runs.length, last?.events.length, pending])

  async function newSession() {
    try {
      const value = await window.desktop.newSession()
      setApp((old) => old ? { ...old, sessions: [...old.sessions, value] } : old)
      setSessionId(value.id); setInput(''); setError(''); setPreview(undefined); setPrivacy(false)
    } catch (e) { setError(message(e)) }
  }

  async function start(demo = false) {
    if (!app || active || sending) return
    if (!demo && !app.settings.model) { setSettingsOpen(true); return }
    const task = demo ? '填写本地联系表单并提交' : input.trim()
    if (!task) return
    setError(''); setSending(true)
    try {
      await window.desktop.start({ sessionId, task, demo })
      if (!demo) setInput('')
    } catch (e) { setError(message(e)) }
    finally { setSending(false) }
  }
  async function control(action: 'pause' | 'resume' | 'stop') {
    if (!active) return
    try { await window.desktop.control({ runId: active.id, action }) }
    catch (e) { setError(message(e)) }
  }

  if (!app) return <div className="boot-screen"><div className="brand-mark">g</div><p>{error || '正在启动 GUI Agent…'}</p></div>
  const showingPreview = preview && session?.runs.some((r) => r.id === preview.runId)
  const displayedTarget = (active?.sessionId === sessionId ? active : last)?.demo ? 'browser' : app.settings.target
  return <div className="app-shell">
    <aside className="sidebar"><div className="brand"><div className="brand-mark">g</div><div><strong>GUI Agent</strong><small>你的电脑操作助手</small></div></div>
      <button className="new-session" onClick={() => { void newSession() }} disabled={!!active}><Plus size={17} /> 新对话</button>
      <div className="sidebar-label">对话</div><nav className="session-list">{[...app.sessions].reverse().map((s) => <button key={s.id} className={s.id === sessionId ? 'selected' : ''} onClick={() => { setSessionId(s.id); setError('') }}>
        <MessageSquare size={15} /><span>{s.title}</span>{s.runs.some((r) => activeStatuses.has(r.status)) && <span className="live-dot" />}</button>)}</nav>
      <div className="sidebar-footer"><div className="local-label"><Monitor size={14} /><span>在本机运行</span><span className="live-dot" /></div>
        <button className="model-settings" disabled={!!active} onClick={() => setSettingsOpen(true)}><Settings2 size={17} /><div><strong>{app.settings.model || '连接你的模型'}</strong><small>{app.settings.provider === 'openai' ? 'OpenAI 兼容接口' : 'Anthropic Messages'}</small></div><ChevronRight size={14} /></button></div>
    </aside>
    <main className="workspace"><header className="workspace-header"><div><span className="header-title">{session?.title || '新对话'}</span><ChevronDown size={13} /></div>
      <div className="header-tools"><span className="environment-pill">{displayedTarget === 'desktop' ? <Monitor size={14} /> : <Globe size={14} />}{displayedTarget === 'desktop' ? '本机桌面' : '独立浏览器'}</span><button className="icon-button" aria-label="打开设置" disabled={!!active} onClick={() => setSettingsOpen(true)}><Settings2 size={17} /></button></div></header>
      <div className="work-area"><section className="chat-column"><div className="messages">
        {!session?.runs.length && <div className="welcome"><div className="welcome-icon"><Workflow size={30} strokeWidth={1.5} /></div><div className="eyebrow">YOUR COMPUTER, IN YOUR WORDS</div>
          <h1>你想在电脑上完成什么？</h1><p>告诉我目标，我来观察界面、执行操作，并检查结果。</p>
          <div className="suggestions"><button onClick={() => setInput('打开记事本，输入一段本周工作计划')}><Monitor size={18} /><span>操作桌面应用<small>打开记事本，写一份工作计划</small></span><ChevronRight size={15} /></button>
            <button onClick={() => setInput('在当前页面填写表单，完成后检查提交结果')}><Globe size={18} /><span>处理网页任务<small>填写表单，并验证提交结果</small></span><ChevronRight size={15} /></button></div>
          <button className="demo-button" disabled={sending} onClick={() => { void start(true) }}><Play size={13} /> 先试运行本地表单 <span>无需 API Key</span></button>
        </div>}
        {session?.runs.map((run) => <RunCard key={run.id} run={run} onError={setError} />)}
        {pending && active?.sessionId === sessionId && <HumanRequest key={pending.requestId} event={pending} onError={setError} onAnswered={() => setPending(undefined)} />}
        <div ref={bottom} />
      </div>
      <div className="composer-area">{error && <div className="error-toast" role="alert"><XCircle size={16} /><span>{error}</span><button className="icon-button" aria-label="关闭错误" onClick={() => setError('')}><X size={14} /></button></div>}
        {active && <div className="run-controls"><span className="control-status"><span className="live-dot" />{statusText[active.status]}</span><div>
          <button className="secondary-button" disabled={active.status === 'starting' || active.status === 'pausing' || active.status === 'waiting'} onClick={() => { void control(active.status === 'paused' ? 'resume' : 'pause') }}>{active.status === 'paused' ? <Play size={13} /> : <Pause size={13} />}{active.status === 'paused' ? '继续执行' : '暂停 / 接管'}</button>
          <button className="stop-button" onClick={() => { void control('stop') }}><Square size={12} fill="currentColor" /> 停止</button></div></div>}
        <div className={`composer ${active ? 'busy' : ''}`}><textarea aria-label="输入任务" placeholder={active ? '任务执行中，暂停后可接管电脑' : '描述你想完成的任务…'} value={input}
          disabled={!!active || sending} onChange={(e) => setInput(e.target.value)} onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); void start() }
          }} />
          <div className="composer-bottom"><button className="model-chip" onClick={() => setSettingsOpen(true)} disabled={!!active}><Cpu size={13} /><span>{app.settings.model || '设置模型'}</span><ChevronDown size={12} /></button>
            <button className="send-button" aria-label="执行任务" disabled={!input.trim() || !!active || sending} onClick={() => { void start() }}>{sending ? <Loader2 size={17} className="spin" /> : <ArrowUp size={19} />}</button></div></div>
        <div className="composer-hint"><ShieldCheck size={12} /> 敏感操作先确认 <span>·</span> Enter 发送，Shift + Enter 换行</div>
      </div></section>
      <aside className="inspector"><div className="inspector-title"><Monitor size={16} /><strong>执行画面</strong><span className="mini-label">{active ? 'LIVE' : 'LOCAL'}</span></div>
        <div className="screen-preview">{showingPreview ? <img src={preview.image} alt="Agent 当前观察到的界面" /> : <div className="empty-preview">{privacy ? <ShieldCheck size={29} /> : <Monitor size={29} />}<p>{privacy ? '敏感界面已保护' : '执行时显示界面'}</p><small>{privacy ? '本次运行已停止展示与发送截图' : '你可以随时暂停并接管操作'}</small></div>}</div>
        {showingPreview && <div className="preview-caption"><span>{preview.title || '当前界面'}</span><small>最近一次观察</small></div>}
        <div className="inspector-section"><div className="mini-label">当前环境</div><div className="info-row"><span>操作对象</span><strong>{last?.demo ? '独立浏览器 · 演示' : app.settings.target === 'desktop' ? '本机桌面' : '独立浏览器'}</strong></div>
          <div className="info-row"><span>系统</span><strong>{app.runtime.platform === 'win32' ? 'Windows' : app.runtime.platform === 'darwin' ? 'macOS' : 'Linux'}</strong></div>
          <div className="info-row"><span>确认策略</span><strong>{app.settings.safetyMode === 'confirm' ? '敏感操作询问' : '敏感操作拒绝'}</strong></div>
          {last?.result?.steps !== undefined && <div className="info-row"><span>执行步骤</span><strong>{last.result.steps}</strong></div>}
        </div>
        <div className="takeover-note"><CircleHelp size={16} /><div><strong>你始终可以接管</strong><p>暂停后直接操作目标应用，再点击继续。</p><small>{app.runtime.shortcutAvailable ? '紧急停止：Ctrl / Cmd + Alt + Shift + Esc' : '停止快捷键未注册，请使用停止按钮'}</small></div></div>
        {!app.settings.model && <div className="setup-note"><Cpu size={18} /><p>接入模型后，直接用自然语言操作电脑。</p><button className="text-button" disabled={!!active} onClick={() => setSettingsOpen(true)}>配置 Base URL 和 API Key <ChevronRight size={13} /></button></div>}
        {app.settings.hasApiKey && !app.settings.keyPersisted && <div className="memory-key-note">当前系统没有可用的安全存储，API Key 仅在本次打开期间保留。</div>}
      </aside></div>
    </main>
    {settingsOpen && <SettingsDialog settings={app.settings} bundled={app.runtime.bundledWorker} onClose={() => setSettingsOpen(false)} onSave={(settings) => setApp((old) => old ? { ...old, settings } : old)} />}
  </div>
}
