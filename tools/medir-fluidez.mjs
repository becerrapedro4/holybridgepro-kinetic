// Mide fluidez del overlay con Chrome headless + CDP.
// Uso: node medir.mjs "http://127.0.0.1:7401/?effect=pop"
import { spawn } from 'node:child_process';
import { setTimeout as sleep } from 'node:timers/promises';

const URL_OBJ = process.argv[2] || 'http://127.0.0.1:7401/?effect=pop';
const TEXTO = process.argv[3] || 'EL SEÑOR ES MI PAZ Y MI FORTALEZA EN TODO MOMENTO Y EN TODO LUGAR ALELUYA';
const PORT = 9223;
const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe';

const chrome = spawn(CHROME, [
  '--headless=new',
  `--remote-debugging-port=${PORT}`,
  '--user-data-dir=C:/Users/CRR003/AppData/Local/Temp/cdp-fluidez',
  '--window-size=1920,1080',
  '--hide-scrollbars',
  '--disable-background-timer-throttling',
  '--disable-renderer-backgrounding',
  '--disable-backgrounding-occluded-windows',
  '--autoplay-policy=no-user-gesture-required',
  URL_OBJ,
], { stdio: 'ignore' });

let salir = () => {};
try {
  let target = null;
  for (let i = 0; i < 60 && !target; i += 1) {
    await sleep(400);
    try {
      const lista = await (await fetch(`http://127.0.0.1:${PORT}/json`)).json();
      target = lista.find((t) => t.type === 'page' && t.url.startsWith('http'));
    } catch { /* todavía no levantó */ }
  }
  if (!target) throw new Error('Chrome no expuso ningún target');

  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });

  let id = 0;
  const pendientes = new Map();
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.id && pendientes.has(msg.id)) {
      pendientes.get(msg.id)(msg);
      pendientes.delete(msg.id);
    }
  };
  const enviar = (method, params = {}) => new Promise((res) => {
    const mio = ++id;
    pendientes.set(mio, res);
    ws.send(JSON.stringify({ id: mio, method, params }));
  });

  await enviar('Runtime.enable');
  await sleep(1200); // que carguen fuentes y SSE

  const expresion = `(async () => {
    const medir = (ms) => new Promise((res) => {
      const t = []; let last = performance.now(); const start = last;
      function step(now) {
        t.push(now - last); last = now;
        if (now - start < ms) requestAnimationFrame(step);
        else {
          const s = t.slice(1).sort((a, b) => a - b);
          const q = (p) => s[Math.min(s.length - 1, Math.floor(s.length * p))];
          res({ frames: s.length,
                fps: +(1000 / (s.reduce((a, b) => a + b, 0) / s.length)).toFixed(1),
                p50: +q(0.5).toFixed(2), p95: +q(0.95).toFixed(2), max: +q(1).toFixed(2),
                sobre20ms: s.filter((x) => x > 20).length });
        }
      }
      requestAnimationFrame(step);
    });
    const texto = ${JSON.stringify(TEXTO)};
    const reposo = await medir(800);
    await fetch('/api/text', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: texto, title: 'Fluidez', progress: null }) });
    const animacion = await medir(2000);
    await new Promise((r) => setTimeout(r, 1200));
    const estable = await medir(800);
    return { palabras: document.querySelectorAll('.w').length, reposo, animacion, estable };
  })()`;

  const r = await enviar('Runtime.evaluate', {
    expression: expresion, awaitPromise: true, returnByValue: true, timeout: 25000,
  });
  if (r.result?.exceptionDetails) {
    console.error('EXCEPCIÓN:', JSON.stringify(r.result.exceptionDetails, null, 2));
  } else {
    console.log(JSON.stringify(r.result.result.value, null, 2));
  }
  ws.close();
} finally {
  chrome.kill();
}
