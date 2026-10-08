import { _electron as electron, expect, test } from '@playwright/test'
import { createServer } from 'node:http'
import { mkdtempSync, readFileSync, rmSync, writeFileSync, mkdirSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'

test('desktop UI drives Chromium, uses a configured API, confirms actions, pauses and stops', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'gui-agent-e2e-'))
  const apiKey = 'desktop-test-key-not-a-real-credential'
  let mode = 'form'
  let actionIndex = 0
  let releasePlanner: (() => void) | undefined
  let plannerWaiting = false
  const requests: Array<{ path: string; auth: string | undefined; images: number }> = []
  const server = createServer(async (request, response) => {
    const chunks: Buffer[] = []
    for await (const chunk of request) chunks.push(Buffer.from(chunk))
    const body = JSON.parse(Buffer.concat(chunks).toString())
    const system = String(body.messages?.[0]?.content || '')
    const parts = body.messages?.[1]?.content || []
    const text = parts.find((part: { type: string }) => part.type === 'text')?.text || ''
    requests.push({ path: request.url!, auth: request.headers.authorization,
      images: parts.filter((part: { type: string }) => part.type === 'image_url').length })
    let content = 'OK'
    if (system.includes('careful planner')) {
      const taskLine = text.split('\n')[0]
      mode = taskLine.includes('发送按钮') ? 'send' : 'form'
      actionIndex = 0
      if (taskLine.includes('等待测试')) {
        plannerWaiting = true
        await new Promise<void>((done) => { releasePlanner = done })
      }
      content = JSON.stringify({ subgoals: [{ goal: mode === 'send' ? 'click Send' : 'fill form and submit',
        expected: 'the page displays the result', expect_text: mode === 'send' ? 'Sent successfully' : 'Submitted: Alice' }] })
    } else if (system.startsWith('You operate')) {
      const actions = mode === 'send' ? [{ type: 'click', target: 'Send' }, { type: 'done' }] : [
        { type: 'click', target: 'Name' }, { type: 'type', text: 'Alice' },
        { type: 'click', target: 'Email' }, { type: 'type', text: 'alice@example.com' },
        { type: 'click', target: 'Pro' }, { type: 'click', target: 'Subscribe to newsletter' },
        { type: 'click', target: 'Submit' }, { type: 'done' },
      ]
      content = JSON.stringify({ thought: 'test fixture action', action: actions[actionIndex++] || { type: 'done' } })
    } else if (!system.includes('testing an API connection')) {
      content = JSON.stringify({ verdict: 'success', evidence: 'test fixture verifier', notes: [] })
    }
    response.writeHead(200, { 'Content-Type': 'application/json' })
    response.end(JSON.stringify({ id: 'fixture', object: 'chat.completion', model: 'test-vision',
      choices: [{ index: 0, message: { role: 'assistant', content }, finish_reason: 'stop' }],
      usage: { prompt_tokens: 10, completion_tokens: 10, total_tokens: 20 } }))
  })
  await new Promise<void>((done) => server.listen(0, '127.0.0.1', done))
  const port = (server.address() as { port: number }).port
  const electronApp = await electron.launch({
    args: [resolve('out/main/index.js'), '--no-sandbox'],
    env: { ...process.env, GUI_AGENT_USER_DATA: directory, GUI_AGENT_TEST_HEADLESS: '1' },
  })
  try {
    const page = await electronApp.firstWindow()
    async function submitTask(task: string) {
      const count = await page.locator('.conversation-pair').count()
      await page.getByLabel('输入任务', { exact: true }).fill(task)
      await page.getByRole('button', { name: '执行任务', exact: true }).click()
      // IPC creates the new run asynchronously. The previous run can still
      // say "done" immediately after clicking; bind assertions to this run.
      await expect(page.locator('.conversation-pair')).toHaveCount(count + 1)
      return page.locator('.conversation-pair').nth(count)
    }
    const errors: string[] = []
    page.on('pageerror', (error) => errors.push(error.message))
    await expect(page.getByText('你想在电脑上完成什么？')).toBeVisible()
    expect(await page.evaluate(() => 'require' in window)).toBe(false)
    expect(await page.evaluate(() => 'process' in window)).toBe(false)
    expect(await page.evaluate(() => 'invoke' in window.desktop || 'readFile' in window.desktop)).toBe(false)
    const screenshots = process.env.GUI_AGENT_SCREENSHOTS
    if (screenshots) {
      mkdirSync(screenshots, { recursive: true })
      await page.screenshot({ path: join(screenshots, 'welcome.png') })
    }

    // Real renderer -> preload -> main process -> Python -> Playwright, no LLM.
    await page.getByRole('button', { name: /先试运行本地表单/ }).click()
    await expect(page.locator('.status-badge').last()).toHaveText('已完成', { timeout: 60000 })
    await expect(page.getByAltText('Agent 当前观察到的界面')).toBeVisible()
    const offline = await page.evaluate(() => window.desktop.load())
    const demo = offline.sessions[offline.sessions.length - 1].runs[0]
    expect(demo.result?.status).toBe('done')
    expect(requests).toHaveLength(0)
    const meta = JSON.parse(readFileSync(join(directory, 'runs', demo.id, 'meta.json'), 'utf8'))
    expect(meta.result.claimed_done).toBe(true)
    if (screenshots) await page.screenshot({ path: join(screenshots, 'completed-demo.png') })

    // Configure an actual loopback OpenAI-compatible HTTP fixture, then run a task
    // through the real SDK. The fixture supplies model decisions, not execution.
    await page.getByRole('button', { name: '打开设置' }).click()
    await page.getByLabel('Base URL', { exact: true }).fill(`http://127.0.0.1:${port}/v1`)
    await page.getByLabel('模型名称', { exact: true }).fill('test-vision')
    await page.getByLabel('API Key', { exact: true }).fill(apiKey)
    await page.getByLabel('操作对象', { exact: true }).selectOption('browser')
    await page.getByLabel('起始网页', { exact: true }).fill(resolve('../tasks/web_assets/form.html'))
    await page.getByRole('button', { name: '测试连接' }).click()
    await expect(page.getByRole('status')).toContainText('模型连接和图片输入测试通过')
    await page.getByRole('button', { name: '保存设置' }).click()
    await expect(page.getByRole('dialog', { name: '模型与执行设置' })).toBeHidden()
    const publicState = await page.evaluate(() => window.desktop.load())
    expect(publicState.settings.hasApiKey).toBe(true)
    expect(JSON.stringify(publicState)).not.toContain(apiKey)
    expect(readFileSync(join(directory, 'state.json'), 'utf8')).not.toContain(apiKey)

    const formRun = await submitTask('填写姓名 Alice 和邮箱 alice@example.com，选择 Pro、订阅并提交')
    await expect(formRun.locator('.status-badge')).toHaveText('已完成', { timeout: 60000 })
    expect(requests.length).toBeGreaterThan(3)
    expect(requests.every((request) => request.path === '/v1/chat/completions' && request.auth === `Bearer ${apiKey}`)).toBe(true)
    expect(requests.some((request) => request.images > 0)).toBe(true)

    // A real page named Send triggers the existing safety guard. Test refusal,
    // then a new task with explicit approval; neither is automatically approved.
    const sendPage = join(directory, 'send.html')
    writeFileSync(sendPage, '<!doctype html><title>Send test</title><button onclick="document.getElementById(\'result\').textContent=\'Sent successfully\'">Send</button><p id="result"></p>')
    await page.getByRole('button', { name: '打开设置' }).click()
    await page.getByLabel('起始网页', { exact: true }).fill(sendPage)
    await page.getByRole('button', { name: '保存设置' }).click()
    await expect(page.getByRole('dialog', { name: '模型与执行设置' })).toBeHidden()
    const refusedRun = await submitTask('发送按钮测试：点击 Send')
    await expect(page.getByText('需要你的确认', { exact: true })).toBeVisible()
    await page.getByRole('button', { name: '拒绝', exact: true }).click()
    await expect(refusedRun.locator('.status-badge')).toHaveText('未完成')
    const approvedRun = await submitTask('发送按钮测试：点击 Send')
    await expect(page.getByText('需要你的确认', { exact: true })).toBeVisible()
    await page.getByRole('button', { name: '允许这次操作' }).click()
    await expect(approvedRun.locator('.status-badge')).toHaveText('已完成')

    // Pause during an in-flight model request, then release the request. A paused
    // worker must not execute its pending action and can be stopped from the UI.
    const pausedRun = await submitTask('等待测试：暂不操作界面')
    await expect.poll(() => plannerWaiting).toBe(true)
    await page.getByRole('button', { name: '暂停 / 接管' }).click()
    await expect(pausedRun.locator('.status-badge')).toHaveText('正在暂停')
    releasePlanner!()
    await expect(pausedRun.locator('.status-badge')).toHaveText('已暂停')
    await page.getByRole('button', { name: '停止', exact: true }).click()
    await expect(pausedRun.locator('.status-badge')).toHaveText('已停止')
    expect(errors).toEqual([])
  } finally {
    releasePlanner?.()
    await electronApp.close()
    await new Promise<void>((done) => server.close(() => done()))
    rmSync(directory, { recursive: true, force: true })
  }
})
