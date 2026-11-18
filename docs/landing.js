'use strict';

/* Landing de HolyBridge Pro: animaciones reales, sin dependencias. */

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

const TYPEFACES = [
  { label: 'Inter (moderna)', css: '"Inter", "Segoe UI", system-ui, sans-serif', weight: 800 },
  { label: 'Georgia (serif)', css: 'Georgia, "Times New Roman", serif', weight: 700 },
  { label: 'Playfair (elegante)', css: '"Playfair Display", Georgia, serif', weight: 800 },
  { label: 'Pacifico (cursiva)', css: '"Pacifico", "Segoe Script", cursive', weight: 400 },
  { label: 'Dancing Script (fina)', css: '"Dancing Script", "Segoe Script", cursive', weight: 700 },
  { label: 'Bebas Neue (condensada)', css: '"Bebas Neue", Impact, sans-serif', weight: 400 },
  { label: 'Trebuchet (redondeada)', css: '"Trebuchet MS", "Segoe UI", sans-serif', weight: 700 },
  { label: 'Courier (mono)', css: '"Courier New", ui-monospace, monospace', weight: 700 },
];

const HIGHLIGHTS = [
  { mode: 'color', label: 'Color' },
  { mode: 'font', label: 'Cambio de tipografía' },
  { mode: 'italic', label: 'Cursiva + color' },
  { mode: 'underline', label: 'Subrayado' },
  { mode: 'box', label: 'Caja de color' },
  { mode: 'glow', label: 'Brillo' },
  { mode: 'none', label: 'Sin resaltado' },
];

const WORDS_A = ['Santo', 'Santo', 'Santo'];
const PHRASES = [
  ['Eres', 'la', 'luz', 'del', 'mundo'],
  ['Santo,', 'Santo,', 'Santo'],
  ['Tu', 'gracia', 'me', 'alcanzó'],
  ['Aleluya,', 'aleluya'],
  ['Jesús', 'vive', 'en', 'mí'],
];

const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

/* --------------------------------------------------------------- palabras */

function words(list, step = 70) {
  return list.map((word, i) => {
    const span = document.createElement('span');
    span.className = 'w';
    span.textContent = word;
    span.style.setProperty('--d', `${i * step}ms`);
    return span;
  });
}

function fill(el, list) {
  el.replaceChildren(...words(list));
}

function replay(el) {
  el.classList.remove('run');
  void el.offsetWidth; /* reinicia las animaciones */
  el.classList.add('run');
}

/* ------------------------------------------------------------ animaciones */

function buildFxGrid() {
  const grid = document.getElementById('fxGrid');
  if (!grid) return;

  const cards = EFFECTS.map((fx) => {
    const card = document.createElement('button');
    card.type = 'button';
    card.className = 'fxcard';
    card.setAttribute('aria-label', `Ver la animación ${fx.label}`);

    const box = document.createElement('div');
    box.className = 'stagebox';
    const sample = document.createElement('div');
    sample.className = `sample fx-${fx.id}`;
    sample.append(...words(['Aleluya', 'eterna'], 90));
    box.append(sample);

    const name = document.createElement('div');
    name.className = 'name';
    const strong = document.createElement('strong');
    strong.textContent = fx.label;
    const code = document.createElement('code');
    code.textContent = fx.id;
    name.append(strong, code);

    card.append(box, name);
    card.addEventListener('click', () => replay(sample));
    card.addEventListener('mouseenter', () => replay(sample));
    return { card, sample };
  });

  grid.append(...cards.map((c) => c.card));

  if (reduced) {
    cards.forEach(({ sample }) => sample.classList.add('run'));
    return;
  }

  /* Al entrar en pantalla se anima; después cada tanto, para que la
     sección esté viva mientras se lee. */
  const visible = new Set();
  const observer = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      const sample = entry.target;
      if (entry.isIntersecting) {
        visible.add(sample);
        replay(sample);
      } else {
        visible.delete(sample);
      }
    });
  }, { threshold: 0.35 });

  cards.forEach(({ sample }) => observer.observe(sample));

  let tick = 0;
  setInterval(() => {
    if (!visible.size) return;
    tick += 1;
    const list = [...visible];
    const sample = list[tick % list.length];
    replay(sample);
  }, 2400);
}

/* ----------------------------------------------------------- tipografías */

function buildTypeGrid() {
  const grid = document.getElementById('typeGrid');
  if (!grid) return;

  grid.append(...TYPEFACES.map((face) => {
    const card = document.createElement('div');
    card.className = 'typecard';

    const sample = document.createElement('p');
    sample.className = 'sample';
    sample.textContent = 'Aleluya';
    sample.style.fontFamily = face.css;
    sample.style.fontWeight = String(face.weight);

    const label = document.createElement('span');
    label.className = 'label';
    label.textContent = face.label;

    card.append(sample, label);
    return card;
  }));
}

/* ------------------------------------------------------------ resaltados */

function buildHighlights() {
  const grid = document.getElementById('hlGrid');
  if (!grid) return;

  grid.append(...HIGHLIGHTS.map(({ mode, label }) => {
    const card = document.createElement('div');
    card.className = 'hlcard';
    card.dataset.mode = mode;

    const sentence = document.createElement('div');
    sentence.className = 'sentence';
    const hl = document.createElement('span');
    hl.className = 'hl';
    hl.textContent = 'gracia';
    sentence.append('Tu ', hl, ' me alcanzó');

    const tag = document.createElement('span');
    tag.className = 'label';
    tag.textContent = label;

    card.append(sentence, tag);
    return card;
  }));
}

/* ----------------------------------------------------------- hero en vivo */

function heroLoop() {
  const a = document.getElementById('heroA');
  const b = document.getElementById('heroB');
  if (!a || !b) return;

  const fx = ['fx-pop', 'fx-rise', 'fx-blur', 'fx-wave', 'fx-bounce', 'fx-punch', 'fx-flip', 'fx-drift'];
  let index = 0;

  const paint = () => {
    const phrase = PHRASES[index % PHRASES.length];
    fill(a, phrase);
    fill(b, phrase);

    /* Mismo efecto en las dos salidas, alternando para mostrar variedad. */
    a.className = `demo ${fx[index % fx.length]}`;
    b.className = a.className;
    replay(a);
    replay(b);

    index += 1;
  };

  paint();
  if (reduced) return;
  setInterval(paint, 3200);
}

/* -------------------------------------------------------------- reveal */

function reveals() {
  const items = document.querySelectorAll('.reveal');
  if (!items.length) return;

  if (reduced) {
    items.forEach((el) => el.classList.add('in'));
    return;
  }

  const observer = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      if (entry.isIntersecting) {
        entry.target.classList.add('in');
        observer.unobserve(entry.target);
      }
    });
  }, { threshold: 0.12, rootMargin: '0px 0px -40px 0px' });

  items.forEach((el) => observer.observe(el));
}

buildFxGrid();
buildTypeGrid();
buildHighlights();
heroLoop();
reveals();
