'use strict';

/**
 * Kinetic Lyrics Bridge
 * --------------------
 * Recibe texto (Holyrics / HolyBridge / HTTP / WebSocket upstream) y lo empuja
 * al overlay de OBS por SSE. Sin dependencias externas, sin polling del DOM.
 *
 * Rutas:
 *   GET  /               -> overlay (OBS Browser Source)
 *   GET  /api/events     -> stream SSE de estado (push, latencia sub-ms en LAN)
 *   GET  /api/state      -> snapshot JSON actual
 *   POST /api/text       -> { text, title, progress, effect, style }
 *   POST /api/control    -> { effect, style, stagger, speed }
 *   POST /api/clear      -> limpia el texto
 *   ANY  /holybridge     -> alias de /api/text (acepta JSON o texto plano)
 */

const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');

const { startHolyricsSource } = require('./lib/holyrics');

const PUBLIC_DIR = path.join(__dirname, 'public');
const FONTS_DIR = path.join(PUBLIC_DIR, 'fonts');
const CONFIG = loadConfig();

fs.mkdirSync(FONTS_DIR, { recursive: true });

const FONT_EXT = new Set(['.ttf', '.otf', '.woff', '.woff2']);

/** Fuentes instaladas por el usuario (public/fonts). */
function listFonts() {
  try {
    return fs
      .readdirSync(FONTS_DIR)
      .filter((file) => FONT_EXT.has(path.extname(file).toLowerCase()))
      .map((file) => {
        const base = path.basename(file, path.extname(file));
        return {
          file,
          family: base,
          key: base.toLowerCase().replace(/[^a-z0-9]+/g, ''),
        };
      });
  } catch {
    return [];
  }
}

const EFFECTS = new Set([
  'pop', 'fade', 'rise', 'drop', 'slide', 'blur', 'flip', 'glitch', 'wave', 'typewriter',
  'bounce', 'spin', 'swing', 'punch', 'stretch', 'drift', 'reveal', 'none',
]);

/** Fuente Holyrics activa (se puede reconfigurar en caliente por HTTP). */
let holyricsSource = null;

/** Configuración que publica el panel (la usa el emisor NDI). */
let panelConfig = {
  effect: CONFIG.effect,
  font: 'inter',
  size: '',
  case: 'asis',
  hlmode: 'color',
  hlfont: 'pacifico',
  highlight: '#ffd23f',
  words: '',
};

/** Campos aceptados del panel (nada de HTML/CSS crudo). */
const PANEL_KEYS = new Set(['effect', 'font', 'size', 'case', 'hlmode', 'hlfont', 'highlight', 'words']);

function sanitizePanel(raw) {
  const out = {};
  if (!raw || typeof raw !== 'object') return out;
  for (const [key, value] of Object.entries(raw)) {
    if (!PANEL_KEYS.has(key)) continue;
    if (typeof value === 'string' && value.length <= 120 && !/[;{}<>]/.test(value)) out[key] = value;
    else if (typeof value === 'number') out[key] = String(value);
  }
  if (out.effect && !EFFECTS.has(out.effect)) delete out.effect;
  return out;
}

/** Estado autoritativo (ultima verdad enviada al overlay). */
const state = {
  revision: 0,
  text: '',
  title: '',
  progress: null,
  effect: CONFIG.effect,
  style: {
    stagger: CONFIG.stagger,
    speed: CONFIG.speed,
  },
  updatedAt: 0,
};

/** @type {Set<import('node:http').ServerResponse>} */
const clients = new Set();

const os = require('node:os');

/* ------------------------------------------------------------------ config */

function loadConfig() {
  const defaults = {
    port: 7333,
    // 0.0.0.0 = acepta conexiones de la red local (celular, otra PC de la
    // iglesia, OBS de otra máquina). Con 127.0.0.1 sólo respondía al propio PC.
    host: '0.0.0.0',
    effect: 'pop',
    stagger: 55,
    speed: 520,
    holyrics: {
      enabled: false,
      ip: '127.0.0.1',
      port: 80,
      intervalMs: 60,
      timeoutMs: 900,
      apiPort: '',
      apiToken: '',
      skipTypes: [],
      format: { uppercase: false, singleLine: false, stripVersion: true },
    },
    upstreamWs: { enabled: false, url: '', textField: 'text' },
    poller: { enabled: false, url: '', intervalMs: 100 },
  };
  try {
    const raw = JSON.parse(fs.readFileSync(path.join(__dirname, 'config.json'), 'utf8'));
    const merged = { ...defaults, ...raw };
    merged.holyrics = { ...defaults.holyrics, ...(raw.holyrics || {}) };
    merged.upstreamWs = { ...defaults.upstreamWs, ...(raw.upstreamWs || {}) };
    merged.poller = { ...defaults.poller, ...(raw.poller || {}) };
    merged.holyrics.format = { ...defaults.holyrics.format, ...((raw.holyrics || {}).format || {}) };
    return applyEnv(merged);
  } catch (err) {
    console.warn('[config] usando defaults:', err.message);
    return applyEnv(defaults);
  }
}

/** Overrides por entorno: sirven para pruebas y para cambiar la IP sin editar JSON. */
function applyEnv(cfg) {
  // BRIDGE_PORT (no PORT): otras herramientas exportan PORT=0 y romperian el bind.
  const port = Number(process.env.BRIDGE_PORT);
  if (Number.isFinite(port) && port > 0) cfg.port = port;
  if (process.env.BRIDGE_HOST) cfg.host = process.env.BRIDGE_HOST;
  if (process.env.HOLYRICS_IP) cfg.holyrics.ip = process.env.HOLYRICS_IP;
  const hPort = Number(process.env.HOLYRICS_PORT);
  if (Number.isFinite(hPort) && hPort > 0) cfg.holyrics.port = hPort;
  const hInterval = Number(process.env.HOLYRICS_INTERVAL);
  if (Number.isFinite(hInterval) && hInterval > 0) cfg.holyrics.intervalMs = hInterval;
  if (process.env.HOLYRICS_ENABLED === '0') cfg.holyrics.enabled = false;
  return cfg;
}

/* -------------------------------------------------------------- utilidades */

const TEXT_KEYS = ['text', 'lyrics', 'content', 'message', 'slide', 'line', 'lyric', 'letra'];
const TITLE_KEYS = ['title', 'song', 'nome', 'name'];
const PROGRESS_KEYS = ['progress', 'percent', 'pct', 'p', 'percentage'];

function pick(obj, keys) {
  for (const k of keys) {
    const v = obj[k];
    if (typeof v === 'string' && v.length) return v;
  }
  return '';
}

function pickNumber(obj, keys) {
  for (const k of keys) {
    const v = obj[k];
    if (typeof v === 'number' && Number.isFinite(v)) return v;
    if (typeof v === 'string' && v.trim() !== '' && Number.isFinite(Number(v))) return Number(v);
  }
  return null;
}

function normalizeProgress(value) {
  if (value === null || value === undefined) return null;
  let n = typeof value === 'number' ? value : Number(value);
  if (!Number.isFinite(n)) return null;
  if (n > 1) n = n / 100;
  return Math.min(1, Math.max(0, n));
}

/** Normaliza cualquier payload entrante al shape del estado. */
function normalizePayload(raw) {
  if (typeof raw === 'string') return { text: raw };
  if (!raw || typeof raw !== 'object') return {};

  const out = {};
  const text = pick(raw, TEXT_KEYS);
  if (text) out.text = text;

  // Algunos bridges mandan lineas o parrafos como array.
  if (!out.text && Array.isArray(raw.lines)) {
    const joined = raw.lines.filter((l) => typeof l === 'string').join('\n');
    if (joined) out.text = joined;
  }
  if (!out.text && Array.isArray(raw.paragraphs)) {
    const joined = raw.paragraphs
      .map((p) => (typeof p === 'string' ? p : p && p.text))
      .filter((t) => typeof t === 'string' && t.length)
      .join('\n');
    if (joined) out.text = joined;
  }

  const title = pick(raw, TITLE_KEYS);
  if (title) out.title = title;

  const progress = pickNumber(raw, PROGRESS_KEYS);
  if (progress !== null) out.progress = normalizeProgress(progress);

  if (typeof raw.effect === 'string' && EFFECTS.has(raw.effect)) out.effect = raw.effect;

  if (raw.style && typeof raw.style === 'object') out.style = sanitizeStyle(raw.style);
  if (typeof raw.stagger === 'number') out.style = { ...(out.style || {}), stagger: clamp(raw.stagger, 0, 400) };
  if (typeof raw.speed === 'number') out.style = { ...(out.style || {}), speed: clamp(raw.speed, 80, 4000) };

  return out;
}

const STYLE_KEYS = new Set([
  'size', 'family', 'font', 'fill', 'hl', 'weight', 'color', 'highlight', 'stagger', 'speed',
  'align', 'uppercase', 'shadow', 'outline', 'outlineColor', 'lineHeight', 'maxWidth',
  'case', 'hlmode', 'hlfont',
]);

function sanitizeStyle(style) {
  const out = {};
  for (const [k, v] of Object.entries(style)) {
    if (!STYLE_KEYS.has(k)) continue;
    if (typeof v === 'number' && Number.isFinite(v)) out[k] = v;
    else if (typeof v === 'boolean') out[k] = v;
    else if (typeof v === 'string' && v.length <= 120 && !/[;{}]/.test(v)) out[k] = v;
  }
  return out;
}

function clamp(n, min, max) {
  return Math.min(max, Math.max(min, n));
}

function getByPath(obj, dotted) {
  if (!dotted) return obj;
  return dotted.split('.').reduce((acc, key) => (acc == null ? acc : acc[key]), obj);
}

/* ---------------------------------------------------------------- broadcast */

function publish(patch) {
  // Un texto nuevo invalida el progreso del slide anterior.
  if ('text' in patch && !('progress' in patch) && patch.text !== state.text) patch.progress = null;

  const next = { ...state, ...patch };
  if (next.effect && !EFFECTS.has(next.effect)) delete next.effect;
  if (patch.style) next.style = { ...state.style, ...patch.style };

  // Si nada cambio (mismo texto, mismo efecto, misma progresion) no re-renderizamos.
  const unchanged =
    next.text === state.text &&
    next.title === state.title &&
    next.effect === state.effect &&
    next.progress === state.progress &&
    JSON.stringify(next.style) === JSON.stringify(state.style);

  Object.assign(state, next);
  if (unchanged) return;

  state.revision += 1;
  state.updatedAt = Date.now();
  broadcast();
}

function broadcast() {
  if (!clients.size) return;
  const frame = `event: text\ndata: ${JSON.stringify(state)}\n\n`;
  for (const res of clients) {
    try {
      res.write(frame);
    } catch {
      clients.delete(res);
    }
  }
}

/* ------------------------------------------------------------------ helpers */

function sendJson(res, status, body) {
  const payload = JSON.stringify(body);
  res.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': Buffer.byteLength(payload),
    'Cache-Control': 'no-store',
    'Access-Control-Allow-Origin': '*',
  });
  res.end(payload);
}

function readRaw(req, limit = 8 * 1024 * 1024) {
  return new Promise((resolve, reject) => {
    let size = 0;
    const chunks = [];
    req.on('data', (chunk) => {
      size += chunk.length;
      if (size > limit) {
        reject(new Error('archivo demasiado grande (máx 8 MB)'));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => resolve(Buffer.concat(chunks)));
    req.on('error', reject);
  });
}

function readBody(req, limit = 1024 * 1024) {
  return new Promise((resolve, reject) => {
    let size = 0;
    const chunks = [];
    req.on('data', (chunk) => {
      size += chunk.length;
      if (size > limit) {
        reject(new Error('body too large'));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
    req.on('error', reject);
  });
}

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.woff2': 'font/woff2',
};

function serveStatic(res, pathname) {
  const rel = pathname === '/' ? 'index.html' : decodeURIComponent(pathname).replace(/^\/+/, '');
  const file = path.join(PUBLIC_DIR, rel);
  if (!file.startsWith(PUBLIC_DIR)) {
    res.writeHead(403).end('forbidden');
    return;
  }
  fs.readFile(file, (err, data) => {
    if (err) {
      res.writeHead(404, { 'Content-Type': 'text/plain' }).end('not found');
      return;
    }
    res.writeHead(200, {
      'Content-Type': MIME[path.extname(file).toLowerCase()] || 'application/octet-stream',
      'Cache-Control': 'no-cache',
      'Access-Control-Allow-Origin': '*',
    });
    res.end(data);
  });
}

/* --------------------------------------------------------------------- SSE */

function handleEvents(req, res) {
  res.writeHead(200, {
    'Content-Type': 'text/event-stream; charset=utf-8',
    'Cache-Control': 'no-cache, no-transform',
    Connection: 'keep-alive',
    'X-Accel-Buffering': 'no',
    'Access-Control-Allow-Origin': '*',
  });
  // retry bajo: si OBS reinicia el source, reconecta casi al instante.
  res.write('retry: 400\n\n');
  res.write(': connected\n\n');
  res.write(`event: text\ndata: ${JSON.stringify(state)}\n\n`);

  clients.add(res);
  const heartbeat = setInterval(() => {
    try {
      res.write(': ping\n\n');
    } catch {
      /* el close handler limpia */
    }
  }, 15000);

  const cleanup = () => {
    clearInterval(heartbeat);
    clients.delete(res);
  };
  req.on('close', cleanup);
  req.on('error', cleanup);
  res.on('error', cleanup);
}

/* ------------------------------------------------------------------ ingest */

async function ingest(req, res, alias) {
  let raw = '';
  try {
    raw = await readBody(req);
  } catch (err) {
    sendJson(res, 413, { ok: false, error: err.message });
    return;
  }

  let payload = {};
  const contentType = String(req.headers['content-type'] || '');
  if (contentType.includes('json') || raw.trim().startsWith('{') || raw.trim().startsWith('[')) {
    try {
      const parsed = JSON.parse(raw);
      payload = Array.isArray(parsed) ? { text: parsed.filter((x) => typeof x === 'string').join('\n') } : parsed;
    } catch {
      payload = { text: raw };
    }
  } else {
    payload = { text: raw };
  }

  // Permite usar la query string como via de paso rapido (?text=...&progress=...).
  const url = new URL(req.url, 'http://localhost');
  for (const key of ['text', 'title', 'progress', 'effect']) {
    if (!(key in payload) && url.searchParams.has(key)) payload[key] = url.searchParams.get(key);
  }

  const patch = normalizePayload(payload);
  if (!('text' in patch) && !('title' in patch) && !('effect' in patch) && !('progress' in patch) && !('style' in patch)) {
    sendJson(res, 400, { ok: false, error: 'payload sin campos reconocibles' });
    return;
  }

  publish(patch);
  if (alias) {
    res.writeHead(204, { 'Access-Control-Allow-Origin': '*' }).end();
    return;
  }
  sendJson(res, 200, { ok: true, revision: state.revision });
}

/* ---------------------------------------------------------------- holyrics */

/**
 * (Re)configura el sondeo de Holyrics sin reiniciar el bridge.
 *
 * La GUI guarda la IP/puerto en holybridgeconfig.json, pero el bridge tiene su
 * propio config.json: sin esto, Kinetic Lyrics seguía leyendo la IP vieja hasta
 * reiniciar todo. Con este endpoint la GUI empuja la configuración al guardar.
 */
function aplicarHolyrics(cambios) {
  const cfg = { ...CONFIG.holyrics, ...(cambios || {}) };
  const ip = String(cfg.ip || '127.0.0.1').trim() || '127.0.0.1';
  const port = Number(cfg.port) || 80;
  const intervalMs = clamp(Number(cfg.intervalMs) || 60, 25, 2000);
  const activa = cfg.enabled !== false && String(cfg.enabled) !== '0';

  if (holyricsSource) {
    try { holyricsSource.stop(); } catch (err) { /* ya estaba detenida */ }
    holyricsSource = null;
  }

  CONFIG.holyrics = { ...cfg, ip, port, intervalMs, enabled: activa };

  if (!activa) {
    console.log('[holyrics] fuente desactivada');
    return { enviando: false, ip, port, intervalMs };
  }

  holyricsSource = startHolyricsSource(CONFIG.holyrics, (patch) => publish(patch));
  console.log(`[holyrics] fuente configurada: ${ip}:${port} cada ${intervalMs}ms`);
  return { enviando: true, ip, port, intervalMs };
}

/** Estado actual de la fuente, sin tocar nada (para diagnóstico de la GUI). */
function aplicarHolyricsEstado() {
  const h = CONFIG.holyrics || {};
  return {
    enviando: Boolean(holyricsSource),
    ip: h.ip,
    port: h.port,
    intervalMs: h.intervalMs,
    apiPort: h.apiPort || '',
  };
}

/* ----------------------------------------------------------------- server */

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  const pathname = url.pathname;

  if (req.method === 'OPTIONS') {
    res.writeHead(204, {
      'Access-Control-Allow-Origin': '*',
      'Access-Control-Allow-Methods': 'GET,POST,OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type, Authorization',
    });
    res.end();
    return;
  }

  if (pathname === '/api/events') return handleEvents(req, res);
  if (pathname === '/api/state') return sendJson(res, 200, state);

  if (pathname === '/api/panel' && req.method === 'GET') return sendJson(res, 200, panelConfig);

  if (pathname === '/api/panel' && req.method === 'POST') {
    try {
      const body = JSON.parse((await readBody(req)) || '{}');
      panelConfig = { ...panelConfig, ...sanitizePanel(body) };
      panelConfig.updatedAt = Date.now();
      return sendJson(res, 200, { ok: true, panel: panelConfig });
    } catch (err) {
      return sendJson(res, 400, { ok: false, error: err.message });
    }
  }

  if (pathname === '/api/holyrics' && req.method === 'GET') {
    return sendJson(res, 200, { ok: true, ...aplicarHolyricsEstado() });
  }

  if (pathname === '/api/holyrics' && req.method === 'POST') {
    try {
      const body = JSON.parse((await readBody(req)) || '{}');
      const cambios = {};
      if (body.ip !== undefined) cambios.ip = String(body.ip).trim();
      if (body.port !== undefined) cambios.port = Number(body.port) || 80;
      if (body.intervalMs !== undefined) cambios.intervalMs = Number(body.intervalMs) || 60;
      if (body.apiPort !== undefined) cambios.apiPort = String(body.apiPort || '');
      if (body.apiToken !== undefined) cambios.apiToken = String(body.apiToken || '');
      if (body.enabled !== undefined) cambios.enabled = Boolean(body.enabled);
      if (body.format && typeof body.format === 'object') cambios.format = body.format;
      const estadoFuente = aplicarHolyrics(cambios);
      return sendJson(res, 200, { ok: true, ...estadoFuente });
    } catch (err) {
      return sendJson(res, 400, { ok: false, error: err.message });
    }
  }

  if (pathname === '/api/fonts' && req.method === 'GET') return sendJson(res, 200, listFonts());

  if (pathname === '/api/fonts' && req.method === 'POST') {
    const name = String(url.searchParams.get('name') || '').trim();
    const ext = path.extname(name).toLowerCase();
    if (!FONT_EXT.has(ext)) {
      return sendJson(res, 400, { ok: false, error: 'formato no soportado (.ttf .otf .woff .woff2)' });
    }
    const safe = `${path.basename(name, path.extname(name)).replace(/[^A-Za-z0-9 _-]/g, '_').slice(0, 60)}${ext}`;
    try {
      const buffer = await readRaw(req);
      if (!buffer.length) return sendJson(res, 400, { ok: false, error: 'archivo vacío' });
      fs.writeFileSync(path.join(FONTS_DIR, safe), buffer);
      console.log(`[fonts] instalada: ${safe} (${Math.round(buffer.length / 1024)} KB)`);
      return sendJson(res, 200, { ok: true, installed: safe, fonts: listFonts() });
    } catch (err) {
      return sendJson(res, 400, { ok: false, error: err.message });
    }
  }

  if (pathname === '/api/text' && req.method === 'POST') return ingest(req, res, false);
  if (pathname === '/holybridge') return ingest(req, res, true);

  if (pathname === '/api/clear' && req.method === 'POST') {
    publish({ text: '', progress: null });
    return sendJson(res, 200, { ok: true });
  }

  if (pathname === '/api/control' && req.method === 'POST') {
    try {
      const body = JSON.parse((await readBody(req)) || '{}');
      const patch = {};
      if (typeof body.effect === 'string' && EFFECTS.has(body.effect)) patch.effect = body.effect;
      if (body.style) patch.style = sanitizeStyle(body.style);
      if (typeof body.stagger === 'number') patch.style = { ...(patch.style || {}), stagger: clamp(body.stagger, 0, 400) };
      if (typeof body.speed === 'number') patch.style = { ...(patch.style || {}), speed: clamp(body.speed, 80, 4000) };
      publish(patch);
      return sendJson(res, 200, { ok: true, effect: state.effect, style: state.style });
    } catch (err) {
      return sendJson(res, 400, { ok: false, error: err.message });
    }
  }

  if (pathname === '/config' || pathname === '/panel') return serveStatic(res, '/config.html');

  if (req.method === 'GET') return serveStatic(res, pathname);
  sendJson(res, 405, { ok: false, error: 'method not allowed' });
});

/* ---------------------------------------------------------------- upstream */

function connectUpstreamWs(cfg) {
  if (!cfg.enabled || !cfg.url) return;
  if (typeof WebSocket !== 'function') {
    console.warn('[ws] este Node no expone WebSocket global; actualiza a Node 22+');
    return;
  }
  let backoff = 300;
  const connect = () => {
    const ws = new WebSocket(cfg.url);
    ws.addEventListener('open', () => {
      backoff = 300;
      console.log(`[ws] conectado a ${cfg.url}`);
    });
    ws.addEventListener('message', (event) => {
      const data = typeof event.data === 'string' ? event.data : '';
      if (!data) return;
      let parsed;
      try {
        parsed = JSON.parse(data);
      } catch {
        parsed = { text: data };
      }
      const payload = {
        text: getByPath(parsed, cfg.textField),
        title: getByPath(parsed, cfg.titleField),
        progress: getByPath(parsed, cfg.progressField),
        effect: parsed && parsed.effect,
      };
      const patch = normalizePayload(payload);
      if (Object.keys(patch).length) publish(patch);
    });
    ws.addEventListener('close', () => {
      setTimeout(connect, backoff);
      backoff = Math.min(backoff * 2, 5000);
    });
    ws.addEventListener('error', () => ws.close());
  };
  connect();
}

let lastPolled = null;

function startPoller(cfg) {
  if (!cfg.enabled || !cfg.url) return;
  const interval = clamp(cfg.intervalMs || 100, 16, 5000);
  const tick = async () => {
    try {
      const init = {
        method: cfg.method || 'POST',
        headers: { 'Content-Type': 'application/json', ...(cfg.headers || {}) },
      };
      if (init.method !== 'GET') init.body = JSON.stringify(cfg.body || {});
      const response = await fetch(cfg.url, init);
      if (response.ok) {
        const data = await response.json().catch(() => null);
        const text = getByPath(data, cfg.textField);
        const title = getByPath(data, cfg.titleField);
        const progress = getByPath(data, cfg.progressField);
        const signature = `${text}|${title}|${progress}`;
        if (text !== undefined && signature !== lastPolled) {
          lastPolled = signature;
          const patch = normalizePayload({ text, title, progress });
          if (Object.keys(patch).length) publish(patch);
        }
      }
    } catch {
      /* upstream caido: se reintenta en el siguiente tick */
    }
    setTimeout(tick, interval);
  };
  console.log(`[poller] consultando ${cfg.url} cada ${interval}ms`);
  tick();
}

server.listen(CONFIG.port, CONFIG.host, () => {
  console.log(`[bridge] escuchando en http://${CONFIG.host}:${CONFIG.port}`);
  console.log(`[bridge] overlay OBS: http://127.0.0.1:${CONFIG.port}/`);
  if (CONFIG.host === '0.0.0.0' || CONFIG.host === '::') {
    for (const [nombre, dirs] of Object.entries(os.networkInterfaces())) {
      for (const dir of dirs || []) {
        if (dir.family !== 'IPv4' || dir.internal) continue;
        console.log(`[bridge] desde la red (${nombre}): http://${dir.address}:${CONFIG.port}/config`);
      }
    }
  }
  aplicarHolyrics({});
  connectUpstreamWs(CONFIG.upstreamWs);
  startPoller(CONFIG.poller);
});

process.on('SIGINT', () => {
  console.log('\n[bridge] cerrando');
  server.close(() => process.exit(0));
});
