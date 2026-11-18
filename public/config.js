'use strict';

/**
 * Panel de configuración del efecto.
 *
 * - Cada tarjeta renderiza la animación REAL (mismas clases y CSS que el overlay).
 * - La preview usa el texto que llega de Holyrics (SSE), no una frase fija.
 * - El link se reconstruye con todas las opciones elegidas.
 */

const EFFECTS = [
  { id: 'pop', label: 'Pop' },
  { id: 'fade', label: 'Fundido' },
  { id: 'rise', label: 'Subida' },
  { id: 'drop', label: 'Caída' },
  { id: 'slide', label: 'Deslizar' },
  { id: 'blur', label: 'Desenfoque' },
  { id: 'flip', label: 'Volteo 3D' },
  { id: 'glitch', label: 'Glitch' },
  { id: 'wave', label: 'Ola' },
  { id: 'typewriter', label: 'Máquina' },
  { id: 'bounce', label: 'Rebote' },
  { id: 'spin', label: 'Giro' },
  { id: 'swing', label: 'Columpio' },
  { id: 'punch', label: 'Golpe' },
  { id: 'stretch', label: 'Estirado' },
  { id: 'drift', label: 'Deriva' },
  { id: 'reveal', label: 'Cortina' },
  { id: 'none', label: 'Sin animación' },
];

const FONTS = {
  inter: { label: 'Inter (moderna)', css: '"Inter", "Segoe UI", system-ui, sans-serif' },
  georgia: { label: 'Georgia (serif)', css: 'Georgia, "Times New Roman", serif' },
  playfair: { label: 'Playfair (elegante)', css: '"Playfair Display", Georgia, serif' },
  pacifico: { label: 'Pacifico (cursiva)', css: '"Pacifico", "Segoe Script", cursive' },
  dancing: { label: 'Dancing Script (cursiva fina)', css: '"Dancing Script", "Segoe Script", cursive' },
  bebas: { label: 'Bebas Neue (condensada)', css: '"Bebas Neue", Impact, sans-serif' },
  trebuchet: { label: 'Trebuchet (redondeada)', css: '"Trebuchet MS", "Segoe UI", sans-serif' },
  courier: { label: 'Courier (mono)', css: '"Courier New", ui-monospace, monospace' },
};

const SIZES = [
  { value: '', label: 'Auto (llenar el cuadro)' },
  { value: '70', label: '70 px · chico' },
  { value: '95', label: '95 px · medio' },
  { value: '120', label: '120 px · grande' },
  { value: '150', label: '150 px · muy grande' },
  { value: '190', label: '190 px · máximo' },
];

const HL_MODES = {
  color: 'Color',
  font: 'Cambio de tipografía',
  italic: 'Cursiva + color',
  underline: 'Subrayado',
  box: 'Caja de color',
  glow: 'Brillo',
  none: 'Sin resaltado',
};

const CASES = ['asis', 'upper', 'lower', 'camel'];
const DEMO = 'Eres *la* luz';

const STAGGER = 70;
const LOOP_MS = 2800;

const grid = document.getElementById('grid');
const urlInput = document.getElementById('url');
const copyBtn = document.getElementById('copy');
const fontSelect = document.getElementById('font');
const sizeSelect = document.getElementById('size');
const caseSelect = document.getElementById('case');
const hlModeSelect = document.getElementById('hlmode');
const hlFontSelect = document.getElementById('hlfont');
const hlInput = document.getElementById('hlcolor');
const wordsInput = document.getElementById('words');
const fileInput = document.getElementById('fontfile');
const fontStatus = document.getElementById('fontstatus');
const liveLabel = document.getElementById('live');

const params = new URLSearchParams(location.search);
const stored = (key, fallback) => localStorage.getItem(`kinetic-${key}`) || fallback;

const state = {
  effect: params.get('effect') || stored('effect', 'pop'),
  font: params.get('font') || stored('font', 'inter'),
  size: params.get('size') !== null ? params.get('size') : stored('size', ''),
  case: params.get('case') || stored('case', 'asis'),
  hlmode: params.get('hlmode') || stored('hlmode', 'color'),
  hlfont: params.get('hlfont') || stored('hlfont', 'pacifico'),
  highlight: params.get('highlight') || stored('highlight', '#ffd23f'),
  words: params.get('hl') || stored('words', ''),
  text: DEMO,
};

if (!EFFECTS.some((e) => e.id === state.effect)) state.effect = 'pop';
if (!FONTS[state.font]) state.font = 'inter';
if (!FONTS[state.hlfont]) state.hlfont = 'pacifico';
if (!HL_MODES[state.hlmode]) state.hlmode = 'color';
if (!CASES.includes(state.case)) state.case = 'asis';

function normWord(word) {
  return String(word)
    .toLowerCase()
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '')
    .replace(/[^\p{L}\p{N}]/gu, '');
}

function wordSet() {
  return new Set(String(state.words || '').split(',').map(normWord).filter(Boolean));
}

function applyCaseWord(word) {
  if (state.case === 'upper') return word.toUpperCase();
  if (state.case === 'lower') return word.toLowerCase();
  if (state.case === 'camel') return word.charAt(0).toUpperCase() + word.slice(1).toLowerCase();
  return word;
}

/* ------------------------------------------------------------------ preview */

/** Texto de la preview: el que llega de Holyrics, o una frase de muestra. */
function previewText() {
  const limpio = String(state.text || '').replace(/\r/g, '').trim();
  if (!limpio) return [{ text: DEMO }];
  return limpio.split('\n').slice(0, 3);
}

function fillPreview(textEl, effect) {
  const resaltar = wordSet();
  const lineas = previewText();

  textEl.classList.remove('uppercase');
  textEl.dataset.hlmode = state.hlmode;

  const frag = document.createDocumentFragment();
  let index = 0;

  for (const linea of lineas) {
    const line = document.createElement('span');
    line.className = 'line';

    const palabras = linea.split(/\s+/).filter(Boolean);
    if (!palabras.length) {
      line.innerHTML = '&nbsp;';
      frag.appendChild(line);
      continue;
    }

    for (const raw of palabras) {
      const marcada = /^\*.+\*$/.test(raw);
      const limpio = raw.replace(/\*/g, '');
      const hl = marcada || resaltar.has(normWord(limpio));

      const wordEl = document.createElement('span');
      wordEl.className = hl ? 'w hl' : 'w';
      const texto = applyCaseWord(limpio);

      if (effect === 'typewriter') {
        for (const ch of texto) {
          const charEl = document.createElement('span');
          charEl.className = 'c';
          charEl.textContent = ch;
          charEl.style.animationDelay = `${index * 55}ms`;
          wordEl.appendChild(charEl);
          index += 1;
        }
      } else {
        wordEl.textContent = texto;
        wordEl.style.animationDelay = `${index * STAGGER}ms`;
        index += 1;
      }
      line.appendChild(wordEl);
    }
    frag.appendChild(line);
  }

  textEl.replaceChildren(frag);
}

/* ----------------------------------------------------------------- tarjetas */

const cards = new Map();

for (const effect of EFFECTS) {
  const card = document.createElement('button');
  card.type = 'button';
  card.className = 'card';
  card.dataset.effect = effect.id;
  card.setAttribute('role', 'radio');
  card.setAttribute('aria-checked', 'false');
  card.setAttribute('aria-label', effect.label);

  const stage = document.createElement('div');
  stage.className = 'stage';

  const text = document.createElement('div');
  text.className = `text fx-${effect.id}`;
  fillPreview(text, effect.id);

  stage.appendChild(text);

  const name = document.createElement('div');
  name.className = 'name';
  const strong = document.createElement('strong');
  strong.textContent = effect.label;
  const code = document.createElement('code');
  code.textContent = effect.id;
  name.append(strong, code);

  card.append(stage, name);
  card.addEventListener('click', () => select({ effect: effect.id }));

  grid.appendChild(card);
  cards.set(effect.id, { card, text });
}

/* ------------------------------------------------------------------ fuentes */

function fontOptions() {
  fontSelect.replaceChildren();
  hlFontSelect.replaceChildren();

  for (const [key, font] of Object.entries(FONTS)) {
    for (const select of [fontSelect, hlFontSelect]) {
      const option = document.createElement('option');
      option.value = key;
      option.textContent = font.label;
      select.appendChild(option);
    }
  }
  for (const size of SIZES) {
    const option = document.createElement('option');
    option.value = size.value;
    option.textContent = size.label;
    sizeSelect.appendChild(option);
  }
}

async function loadInstalledFonts() {
  try {
    const res = await fetch('/api/fonts');
    if (!res.ok) return;
    const list = await res.json();
    if (!Array.isArray(list) || !list.length) return;

    const style = document.createElement('style');
    style.textContent = list
      .map((f) => `@font-face{font-family:"${f.family}";src:url("/fonts/${encodeURIComponent(f.file)}");font-display:swap}`)
      .join('\n');
    document.head.appendChild(style);

    for (const f of list) {
      if (!FONTS[f.key]) FONTS[f.key] = { label: `${f.family} (instalada)`, css: `"${f.family}", sans-serif` };
    }
    fontOptions();
    applyVisualState();
  } catch {
    /* sin fuentes extra */
  }
}

/* --------------------------------------------------------------- selección  */

function buildUrl() {
  const query = new URLSearchParams();
  query.set('effect', state.effect);
  if (state.font !== 'inter') query.set('font', state.font);
  if (state.size) query.set('size', state.size);
  if (state.case !== 'asis') query.set('case', state.case);
  if (state.hlmode !== 'color') query.set('hlmode', state.hlmode);
  if (state.hlmode === 'font' && state.hlfont !== 'pacifico') query.set('hlfont', state.hlfont);
  if (state.highlight && state.highlight.toLowerCase() !== '#ffd23f') query.set('highlight', state.highlight);
  if (state.words) query.set('hl', state.words);
  // Si el panel se abrió por el proxy de la GUI (/config), el overlay vive en
  // /overlay del mismo origen; en el bridge directo (7333) vive en la raíz.
  const rutaOverlay = location.pathname.startsWith('/config') ? '/overlay' : '/';
  return `${location.origin}${rutaOverlay}?${query.toString()}`;
}

function rerenderPreviews() {
  for (const [id, entry] of cards) {
    entry.text.className = `text fx-${id} hlm-${state.hlmode}`;
    entry.text.style.fontFamily = FONTS[state.font].css;
    const size = Number(state.size) || 0;
    entry.text.style.fontSize = size ? `${Math.min(30, Math.max(16, size * 0.2))}px` : '';
    fillPreview(entry.text, id);
  }
}

function applyVisualState() {
  for (const [id, entry] of cards) {
    entry.card.setAttribute('aria-checked', id === state.effect ? 'true' : 'false');
  }
  document.documentElement.style.setProperty('--fx-highlight', state.highlight);
  document.documentElement.style.setProperty('--fx-hl-family', FONTS[state.hlfont].css);

  urlInput.value = buildUrl();
  fontSelect.value = state.font;
  sizeSelect.value = state.size;
  caseSelect.value = state.case;
  hlModeSelect.value = state.hlmode;
  hlFontSelect.value = state.hlfont;
  hlInput.value = state.highlight;
  wordsInput.value = state.words;

  localStorage.setItem('kinetic-effect', state.effect);
  localStorage.setItem('kinetic-font', state.font);
  localStorage.setItem('kinetic-size', state.size);
  localStorage.setItem('kinetic-case', state.case);
  localStorage.setItem('kinetic-hlmode', state.hlmode);
  localStorage.setItem('kinetic-hlfont', state.hlfont);
  localStorage.setItem('kinetic-highlight', state.highlight);
  localStorage.setItem('kinetic-words', state.words);
}

/* Publica la config en el bridge para que el emisor NDI use lo mismo. */
let publishTimer = 0;
function publishPanel() {
  clearTimeout(publishTimer);
  publishTimer = setTimeout(() => {
    fetch('/api/panel', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        effect: state.effect,
        font: state.font,
        size: state.size,
        case: state.case,
        hlmode: state.hlmode,
        hlfont: state.hlfont,
        highlight: state.highlight,
        words: state.words,
      }),
    }).catch(() => {});
  }, 150);
}

function select(patch) {
  Object.assign(state, patch);
  applyVisualState();
  rerenderPreviews();
  publishPanel();

  const query = new URLSearchParams({ effect: state.effect });
  if (state.case !== 'asis') query.set('case', state.case);
  if (state.hlmode !== 'color') query.set('hlmode', state.hlmode);
  if (state.words) query.set('hl', state.words);
  history.replaceState(null, '', `?${query.toString()}`);
}

fontSelect.addEventListener('change', () => select({ font: fontSelect.value }));
sizeSelect.addEventListener('change', () => select({ size: sizeSelect.value }));
caseSelect.addEventListener('change', () => select({ case: caseSelect.value }));
hlModeSelect.addEventListener('change', () => select({ hlmode: hlModeSelect.value }));
hlFontSelect.addEventListener('change', () => select({ hlfont: hlFontSelect.value }));
hlInput.addEventListener('input', () => select({ highlight: hlInput.value }));
wordsInput.addEventListener('input', () => select({ words: wordsInput.value.trim() }));

/* ------------------------------------------------------- instalar fuentes  */

fileInput.addEventListener('change', async () => {
  const file = fileInput.files && fileInput.files[0];
  if (!file) return;
  fontStatus.textContent = 'Subiendo…';
  try {
    const res = await fetch(`/api/fonts?name=${encodeURIComponent(file.name)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/octet-stream' },
      body: file,
    });
    const data = await res.json();
    if (!res.ok || !data.ok) throw new Error(data.error || 'No se pudo instalar');
    fontStatus.textContent = `Instalada: ${data.installed}`;
    await loadInstalledFonts();
  } catch (err) {
    fontStatus.textContent = `Error: ${err.message}`;
  } finally {
    fileInput.value = '';
  }
});

/* ------------------------------------------------------------ preview viva */

let ultimoTexto = '';
const source = new EventSource('/api/events');
source.addEventListener('text', (event) => {
  let payload;
  try {
    payload = JSON.parse(event.data);
  } catch {
    return;
  }
  const texto = typeof payload.text === 'string' ? payload.text : '';
  if (texto === ultimoTexto) return;
  ultimoTexto = texto;
  state.text = texto || DEMO;
  liveLabel.textContent = texto ? '· en vivo con Holyrics' : '· muestra (Holyrics sin texto)';
  rerenderPreviews();
});

/* ------------------------------------------------------------------ arranque */

fontOptions();
applyVisualState();
rerenderPreviews();
loadInstalledFonts();
publishPanel();

urlInput.addEventListener('focus', () => urlInput.select());

copyBtn.addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText(urlInput.value);
  } catch {
    urlInput.select();
    document.execCommand('copy');
  }
  copyBtn.textContent = 'Copiado';
  copyBtn.classList.add('done');
  setTimeout(() => {
    copyBtn.textContent = 'Copiar';
    copyBtn.classList.remove('done');
  }, 1400);
});

// Las previews se repiten para que el efecto se entienda de un vistazo.
setInterval(() => {
  for (const [id, entry] of cards) fillPreview(entry.text, id);
}, LOOP_MS);
