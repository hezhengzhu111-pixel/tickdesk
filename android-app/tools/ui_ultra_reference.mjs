import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { existsSync } from 'node:fs';
import { mkdir, mkdtemp, readFile, readdir, realpath, rm, writeFile } from 'node:fs/promises';
import { isAbsolute, join, relative, sep } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { setTimeout as delay } from 'node:timers/promises';

const app = fileURLToPath(new URL('../', import.meta.url));
const output = join(app, 'mobile/build/reports',
  process.argv.includes('--refined') ? 'ultra-1-refined' : 'ultra-1-design');
const prototype = pathToFileURL(join(app, '../docs/app-ui/tickdesk_17_ultra_1.html'));
const browser = process.env.TICKDESK_CHROME || 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const titles = ['事项收件箱', '事项详情', '买入回填', '选股', '持仓', '设置', '分析暂停', '锁屏推送',
  '前台全屏强提醒', '锁屏强提醒', '对话', '对话富文本', '截图导入回填'];

async function capture() {
  const directory = join(output, 'reference');
  await mkdir(directory, { recursive: true });
  const profile = await mkdtemp(join(output, 'chrome-'));
  const chrome = spawn(browser, ['--headless=new', '--no-first-run', '--disable-background-networking',
    '--remote-debugging-port=0', '--window-size=1440,1080', `--user-data-dir=${profile}`, 'about:blank'],
  { windowsHide: true, stdio: 'ignore' });
  let startupError;
  chrome.on('error', error => { startupError = error; });
  let socket;
  try {
    const endpoint = join(profile, 'DevToolsActivePort');
    for (let attempt = 0; attempt < 80 && !existsSync(endpoint); attempt++) {
      if (startupError) throw startupError;
      if (chrome.exitCode !== null) throw new Error('Chrome exited before the reference opened');
      await delay(100);
    }
    const port = (await readFile(endpoint, 'utf8')).split('\n')[0].trim();
    const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
    socket = new WebSocket(targets.find(target => target.type === 'page').webSocketDebuggerUrl);
    await new Promise((resolve, reject) => {
      socket.addEventListener('open', resolve, { once: true });
      socket.addEventListener('error', reject, { once: true });
    });
    let nextId = 0;
    function call(method, params = {}) {
      const id = ++nextId;
      return new Promise((resolve, reject) => {
        const timeout = setTimeout(() => {
          socket.removeEventListener('message', receive);
          reject(new Error(`${method} timed out`));
        }, 15000);
        function receive(event) {
          const message = JSON.parse(event.data);
          if (message.id !== id) return;
          socket.removeEventListener('message', receive);
          clearTimeout(timeout);
          if (message.error) reject(new Error(message.error.message));
          else resolve(message.result);
        }
        socket.addEventListener('message', receive);
        socket.send(JSON.stringify({ id, method, params }));
      });
    }
    await call('Page.enable');
    await call('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-reduced-motion', value: 'reduce' }] });
    await call('Page.navigate', { url: prototype.href });
    const evaluate = async expression => {
      const response = await call('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
      if (response.exceptionDetails) throw new Error(response.exceptionDetails.exception?.description || expression);
      return response.result.value;
    };
    for (let attempt = 0; attempt < 80; attempt++) {
      if (await evaluate('Boolean(document.querySelector("#screen-1.active"))')) break;
      if (attempt === 79) throw new Error('The supplied prototype did not load');
      await delay(100);
    }
    await evaluate('Promise.race([document.fonts.ready.then(() => true), new Promise(resolve => setTimeout(resolve, 2500))])');
    for (let screen = 1; screen <= titles.length; screen++) {
      const clip = await evaluate(`(() => {
        document.querySelector('#screenNav [data-go="${screen}"]').click();
        const active = document.querySelector('#screen-${screen}.active');
        if (!active) throw new Error('Screen ${screen} is not active');
        active.scrollTop = 0;
        const rect = document.querySelector('.phone-screen').getBoundingClientRect();
        return {x: rect.x + scrollX, y: rect.y + scrollY, width: rect.width, height: rect.height, scale: 1};
      })()`);
      const shot = await call('Page.captureScreenshot', { format: 'png', clip, captureBeyondViewport: true });
      await writeFile(join(directory, `${String(screen).padStart(2, '0')}.png`), Buffer.from(shot.data, 'base64'));
    }
  } finally {
    socket?.close();
    if (!startupError && chrome.exitCode === null) {
      const exited = once(chrome, 'exit');
      chrome.kill();
      await Promise.race([exited, delay(2000)]);
    }
    const profilePath = await realpath(profile);
    const child = relative(await realpath(output), profilePath);
    if (!child.startsWith('chrome-') || child.includes(sep) || isAbsolute(child)) {
      throw new Error('Chrome test profile resolved outside the report directory');
    }
    await rm(profilePath, { recursive: true, maxRetries: 5, retryDelay: 150 });
  }
}

async function report() {
  const variants = ['android-412', 'android-320'];
  const screenshots = await Promise.all(variants.map(async name => ({ name, files: existsSync(join(output, name))
    ? await readdir(join(output, name)) : [] })));
  const sections = titles.map((title, index) => {
    const id = String(index + 1).padStart(2, '0');
    const platforms = screenshots.map(({ name, files }) => {
      const captures = files.filter(file => file.startsWith(`${id}-`));
      if (!captures.length) {
        const note = id === '08' || id === '10' ? '锁屏由 Android 系统呈现，参见开发记录中的验收范围。' : '尚无此尺寸的截图。';
        return `<figure><figcaption>${name}</figcaption><p>${note}</p></figure>`;
      }
      return captures.map(file => `<figure><figcaption>${name} · ${file.slice(3, -4)}</figcaption><img loading="lazy" src="${name}/${file}" alt="${title} Android 截图"></figure>`).join('');
    }).join('');
    return `<section id="screen-${id}"><h2>${id} ${title}</h2><div><figure><figcaption>tickdesk_17_ultra_1.html</figcaption><img loading="lazy" src="reference/${id}.png" alt="${title}设计稿"></figure>${platforms}</div></section>`;
  }).join('\n');
  await writeFile(join(output, 'index.html'), `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>TickDesk 17 Ultra 逐屏对照</title>
<style>body{background:#080b11;color:#f8fafc;font:15px/1.55 system-ui;margin:24px}section{border-top:1px solid #273144;padding:14px 0}section div{display:flex;gap:14px;flex-wrap:wrap}figure{width:min(100%,394px);margin:0}figcaption,p{color:#94a3b8;margin-bottom:8px}img{width:100%;height:auto;display:block;border-radius:12px}a{color:#60a5fa}</style>
<h1>TickDesk · 17 Ultra 逐屏对照</h1><p>原型使用内置演示数据，Android 使用 instrumentation 隔离数据。系统状态栏、锁屏样式及换行随设备和系统设置变化。</p><nav>${titles.map((title, index) => `<a href="#screen-${String(index + 1).padStart(2, '0')}">${title}</a>`).join(' · ')}</nav>${sections}</html>`, 'utf8');
  console.log(join(output, 'index.html'));
}

if (!process.argv.includes('--report-only')) await capture();
await report();
