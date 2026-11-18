# -*- coding: utf-8 -*-
"""Kinetic Lyrics -> NDI.

Emite la letra animada por NDI (RGBA con alpha), con los MISMOS efectos,
tipografías y modos de resaltado que el overlay web.

La configuración sale del panel (`/config`): el panel la publica en
`POST /api/panel` y este proceso la lee cada 700 ms. El texto sale de
`/api/state` (lo que Holyrics está mostrando).

Requiere Python 3.9 (donde anda NDI):
    py -3.9 -m pip install -r ndi/requirements-3.9.txt

Uso:
    ndi\\run-ndi.bat                       (usa la config del panel)
    py -3.9 ndi/kinetic_ndi.py --preview f.png --text "*PAZ* de Dios"
    py -3.9 ndi/kinetic_ndi.py --effect bounce --size 150 --case upper
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
import threading
import time
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

try:  # NDI es opcional: sin el paquete se puede usar --preview
    import NDIlib as ndi
except Exception:  # pragma: no cover
    ndi = None

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

def _raiz_proyecto() -> Path:
    """Carpeta que contiene public/fonts.

    Suelto, el script vive en <raiz>/ndi/kinetic_ndi.py. Congelado con
    PyInstaller, __file__ apunta a la carpeta temporal del .exe, así que hay
    que mirar donde está el ejecutable (ndi/HolyBridgeNDI.exe -> <raiz>).
    """
    candidatas = []
    if getattr(sys, 'frozen', False):
        exe_dir = Path(sys.executable).resolve().parent
        candidatas += [exe_dir.parent, exe_dir]
    else:
        candidatas.append(Path(__file__).resolve().parent.parent)
    candidatas.append(Path.cwd())
    for base in candidatas:
        if (base / 'public' / 'fonts').is_dir():
            return base
    return candidatas[0]


RAIZ = _raiz_proyecto()
FUENTES_PROPIAS = RAIZ / 'public' / 'fonts'
FUENTES_WIN = Path('C:/Windows/Fonts')

# clave del panel -> archivos candidatos (el primero que exista gana)
FONT_FILES = {
    'inter': [FUENTES_PROPIAS / 'Inter.ttf', FUENTES_WIN / 'segoeuib.ttf', FUENTES_WIN / 'arialbd.ttf'],
    'georgia': [FUENTES_WIN / 'georgiab.ttf', FUENTES_WIN / 'georgia.ttf'],
    'playfair': [FUENTES_PROPIAS / 'PlayfairDisplay.ttf', FUENTES_WIN / 'georgiab.ttf'],
    'pacifico': [FUENTES_PROPIAS / 'Pacifico.ttf', FUENTES_WIN / 'segoesc.ttf'],
    'dancing': [FUENTES_PROPIAS / 'DancingScript.ttf', FUENTES_WIN / 'BRUSHSCI.TTF'],
    'bebas': [FUENTES_PROPIAS / 'BebasNeue.ttf', FUENTES_WIN / 'impact.ttf'],
    'trebuchet': [FUENTES_WIN / 'trebucbd.ttf', FUENTES_WIN / 'segoeuib.ttf'],
    'courier': [FUENTES_WIN / 'courbd.ttf', FUENTES_WIN / 'consola.ttf'],
}

EFFECTS = {
    'pop', 'fade', 'rise', 'drop', 'slide', 'blur', 'flip', 'glitch', 'wave', 'typewriter',
    'bounce', 'spin', 'swing', 'punch', 'stretch', 'drift', 'reveal', 'none',
}
HL_MODES = {'color', 'font', 'italic', 'underline', 'box', 'glow', 'none'}


# --------------------------------------------------------------------- helpers

def ease_out_cubic(p: float) -> float:
    return 1 - (1 - p) ** 3


def ease_out_back(p: float, s: float = 1.70158) -> float:
    p -= 1
    return 1 + p * p * ((s + 1) * p + s)


def clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def norm_word(word: str) -> str:
    import unicodedata

    base = unicodedata.normalize('NFD', word.lower())
    base = ''.join(c for c in base if unicodedata.category(c) != 'Mn')
    return ''.join(c for c in base if c.isalnum())


def apply_case(word: str, mode: str) -> str:
    if mode == 'upper':
        return word.upper()
    if mode == 'lower':
        return word.lower()
    if mode == 'camel':
        return word[:1].upper() + word[1:].lower()
    return word


def hex_rgba(valor: str, alpha: int = 255):
    valor = (valor or '#ffffff').strip().lstrip('#')
    if len(valor) == 3:
        valor = ''.join(c * 2 for c in valor)
    try:
        return (int(valor[0:2], 16), int(valor[2:4], 16), int(valor[4:6], 16), alpha)
    except Exception:
        return (255, 255, 255, alpha)


def _clave(texto: str) -> str:
    """ 'Peterfont-Regular.otf' y 'peterfontregular' tienen que coincidir."""
    return ''.join(c for c in texto.lower() if c.isalnum())


def buscar_fuente(key: str, extra: str | None = None) -> Path | None:
    """Resuelve la fuente elegida en el panel a un archivo del disco.

    Las fuentes instaladas desde el panel vienen con nombres que no coinciden
    literalmente con la clave ('peterfontregular' vs 'Peterfont-Regular.otf'),
    así que se comparan normalizadas. Antes caía siempre en Arial y por eso
    NDI no respetaba la tipografía elegida.
    """
    if extra:
        ruta = Path(extra)
        if ruta.exists():
            return ruta

    clave = _clave(key)

    # 1) fuentes del proyecto (descargadas o instaladas desde el panel)
    if FUENTES_PROPIAS.exists():
        archivos = sorted(f for f in FUENTES_PROPIAS.iterdir()
                          if f.suffix.lower() in ('.ttf', '.otf'))
        for f in archivos:
            if _clave(f.stem) == clave:
                return f
        for f in archivos:
            if _clave(f.stem).startswith(clave) or clave.startswith(_clave(f.stem)):
                return f

    # 2) mapa conocido -> fuentes del sistema
    for cand in FONT_FILES.get(key, []):
        if Path(cand).exists():
            return Path(cand)

    return None


# ------------------------------------------------------------------ animación

REST = (1.0, 1.0, 0.0, 0.0, 1.0, 0.0, 0.0, 1.0)
# (sx, sy, dx, dy, alpha, rot, blur, crop)


def effect_state(effect: str, p: float, em: float, i: int):
    if p >= 1 or effect == 'none':
        return REST
    a = clamp(p)
    e = ease_out_cubic(a)
    b = ease_out_back(a)

    if effect == 'pop':
        s = 0.55 + 0.45 * b
        return (s, s, 0, 0, clamp(a * 2.2), 0, 0, 1)
    if effect == 'fade':
        # Fundido puro: sólo opacidad, sin desplazamiento ni escala.
        return (1, 1, 0, 0, a, 0, 0, 1)
    if effect == 'rise':
        return (1, 1, 0, (1 - e) * em * 0.42, clamp(a * 1.8), 0, 0, 1)
    if effect == 'drop':
        return (1, 1, 0, -(1 - e) * em * 0.42, clamp(a * 1.8), 0, 0, 1)
    if effect == 'slide':
        return (1, 1, -(1 - e) * em * 0.5, 0, clamp(a * 1.8), (1 - a) * -9, 0, 1)
    if effect == 'blur':
        return (1 + 0.09 * (1 - a), 1 + 0.09 * (1 - a), 0, 0, clamp(a * 2), 0, 16 * (1 - a), 1)
    if effect == 'flip':
        return (1, max(0.02, math.cos((1 - a) * math.pi / 2)), 0, 0, a, 0, 0, 1)
    if effect == 'glitch':
        j = random.uniform(-0.14, 0.14) * em * (1 - a)
        return (1, 1, j, 0, clamp(a * 2), 0, 0, 1)
    if effect == 'wave':
        dy = (1 - e) * em * 0.5 - math.sin(a * math.pi) * em * 0.06
        return (1, 1, 0, dy, clamp(a * 1.6), (1 - a) * 5, 0, 1)
    if effect == 'typewriter':
        return (1, 1, 0, 0, 1, 0, 0, clamp(a * 1.05))
    if effect == 'bounce':
        s = 0.7 + 0.3 * ease_out_back(a, 2.4)
        return (s, s, 0, (1 - ease_out_back(a, 2.4)) * em * 0.6, clamp(a * 2), 0, 0, 1)
    if effect == 'spin':
        s = 0.2 + 0.8 * e
        return (s, s, 0, 0, clamp(a * 2.2), -210 * (1 - e), 0, 1)
    if effect == 'swing':
        return (1, 1, 0, 0, clamp(a * 2), -26 * (1 - ease_out_back(a, 2.0)), 0, 1)
    if effect == 'punch':
        s = 1 + 0.75 * (1 - e)
        return (s, s, 0, 0, clamp(a * 1.6), 0, 10 * (1 - a), 1)
    if effect == 'stretch':
        return (1.1 - 0.1 * e, 0.15 + 0.85 * b, 0, 0, clamp(a * 1.8), 0, 0, 1)
    if effect == 'drift':
        return (1, 1, -(1 - e) * em * 0.7, (1 - e) * em * 0.2, clamp(a * 1.6), (1 - a) * -9, 0, 1)
    if effect == 'reveal':
        return (1, 1, 0, 0, 1, 0, 0, clamp(a * 1.05))
    # por defecto
    return (1, 1, 0, 0, a, 0, 0, 1)


# ------------------------------------------------------------------- render NDI


class KineticNDI:
    def __init__(self, args):
        self.args = args
        self.width, self.height = args.width, args.height
        self.fps = args.fps
        self.texto = ''
        self.panel = {
            'effect': args.effect,
            'font': args.font_key,
            'size': str(args.size),
            'case': args.case,
            'hlmode': args.hlmode,
            'hlfont': args.hlfont,
            'highlight': args.hl_color,
            'words': args.hl,
        }
        self.words: list[dict] = []
        self.inicio = time.monotonic()
        self.lock = threading.Lock()
        self.sender = None
        self._tamano_usado = 0    # tamaño real después del ajuste al cuadro
        self._buf = None          # buffer RGBA reutilizado (evita un alloc por frame)
        self._vf = None           # estructura NDI reutilizada
        self._cache_fuentes: dict = {}
        self._firma = None

    # -- fuentes -----------------------------------------------------------
    def _fuente(self, clave: str, tamano: int):
        cache_key = (clave, tamano)
        if cache_key in self._cache_fuentes:
            return self._cache_fuentes[cache_key]
        ruta = buscar_fuente(clave, self.args.font if clave == self.panel['font'] else None)
        fuente = None
        if ruta:
            try:
                fuente = ImageFont.truetype(str(ruta), tamano)
            except Exception:
                fuente = None
        if fuente is None:
            print(f'⚠️ NDI: no encuentro la fuente "{clave}" (uso Arial). ' +
                  'Revisá que el archivo esté en public/fonts.', flush=True)
            for fb in ('C:/Windows/Fonts/arialbd.ttf', 'C:/Windows/Fonts/arial.ttf'):
                try:
                    fuente = ImageFont.truetype(fb, tamano)
                    break
                except Exception:
                    continue
        if fuente is None:
            fuente = ImageFont.load_default()
        self._cache_fuentes[cache_key] = fuente
        return fuente

    # -- configuración -----------------------------------------------------
    def set_panel(self, panel: dict):
        cambios = {k: v for k, v in panel.items() if self.panel.get(k) != v}
        if not cambios:
            return
        self.panel.update(cambios)
        if cambios.get('effect') and self.panel['effect'] not in EFFECTS:
            self.panel['effect'] = 'pop'
        if cambios.get('hlmode') and self.panel['hlmode'] not in HL_MODES:
            self.panel['hlmode'] = 'color'
        self._cache_fuentes.clear()
        with self.lock:
            self.words = self._preparar(self.texto)
            self.inicio = time.monotonic()
        print(f"🎛️  Panel: efecto={self.panel['effect']} fuente={self.panel['font']} "
              f"resaltado={self.panel['hlmode']} case={self.panel['case']}", flush=True)

    def set_texto(self, texto: str):
        if texto == self.texto:
            return
        with self.lock:
            self.texto = texto
            self.words = self._preparar(texto)
            self.inicio = time.monotonic()

    # -- preparación de palabras ------------------------------------------
    def _tamano(self) -> int:
        try:
            size = int(self.panel.get('size') or 0)
        except (TypeError, ValueError):
            size = 0
        return size if size > 0 else int(self.height * 0.13)

    def _preparar(self, texto: str) -> list[dict]:
        resaltar = {norm_word(w) for w in str(self.panel.get('words') or '').split(',') if w.strip()}
        modo = self.panel.get('hlmode') or 'color'
        case = self.panel.get('case') or 'asis'
        base_key = self.panel.get('font') or 'inter'
        hl_key = self.panel.get('hlfont') or 'pacifico'

        # 1) palabras tal cual llegan (cada renglón de Holyrics se respeta)
        grupos: list[list[dict]] = []
        for linea in texto.replace('\r', '').split('\n'):
            grupo = []
            for raw in linea.split():
                marcada = raw.startswith('*') and raw.endswith('*') and len(raw) > 2
                limpio = raw.strip('*')
                if not limpio:
                    continue
                hl = marcada or norm_word(limpio) in resaltar
                grupo.append({
                    'texto': apply_case(limpio, case),
                    'hl': hl,
                    'clave': hl_key if (hl and modo in ('font', 'italic')) else base_key,
                })
            if grupo:
                grupos.append(grupo)
        if not grupos:
            grupos = [[{'texto': ' ', 'hl': False, 'clave': base_key}]]

        # 2) tamaño: el pedido, achicado hasta que TODO entre en el cuadro.
        #    Antes el texto se salía del lienzo porque no se medía el ancho.
        ancho_util = int(self.width * 0.92)
        alto_util = int(self.height * 0.90)
        tamano = self._tamano()
        # Se respetan los renglones de Holyrics: si la frase no entra, se achica
        # la letra en vez de partirla en más líneas por cuenta propia.
        lineas = grupos
        for _ in range(60):
            if tamano <= 24:
                break
            alto = len(lineas) * tamano * 1.45
            ancho = max((self._ancho_linea(l, tamano) for l in lineas), default=0)
            if ancho <= ancho_util and alto <= alto_util:
                break
            tamano = max(24, int(tamano * 0.94))
        # Último recurso: un renglón larguísimo que ni al mínimo entra. Ahí sí se
        # permite cortar, para que nada se salga del cuadro.
        if max((self._ancho_linea(l, tamano) for l in lineas), default=0) > ancho_util:
            lineas = self._repartir(grupos, tamano, ancho_util)
            for _ in range(60):
                if tamano <= 20:
                    break
                alto = len(lineas) * tamano * 1.45
                ancho = max((self._ancho_linea(l, tamano) for l in lineas), default=0)
                if ancho <= ancho_util and alto <= alto_util:
                    break
                tamano = max(20, int(tamano * 0.94))
                lineas = self._repartir(grupos, tamano, ancho_util)
        self._tamano_usado = tamano

        # 3) imágenes y acomodo (cada renglón centrado, el bloque centrado)
        palabras: list[dict] = []
        for linea in lineas:
            for p in linea:
                img = self._render_palabra(p['texto'], p['hl'], tamano, modo)
                palabras.append({'texto': p['texto'], 'img': img,
                                 'ancho': img.width, 'alto': img.height})
            palabras.append({'nueva_linea': True})

        self._layout(palabras, tamano)

        # Cada palabra guarda CUÁNDO empieza su animación. Si la palabra ya
        # estaba en pantalla (Holyrics manda el versículo y un instante después
        # la cita), conserva su hora: sólo anima lo nuevo en vez de repetir
        # toda la entrada.
        ahora = time.monotonic()
        escalon = float(self.args.stagger) / 1000.0
        anteriores = getattr(self, 'words', None) or []
        vizuales = [p for p in palabras if not p.get('nueva_linea')]
        previas = [p for p in anteriores if not p.get('nueva_linea')]
        for i, p in enumerate(vizuales):
            prev = previas[i] if i < len(previas) else None
            igual = prev and prev.get('texto') == p['texto'] and prev.get('hl') == p.get('hl')
            p['t0'] = prev.get('t0', ahora + i * escalon) if igual else ahora + i * escalon
        return vizuales

    def _repartir(self, grupos: list[list[dict]], tamano: int, ancho_util: float) -> list[list[dict]]:
        """Parte cada renglón en líneas que entren a lo ancho."""
        espacio = tamano * 0.26
        pad = tamano * 0.16 * 2
        salida: list[list[dict]] = []
        for grupo in grupos:
            actual: list[dict] = []
            ancho = 0.0
            for p in grupo:
                w = self._fuente(p['clave'], tamano).getlength(p['texto']) + pad
                extra = w if not actual else w + espacio
                if actual and ancho + extra > ancho_util:
                    salida.append(actual)
                    actual, ancho = [p], w
                else:
                    actual.append(p)
                    ancho += extra
            if actual:
                salida.append(actual)
        return salida or [[]]

    def _ancho_linea(self, linea: list[dict], tamano: int) -> float:
        if not linea:
            return 0.0
        espacio = tamano * 0.26
        pad = tamano * 0.16 * 2
        return sum(self._fuente(p['clave'], tamano).getlength(p['texto']) + pad
                   for p in linea) + espacio * (len(linea) - 1)

    def _render_palabra(self, texto: str, hl: bool, tamano: int, modo: str) -> Image.Image:
        texto = texto or ' '
        base_key = self.panel.get('font') or 'inter'
        hl_key = self.panel.get('hlfont') or 'pacifico'
        color = hex_rgba(self.args.color)
        hl_color = hex_rgba(self.panel.get('highlight') or self.args.hl_color)

        usa_hl_font = hl and modo in ('font', 'italic')
        fuente = self._fuente(hl_key if usa_hl_font else base_key, tamano)

        pintar = hl_color if hl and modo in ('color', 'italic', 'underline', 'glow') else color
        pad = int(tamano * 0.16)
        bbox = fuente.getbbox(texto)
        w = max(1, bbox[2] - bbox[0] + pad * 2)
        # Alto FIJO para todas las palabras y línea base comuna: antes cada
        # palabra se recortaba a su propio bbox y las que tenían tilde o cola
        # quedaban más arriba que las demás en el NDI.
        h = max(1, int(tamano * 1.45))

        img = Image.new('RGBA', (w, h), (0, 0, 0, 0))
        asc, _desc = fuente.getmetrics()
        base_y = int(h * 0.78)
        pos = (pad - bbox[0], base_y - asc)

        if hl and modo == 'glow':
            halo = Image.new('RGBA', (w, h), (0, 0, 0, 0))
            ImageDraw.Draw(halo).text(pos, texto, font=fuente, fill=hl_color)
            halo = halo.filter(ImageFilter.GaussianBlur(max(2, tamano // 14)))
            img = Image.alpha_composite(img, halo)

        draw = ImageDraw.Draw(img)

        if hl and modo == 'box':
            caja = [max(0, pos[0] + bbox[0] - int(tamano * 0.18)),
                    max(0, pos[1] + bbox[1] - int(tamano * 0.10)),
                    pos[0] + bbox[2] + int(tamano * 0.18),
                    pos[1] + bbox[3] + int(tamano * 0.10)]
            draw.rounded_rectangle(caja, radius=int(tamano * 0.14), fill=hl_color)
            draw.text(pos, texto, font=fuente, fill=(16, 20, 26, 255))
        else:
            draw.text(pos, texto, font=fuente, fill=pintar)

        if hl and modo == 'underline':
            y = pos[1] + bbox[3] + int(tamano * 0.06)
            draw.line([(pos[0] + bbox[0], y), (pos[0] + bbox[2], y)],
                      fill=hl_color, width=max(2, tamano // 22))

        return img

    def _layout(self, palabras: list[dict], tamano: int):
        lineas: list[list[dict]] = [[]]
        for p in palabras:
            if p.get('nueva_linea'):
                lineas.append([])
            else:
                lineas[-1].append(p)

        lineas = [l for l in lineas if l]
        espacio = int(tamano * 0.26)
        # Todas las palabras comparten alto de imagen, así que centrarlas en la
        # línea deja todas sobre la misma línea base.
        alto_linea = max([int(tamano * 1.18)] + [p['alto'] for l in lineas for p in l])
        total = len(lineas) * alto_linea
        y = (self.height - total) // 2

        for linea in lineas:
            ancho = sum(p['ancho'] for p in linea) + espacio * max(0, len(linea) - 1)
            x = (self.width - ancho) // 2
            for p in linea:
                p['x'] = x
                p['y'] = y + (alto_linea - p['alto']) // 2
                x += p['ancho'] + espacio
            y += alto_linea

    # -- composición -------------------------------------------------------
    def render(self) -> Image.Image:
        with self.lock:
            palabras = list(self.words)
            inicio = self.inicio

        frame = Image.new('RGBA', (self.width, self.height), (0, 0, 0, 0))
        em = float(getattr(self, '_tamano_usado', None) or self._tamano())
        dur = max(0.08, float(self.args.speed) / 1000.0)
        stagger = float(self.args.stagger) / 1000.0
        ahora = time.monotonic()
        efecto = self.panel.get('effect') or 'pop'

        for i, p in enumerate(palabras):
            # Cada palabra tiene su propia hora de arranque: las que ya estaban
            # en pantalla no vuelven a animarse cuando llega la cita.
            prog = (ahora - p.get('t0', inicio)) / dur
            sx, sy, dx, dy, alpha, rot, blur, crop = effect_state(efecto, prog, em, i)
            if alpha <= 0.01:
                continue

            img = p['img']
            if crop < 0.999:
                img = img.crop((0, 0, max(1, int(img.width * crop)), img.height))
            if abs(sx - 1) > 0.01 or abs(sy - 1) > 0.01:
                img = img.resize((max(1, int(img.width * sx)), max(1, int(img.height * sy))),
                                 Image.BILINEAR)
            if abs(rot) > 0.5:
                img = img.rotate(rot, resample=Image.BICUBIC, expand=True)
            if blur > 0.4:
                img = img.filter(ImageFilter.GaussianBlur(blur))
            if alpha < 0.999:
                img = img.copy()
                img.putalpha(img.getchannel('A').point(lambda v, a=alpha: int(v * a)))

            x = int(p['x'] + dx - (img.width - p['ancho']) / 2)
            y = int(p['y'] + dy - (img.height - p['alto']) / 2)
            _paste_safe(frame, img, x, y)

        return frame

    # -- NDI ---------------------------------------------------------------
    def iniciar_ndi(self) -> bool:
        if ndi is None:
            print('⚠️ NDIlib no está instalado: no se emite NDI (usá --preview para probar).')
            print('   py -3.9 -m pip install -r ndi/requirements-3.9.txt')
            return False
        if not ndi.initialize():
            print('⚠️ NDI no pudo inicializar (¿falta el runtime de NDI?).')
            return False
        # El runtime rechaza nombres repetidos: send_create devuelve NULL y el
        # wrapper de ndi-python lanza RuntimeError ("Could not allocate capsule
        # object"). Si ya hay otro emisor con ese nombre, salimos con sufijo en
        # vez de morir (pasa cuando quedó un emisor viejo abierto).
        self.sender = None
        nombre = self.args.name
        for nombre in [self.args.name] + [f'{self.args.name} {n}' for n in range(2, 6)]:
            send = ndi.SendCreate()
            send.ndi_name = nombre
            try:
                self.sender = ndi.send_create(send)
            except Exception:
                self.sender = None
            if self.sender is not None:
                if nombre != self.args.name:
                    print(f'⚠️ Ya había una fuente "{self.args.name}" en la red: '
                          f'emitiendo como "{nombre}".', flush=True)
                break
        if self.sender is None:
            print('⚠️ No se pudo crear el sender NDI (¿nombre ocupado o runtime ausente?).')
            return False
        print(f'📡 NDI emitiendo como "{nombre}" ({self.width}x{self.height}@{self.fps})', flush=True)
        print('   En OBS: Fuente -> NDI Source -> elegí "Kinetic Lyrics".', flush=True)
        return True

    def enviar(self, frame: Image.Image):
        """Manda un frame RGBA.

        El buffer y la estructura se crean UNA vez y se reutilizan: crear un
        numpy array nuevo por frame a 60 fps terminaba en un crash nativo
        (0xC0000005) dentro de la librería NDI.
        """
        if self.sender is None:
            return
        import numpy as np

        if self._buf is None or self._buf.shape[:2] != (frame.height, frame.width):
            self._buf = np.zeros((frame.height, frame.width, 4), dtype=np.uint8)
            self._vf = ndi.VideoFrameV2()
            self._vf.xres, self._vf.yres = frame.width, frame.height
            self._vf.FourCC = ndi.FOURCC_VIDEO_TYPE_RGBA
        np.copyto(self._buf, np.asarray(frame, dtype=np.uint8))
        self._vf.data = self._buf
        ndi.send_send_video_v2(self.sender, self._vf)

    def cerrar(self):
        if self.sender is not None and ndi is not None:
            try:
                ndi.send_destroy(self.sender)
            except Exception:
                pass
        if ndi is not None:
            try:
                ndi.destroy()
            except Exception:
                pass


def _paste_safe(base: Image.Image, img: Image.Image, x: int, y: int):
    x0, y0 = max(0, x), max(0, y)
    x1 = min(base.width, x + img.width)
    y1 = min(base.height, y + img.height)
    if x1 <= x0 or y1 <= y0:
        return
    crop = img.crop((x0 - x, y0 - y, x1 - x, y1 - y))
    base.paste(crop, (x0, y0), crop)


# ------------------------------------------------------------------ bridge IO

def _get_json(url: str, timeout: float = 1.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode('utf-8'))
    except Exception:
        return None


def hilo_bridge(app: KineticNDI, bridge: str):
    """Lee texto y configuración del panel (dos sondeos independientes)."""
    ultimo_panel = 0.0
    while True:
        estado = _get_json(f'{bridge}/api/state')
        if estado and estado.get('text'):
            app.set_texto(estado.get('text') or '')

        ahora = time.time()
        if ahora - ultimo_panel > 0.7:
            ultimo_panel = ahora
            panel = _get_json(f'{bridge}/api/panel')
            if isinstance(panel, dict):
                app.set_panel(panel)
        time.sleep(0.06)


# ----------------------------------------------------------------------- main

def main():
    parser = argparse.ArgumentParser(description='Letras cinéticas por NDI')
    parser.add_argument('--bridge', default='http://127.0.0.1:7333')
    parser.add_argument('--name', default='Kinetic Lyrics')
    parser.add_argument('--width', type=int, default=1920)
    parser.add_argument('--height', type=int, default=1080)
    parser.add_argument('--fps', type=int, default=60)
    parser.add_argument('--font', default='', help='archivo .ttf propio (opcional)')
    parser.add_argument('--font-key', default='inter', help='clave del panel: inter, pacifico, ...')
    parser.add_argument('--size', type=int, default=0, help='0 = automático (13%% del alto)')
    parser.add_argument('--effect', default='pop', choices=sorted(EFFECTS))
    parser.add_argument('--case', default='asis', choices=['asis', 'upper', 'lower', 'camel'])
    parser.add_argument('--hlmode', default='color', choices=sorted(HL_MODES))
    parser.add_argument('--hlfont', default='pacifico')
    parser.add_argument('--hl', default='', help='palabras a resaltar, separadas por coma')
    parser.add_argument('--hl-color', default='#ffd23f')
    parser.add_argument('--color', default='#ffffff')
    parser.add_argument('--stagger', type=float, default=55.0)
    parser.add_argument('--speed', type=float, default=520.0)
    parser.add_argument('--text', default='', help='texto de prueba (sin bridge)')
    parser.add_argument('--preview', default='', help='guarda un PNG en vez de emitir NDI')
    parser.add_argument('--bg', default='#000000', help='fondo del PNG de preview (NDI sale transparente)')
    parser.add_argument('--no-bg', action='store_true', help='preview con fondo transparente')
    args = parser.parse_args()

    app = KineticNDI(args)

    if args.text:
        app.set_texto(args.text)
        app.set_panel({'effect': args.effect, 'font': args.font_key, 'size': str(args.size),
                       'case': args.case, 'hlmode': args.hlmode, 'hlfont': args.hlfont,
                       'highlight': args.hl_color, 'words': args.hl})
    else:
        threading.Thread(target=hilo_bridge, args=(app, args.bridge), daemon=True).start()

    if args.preview:
        time.sleep(args.speed / 1000.0 + args.stagger / 1000.0 * 8)
        frame = app.render()
        if not args.no_bg:
            fondo = Image.new('RGBA', frame.size, hex_rgba(args.bg))
            frame = Image.alpha_composite(fondo, frame)
        frame.save(args.preview)
        print(f'🖼️  Cuadro guardado en {args.preview} ({frame.width}x{frame.height})', flush=True)
        return

    if not app.iniciar_ndi():
        return

    intervalo = 1.0 / max(1, args.fps)
    ultimo_aviso = time.monotonic()
    try:
        while True:
            inicio = time.monotonic()
            app.enviar(app.render())
            # Cada 8 s avisa si hay o no receptores: sirve para saber si la
            # fuente está llegando a OBS/vMix o si nadie se conectó todavía.
            if time.monotonic() - ultimo_aviso > 8:
                ultimo_aviso = time.monotonic()
                try:
                    conexiones = ndi.send_get_no_connections(app.sender, 0)
                    print(f'   receptores conectados: {conexiones}', flush=True)
                except Exception:
                    pass
            resto = intervalo - (time.monotonic() - inicio)
            if resto > 0:
                time.sleep(resto)
    except KeyboardInterrupt:
        print('\n⏹ Cerrando NDI', flush=True)
    finally:
        app.cerrar()


if __name__ == '__main__':
    main()
