'use strict';

/**
 * Fuente Holyrics de baja latencia.
 *
 * Replica el protocolo que usa HolyBridge Pro (HolyBridgeProV6.py):
 *   principal : GET  http://{ip}:{port}/view/text.json   -> data.map.text (HTML) + data.map.type
 *   respaldo  : POST http://{ip}:{apiPort}/api/GetCPInfo?token=... -> data.data.text
 *
 * Diferencias deliberadas para latencia:
 *   - socket keep-alive (no handshake TCP por sondeo)
 *   - cadencia 60 ms por defecto (HolyBridge usa 500 ms), con correccion de deriva
 *   - sin BeautifulSoup ni parseo de HTML si el texto no cambio (huella cruda)
 */

const http = require('node:http');

/**
 * Entidades HTML que manda Holyrics.
 *
 * Antes sólo estaban las básicas y las tipográficas: los acentos y la ñ venían
 * como `Se&ntilde;or` / `am&oacute;` y se mostraban tal cual ("no detectaba"
 * las letras con tilde). Acá va la tabla latina completa, en minúscula y
 * mayúscula, más `&iquest;` / `&iexcl;` del español.
 */
const ENTITIES = {
  amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ',
  ndash: '–', mdash: '—', hellip: '…', rsquo: '’', lsquo: '‘', ldquo: '“', rdquo: '”',
  // Signos y símbolos
  iexcl: '¡', iquest: '¿', cent: '¢', pound: '£', curren: '¤', yen: '¥', brvbar: '¦',
  sect: '§', uml: '¨', copy: '©', ordf: 'ª', laquo: '«', not: '¬', shy: '\u00ad',
  reg: '®', macr: '¯', deg: '°', plusmn: '±', sup2: '²', sup3: '³', acute: '´',
  micro: 'µ', para: '¶', middot: '·', cedil: '¸', sup1: '¹', ordm: 'º', raquo: '»',
  frac14: '¼', frac12: '½', frac34: '¾', times: '×', divide: '÷', euro: '€',
  // Mayúsculas acentuadas
  Agrave: 'À', Aacute: 'Á', Acirc: 'Â', Atilde: 'Ã', Auml: 'Ä', Aring: 'Å', AElig: 'Æ',
  Ccedil: 'Ç', Egrave: 'È', Eacute: 'É', Ecirc: 'Ê', Euml: 'Ë', Igrave: 'Ì', Iacute: 'Í',
  Icirc: 'Î', Iuml: 'Ï', ETH: 'Ð', Ntilde: 'Ñ', Ograve: 'Ò', Oacute: 'Ó', Ocirc: 'Ô',
  Otilde: 'Õ', Ouml: 'Ö', Oslash: 'Ø', Ugrave: 'Ù', Uacute: 'Ú', Ucirc: 'Û', Uuml: 'Ü',
  Yacute: 'Ý', THORN: 'Þ',
  // Minúsculas acentuadas
  agrave: 'à', aacute: 'á', acirc: 'â', atilde: 'ã', auml: 'ä', aring: 'å', aelig: 'æ',
  ccedil: 'ç', egrave: 'è', eacute: 'é', ecirc: 'ê', euml: 'ë', igrave: 'ì', iacute: 'í',
  icirc: 'î', iuml: 'ï', eth: 'ð', ntilde: 'ñ', ograve: 'ò', oacute: 'ó', ocirc: 'ô',
  otilde: 'õ', ouml: 'ö', oslash: 'ø', ugrave: 'ù', uacute: 'ú', ucirc: 'û', uuml: 'ü',
  yacute: 'ý', thorn: 'þ', yuml: 'ÿ', szlig: 'ß',
};

const VERSION_RE = /^(RVR|RV|NVI|LBLA|NBLA|DHH|TLA|PDT|RVA|KJV|NIV|ESV|NASB|ARC|ARA|NTLH|MSG|NTV|BLPH)[\s-]*\d{0,4}$/i;

function decodeEntities(input) {
  return String(input).replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi, (match, code) => {
    if (code[0] === '#') {
      const num = code[1] === 'x' || code[1] === 'X' ? parseInt(code.slice(2), 16) : parseInt(code.slice(1), 10);
      return Number.isFinite(num) ? String.fromCodePoint(num) : match;
    }
    // Ojo: `code` distingue mayúsculas (&Aacute; vs &aacute;), así que se busca
    // primero tal cual y recién después en minúscula (para &AMP;, &NBSP;...).
    for (const key of [code, code.toLowerCase(), code.toUpperCase()]) {
      if (Object.prototype.hasOwnProperty.call(ENTITIES, key)) return ENTITIES[key];
    }
    return match;
  });
}

/** HTML de Holyrics -> texto plano con saltos de linea reales. */
function htmlToText(html) {
  if (!html) return '';
  const withBreaks = String(html)
    .replace(/<br\s*\/?>/gi, '\n')
    .replace(/<\/(p|div|li|h[1-6]|tr|blockquote)>/gi, '\n');
  const plain = decodeEntities(withBreaks.replace(/<[^>]*>/g, ''));
  const lines = plain
    .replace(/\u00a0/g, ' ')
    .split('\n')
    .map((line) => line.replace(/[ \t]+/g, ' ').trim());

  const out = [];
  for (const line of lines) {
    if (line === '' && (out.length === 0 || out[out.length - 1] === '')) continue;
    out.push(line);
  }
  while (out.length && out[out.length - 1] === '') out.pop();
  return out.join('\n').trim();
}

/** Quita la version de la Biblia: «Juan 3:16 (RVR1960)» -> «Juan 3:16». */
function stripVersion(cita) {
  if (!cita) return cita;
  let limpia = cita.trim();
  let previo;
  do {
    previo = limpia;
    limpia = limpia.replace(/\s*[([][^()[\]]*[)\]]\s*$/, '').trim();
  } while (limpia !== previo && limpia);

  const partes = limpia.split(/\s+/);
  if (partes.length > 1) {
    const ultima = partes[partes.length - 1];
    const penultima = partes[partes.length - 2];
    if (VERSION_RE.test(ultima)) partes.pop();
    else if (VERSION_RE.test(`${penultima}${ultima}`)) partes.splice(-2);
    limpia = partes.join(' ').replace(/[\s,;:\-–—·]+$/, '').trim();
  }
  return limpia || cita;
}

/** Payload de Holyrics (HTML + type) -> patch del bridge. */
function buildPatch(rawHtml, type, format) {
  const opts = format || {};
  const html = rawHtml || '';

  const descMatch = html.match(/<desc[^>]*>([\s\S]*?)<\/desc>/i);
  let cita = descMatch ? htmlToText(descMatch[1]) : '';
  const sinDesc = descMatch ? html.replace(descMatch[0], '') : html;
  let versiculo = htmlToText(sinDesc);

  if (opts.uppercase) {
    versiculo = versiculo.toUpperCase();
    cita = cita.toUpperCase();
  }
  if (opts.stripVersion) cita = stripVersion(cita);

  let text = versiculo;
  if (cita) {
    if (!versiculo) text = cita;
    else text = opts.singleLine ? `${versiculo} ${cita}` : `${versiculo}\n${cita}`;
  }

  return { text, title: cita || '', type: type || '' };
}

function clamp(n, min, max) {
  return Math.min(max, Math.max(min, n));
}

function getJson(agent, host, port, path, timeout, method = 'GET', body = null) {
  return new Promise((resolve, reject) => {
    const req = http.request(
      {
        agent,
        host,
        port,
        path,
        method,
        timeout,
        headers: body ? { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) } : {},
      },
      (res) => {
        if (res.statusCode !== 200) {
          res.resume();
          reject(new Error(`HTTP ${res.statusCode}`));
          return;
        }
        let data = '';
        res.setEncoding('utf8');
        res.on('data', (chunk) => {
          data += chunk;
          if (data.length > 2 * 1024 * 1024) req.destroy(new Error('respuesta demasiado grande'));
        });
        res.on('end', () => {
          try {
            resolve(JSON.parse(data));
          } catch (err) {
            reject(new Error('JSON invalido'));
          }
        });
      },
    );
    req.on('timeout', () => req.destroy(new Error('timeout')));
    req.on('error', reject);
    if (body) req.write(body);
    req.end();
  });
}

/**
 * Arranca el sondeo de Holyrics.
 * @param {object} cfg
 * @param {(patch: object) => void} onChange
 */
function startHolyricsSource(cfg, onChange) {
  const state = {
    ip: cfg.ip || '127.0.0.1',
    port: Number(cfg.port) || 80,
    intervalMs: clamp(Number(cfg.intervalMs) || 60, 25, 2000),
    timeoutMs: clamp(Number(cfg.timeoutMs) || 900, 100, 5000),
    apiPort: Number(cfg.apiPort) || null,
    apiToken: cfg.apiToken || '',
    skipTypes: Array.isArray(cfg.skipTypes) ? cfg.skipTypes.map((t) => String(t).toLowerCase()) : [],
    format: { uppercase: false, singleLine: false, stripVersion: true, ...(cfg.format || {}) },
  };

  const agent = new http.Agent({ keepAlive: true, maxSockets: 4, keepAliveMsecs: 15000 });
  let lastSignature = null;
  let failures = 0;
  let stopped = false;

  const label = `${state.ip}:${state.port}`;

  async function readOnce() {
    const data = await getJson(agent, state.ip, state.port, '/view/text.json', state.timeoutMs);
    const map = (data && data.map) || {};
    return { raw: typeof map.text === 'string' ? map.text : '', type: map.type || '' };
  }

  async function readViaApi() {
    if (!state.apiToken) return null;
    const port = state.apiPort || 8091;
    const path = `/api/GetCPInfo?token=${encodeURIComponent(state.apiToken)}`;
    const data = await getJson(agent, state.ip, port, path, state.timeoutMs, 'POST', '{}');
    if (!data || data.status !== 'ok') return null;
    const text = (data.data && data.data.text) || '';
    return { raw: text, type: 'API' };
  }

  async function tick() {
    if (stopped) return;
    const started = Date.now();

    try {
      let reading = null;
      try {
        reading = await readOnce();
      } catch (err) {
        reading = await readViaApi();
        if (!reading) throw err;
      }

      if (failures > 0) console.log(`[holyrics] reconectado a ${label}`);
      failures = 0;

      const type = String(reading.type || '');
      const muted = state.skipTypes.includes(type.toLowerCase());
      const signature = `${type}|${reading.raw}`;

      if (signature !== lastSignature) {
        lastSignature = signature;
        const patch = buildPatch(muted ? '' : reading.raw, type, state.format);
        onChange(patch);
      }
    } catch (err) {
      failures += 1;
      if (failures === 1 || failures % 40 === 0) {
        console.warn(`[holyrics] sin respuesta de ${label}: ${err.message}`);
      }
      if (failures === 1) {
        // No dejamos el ultimo texto como si estuviera al aire (criterio de HolyBridge).
        lastSignature = null;
        onChange({ text: '', progress: null });
      }
    }

    // Cadencia fija con correccion de deriva: el costo del request no suma.
    const elapsed = Date.now() - started;
    const wait = failures
      ? Math.min(2000, state.intervalMs * 5 * failures)
      : Math.max(0, state.intervalMs - elapsed);
    setTimeout(tick, wait);
  }

  console.log(`[holyrics] sondeando http://${label}/view/text.json cada ${state.intervalMs}ms`);
  tick();

  return {
    stop() {
      stopped = true;
      agent.destroy();
    },
    status() {
      return { label, lastSignature, failures, ...state };
    },
  };
}

module.exports = { startHolyricsSource, buildPatch, htmlToText, stripVersion, decodeEntities };
