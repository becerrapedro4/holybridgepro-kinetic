'use strict';

/**
 * Kinetic Lyrics — motor de render del overlay.
 *
 * Latencia: SSE => render en el mismo frame (sin debounce, sin espera).
 * Coste: se tokeniza solo cuando el texto cambia; las animaciones son CSS
 * (compositor GPU).
 *
 * Sintaxis del texto: *palabra* o *varias palabras* quedan resaltadas.
 */

const els = {
  stage: document.getElementById('stage'),
  text: document.getElementById('text'),
};

/**
 * Lienzo fijo (por defecto 1920×1080, el mismo del NDI).
 *
 * El escenario se dibuja siempre a ese tamaño y se escala con transform para
 * entrar en la ventana: así la composición y el tamaño de letra no dependen de
 * cuánto mida el navegador (OBS 1080p, celular o el panel embebido) y queda
 * idéntica al NDI. Sin parámetros, se comporta como antes (llena la ventana).
 */
const CANVAS = (() => {
  const p = new URLSearchParams(location.search);
  const w = Math.round(Number(p.get('w')));
  const h = Math.round(Number(p.get('h')));
  return w > 0 && h > 0 ? { w, h } : null;
})();

function escalarLienzo() {
  if (!CANVAS) return;
  const k = Math.min(window.innerWidth / CANVAS.w, window.innerHeight / CANVAS.h) || 1;
  els.stage.style.transform = `translate(-50%, -50%) scale(${k})`;
}

function aplicarLienzo() {
  if (!CANVAS) return;
  els.stage.classList.add('canvas-fijo');
  els.stage.style.width = `${CANVAS.w}px`;
  els.stage.style.height = `${CANVAS.h}px`;
  // Márgenes en px del lienzo (un % acá se resolvería contra la ventana, no
  // contra el lienzo, porque el escenario está posicionado en absoluto).
  els.stage.style.padding = `${Math.round(CANVAS.h * 0.05)}px ${Math.round(CANVAS.w * 0.06)}px`;
  escalarLienzo();
}

function rerender() {
  if (!view.text) return;
  const { fragment, wordNodes } = tokenize(view.text);
  els.text.replaceChildren(fragment);
  view.words = wordNodes;
  view.wordCount = wordNodes.length;
  fit();
  applyProgress(view.progress);
}

const EFFECTS = [
  'pop', 'fade', 'rise', 'drop', 'slide', 'blur', 'flip', 'glitch', 'wave', 'typewriter',
  'bounce', 'spin', 'swing', 'punch', 'stretch', 'drift', 'reveal', 'none',
];

/** Tipografías disponibles (clave -> familia CSS). Las cursivas caen a fuentes
 *  del sistema si no hay internet, así nunca queda una letra "rota". */
const FONTS = {
  inter: '"Inter", "Segoe UI", system-ui, -apple-system, sans-serif',
  georgia: 'Georgia, "Times New Roman", serif',
  playfair: '"Playfair Display", Georgia, "Times New Roman", serif',
  pacifico: '"Pacifico", "Segoe Script", "Brush Script MT", cursive',
  dancing: '"Dancing Script", "Segoe Script", "Brush Script MT", cursive',
  bebas: '"Bebas Neue", Impact, "Arial Narrow", sans-serif',
  trebuchet: '"Trebuchet MS", "Segoe UI", sans-serif',
  courier: '"Courier New", ui-monospace, monospace',
};

const params = new URLSearchParams(location.search);

const view = {
  effect: 'pop',
  style: {},
  text: '',
  title: '',
  progress: null,
  revision: -1,
  words: [],
  wordCount: 0,
  caseMode: 'asis',
  hlMode: 'color',
  lastHlMode: 'color',
};

const HL_MODES = ['color', 'font', 'italic', 'underline', 'box', 'glow', 'none'];

/* ------------------------------------------------------------ configuración */

const OVERRIDES = {
  effect: params.get('effect'),
  stagger: num(params.get('stagger')),
  speed: num(params.get('speed')),
  size: num(params.get('size')),
  fill: num(params.get('fill')),
  weight: num(params.get('weight')),
  color: params.get('color'),
  highlight: params.get('highlight') || params.get('hi'),
  font: params.get('font'),
  family: params.get('family'),
  align: params.get('align'),
  uppercase: params.has('uppercase') ? params.get('uppercase') !== '0' : null,
  outline: num(params.get('outline')),
  outlineColor: params.get('outlineColor'),
  maxWidth: params.get('maxWidth'),
  lineHeight: num(params.get('lineHeight')),
  hl: params.get('hl'),
  case: params.get('case'),
  hlmode: params.get('hlmode'),
  hlfont: params.get('hlfont'),
};

/** Palabras a resaltar siempre (vienen del panel como ?hl=paz,cristo). */
let HL_WORDS = new Set();

/** Mayúsculas / minúsculas / Camel Case / tal cual. */
function applyCaseWord(word, mode) {
  if (mode === 'upper') return word.toUpperCase();
  if (mode === 'lower') return word.toLowerCase();
  if (mode === 'camel') {
    return word.charAt(0).toUpperCase() + word.slice(1).toLowerCase();
  }
  return word;
}

function normWord(word) {
  return String(word)
    .toLowerCase()
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '')
    .replace(/[^\p{L}\p{N}]/gu, '');
}

function setHighlightWords(raw) {
  const lista = String(raw || '')
    .split(',')
    .map((w) => normWord(w))
    .filter(Boolean);
  HL_WORDS = new Set(lista);
}

function num(value) {
  if (value === null || value === '' || value === undefined) return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

/* ------------------------------------------------------------------ estilos */

function applyStyle(style) {
  const merged = { ...OVERRIDES };
  for (const [key, value] of Object.entries(style || {})) {
    if (merged[key] === null || merged[key] === undefined) merged[key] = value;
  }

  if (!EFFECTS.includes(merged.effect)) merged.effect = 'pop';
  merged.effect = merged.effect || 'pop';

  const root = document.documentElement.style;

  if (merged.speed != null) root.setProperty('--fx-speed', `${Math.min(4000, Math.max(80, merged.speed))}ms`);
  if (merged.stagger != null) root.setProperty('--fx-stagger', `${Math.min(400, Math.max(0, merged.stagger))}ms`);
  if (merged.fill != null) root.setProperty('--fx-fill', String(Math.min(0.9, Math.max(0.05, merged.fill))));
  if (merged.color) root.setProperty('--fx-color', merged.color);
  if (merged.highlight) root.setProperty('--fx-highlight', merged.highlight);
  if (merged.weight != null) root.setProperty('--fx-weight', String(merged.weight));
  if (merged.align) root.setProperty('--fx-align', merged.align);
  if (merged.outline != null) root.setProperty('--fx-outline', `${merged.outline}px`);
  if (merged.outlineColor) root.setProperty('--fx-outline-color', merged.outlineColor);
  if (merged.lineHeight != null) root.setProperty('--fx-lh', String(merged.lineHeight));
  if (merged.maxWidth) root.setProperty('--fx-maxw', merged.maxWidth);
  if (typeof merged.uppercase === 'boolean') els.text.classList.toggle('uppercase', merged.uppercase);

  // Tipografía: por clave (font=pacifico) o por familia CSS directa.
  const familia = FONTS[merged.font] || merged.family || FONTS.inter;
  root.setProperty('--fx-family', familia);

  if (merged.hl !== null && merged.hl !== undefined) setHighlightWords(merged.hl);

  // Modo de resaltado (color, tipografía, cursiva, subrayado, caja, brillo).
  const modo = HL_MODES.includes(merged.hlmode) ? merged.hlmode : 'color';
  if (modo !== view.hlMode) {
    els.text.classList.remove(`hlm-${view.hlMode}`);
    els.text.classList.add(`hlm-${modo}`);
    view.hlMode = modo;
  }
  const familiaHl = FONTS[merged.hlfont] || merged.hlfontFamily;
  if (familiaHl) root.setProperty('--fx-hl-family', familiaHl);

  const nuevoCase = ['upper', 'lower', 'camel'].includes(merged.case) ? merged.case : 'asis';
  const cambioTexto = nuevoCase !== view.caseMode;
  view.caseMode = nuevoCase;

  if (merged.effect !== view.effect) {
    els.text.classList.remove(`fx-${view.effect}`);
    els.text.classList.add(`fx-${merged.effect}`);
    view.effect = merged.effect;
  }
  view.style = merged;

  // Cambió el formato: hay que re-tokenizar el texto que ya está en pantalla.
  if (cambioTexto || modo !== view.lastHlMode) {
    view.lastHlMode = modo;
    rerender();
  }
}

/* --------------------------------------------------------------- tokenizado */

/**
 * Convierte una línea en palabras, marcando las que van entre *asteriscos*.
 * Soporta frases resaltadas de varias palabras: "*Cristo es mi paz*".
 */
function splitWords(lineText) {
  const out = [];
  const pushPlain = (txt) => {
    for (const word of txt.split(/\s+/)) {
      if (!word) continue;
      out.push({ text: word, hl: HL_WORDS.has(normWord(word)) });
    }
  };
  const pushHl = (txt) => {
    for (const word of txt.split(/\s+/)) if (word) out.push({ text: word, hl: true });
  };

  const re = /\*([^*]+)\*/g;
  let last = 0;
  let match;
  while ((match = re.exec(lineText))) {
    pushPlain(lineText.slice(last, match.index));
    pushHl(match[1]);
    last = match.index + match[0].length;
  }
  pushPlain(lineText.slice(last));
  return out;
}

function staggerMs() {
  const raw = getComputedStyle(document.documentElement).getPropertyValue('--fx-stagger');
  const n = parseFloat(raw);
  return Number.isFinite(n) ? n : 55;
}

/** Texto comparable: sin espacios de más ni saltos repetidos. */
function normText(text) {
  return String(text || '').replace(/\s+/g, ' ').trim();
}

/**
 * Convierte el texto en palabras.
 *
 * `reuse` son los <span> que ya están en pantalla: si la palabra coincide, se
 * mueve el nodo tal cual en vez de crear uno nuevo. Así, cuando Holyrics manda
 * primero el versículo y después agrega la cita, la frase no se vuelve a
 * animar desde cero: sólo entra lo que es nuevo.
 */
function tokenize(text, reuse) {
  const lines = String(text).replace(/\r\n?/g, '\n').split('\n');
  const wordNodes = [];
  const fragment = document.createDocumentFragment();
  const charMode = view.effect === 'typewriter';
  const stagger = staggerMs();
  let index = 0;
  let reuseAt = 0;

  for (const lineText of lines) {
    const line = document.createElement('span');
    line.className = 'line';
    // Con lienzo fijo y tamaño automático cada renglón va entero, como lo manda
    // Holyrics: el ancho se resuelve achicando la letra, no cortando la línea.
    if (CANVAS && !view.style.size) line.style.whiteSpace = 'nowrap';
    const words = splitWords(lineText);

    if (!words.length) {
      line.innerHTML = '&nbsp;';
      fragment.appendChild(line);
      continue;
    }

    for (const item of words) {
      const visible = applyCaseWord(item.text, view.caseMode);
      const previo = reuse && reuse[reuseAt];
      const pareceIgual = previo
        && normWord(previo.textContent) === normWord(visible)
        && previo.classList.contains('hl') === item.hl;

      let span;
      if (pareceIgual) {
        // Ya estaba en pantalla: se reutiliza el nodo (conserva su animación
        // terminada) y no consume un turno del escalonado.
        span = previo;
        reuseAt += 1;
        span.className = item.hl ? 'w hl' : 'w';
        span.dataset.i = String(wordNodes.length);
        wordNodes.push(span);
        line.appendChild(span);
        continue;
      }

      span = document.createElement('span');
      span.className = item.hl ? 'w hl' : 'w';

      if (charMode) {
        for (const ch of applyCaseWord(item.text, view.caseMode)) {
          const cspan = document.createElement('span');
          cspan.className = 'c';
          cspan.textContent = ch;
          cspan.style.animationDelay = `${index * 26}ms`;
          if (item.hl) cspan.style.color = 'var(--fx-highlight)';
          span.appendChild(cspan);
          index += 1;
        }
      } else {
        span.textContent = visible;
        span.style.animationDelay = `${index * stagger}ms`;
        index += 1;
      }

      span.dataset.i = String(wordNodes.length);
      wordNodes.push(span);
      line.appendChild(span);
    }

    fragment.appendChild(line);
  }

  return { fragment, wordNodes };
}

/* ------------------------------------------------------------------ render  */

function render(payload) {
  const nextText = typeof payload.text === 'string' ? payload.text : view.text;
  // Comparación tolerante a espacios: un cambio cosmético no re-anima nada.
  const textChanged = normText(nextText) !== normText(view.text);
  // Texto nuevo sin progreso explícito => se limpia el karaoke anterior.
  const progress = payload.progress === undefined
    ? (textChanged ? null : view.progress)
    : payload.progress;
  const progressChanged = progress !== view.progress;

  view.text = nextText;
  view.title = payload.title || view.title;
  view.progress = progress;

  if (!textChanged && !progressChanged) return;
  if (!textChanged) {
    applyProgress(progress);
    return;
  }

  const { fragment, wordNodes } = tokenize(nextText, view.words);
  els.text.replaceChildren(fragment);
  view.words = wordNodes;
  view.wordCount = wordNodes.length;

  fit();
  applyProgress(progress);
}

function applyProgress(progress) {
  if (progress == null || view.wordCount === 0) {
    if (progress == null && view.words.some((w) => w.classList.contains('on'))) {
      for (const w of view.words) w.classList.remove('on');
    }
    return;
  }
  const limit = Math.round(progress * view.wordCount);
  for (let i = 0; i < view.wordCount; i += 1) {
    const on = i < limit;
    const word = view.words[i];
    if (word.classList.contains('on') !== on) word.classList.toggle('on', on);
  }
}

/* --------------------------------------------------------------------- fit  */

/**
 * Ajusta el tamaño para llenar el cuadro. `size` (px) lo fija a mano;
 * `fill` es la fracción de alto que ocupa el texto cuando no hay size.
 */
function fitsAt(size, stageW, stageH) {
  els.text.style.setProperty('--fx-fs', `${size}px`);
  return els.text.scrollHeight <= stageH && els.text.scrollWidth <= stageW + 1;
}

/**
 * Ajusta el tamaño al cuadro.
 *
 * `size` (px) manda: sólo se achica si no entra.
 * Sin `size`, se busca el MAYOR tamaño que entre: así una tipografía con
 * métricas raras (o instalada a mano) no queda diminuta, que era el problema
 * con las fuentes del panel.
 */
function fit() {
  const explicit = view.style.size;
  // El relleno del escenario ya es el margen: descontarlo evita que el texto
  // se mida contra un área más grande de la que realmente tiene.
  const estilo = getComputedStyle(els.stage);
  const padW = (parseFloat(estilo.paddingLeft) || 0) + (parseFloat(estilo.paddingRight) || 0);
  const padH = (parseFloat(estilo.paddingTop) || 0) + (parseFloat(estilo.paddingBottom) || 0);
  const stageH = els.stage.clientHeight - padH;
  const stageW = els.stage.clientWidth - padW;
  if (stageH <= 0 || stageW <= 0) return;

  // Con lienzo fijo se respetan los renglones de Holyrics (sin cortes extra) y
  // el tamaño sale de una sola medición: el mayor que entre a lo ancho Y a lo
  // alto. Así el texto ocupa todo el ancho disponible y no salta de tamaño por
  // renglón dentro de la misma frase.
  if (!explicit && CANVAS) {
    const ref = Math.max(24, Math.round(CANVAS.h * 0.13));
    els.text.style.setProperty('--fx-fs', `${ref}px`);
    const alto = els.text.scrollHeight || 1;
    const ancho = els.text.scrollWidth || 1;
    let size = Math.floor(ref * Math.min(stageH / alto, stageW / (ancho + 1)));
    // Tope: ni una palabra suelta se vuelve gigante.
    size = Math.max(10, Math.min(size, Math.round(CANVAS.h * 0.6)));
    els.text.style.setProperty('--fx-fs', `${size}px`);
    for (let i = 0; i < 3; i += 1) {
      if ((els.text.scrollHeight <= stageH && els.text.scrollWidth <= stageW + 1) || size <= 10) break;
      size = Math.max(10, Math.round(size * 0.96));
      els.text.style.setProperty('--fx-fs', `${size}px`);
    }
    return;
  }

  // Tope de altura: llena el cuadro sin que una sola palabra quede gigante.
  const budgetH = explicit ? stageH : Math.round(stageH * 0.75);

  // Antes se hacía búsqueda binaria: ~11 reflows forzados por cambio de texto
  // (un tirón de hasta 100 ms justo cuando entra la frase). El alto y el ancho
  // escalan de forma casi lineal con el font-size, así que alcanza con medir
  // una vez a un tamaño de referencia y corregir en 2-3 pasadas.
  const ref = explicit ? Math.max(12, Number(explicit)) : 100;
  els.text.style.setProperty('--fx-fs', `${ref}px`);
  const alto = els.text.scrollHeight;
  const ancho = els.text.scrollWidth;
  if (!alto || !ancho) {
    els.text.style.setProperty('--fx-fs', `${ref}px`);
    return;
  }

  let size = Math.floor(ref * Math.min(budgetH / alto, stageW / (ancho + 1)));
  size = Math.max(12, explicit ? Math.min(Math.max(12, Number(explicit)), size) : size);
  els.text.style.setProperty('--fx-fs', `${size}px`);

  for (let i = 0; i < 4; i += 1) {
    const sobra = els.text.scrollHeight > budgetH || els.text.scrollWidth > stageW + 1;
    if (sobra) {
      size = Math.max(12, Math.round(size * 0.94));
      els.text.style.setProperty('--fx-fs', `${size}px`);
      continue;
    }
    if (explicit) break;
    // Si quedó holgura, un último empujón (el salto de línea cambia el alto).
    const holgura = 1 - els.text.scrollHeight / budgetH;
    if (holgura < 0.06) break;
    const mayor = Math.min(Math.round(size * 1.06), size + 40);
    if (mayor <= size) break;
    size = mayor;
    els.text.style.setProperty('--fx-fs', `${size}px`);
  }
}

/* ------------------------------------------------------------------- canal  */

let source = null;

function connect() {
  if (source) source.close();
  source = new EventSource('/api/events');

  source.addEventListener('text', (event) => {
    let payload;
    try {
      payload = JSON.parse(event.data);
    } catch {
      return;
    }
    if (typeof payload.revision === 'number') {
      if (payload.revision < view.revision) return;
      view.revision = payload.revision;
    }
    applyStyle(payload.style);
    render(payload);
  });

  source.addEventListener('error', () => {
    // EventSource reintenta solo (retry: 400): nunca parpadea a negro en vivo.
  });
}

/* ------------------------------------------------------------------ arranque */

/* Fuentes instaladas desde el panel (public/fonts). */
async function loadInstalledFonts() {
  try {
    const res = await fetch('/api/fonts');
    if (!res.ok) return;
    const list = await res.json();
    if (!Array.isArray(list) || !list.length) return;
    const style = document.createElement('style');
    style.id = 'fx-installed-fonts';
    style.textContent = list
      .map((f) => `@font-face{font-family:"${f.family}";src:url("/fonts/${encodeURIComponent(f.file)}");font-display:swap}`)
      .join('\n');
    document.head.appendChild(style);
    for (const f of list) {
      if (!FONTS[f.key]) FONTS[f.key] = `"${f.family}", sans-serif`;
    }
  } catch {
    /* sin fuentes extra */
  }
}

aplicarLienzo();
applyStyle({});
connect();
loadInstalledFonts().then(() => {
  applyStyle(view.style);
  rerender();
});

let fitTimer = 0;
const scheduleFit = () => {
  clearTimeout(fitTimer);
  fitTimer = setTimeout(fit, 60);
};
window.addEventListener('resize', () => {
  escalarLienzo();
  scheduleFit();
});

// En vMix/OBS el navegador interno cambia de tamaño después de cargar la página
// (y a veces sin disparar `resize`): sin esto el lienzo quedaba escalado a un
// tamaño viejo y el texto se veía chiquito en una esquina.
requestAnimationFrame(escalarLienzo);
setTimeout(escalarLienzo, 250);
setTimeout(escalarLienzo, 1200);
document.addEventListener('visibilitychange', escalarLienzo);
if (typeof ResizeObserver === 'function') {
  new ResizeObserver(scheduleFit).observe(els.stage);
  new ResizeObserver(() => {
    escalarLienzo();
    scheduleFit();
  }).observe(document.documentElement);
}
if (document.fonts && document.fonts.ready) {
  // Solo reajustar: re-tokenizar acá volvía a animar la frase ya visible.
  document.fonts.ready.then(fit);
}
