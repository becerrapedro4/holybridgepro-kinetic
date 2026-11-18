# -*- coding: utf-8 -*-
# Holyrics Bridge Pro (v6.5.3) — puente entre Holyrics y vMix / Resolume / OBS
import sys
import os
import re
import functools
import unicodedata
import ipaddress
import concurrent.futures
import threading
import time
import html
import json
import webbrowser
import socket
import subprocess
import logging
import winreg
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
from pythonosc.udp_client import SimpleUDPClient
import pystray
from PIL import Image
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

# ─── FIX PARA .EXE ──────────────────────────────────────────────────────
class DummyStream:
    def isatty(self): return False
    def write(self, *args): pass
    def flush(self): pass

if sys.stdout is None: sys.stdout = DummyStream()
if sys.stderr is None: sys.stderr = DummyStream()

# La consola de Windows usa cp1252/cp850 y los emojis de los print() lanzaban
# UnicodeEncodeError: dentro de un hilo eso lo mataba en silencio (el worker se
# quedaba muerto). Forzamos UTF-8 tolerante para que ningún print rompa nada.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

try:
    sys.stdout.isatty = lambda: False
    sys.stderr.isatty = lambda: False
except Exception:
    pass
logging.getLogger().handlers = []

logging.getLogger().setLevel(logging.CRITICAL + 1)

# ─── NICEUI ─────────────────────────────────────────────────────────────
from nicegui import ui, app

# ─── OBS ──────────────────────────────────────────────────────────────
try:
    import obsws_python as obsws
    OBS_AVAILABLE = True
except ImportError:
    OBS_AVAILABLE = False

# ─── VARIABLES GLOBALES ──────────────────────────────────────────────
tray_icon = None
running = False
worker_thread = None
ultimo_texto = ""
ultimo_texto_raw = ""
ultimo_tipo_enviado = None

# El hilo del worker vive mientras la app esté abierta: encender/apagar el bridge
# sólo cambia este evento. Así no hay carreras al reiniciar ni hilos huérfanos.
bridge_on = threading.Event()
# Auto-arranque por condiciones (ver autoarm_worker): si el operador aprieta
# STOP, se cancela para esta sesión y no vuelve a arrancar solo.
_autoarm_listo = threading.Event()
_autoarm_cancelado = threading.Event()
worker_lock = threading.Lock()

# Sesiones HTTP con keep-alive: evita abrir una conexión TCP nueva en cada consulta.
# Los reintentos cubren el caso típico de la conexión keep-alive que el servidor ya cerró.
def _sesion_http():
    sesion = requests.Session()
    reintentos = Retry(total=2, connect=2, read=1, backoff_factor=0.1,
                       allowed_methods=["GET", "POST"], status_forcelist=[502, 503, 504])
    sesion.mount("http://", HTTPAdapter(max_retries=reintentos, pool_maxsize=8))
    return sesion

sesion_holyrics = _sesion_http()

# ─── v6.1: una PC de vMix caída no puede frenar a las demás ──────────
# Antes, cada SetText a una PC apagada reintentaba la conexión (3 intentos de
# 2 s) y el hilo de envío quedaba trabado: la PC que SÍ estaba conectada
# recibía el texto recién al terminar esa espera (o directamente no lo
# recibía, porque el envío siguiente lo pisaba en la cola).
# Ahora: sin reintentos de conexión, timeout corto, corte por PC (mientras está
# caída se la saltea) y cada PC recibe en su propio hilo, en paralelo.
def _sesion_vmix():
    sesion = requests.Session()
    sesion.mount("http://", HTTPAdapter(max_retries=Retry(total=0, connect=0, read=0)))
    return sesion

sesion_vmix = _sesion_vmix()
VMIX_TIMEOUT = (0.8, 2.0)   # (conectar, leer) en segundos

_vmix_salud = {}            # (ip, puerto) -> {"caido_hasta": ts, "fallas": n}
_vmix_salud_lock = threading.Lock()

class _VmixCaido(Exception):
    """Se corta el envío a UNA PC (su vMix no responde).

    caido=True  -> el equipo no acepta la conexión: se lo saltea un rato.
    caido=False -> conectó pero no contestó: se corta este envío, sin castigo.
    """
    def __init__(self, motivo, caido=True):
        super().__init__(str(motivo))
        self.caido = caido

def _vmix_clave(destino):
    return (str(destino.get("ip", "")).strip(), str(destino.get("port", "")).strip())

def _vmix_etiqueta(destino):
    """Nombre para los avisos: «PC Cámara 2 (192.168.0.9:8088)»."""
    nombre = str(destino.get("nombre", "") or "").strip()
    equipo = f"{destino.get('ip')}:{destino.get('port')}"
    return f"{nombre} ({equipo})" if nombre else equipo

def _vmix_disponible(destino):
    with _vmix_salud_lock:
        estado = _vmix_salud.get(_vmix_clave(destino))
        return not estado or time.time() >= estado["caido_hasta"]

def _vmix_marcar_ok(destino):
    with _vmix_salud_lock:
        _vmix_salud.pop(_vmix_clave(destino), None)
        sigue_caida = bool(_vmix_salud)
    # La PC volvió: el aviso viejo ya no corresponde (si no, el Home mostraría
    # "vMix sin conexión" para siempre aunque esté enviando bien).
    if not sigue_caida and ui_state["status_msg"].startswith("vMix "):
        ui_state["status_msg"] = ""

def _vmix_marcar_caido(destino, motivo=""):
    """Corta el circuito de esa PC: 5 s, 10 s, 15 s... hasta 30 s entre pruebas."""
    with _vmix_salud_lock:
        estado = _vmix_salud.get(_vmix_clave(destino)) or {"fallas": 0}
        estado["fallas"] += 1
        espera = min(30.0, 5.0 * estado["fallas"])
        estado["caido_hasta"] = time.time() + espera
        _vmix_salud[_vmix_clave(destino)] = estado
    ui_state["status_msg"] = (f"vMix {_vmix_etiqueta(destino)} sin conexión"
                              f" — reintento en {espera:.0f} s")

def _vmix_pedido(url, inp, field, val, auth, destino):
    """Un SetText, con los errores ya clasificados (ver _VmixCaido)."""
    try:
        r = sesion_vmix.get(url, params={"Function": "SetText", "Input": inp,
                                         "SelectedName": field, "Value": val},
                            timeout=VMIX_TIMEOUT, auth=auth)
    except requests.exceptions.ConnectTimeout as ex:
        raise _VmixCaido("timeout al conectar") from ex
    except requests.exceptions.ConnectionError as ex:
        raise _VmixCaido("sin respuesta") from ex
    except requests.exceptions.Timeout as ex:
        raise _VmixCaido("vMix no respondió", caido=False) from ex
    except Exception as ex:
        raise _VmixCaido(str(ex)[:40], caido=False) from ex
    if r.status_code != 200:
        _avisar_error_vmix(r)
    else:
        _vmix_marcar_ok(destino)

def _vmix_auth(destino=None):
    """Credenciales HTTP Basic del Web Controller de vMix (o None si no hay).

    vMix devuelve 401 a las peticiones que llegan de OTRA PC cuando el Web
    Controller tiene contraseña: en la misma máquina no la pide mientras esté
    marcado "Allow software on this computer access without login".
    """
    if destino is None:
        usuario = str(live_state.get("vmix_user", "") or "").strip()
        clave = str(live_state.get("vmix_pass", "") or "")
    else:
        usuario = str(destino.get("user", "") or "").strip()
        clave = str(destino.get("pass", "") or "")
    if not usuario and not clave:
        return None
    return (usuario or "vmix", clave)

# Se activa cuando vMix responde 401: la GUI lo detecta y abre el diálogo de clave.
aviso_401 = threading.Event()

def _avisar_error_vmix(r):
    """Muestra en la barra de estado un rechazo de vMix (antes se perdía en silencio)."""
    if r.status_code == 401:
        ui_state["status_msg"] = "vMix 401: falta la clave del Web Controller"
        aviso_401.set()
    else:
        ui_state["status_msg"] = f"vMix HTTP {r.status_code}"

# Puerto de la REST API de Resolume (es fijo en Resolume)
RESOLUME_REST_PORT = 8080

def vmix_active():     return live_state.get("vmix_enabled", False)
def resolume_active(): return live_state.get("resolume_enabled", False)
def obs_active():      return live_state.get("obs_enabled", False)

def set_vmix_active(val):     live_state["vmix_enabled"] = bool(val)
def set_resolume_active(val): live_state["resolume_enabled"] = bool(val)
def set_obs_active(val):      live_state["obs_enabled"] = bool(val)

# ─── UI ELEMENTS ──────────────────────────────────────────────────────
holyrics_ip = None
holyrics_port = None
intervalo = None
auto_start_toggle = None
vmix_toggle = None
vmix_ip = None
vmix_port = None
vmix_user = None
vmix_pass = None
vmix_multi_toggle = None
vmix_input_biblia = None
vmix_field_biblia = None
vmix_input_biblia2 = None
vmix_field_biblia2 = None
vmix_input_letras = None
vmix_field_letras = None
vmix_input_letras2 = None
vmix_field_letras2 = None
vmix_input_texto = None
vmix_field_texto = None
vmix_input_texto2 = None
vmix_field_texto2 = None
resolume_toggle = None
resolume_multi_toggle = None
reso_ip = None
reso_port = None
reso_efecto = None
reso_param = None
reso_fade = None
reso_layer_biblia = None
reso_clip_biblia = None
reso_layer_biblia2 = None
reso_clip_biblia2 = None
reso_layer_letras = None
reso_clip_letras = None
reso_layer_letras2 = None
reso_clip_letras2 = None
reso_layer_texto = None
reso_clip_texto = None
reso_layer_texto2 = None
reso_clip_texto2 = None
obs_toggle = None
obs_salida_unica_toggle = None
obs_source_unica = None
obs_multi_toggle = None
obs_ip = None
obs_port = None
obs_password = None
obs_source_biblia = None
obs_source_biblia2 = None
obs_source_letras = None
obs_source_letras2 = None
obs_source_texto = None
obs_source_texto2 = None
vmix_split_toggle = None
vmix_input_versiculo = None
vmix_field_versiculo = None
vmix_input_cita = None
vmix_field_cita = None
reso_split_toggle = None
reso_layer_versiculo = None
reso_clip_versiculo = None
reso_layer_cita = None
reso_clip_cita = None
obs_split_toggle = None
obs_source_versiculo = None
obs_source_cita = None
app_port_input = None
status_dot = None
tipo_label = None
preview_text = None

# ─── CONFIGURACIÓN REGISTRO ──────────────────────────────────────────
REG_PATH = r"Software\HolyricsBridge"

# ─── AUTOARRANQUE CON WINDOWS ────────────────────────────────────────
# El switch "Auto-Start" ahora hace las DOS cosas: registra la app en
# HKCU\...\Run (se abre sola al iniciar sesión) y arranca el bridge solo.
# Antes sólo hacía lo segundo, así que el usuario tenía que abrir el exe a mano.
AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_VALUE = "HolyBridgePro"


def _autostart_target():
    """Comando con el que Windows debe abrir la app.

    Congelado: el propio .exe. Desde el código: pythonw (sin consola) + script.
    """
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    exe_dir = os.path.dirname(sys.executable)
    pyw = os.path.join(exe_dir, "pythonw.exe")
    interprete = pyw if os.path.exists(pyw) else sys.executable
    return f'"{interprete}" "{os.path.abspath(__file__)}"'


def windows_autostart_activo():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY) as k:
            winreg.QueryValueEx(k, AUTOSTART_VALUE)
        return True
    except OSError:
        return False


def set_windows_autostart(activar):
    """Crea o borra la entrada de arranque automático. Devuelve True si quedó bien."""
    activar = bool(activar)
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY) as k:
            if activar:
                winreg.SetValueEx(k, AUTOSTART_VALUE, 0, winreg.REG_SZ, _autostart_target())
            else:
                try:
                    winreg.DeleteValue(k, AUTOSTART_VALUE)
                except FileNotFoundError:
                    pass
        print(f"🔁 Autoarranque {'ACTIVADO' if activar else 'desactivado'} ({AUTOSTART_VALUE})", flush=True)
        return True
    except OSError as exc:
        print(f"⚠️ No se pudo configurar el autoarranque con Windows: {exc}")
        return False

DEFAULTS = {
    "holyrics_ip": "127.0.0.1",
    "holyrics_port": "80",
    "holyrics_api_port": "",
    "holyrics_api_token": "",
    "intervalo": "0.5",
    "vmix_enabled": "0",
    "vmix_nombre": "",
    "vmix_ip": "127.0.0.1",
    "vmix_port": "8088",
    "vmix_user": "",
    "vmix_pass": "",
    "vmix_multi": "0",
    "vmix_input_biblia": "HOLYRICS",
    "vmix_field_biblia": "BIBLIA.Text",
    "vmix_input_biblia2": "HOLYRICS",
    "vmix_field_biblia2": "BIBLIA2.Text",
    "vmix_input_letras": "HOLYRICS",
    "vmix_field_letras": "LETRAS.Text",
    "vmix_input_letras2": "HOLYRICS",
    "vmix_field_letras2": "LETRAS2.Text",
    "vmix_input_texto": "HOLYRICS",
    "vmix_field_texto": "TEXTO.Text",
    "vmix_input_texto2": "HOLYRICS",
    "vmix_field_texto2": "TEXTO2.Text",
    "resolume_enabled": "0",
    "resolume_multi": "0",
    "reso_nombre": "",
    "reso_ip": "127.0.0.1",
    "reso_port": "7000",
    "reso_efecto": "blocktextgenerator",
    "reso_param": "text/params/lines",
    "reso_fade": "0.0",
    "reso_layer_biblia": "1",
    "reso_clip_biblia": "8",
    "reso_layer_biblia2": "1",
    "reso_clip_biblia2": "8",
    "reso_layer_letras": "1",
    "reso_clip_letras": "9",
    "reso_layer_letras2": "1",
    "reso_clip_letras2": "9",
    "reso_layer_texto": "1",
    "reso_clip_texto": "10",
    "reso_layer_texto2": "1",
    "reso_clip_texto2": "10",
    "obs_enabled": "0",
    "obs_salida_unica": "0",
    "obs_source_unica": "HOLYRICS",
    "obs_multi": "0",
    "obs_nombre": "",
    "obs_ip": "127.0.0.1",
    "obs_port": "4455",
    "obs_password": "",
    "obs_source_biblia": "BIBLIA",
    "obs_source_biblia2": "BIBLIA2",
    "obs_source_letras": "LETRAS",
    "obs_source_letras2": "LETRAS2",
    "obs_source_texto": "TEXTO",
    "obs_source_texto2": "TEXTO2",
    "vmix_split_biblia": "0",
    "vmix_input_versiculo": "HOLYRICS",
    "vmix_field_versiculo": "VERSICULO.Text",
    "vmix_input_cita": "HOLYRICS",
    "vmix_field_cita": "CITA.Text",
    "reso_split_biblia": "0",
    "reso_layer_versiculo": "1",
    "reso_clip_versiculo": "11",
    "reso_layer_cita": "1",
    "reso_clip_cita": "12",
    "obs_split_biblia": "0",
    "obs_source_versiculo": "VERSICULO",
    "obs_source_cita": "CITA",
    "auto_start": "0",
    "ndi_enabled": "0",
    "app_port": "8095",
    # Formato del texto que sale hacia vMix/Resolume/OBS (switches del Home):
    # todo en mayúsculas, minúsculas respetando nombres propios, sin la versión
    # de la Biblia en la cita y sin el salto de línea entre texto y cita.
    "texto_mayusculas": "0",
    "texto_minusculas": "0",
    "texto_sin_version": "0",
    "texto_sin_salto": "0",
    # Listas JSON de PCs extra (además de la principal) por destino
    "vmix_extras": "[]",
    "reso_extras": "[]",
    "obs_extras": "[]",
}

# La configuración se guarda en DOS lugares: el registro de Windows y un archivo
# holybridgeconfig.json junto al ejecutable. El archivo es portable (se puede copiar
# a otra PC) y sirve de respaldo si el registro falla o está bloqueado.
def _app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

CONFIG_PATH = os.path.join(_app_dir(), "holybridgeconfig.json")

# Logos de los programas (se sirven en /assets). En el exe viven en _MEIPASS/assets.
def _assets_dir():
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "assets")

ASSETS_DIR = _assets_dir()
try:
    if os.path.isdir(ASSETS_DIR):
        app.add_static_files("/assets", ASSETS_DIR)
except Exception:
    pass

def _leer_json():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            datos = json.load(f)
        return datos if isinstance(datos, dict) else {}
    except Exception:
        return {}

def _escribir_json(cambios):
    """Fusiona los cambios en el JSON sin pisar el resto de las claves."""
    try:
        datos = _leer_json()
        datos.update(cambios)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(datos, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False

def reg_get(key):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_PATH) as k:
            val, _ = winreg.QueryValueEx(k, key)
            return val
    except:
        return DEFAULTS.get(key, "")

def reg_set(key, value):
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, REG_PATH) as k:
            winreg.SetValueEx(k, key, 0, winreg.REG_SZ, str(value))
    except:
        pass

def persistir(cambios):
    """Escribe los cambios en el registro Y en holybridgeconfig.json de una sola vez."""
    plano = {}
    for key, valor in cambios.items():
        if valor is True:
            txt = "1"
        elif valor is False:
            txt = "0"
        else:
            txt = "" if valor is None else str(valor)
        plano[key] = txt
        reg_set(key, txt)
    _escribir_json(plano)

# Claves que funcionan como interruptor: "1"/"0" en el registro, bool en memoria
BOOL_KEYS = {"vmix_enabled", "vmix_multi", "resolume_enabled", "resolume_multi",
             "obs_enabled", "obs_salida_unica", "obs_multi", "vmix_split_biblia",
             "reso_split_biblia", "obs_split_biblia", "auto_start", "ndi_enabled",
             "texto_mayusculas", "texto_minusculas", "texto_sin_version", "texto_sin_salto"}

def load_config():
    # Orden de prioridad: defaults < registro < holybridgeconfig.json
    # (el JSON gana para poder copiar la config a otra PC).
    cfg = {k: reg_get(k) for k in DEFAULTS}
    for key, valor in _leer_json().items():
        if key in DEFAULTS:
            cfg[key] = valor
    cfg = {k: ("" if v is None else str(v)) for k, v in cfg.items()}
    for key in BOOL_KEYS:
        cfg[key] = cfg[key] == "1"
    return cfg

config = load_config()
APP_PORT = int(config["app_port"])
# Panel del overlay de letras cinéticas (bridge Node que lee Holyrics y anima
# el texto para OBS). Se abre desde el menú lateral, debajo de OBS.
def _ip_local():
    """IP de este PC en la red local.

    El panel de Letras cinéticas tiene que abrirse también desde el celular o
    desde otra PC: con 127.0.0.1 sólo existe en esta máquina.
    """
    try:
        _s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        _s.settimeout(0.4)
        _s.connect(("8.8.8.8", 80))
        ip = _s.getsockname()[0]
        _s.close()
        return ip
    except Exception:
        return "127.0.0.1"

KINETIC_PORT = 7333
KINETIC_URL = f"http://{_ip_local()}:{KINETIC_PORT}/config"

# ─── LETRAS CINÉTICAS ADENTRO DE LA GUI (proxy al bridge) ─────────────
# La GUI (8095) y el bridge de letras (7333) son procesos y puertos distintos.
# Desde otra PC el panel no abría porque el firewall de Windows bloquea 7333:
# el node.exe que trae la instalación es "un programa nuevo" y no tiene regla.
# Sirviendo el bridge por el mismo puerto de la GUI, el panel queda embebido en
# la misma página y funciona desde cualquier dispositivo sin abrir nada más.
_BRIDGE_BASE = f"http://127.0.0.1:{KINETIC_PORT}"
_CINETICA_CAMINOS = {
    "/config", "/config.html", "/config.js", "/config.css",
    "/overlay", "/overlay.js", "/overlay.css",
    "/holybridge",
    "/api/state", "/api/panel", "/api/text", "/api/control",
    "/api/clear", "/api/fonts", "/api/events",
}


def _es_cinetica(ruta: str) -> bool:
    return ruta in _CINETICA_CAMINOS or ruta.startswith("/fonts/")


@app.middleware("http")
async def _proxy_cinetica(request, call_next):
    """Reenvía al bridge los pedidos del panel, tipografías y texto."""
    ruta = request.url.path
    if not _es_cinetica(ruta):
        return await call_next(request)

    url = _BRIDGE_BASE + ("/" if ruta == "/overlay" else ruta)
    if request.url.query:
        url += "?" + str(request.url.query)
    cuerpo = await request.body()
    cabeceras = {k: v for k, v in request.headers.items()
                 if k.lower() in ("content-type", "accept", "cache-control", "last-event-id")}
    sse = ruta == "/api/events"

    def _pedir():
        return requests.request(request.method, url, data=cuerpo or None,
                                headers=cabeceras, stream=True,
                                timeout=(3, None) if sse else (5, 30))

    try:
        respuesta = await run_in_threadpool(_pedir)
    except Exception:
        return JSONResponse(
            {"ok": False, "error": "El bridge de letras no está corriendo (prendé el switch de NDI o reiniciá HolyBridge)"},
            status_code=503)

    salida = {}
    for clave in ("content-type", "cache-control", "content-disposition", "last-modified"):
        if respuesta.headers.get(clave):
            salida[clave] = respuesta.headers[clave]
    if sse:
        salida["cache-control"] = "no-cache"
        salida["x-accel-buffering"] = "no"
    return StreamingResponse(respuesta.iter_content(64 if sse else 4096),
                             status_code=respuesta.status_code, headers=salida)

# ─── EMISOR NDI (letras cinéticas) ─────────────────────────────────────
# Se lanza como proceso aparte con Python 3.9 (es la versión donde anda NDI).
# Lee el texto del bridge (7333) y la configuración del panel de Letras
# cinéticas, así por NDI sale exactamente lo mismo que por la web.
NDI_LOG = os.path.join(_app_dir(), "kinetic_ndi.log")
_ndi_proceso = None
_ndi_error = ""

def _ndi_script():
    for base in (_app_dir(), os.getcwd()):
        ruta = os.path.join(base, "ndi", "kinetic_ndi.py")
        if os.path.isfile(ruta):
            return ruta
    return None

def _ndi_exe():
    """Emisor ya empaquetado (carpeta portable): no necesita Python instalado."""
    for base in (_app_dir(), os.getcwd()):
        ruta = os.path.join(base, "ndi", "HolyBridgeNDI.exe")
        if os.path.isfile(ruta):
            return ruta
    return None

def _python_39():
    """Ruta REAL del python.exe que tenga NDI + Pillow + numpy.

    Dos cosas aprendidas a golpes:
      1) No sirve lanzar "py -3.9" como proceso: el lanzador arranca python y
         termina enseguida, así el emisor quedaba huérfano (el switch lo daba
         por muerto al instante y nadie lo podía parar). Resolvemos el .exe.
      2) El paquete ndi-python actual pide Python 3.11-3.15; la 3.9 usa la
         5.1.1.1. Probamos del más nuevo al más viejo y nos quedamos con el
         primero que tenga las tres dependencias instaladas.
    """
    for intento in (["py", "-3.12"], ["py", "-3.11"], ["py", "-3.10"],
                    ["py", "-3.9"], ["py", "-3"], ["python"]):
        try:
            r = subprocess.run(intento + ["-c", "import sys;print(sys.executable)"],
                               capture_output=True, timeout=10, text=True)
            if r.returncode != 0:
                continue
            ruta = (r.stdout or "").strip().splitlines()[-1]
            if not os.path.isfile(ruta):
                continue
            deps = subprocess.run([ruta, "-c", "import NDIlib, PIL, numpy"],
                                  capture_output=True, timeout=25)
            if deps.returncode == 0:
                return [ruta]
        except Exception:
            continue
    return None

def ndi_estado():
    return "on" if (_ndi_proceso is not None and _ndi_proceso.poll() is None) else "off"

def ndi_arrancar():
    global _ndi_proceso, _ndi_error
    if ndi_estado() == "on":
        return True
    exe = _ndi_exe()
    if exe:
        cmd = [exe]
    else:
        script = _ndi_script()
        if not script:
            _ndi_error = "falta la carpeta ndi/ (HolyBridgeNDI.exe o kinetic_ndi.py) junto al exe"
            print("⚠️ NDI: " + _ndi_error)
            return False
        py = _python_39()
        if not py:
            _ndi_error = "no encuentro Python (instalá 3.9 y ndi-python)"
            print("⚠️ NDI: " + _ndi_error)
            return False
        cmd = py + ["-u", script]
    try:
        log = open(NDI_LOG, "a", encoding="utf-8", errors="replace")
        log.write("\n=== %s · arrancando emisor NDI ===\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
        log.flush()
        _ndi_proceso = subprocess.Popen(
            cmd, cwd=_app_dir(), stdout=log, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        _ndi_error = ""
        print("📡 NDI: emisor lanzado (" + " ".join(cmd) + ", pid " + str(_ndi_proceso.pid) + ")")
        # Confirmamos que sigue vivo: si murió, mostramos el motivo en el log.
        time.sleep(2.5)
        if _ndi_proceso.poll() is not None:
            _ndi_error = "el emisor se cerró (ver kinetic_ndi.log)"
            print("⚠️ NDI: " + _ndi_error)
            return False
        return True
    except Exception as e:
        _ndi_error = str(e)[:120]
        print("⚠️ NDI: " + _ndi_error)
        return False

def ndi_detener():
    global _ndi_proceso
    if _ndi_proceso is None:
        return
    try:
        if _ndi_proceso.poll() is None:
            _ndi_proceso.terminate()
            try:
                _ndi_proceso.wait(timeout=4)
            except Exception:
                _ndi_proceso.kill()
    except Exception:
        pass
    _ndi_proceso = None
    print("⏹  NDI: emisor detenido")

def ndi_sincronizar():
    """Alinea el emisor con el switch (lo llama el worker, también desde el tray)."""
    if live_state.get("ndi_enabled"):
        if ndi_estado() != "on":
            ndi_arrancar()
    elif ndi_estado() == "on":
        ndi_detener()
# `config` y `live_state` son EL MISMO diccionario a propósito: así cada sesión
# nueva (cualquier dispositivo que abra la GUI) arma sus controles con los
# valores ACTUALES, no con los del arranque, y ve lo que guardó otro dispositivo.
live_state = config
ui_state = {
    "texto": "",
    # Texto tal cual llega de Holyrics (con sus etiquetas), antes de formatear:
    # el Inicio lo muestra al lado de lo que realmente sale.
    "texto_raw": "",
    "tipo": "",
    "versiculo": "",
    "cita": "",
    "dot": "off",
    "status_msg": "",
    # Estadísticas del Home (se actualizan solas con cada envío).
    "envios": 0,
    "ultima": "",
    "inicio_ts": None,
}

# ─── DESTINOS EXTRA (varias PCs del mismo tipo) ──────────────────────
# Cada lista contiene dicts con ip/port (+ credenciales donde corresponda).
# Se envían los MISMOS datos a la PC principal y a todas las extra.
def _parse_extras(key):
    try:
        datos = json.loads(config.get(key, "[]") or "[]")
    except Exception:
        return []
    return [dict(d) for d in datos
            if isinstance(d, dict) and str(d.get("ip", "")).strip()]

destinos_extra = {
    "vmix": _parse_extras("vmix_extras"),
    "reso": _parse_extras("reso_extras"),
    "obs":  _parse_extras("obs_extras"),
}
# Un slot de tarjetas de PCs extra por sesión: cuando un dispositivo agrega,
# borra o prende/apaga una PC, se redibujan las tarjetas de los demás.
_extra_slots = {}   # id de cliente -> función que redibuja sus tarjetas extra

def _refrescar_extras(origen=None):
    for cid, fn in list(_extra_slots.items()):
        if cid == origen:
            continue
        try:
            fn()
        except Exception:
            pass

# ─── PERSISTENCIA ────────────────────────────────────────────────────
# Mapa "clave de configuración -> control de la GUI" de la ÚLTIMA sesión dibujada.
# Lo usan el menú del tray y el diálogo de clave de vMix, pero el guardado de la
# GUI NO depende de él (ver save_config): con varios dispositivos conectados cada
# uno tiene sus propios controles y guarda lo suyo.
UI_FIELDS = {}

# Refresco en vivo entre dispositivos: cada sesión registrada acá recibe los
# cambios hechos desde las demás, para que todos vean los mismos valores.
_sesiones = {}   # id de cliente -> función que aplica un dict de cambios a esa sesión

def _normalizar(key, valor):
    """Convierte el valor crudo de un control al tipo que espera live_state."""
    if key in BOOL_KEYS:
        # bool("0") es True: si el valor llega como texto hay que interpretarlo.
        if isinstance(valor, str):
            return valor.strip().lower() in ("1", "true", "on", "yes", "si", "sí")
        return bool(valor)
    return "" if valor is None else str(valor)

def _cliente_actual():
    """Id del cliente que ejecuta esto, o None si corre en el hilo principal."""
    try:
        return ui.context.client.id
    except Exception:
        return None

def _difundir(cambios):
    """Refleja los cambios en las DEMÁS sesiones abiertas (no en la de origen)."""
    origen = _cliente_actual()
    for cid, refrescar in list(_sesiones.items()):
        if cid == origen:
            continue
        try:
            refrescar(cambios)
        except Exception:
            pass

def _aplicar_cambios(cambios):
    """Aplica en caliente, persiste y difunde a las otras sesiones.

    Descarta las claves cuyo valor no cambió: eso corta cualquier rebote cuando
    una sesión recibe por difusión un valor igual al que ya tenía.
    """
    nuevos = {k: v for k, v in cambios.items() if live_state.get(k) != v}
    if not nuevos:
        return False
    live_state.update(nuevos)   # aplicación en caliente: el worker lee de acá
    persistir(nuevos)
    _difundir(nuevos)
    if "auto_start" in nuevos:
        # El switch tiene que tocar el registro: si no, sólo arrancaría el worker
        # y la app seguiría sin abrirse sola con Windows.
        set_windows_autostart(nuevos["auto_start"])
    if any(k in nuevos for k in _CLAVES_FORMATO):
        _reaplicar_formato()   # el switch se nota ya mismo, sin esperar a Holyrics
    return True

def save_config(evento):
    """Guarda el valor del control que disparó el evento.

    OJO: NiceGUI sólo pasa el evento al handler si declara un parámetro
    posicional sin valor por defecto (si no, lo llama sin argumentos). Cada
    control lleva su clave en `control._hb_key`, así el evento nos dice
    EXACTAMENTE qué guardar: cada sesión guarda lo suyo en vez de recorrer un
    mapa global que la última sesión conectada hubiera sobrescrito.
    """
    key = getattr(getattr(evento, "sender", None), "_hb_key", None)
    if not key:
        return
    _aplicar_cambios({key: _normalizar(key, getattr(evento, "value", None))})

def guardar_desde_ui():
    """Lee los controles de la última sesión dibujada y persiste (para el tray)."""
    cambios = {}
    for key, control in UI_FIELDS.items():
        if control is None:
            continue
        try:
            cambios[key] = _normalizar(key, control.value)
        except Exception:
            continue
    _aplicar_cambios(cambios)

def guardar_clave(key, valor):
    """Persiste una clave que NO viene de la GUI (menú del tray) y la difunde."""
    _aplicar_cambios({key: valor})
    control = UI_FIELDS.get(key)
    if control is not None:
        try:
            control.value = valor
        except Exception:
            pass

# ─── FORMATO DEL TEXTO QUE SE ENVÍA ──────────────────────────────────
# Los switches del Home deciden cómo sale el texto hacia vMix/Resolume/OBS.
_CLAVES_FORMATO = ("texto_mayusculas", "texto_minusculas",
                   "texto_sin_version", "texto_sin_salto")

def _sin_acentos(palabra):
    """Clave de comparación: sin tildes, sin diéresis y en minúsculas."""
    plano = unicodedata.normalize("NFKD", palabra)
    return "".join(c for c in plano if not unicodedata.combining(c)).lower()

# Nombres propios, lugares, libros y títulos divinos que respeta el modo
# "Minúsculas". Lista corta y pensada para ampliarse: la clave es la palabra sin
# tildes y en minúsculas (así se compara sin depender de cómo venga escrita).
_PROPIOS = {
    "dios": "Dios", "jehova": "Jehová", "yahve": "Yahvé", "yahveh": "Yahveh",
    "elohim": "Elohim", "adonai": "Adonai", "jesus": "Jesús", "cristo": "Cristo",
    "mesias": "Mesías", "emanuel": "Emanuel", "emmanuel": "Emmanuel",
    "salvador": "Salvador", "espiritu": "Espíritu", "santo": "Santo",
    "senor": "Señor", "biblia": "Biblia",
    # Lugares
    "israel": "Israel", "juda": "Judá", "jerusalen": "Jerusalén", "sion": "Sion",
    "zion": "Zion", "galilea": "Galilea", "judea": "Judea", "samaria": "Samaria",
    "belen": "Belén", "nazaret": "Nazaret", "egipto": "Egipto", "sinai": "Sinaí",
    "jordan": "Jordán", "cana": "Caná", "betania": "Betania", "getsemani": "Getsemaní",
    "golgota": "Gólgota", "eden": "Edén", "babilonia": "Babilonia", "roma": "Roma",
    "efeso": "Éfeso", "corinto": "Corinto", "filipos": "Filipos",
    "tesalonica": "Tesalónica", "antioquia": "Antioquía", "damasco": "Damasco",
    "emaus": "Emaús", "hebron": "Hebrón", "betel": "Betel", "canaan": "Canaán",
    # Personas y libros
    "adan": "Adán", "eva": "Eva", "noe": "Noé", "abraham": "Abraham",
    "sara": "Sara", "isaac": "Isaac", "rebeca": "Rebeca", "jacob": "Jacob",
    "raquel": "Raquel", "jose": "José", "moises": "Moisés", "aaron": "Aarón",
    "josue": "Josué", "caleb": "Caleb", "gedeon": "Gedeón", "sanson": "Sansón",
    "rut": "Rut", "ana": "Ana", "samuel": "Samuel", "saul": "Saúl",
    "david": "David", "salomon": "Salomón", "absalon": "Absalón", "elias": "Elías",
    "eliseo": "Eliseo", "jonatan": "Jonatán", "ester": "Ester", "job": "Job",
    "daniel": "Daniel", "ezequiel": "Ezequiel", "isaias": "Isaías",
    "jeremias": "Jeremías", "oseas": "Oseas", "joel": "Joel", "amos": "Amós",
    "jonas": "Jonás", "miqueas": "Miqueas", "nahum": "Nahúm", "habacuc": "Habacuc",
    "sofonias": "Sofonías", "hageo": "Hageo", "zacarias": "Zacarías",
    "malaquias": "Malaquías", "mateo": "Mateo", "marcos": "Marcos",
    "lucas": "Lucas", "juan": "Juan", "genesis": "Génesis", "exodo": "Éxodo",
    "levitico": "Levítico", "deuteronomio": "Deuteronomio", "esdras": "Esdras",
    "nehemias": "Nehemías", "isai": "Isaí", "noemi": "Noemí", "maria": "María",
    "marta": "Marta", "lazaro": "Lázaro", "magdalena": "Magdalena",
    "elisabet": "Elisabet", "simeon": "Simeón", "herodes": "Herodes",
    "pilato": "Pilato", "barrabas": "Barrabás", "esteban": "Esteban",
    "felipe": "Felipe", "andres": "Andrés", "bartolome": "Bartolomé",
    "bernabe": "Bernabé", "silas": "Silas", "apolos": "Apolos",
    "priscila": "Priscila", "aquila": "Aquila", "lidia": "Lidia",
    "cornelio": "Cornelio", "ananias": "Ananías", "saulo": "Saulo",
    "pablo": "Pablo", "gabriel": "Gabriel", "miguel": "Miguel", "rafael": "Rafael",
    "satanas": "Satanás", "lucifer": "Lucifer", "faraon": "Faraón",
    "nabucodonosor": "Nabucodonosor", "melquisedec": "Melquisedec", "enoc": "Enoc",
    "romanos": "Romanos", "corintios": "Corintios", "galatas": "Gálatas",
    "efesios": "Efesios", "filipenses": "Filipenses", "colosenses": "Colosenses",
    "tesalonicenses": "Tesalonicenses", "timoteo": "Timoteo", "tito": "Tito",
    "filemon": "Filemón", "hebreos": "Hebreos", "santiago": "Santiago",
    "pedro": "Pedro", "judas": "Judas", "apocalipsis": "Apocalipsis",
}

# Piezas de una línea: palabras (letras y números, con apóstrofo interno a lo
# "d'angelo"), espacios y signos. Así "RVR1960" queda como UNA pieza y se puede
# reconocer como sigla en vez de partirla en "RVR" + "1960".
_RE_PIEZAS = re.compile(r"[^\W_]+(?:['’][^\W_]+)?|\s+|[^\w\s]+", re.UNICODE)
_RE_PALABRA = re.compile(r"[^\W_]+(?:['’][^\W_]+)?", re.UNICODE)

# ─── VERSIONES DE LA BIBLIA ──────────────────────────────────────────
# Nombres con los que Holyrics puede identificar la versión en la cita. Se
# comparan normalizados (sin acentos, espacios ni guiones), así «RVR 1960»,
# «rvr-1960» y «Rvr1960» son la misma entrada. Incluye las que faltaban:
# RV1960 (Reina-Valera 1960) y RVR60 (Reina-Valera Revisada 1960).
VERSIONES_BIBLIA = (
    "RV1960", "RVR1960", "RVR60", "RV60", "RVA", "RV1995", "RV95", "RV1909",
    "REINAVALERA1960", "REINAVALERA60", "REINAVALERA95",
    "NVI", "NTV", "NBLA", "LBLA", "DHH", "TLA", "PDT", "RVC", "BDO", "JER",
    "KJV", "NKJV", "ESV", "NIV", "NASB", "BLS", "CST", "SG21", "SCH", "NVT",
    "ARC", "ARA", "LUT", "RV",
)

def _normalizar_version(texto):
    """«RVR 1960» / «rvr-1960» / «Rvr1960» -> «RVR1960»."""
    return re.sub(r"[^A-Z0-9]", "", _sin_acentos(str(texto or "")).upper())

# Normalizada -> forma correcta para mostrar (siempre en mayúsculas).
_VERS_CANON = {_normalizar_version(v): v for v in VERSIONES_BIBLIA}

def _minusculas_inteligente(texto):
    """Todo en minúsculas, respetando nombres propios y las reglas ortográficas.

    - Cada renglón y cada frase arranca con mayúscula.
    - Los nombres propios de la lista se escriben como corresponde.
    - Las palabras que YA venían bien escritas (mayúscula inicial y el resto en
      minúsculas, como «Dios» o «Jesús») se respetan tal cual: así los nombres
      propios que no están en la lista no se pierden. Lo que llega TODO EN
      MAYÚSCULAS se pasa a minúsculas y se vuelve a capitalizar.
    """
    lineas = []
    for linea in texto.split("\n"):
        salida = []
        inicio = True
        for pieza in _RE_PIEZAS.findall(linea):
            if not pieza.strip():
                salida.append(pieza)
                continue
            if not _RE_PALABRA.fullmatch(pieza):
                salida.append(pieza)          # signos o números: van tal cual
                if any(sig in pieza for sig in ".!?…;:—"):
                    inicio = True
                continue
            propio = _PROPIOS.get(_sin_acentos(pieza))
            version = _VERS_CANON.get(_normalizar_version(pieza))
            bien_escrita = pieza[:1].isupper() and any(c.islower() for c in pieza)
            sigla = pieza.isupper() and any(c.isdigit() for c in pieza)
            if version:
                # Versión de la Biblia (nvi, rvr60, rv1960...): siempre en
                # mayúsculas, como se escribe de verdad.
                salida.append(version)
            elif sigla:
                salida.append(pieza)          # siglas con números (RVR1960): tal cual
            elif propio:
                salida.append(propio)
            elif bien_escrita:
                salida.append(pieza)          # ya venía con mayúscula: es propio
            elif inicio:
                salida.append(pieza[:1].upper() + pieza[1:].lower())
            else:
                salida.append(pieza.lower())
            inicio = False
        lineas.append("".join(salida))
    return "\n".join(lineas)

def _formatear_texto(texto):
    """Aplica el formato elegido en el Home (o lo deja tal cual llega)."""
    if not texto:
        return texto
    if live_state.get("texto_mayusculas"):
        return texto.upper()
    if live_state.get("texto_minusculas"):
        return _minusculas_inteligente(texto)
    return texto

def _cita_sin_version(cita):
    """Quita la versión de la Biblia de la cita.

    «Juan 3:16 (RVR1960)», «Juan 3:16 [RVR60]» y también sin paréntesis:
    «Juan 3:16 RVR60» o «Juan 3:16 - RV1960» quedan como «Juan 3:16».
    """
    if not cita:
        return cita
    limpia = cita.strip()
    # 1) Paréntesis o corchete final (cualquier contenido): es la versión.
    while True:
        nueva = re.sub(r"\s*[\(\[][^()\[\]]*[\)\]]\s*$", "", limpia).strip()
        if nueva == limpia or not nueva:
            break
        limpia = nueva
    # 2) Versión suelta al final, sin paréntesis: sólo si es un nombre conocido,
    #    así «Salmos 23» o «Juan 3:16» nunca se tocan.
    for n in (1, 2):
        partes = limpia.rsplit(None, n)
        if len(partes) == n + 1 and _normalizar_version(" ".join(partes[1:])) in _VERS_CANON:
            limpia = partes[0].strip().rstrip(" ,;:-–—·").strip()
            break
    return limpia or cita

def _unir_versiculo_cita(versiculo, cita):
    """Une el texto con la cita; el switch decide si va con salto de línea."""
    if not cita:
        return versiculo
    return f"{versiculo}{' ' if live_state.get('texto_sin_salto') else chr(10)}{cita}".strip()

def parse_holyrics_text(texto_raw):
    decoded = html.unescape(texto_raw)
    soup = BeautifulSoup(decoded, "html.parser")
    desc_tag = soup.find("desc")
    cita = desc_tag.get_text().strip() if desc_tag else ""
    if desc_tag:
        desc_tag.decompose()
    versiculo = soup.get_text(separator="\n").strip()
    if live_state.get("texto_sin_version"):
        cita = _cita_sin_version(cita)
    versiculo = _formatear_texto(versiculo)
    cita = _formatear_texto(cita)
    texto_completo = _unir_versiculo_cita(versiculo, cita)
    return texto_completo, versiculo, cita

def _reaplicar_formato():
    """Cambió un switch de formato: se reenvía el texto actual ya reformateado.

    Sin esto, el switch recién se notaba cuando Holyrics cambiaba de versículo.
    """
    if not ultimo_texto_raw:
        return
    texto, versiculo, cita = parse_holyrics_text(ultimo_texto_raw)
    ui_state["texto"], ui_state["versiculo"], ui_state["cita"] = texto, versiculo, cita
    if not texto:
        return
    tipo = ui_state.get("tipo", "")
    if vmix_active(): enviador_vmix.enviar((texto, tipo))
    if resolume_active(): enviador_resolume.enviar((texto, tipo))
    if obs_active(): enviador_obs.enviar((texto, tipo))

# ─── ENVÍO A DESTINOS ──────────────────────────────────────────────
# Lista de PCs que reciben cada envío: la principal (de la GUI) + las extra.
def _dedupe_targets(lista):
    """Descarta entradas sin IP y repetidas (misma IP y puerto)."""
    vistos, unicos = set(), []
    for t in lista:
        ip = str(t.get("ip", "")).strip()
        if not ip:
            continue
        clave = (ip, str(t.get("port", "")).strip())
        if clave in vistos:
            continue
        vistos.add(clave)
        t["ip"] = ip
        unicos.append(t)
    return unicos

# Claves de parámetros por servicio. Las PCs extra guardan las suyas con el
# nombre SIN prefijo (multi, input_biblia, ...) y el envío las lee igual que las
# de la principal, así cada vMix/Resolume/OBS puede apuntar a su propio archivo.
_CFG_BOOL = {
    "vmix": {"multi": "vmix_multi", "split_biblia": "vmix_split_biblia"},
    "reso": {"multi": "resolume_multi", "split_biblia": "reso_split_biblia"},
    "obs":  {"salida_unica": "obs_salida_unica", "multi": "obs_multi",
             "split_biblia": "obs_split_biblia"},
}
_CFG_TEXT = {
    "vmix": ("input_biblia", "field_biblia", "input_biblia2", "field_biblia2",
             "input_letras", "field_letras", "input_letras2", "field_letras2",
             "input_texto", "field_texto", "input_texto2", "field_texto2",
             "input_versiculo", "field_versiculo", "input_cita", "field_cita"),
    "reso": ("efecto", "param", "fade",
             "layer_biblia", "clip_biblia", "layer_biblia2", "clip_biblia2",
             "layer_letras", "clip_letras", "layer_letras2", "clip_letras2",
             "layer_texto", "clip_texto", "layer_texto2", "clip_texto2",
             "layer_versiculo", "clip_versiculo", "layer_cita", "clip_cita"),
    "obs":  ("source_unica", "source_biblia", "source_biblia2", "source_letras",
             "source_letras2", "source_texto", "source_texto2",
             "source_versiculo", "source_cita"),
}
_PREF = {"vmix": "vmix_", "reso": "reso_", "obs": "obs_"}

def _cfg_extra(servicio, extra):
    """Parámetros de envío de una PC extra.

    Devuelve un dict con las MISMAS claves que usa el envío (las de la PC
    principal) pero con los valores propios de esa PC; lo que la extra no
    definió se hereda de la principal. Así cada vMix puede apuntar a su propio
    archivo de título sin tocar a los demás.
    """
    s = live_state
    pref = _PREF[servicio]
    cfg = {}
    for k, live_key in _CFG_BOOL[servicio].items():
        v = extra.get(k, None)
        cfg[live_key] = bool(s.get(live_key, False)) if v in (None, "") else bool(v)
    for k in _CFG_TEXT[servicio]:
        v = extra.get(k, "")
        v = "" if v is None else str(v)
        cfg[pref + k] = v if v != "" else str(s.get(pref + k, "") or "")
    return cfg

def _vmix_targets():
    s = live_state
    usuario, clave = s.get("vmix_user", ""), s.get("vmix_pass", "")
    lista = [{"ip": s["vmix_ip"], "port": s["vmix_port"],
              "nombre": s.get("vmix_nombre", ""),
              "user": usuario, "pass": clave, "cfg": s}]
    # Las PCs extra heredan credenciales e inputs salvo que definan los suyos.
    for d in destinos_extra["vmix"]:
        if d.get("enabled", True) is False:
            continue
        lista.append({"ip": d.get("ip", ""), "port": d.get("port", "8088"),
                      "nombre": d.get("nombre", ""),
                      "user": d.get("user") or usuario, "pass": d.get("pass") or clave,
                      "cfg": _cfg_extra("vmix", d)})
    return _dedupe_targets(lista)

def _reso_targets():
    s = live_state
    lista = [{"ip": s["reso_ip"], "port": s["reso_port"], "cfg": s}]
    for d in destinos_extra["reso"]:
        if d.get("enabled", True) is False:
            continue
        lista.append({"ip": d.get("ip", ""), "port": d.get("port", "7000"),
                      "cfg": _cfg_extra("reso", d)})
    return _dedupe_targets(lista)

def _obs_targets():
    s = live_state
    lista = [{"ip": s["obs_ip"], "port": s["obs_port"],
              "password": s.get("obs_password", ""), "cfg": s}]
    for d in destinos_extra["obs"]:
        if d.get("enabled", True) is False:
            continue
        lista.append({"ip": d.get("ip", ""), "port": d.get("port", "4455"),
                      "password": d.get("password", ""), "cfg": _cfg_extra("obs", d)})
    return _dedupe_targets(lista)

# ─── AUTO-ARRANQUE POR CONDICIONES ───────────────────────────────────
# El bridge arranca solo cuando se cumplen las TRES cosas:
#   1. hay al menos un destino configurado (prende + tiene IP),
#   2. Holyrics responde,
#   3. al menos un destino responde.
# Se reintenta durante ~2 minutos (vMix/Resolume/OBS suelen abrirse después),
# así que no importa el orden en que se prendan las máquinas.

def _destinos_configurados():
    """[(etiqueta, ip, puerto)] de los destinos ACTIVOS y con IP cargada."""
    s = live_state
    lista = []
    if s.get("vmix_enabled") and str(s.get("vmix_ip", "")).strip():
        lista += [("vMix", d["ip"], d["port"]) for d in _vmix_targets()]
    if s.get("resolume_enabled") and str(s.get("reso_ip", "")).strip():
        lista += [("Resolume", d["ip"], d["port"]) for d in _reso_targets()]
    if s.get("obs_enabled") and str(s.get("obs_ip", "")).strip():
        lista += [("OBS", d["ip"], d["port"]) for d in _obs_targets()]
    return lista


def _holyrics_responde():
    s = live_state
    try:
        r = requests.get(f"http://{s.get('holyrics_ip', '127.0.0.1')}:"
                         f"{s.get('holyrics_port', '80')}/view/text.json", timeout=1.5)
        return r.status_code == 200
    except Exception:
        return False


def _destino_responde(nombre, ip, port):
    """Ping liviano a un destino. No envía texto ni cambia nada."""
    try:
        if nombre == "vMix":
            s = live_state
            r = requests.get(f"http://{ip}:{port}/api/", timeout=1.5,
                             auth=(s.get("vmix_user", ""), s.get("vmix_pass", "")))
            return r.status_code == 200
        if nombre == "Resolume":
            r = requests.get(f"http://{ip}:{RESOLUME_REST_PORT}/api/v1/composition", timeout=1.5)
            return r.status_code == 200
        # OBS: alcanza con que el WebSocket acepte la conexión TCP.
        with socket.create_connection((str(ip), int(port)), timeout=1.5):
            return True
    except Exception:
        return False


def _autoarm_estado():
    """(¿arrancar?, motivo)"""
    destinos = _destinos_configurados()
    if not destinos:
        return False, "sin destinos configurados"
    if not _holyrics_responde():
        return False, "Holyrics sin respuesta"
    for nombre, ip, port in destinos:
        if _destino_responde(nombre, ip, port):
            return True, f"{nombre} {ip}:{port}"
    return False, "ningún destino responde"


def autoarm_worker():
    """Espera las condiciones y arranca el bridge sin intervención."""
    for intento in range(1, 41):          # 40 × 3 s ≈ 2 minutos
        if _autoarm_cancelado.is_set() or _autoarm_listo.is_set():
            return
        if bridge_on.is_set():
            _autoarm_listo.set()
            return
        ok, motivo = _autoarm_estado()
        if ok:
            print(f"▶️ Auto-arranque: {motivo} — bridge ONLINE", flush=True)
            _autoarm_listo.set()
            toggle_bridge(True, silent=True)
            return
        if intento in (1, 10, 25, 40):
            print(f"⏳ Auto-arranque esperando: {motivo}", flush=True)
        time.sleep(3)
    print("⏹ Auto-arranque: no se dieron las condiciones, queda apagado", flush=True)


# Cada PC sale por su propio hilo: una que no responde no demora a las otras.
_pool_vmix = concurrent.futures.ThreadPoolExecutor(max_workers=8,
                                                   thread_name_prefix="vmix-pc")

def _vmix_fan_out(accion, destinos, *args):
    """Lanza `accion` sobre cada PC en paralelo y espera a que terminen todas."""
    futuros = [_pool_vmix.submit(accion, d, *args) for d in destinos]
    for f in futuros:
        try:
            f.result()
        except Exception:
            pass

def enviar_vmix(texto, tipo):
    """Envía a la PC principal y a todas las extra (mismos Inputs/Campos)."""
    _vmix_fan_out(_vmix_enviar_a, _vmix_targets(), texto, tipo)

def _vmix_enviar_a(destino, texto, tipo):
    if not _vmix_disponible(destino):     # caída: se saltea sin abrir conexión
        return
    try:
        s = destino.get("cfg") or live_state
        url = f"http://{destino['ip']}:{destino['port']}/api/"
        auth = _vmix_auth(destino)
        multi = s.get("vmix_multi", False)
        def send(inp, field, val):
            _vmix_pedido(url, inp, field, val, auth, destino)
        def is_same(a, b, c, d): return a == c and b == d

        bi, bf = s["vmix_input_biblia"], s["vmix_field_biblia"]
        bi2, bf2 = s["vmix_input_biblia2"], s["vmix_field_biblia2"]
        li, lf = s["vmix_input_letras"], s["vmix_field_letras"]
        li2, lf2 = s["vmix_input_letras2"], s["vmix_field_letras2"]
        ti, tf = s["vmix_input_texto"], s["vmix_field_texto"]
        ti2, tf2 = s["vmix_input_texto2"], s["vmix_field_texto2"]
        split = s.get("vmix_split_biblia", False)
        vi, vf = s["vmix_input_versiculo"], s["vmix_field_versiculo"]
        ci, cf = s["vmix_input_cita"], s["vmix_field_cita"]

        def clear_split():
            for x, y in [(vi, vf), (ci, cf)]:
                if x: send(x, y, " ")

        if tipo == "BIBLE":
            send(bi, bf, texto)
            if multi: send(bi2, bf2, texto)
            if split:
                versiculo = ui_state.get("versiculo", texto)
                cita = ui_state.get("cita", "")
                send(vi, vf, versiculo)
                send(ci, cf, cita)
            else:
                clear_split()
            activos = {(bi, bf), (bi2, bf2)}
            if split: activos |= {(vi, vf), (ci, cf)}
            for x, y in [(li, lf), (li2, lf2) if multi else (None, None),
                         (ti, tf), (ti2, tf2) if multi else (None, None)]:
                if x and (x, y) not in activos:
                    send(x, y, " ")
        elif tipo == "MUSIC":
            send(li, lf, texto)
            if multi: send(li2, lf2, texto)
            clear_split()
            for x, y in [(bi, bf), (bi2, bf2) if multi else (None, None),
                         (ti, tf), (ti2, tf2) if multi else (None, None)]:
                if x and not is_same(x, y, li, lf) and not (multi and is_same(x, y, li2, lf2)):
                    send(x, y, " ")
        elif tipo == "TEXT":
            send(ti, tf, texto)
            if multi: send(ti2, tf2, texto)
            clear_split()
            for x, y in [(bi, bf), (bi2, bf2) if multi else (None, None),
                         (li, lf), (li2, lf2) if multi else (None, None)]:
                if x and not is_same(x, y, ti, tf) and not (multi and is_same(x, y, ti2, tf2)):
                    send(x, y, " ")
    except _VmixCaido as ex:
        # Se corta acá: no tiene sentido seguir con los demás campos de esta PC.
        if ex.caido:
            _vmix_marcar_caido(destino, ex)
        else:
            ui_state["status_msg"] = f"vMix {_vmix_etiqueta(destino)}: {str(ex)[:40]}"
    except Exception:
        pass

def _reso_fade_opacity(client, targets, fade_dur, texto, efecto, param):
    steps = 12
    step_t = fade_dur / steps
    for i in range(steps, -1, -1):
        val = i / steps
        for layer, clip in targets:
            client.send_message(f"/composition/layers/{layer}/clips/{clip}/video/opacity", val)
        time.sleep(step_t)
    for layer, clip in targets:
        client.send_message(f"/composition/layers/{layer}/clips/{clip}/video/source/{efecto}/{param}", texto)
    for i in range(0, steps + 1):
        val = i / steps
        for layer, clip in targets:
            client.send_message(f"/composition/layers/{layer}/clips/{clip}/video/opacity", val)
        time.sleep(step_t)

def enviar_resolume(texto, tipo):
    """Envía a la PC principal y a todas las extra."""
    for destino in _reso_targets():
        _reso_enviar_a(destino, texto, tipo)

def _reso_enviar_a(destino, texto, tipo):
    try:
        s = destino.get("cfg") or live_state
        client = SimpleUDPClient(destino["ip"], int(destino["port"]))
        efecto = s.get("reso_efecto", "blocktextgenerator")
        param = s.get("reso_param", "text/params/lines")
        multi = s.get("resolume_multi", False)
        fade_dur = float(s.get("reso_fade", "0.0"))
        def osc_addr(layer, clip):
            return f"/composition/layers/{layer}/clips/{clip}/video/source/{efecto}/{param}"
        lb, cb = s["reso_layer_biblia"], s["reso_clip_biblia"]
        lb2, cb2 = s["reso_layer_biblia2"], s["reso_clip_biblia2"]
        ll, cl = s["reso_layer_letras"], s["reso_clip_letras"]
        ll2, cl2 = s["reso_layer_letras2"], s["reso_clip_letras2"]
        lt, ct = s["reso_layer_texto"], s["reso_clip_texto"]
        lt2, ct2 = s["reso_layer_texto2"], s["reso_clip_texto2"]
        lv, cv = s["reso_layer_versiculo"], s["reso_clip_versiculo"]
        lc, cc = s["reso_layer_cita"], s["reso_clip_cita"]
        split = s.get("reso_split_biblia", False)
        def clear_split_reso():
            client.send_message(osc_addr(lv, cv), " ")
            client.send_message(osc_addr(lc, cc), " ")
        if tipo == "BIBLE":
            if split:
                versiculo = ui_state.get("versiculo", texto)
                cita = ui_state.get("cita", "")
                active = [(lv, cv), (lc, cc)]
                for l, c in [(lb, cb), (lb2, cb2) if multi else (None, None)]:
                    if l and (l, c) not in set(active):
                        client.send_message(osc_addr(l, c), " ")
            else:
                active = [(lb, cb)]
                if multi: active.append((lb2, cb2))
                clear_split_reso()
            clear_candidates = [(ll, cl), (lt, ct)]
            if multi: clear_candidates += [(ll2, cl2), (lt2, ct2)]
        elif tipo == "MUSIC":
            active = [(ll, cl)]
            if multi: active.append((ll2, cl2))
            clear_candidates = [(lb, cb), (lt, ct)]
            if multi: clear_candidates += [(lb2, cb2), (lt2, ct2)]
            clear_split_reso()
        elif tipo == "TEXT":
            active = [(lt, ct)]
            if multi: active.append((lt2, ct2))
            clear_candidates = [(lb, cb), (ll, cl)]
            if multi: clear_candidates += [(lb2, cb2), (ll2, cl2)]
            clear_split_reso()
        else:
            return
        active_set = set(active)
        seen = set()
        for l, c in clear_candidates:
            if (l, c) not in active_set and (l, c) not in seen:
                client.send_message(osc_addr(l, c), " ")
                seen.add((l, c))
        if tipo == "BIBLE" and split:
            versiculo = ui_state.get("versiculo", texto)
            cita = ui_state.get("cita", "")
            if fade_dur > 0.0:
                # El fade corre dentro del hilo de envío (no en uno aparte): así nunca
                # se superponen dos fades y los cambios intermedios se descartan.
                _reso_fade_opacity(client, [(lv, cv)], fade_dur, versiculo, efecto, param)
                _reso_fade_opacity(client, [(lc, cc)], fade_dur, cita, efecto, param)
            else:
                client.send_message(osc_addr(lv, cv), versiculo)
                client.send_message(osc_addr(lc, cc), cita)
        elif fade_dur > 0.0:
            _reso_fade_opacity(client, active, fade_dur, texto, efecto, param)
        else:
            for l, c in active:
                client.send_message(osc_addr(l, c), texto)
    except:
        pass

# ─── OBS (un cliente reutilizado por cada PC) ────────────────────────
_obs_clientes = {}

def _obs_clave(destino):
    return (str(destino.get("ip", "")), str(destino.get("port", "")))

def _obs_cliente(destino):
    """Devuelve el cliente OBS de esa PC, reutilizándolo entre cambios.

    Abrir un WebSocket nuevo en cada cambio de texto costaba cientos de ms.
    """
    if not OBS_AVAILABLE:
        return None
    clave = _obs_clave(destino)
    cl = _obs_clientes.get(clave)
    if cl is not None:
        return cl
    try:
        cl = obsws.ReqClient(host=destino["ip"], port=int(destino["port"]),
                             password=destino.get("password", ""), timeout=3)
        _obs_clientes[clave] = cl
    except Exception as ex:
        cl = None
        ui_state["status_msg"] = f"OBS: {str(ex)[:50]}"
    return cl

def _obs_cerrar(destino):
    """Cierra el cliente OBS de esa PC, para que el próximo envío reconecte."""
    cl = _obs_clientes.pop(_obs_clave(destino), None)
    if cl is not None:
        try:
            cl.base_client.ws.close()
        except Exception:
            pass

def _obs_set_text(src, valor, destino, silent=True):
    """Escribe el texto de una fuente OBS; si la conexión se cayó, reconecta una vez."""
    if not src:
        return
    for intento in (1, 2):
        cl = _obs_cliente(destino)
        if cl is None:
            return
        try:
            cl.set_input_settings(name=src, settings={"text": valor}, overlay=True)
            return
        except Exception as ex:
            _obs_cerrar(destino)
            if intento == 2 and not silent:
                ui_state["status_msg"] = f"OBS: {str(ex)[:50]}"

def enviar_obs(texto, tipo):
    """Envía a la PC principal y a todas las extra."""
    if not OBS_AVAILABLE:
        return
    for destino in _obs_targets():
        _obs_enviar_a(destino, texto, tipo)

def _obs_enviar_a(destino, texto, tipo):
    try:
        s = destino.get("cfg") or live_state
        multi = s.get("obs_multi", False)
        split = s.get("obs_split_biblia", False)
        if s.get("obs_salida_unica", False):
            src_unica = s.get("obs_source_unica", "").strip()
            if not src_unica:
                ui_state["status_msg"] = "OBS: nombre de fuente vacío"
                return
            if tipo == "BIBLE" and split:
                versiculo = ui_state.get("versiculo", texto)
                cita = ui_state.get("cita", "")
                _obs_set_text(src_unica, _unir_versiculo_cita(versiculo, cita), destino, silent=False)
            else:
                _obs_set_text(src_unica, texto, destino, silent=False)
            return
        sb, sb2 = s["obs_source_biblia"], s["obs_source_biblia2"]
        sl, sl2 = s["obs_source_letras"], s["obs_source_letras2"]
        st, st2 = s["obs_source_texto"], s["obs_source_texto2"]
        sv, sc = s.get("obs_source_versiculo", "VERSICULO"), s.get("obs_source_cita", "CITA")
        if tipo == "BIBLE":
            _obs_set_text(sb, texto, destino, silent=False)
            if multi: _obs_set_text(sb2, texto, destino)
            if split:
                versiculo = ui_state.get("versiculo", texto)
                cita = ui_state.get("cita", "")
                _obs_set_text(sv, versiculo, destino)
                _obs_set_text(sc, cita, destino)
            else:
                _obs_set_text(sv, " ", destino); _obs_set_text(sc, " ", destino)
            for src in [sl, sl2 if multi else None, st, st2 if multi else None]:
                _obs_set_text(src, " ", destino)
        elif tipo == "MUSIC":
            _obs_set_text(sl, texto, destino, silent=False)
            if multi: _obs_set_text(sl2, texto, destino)
            _obs_set_text(sv, " ", destino); _obs_set_text(sc, " ", destino)
            for src in [sb, sb2 if multi else None, st, st2 if multi else None]:
                _obs_set_text(src, " ", destino)
        elif tipo == "TEXT":
            _obs_set_text(st, texto, destino, silent=False)
            if multi: _obs_set_text(st2, texto, destino)
            _obs_set_text(sv, " ", destino); _obs_set_text(sc, " ", destino)
            for src in [sb, sb2 if multi else None, sl, sl2 if multi else None]:
                _obs_set_text(src, " ", destino)
    except Exception as ex:
        ui_state["status_msg"] = f"OBS: {str(ex)[:50]}"

def limpiar_vmix():
    _vmix_fan_out(_vmix_limpiar_a, _vmix_targets())

def _vmix_limpiar_a(destino):
    if not _vmix_disponible(destino):
        return
    try:
        s = destino.get("cfg") or live_state
        url = f"http://{destino['ip']}:{destino['port']}/api/"
        multi = s.get("vmix_multi", False)
        split = s.get("vmix_split_biblia", False)
        seen = set()
        pairs = [(s["vmix_input_biblia"], s["vmix_field_biblia"]),
                 (s["vmix_input_letras"], s["vmix_field_letras"]),
                 (s["vmix_input_texto"], s["vmix_field_texto"])]
        if multi:
            pairs += [(s["vmix_input_biblia2"], s["vmix_field_biblia2"]),
                      (s["vmix_input_letras2"], s["vmix_field_letras2"]),
                      (s["vmix_input_texto2"], s["vmix_field_texto2"])]
        if split:
            pairs += [(s["vmix_input_versiculo"], s["vmix_field_versiculo"]),
                      (s["vmix_input_cita"], s["vmix_field_cita"])]
        auth = _vmix_auth(destino)
        for inp, field in pairs:
            if (inp, field) not in seen:
                _vmix_pedido(url, inp, field, " ", auth, destino)
                seen.add((inp, field))
    except _VmixCaido as ex:
        if ex.caido:
            _vmix_marcar_caido(destino, ex)
    except Exception:
        pass

def limpiar_resolume():
    for destino in _reso_targets():
        _reso_limpiar_a(destino)

def _reso_limpiar_a(destino):
    try:
        s = destino.get("cfg") or live_state
        client = SimpleUDPClient(destino["ip"], int(destino["port"]))
        efecto = s.get("reso_efecto", "blocktextgenerator")
        param = s.get("reso_param", "text/params/lines")
        multi = s.get("resolume_multi", False)
        seen = set()
        pairs = [(s["reso_layer_biblia"], s["reso_clip_biblia"]),
                 (s["reso_layer_letras"], s["reso_clip_letras"]),
                 (s["reso_layer_texto"], s["reso_clip_texto"])]
        if multi:
            pairs += [(s["reso_layer_biblia2"], s["reso_clip_biblia2"]),
                      (s["reso_layer_letras2"], s["reso_clip_letras2"]),
                      (s["reso_layer_texto2"], s["reso_clip_texto2"])]
        for layer, clip in pairs:
            if (layer, clip) not in seen:
                client.send_message(f"/composition/layers/{layer}/clips/{clip}/video/source/{efecto}/{param}", " ")
                seen.add((layer, clip))
    except:
        pass

def limpiar_obs():
    if not OBS_AVAILABLE:
        return
    for destino in _obs_targets():
        _obs_limpiar_a(destino)

def _obs_limpiar_a(destino):
    s = destino.get("cfg") or live_state
    if s.get("obs_salida_unica", False):
        srcs = [s.get("obs_source_unica", "")]
    else:
        multi = s.get("obs_multi", False)
        split = s.get("obs_split_biblia", False)
        srcs = [s["obs_source_biblia"], s["obs_source_letras"], s["obs_source_texto"]]
        if multi:
            srcs += [s["obs_source_biblia2"], s["obs_source_letras2"], s["obs_source_texto2"]]
        if split:
            srcs += [s.get("obs_source_versiculo", ""), s.get("obs_source_cita", "")]
    visto = set()
    for src in srcs:
        if src and src not in visto:
            _obs_set_text(src, " ", destino)
            visto.add(src)

# ─── AUTO-DETECT ─────────────────────────────────────────────────────
def get_local_networks():
    redes = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None):
            ip = info[4][0]
            if ':' in ip or ip.startswith('127.') or ip.startswith('169.254.'):
                continue
            try:
                net = ipaddress.IPv4Network(f"{ip}/24", strict=False)
                if net not in redes:
                    redes.append(net)
            except:
                pass
    except:
        pass
    lo = ipaddress.IPv4Network("127.0.0.0/24", strict=False)
    if lo not in redes:
        redes.append(lo)
    return redes

def _check_holyrics_ip(ip_str, port, timeout=0.5):
    try:
        r = requests.get(f"http://{ip_str}:{port}/view/text.json", timeout=timeout)
        return r.status_code == 200 and "map" in r.json()
    except:
        return False

def autodetect_holyrics(port, result_label, detect_btn):
    def run():
        result_label.text = "🔍 Escaneando red..."
        detect_btn.props('disabled')
        redes = get_local_networks()
        hosts = []
        for net in redes:
            hosts += [str(h) for h in net.hosts()]
        seen = set(); unique_hosts = []
        for h in hosts:
            if h not in seen:
                seen.add(h); unique_hosts.append(h)
        found = None
        with concurrent.futures.ThreadPoolExecutor(max_workers=64) as ex:
            futures = {ex.submit(_check_holyrics_ip, ip, port): ip for ip in unique_hosts}
            for future in concurrent.futures.as_completed(futures):
                if future.result():
                    found = futures[future]
                    for f in futures:
                        f.cancel()
                    break
        detect_btn.props(remove='disabled')
        if found:
            guardar_clave("holyrics_ip", found)
            result_label.text = f"✓ Holyrics encontrado en {found}"
            result_label.style('color: rgba(0,220,150,0.9);')
        else:
            result_label.text = "✗ No encontrado — verificar IP y puerto"
            result_label.style('color: rgba(255,80,80,0.9);')
    threading.Thread(target=run, daemon=True).start()

def test_vmix(result_label, test_btn):
    def run():
        result_label.text = "⏳ Probando..."
        test_btn.props('disabled')
        try:
            s = live_state
            r = requests.get(f"http://{s['vmix_ip']}:{s['vmix_port']}/api/", timeout=3, auth=_vmix_auth())
            # Respondió: si estaba marcada como caída, vuelve a usarse ya mismo.
            _vmix_marcar_ok({"ip": s['vmix_ip'], "port": s['vmix_port']})
            if r.status_code == 200:
                result_label.text = "✓ vMix conectado"
                result_label.style('color: rgba(0,220,150,0.9);')
            elif r.status_code == 401:
                result_label.text = "✗ 401: vMix pide usuario/clave (Web Controller)"
                result_label.style('color: rgba(255,80,80,0.9);')
                aviso_401.set()
            else:
                result_label.text = f"✗ HTTP {r.status_code}"
                result_label.style('color: rgba(255,80,80,0.9);')
        except requests.exceptions.ConnectionError:
            result_label.text = "✗ Sin respuesta — verificar IP/puerto"
            result_label.style('color: rgba(255,80,80,0.9);')
        except requests.exceptions.Timeout:
            result_label.text = "✗ Timeout — vMix no responde"
            result_label.style('color: rgba(255,80,80,0.9);')
        except Exception as e:
            result_label.text = f"✗ {str(e)[:40]}"
            result_label.style('color: rgba(255,80,80,0.9);')
        test_btn.props(remove='disabled')
    threading.Thread(target=run, daemon=True).start()

def test_resolume(result_label, test_btn):
    def run():
        result_label.text = "⏳ Probando..."
        test_btn.props('disabled')
        try:
            s = live_state
            r = requests.get(f"http://{s['reso_ip']}:{RESOLUME_REST_PORT}/api/v1/composition", timeout=3)
            if r.status_code == 200:
                result_label.text = "✓ Resolume conectado (REST API)"
                result_label.style('color: rgba(0,220,150,0.9);')
            else:
                result_label.text = f"✗ REST HTTP {r.status_code}"
                result_label.style('color: rgba(255,180,0,0.9);')
        except requests.exceptions.ConnectionError:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.settimeout(1)
                sock.sendto(b"", (s["reso_ip"], int(s["reso_port"])))
                sock.close()
                result_label.text = "~ Puerto OSC alcanzable (UDP, sin confirmación)"
                result_label.style('color: rgba(255,180,0,0.9);')
            except:
                result_label.text = "✗ No se puede alcanzar el host"
                result_label.style('color: rgba(255,80,80,0.9);')
        except Exception as e:
            result_label.text = f"✗ {str(e)[:40]}"
            result_label.style('color: rgba(255,80,80,0.9);')
        test_btn.props(remove='disabled')
    threading.Thread(target=run, daemon=True).start()

def test_obs(result_label, test_btn):
    if not OBS_AVAILABLE:
        result_label.text = "✗ obsws-python no instalado"
        result_label.style('color: rgba(255,80,80,0.9);')
        return
    def run():
        result_label.text = "⏳ Probando..."
        test_btn.props('disabled')
        try:
            s = live_state
            cl = obsws.ReqClient(host=s["obs_ip"], port=int(s["obs_port"]), password=s["obs_password"], timeout=3)
            version = cl.get_version()
            result_label.text = f"✓ OBS {version.obs_version}"
            result_label.style('color: rgba(0,220,150,0.9);')
            cl.base_client.ws.close()
        except Exception as e:
            msg = str(e)
            if "password" in msg.lower() or "auth" in msg.lower():
                result_label.text = "✗ Password incorrecto"
            elif "refused" in msg.lower() or "connect" in msg.lower():
                result_label.text = "✗ OBS no responde — verificar WebSocket"
            else:
                result_label.text = f"✗ {msg[:40]}"
            result_label.style('color: rgba(255,80,80,0.9);')
        test_btn.props(remove='disabled')
    threading.Thread(target=run, daemon=True).start()

# ─── TEST DE UN DESTINO EXTRA ───────────────────────────────────────
_C_OK = "rgba(0,220,150,0.9)"
_C_BAD = "rgba(255,80,80,0.9)"
_C_WARN = "rgba(255,180,0,0.9)"

def _destino_extra(servicio, indice):
    """Arma el destino de una PC extra para poder probarla por separado."""
    lista = destinos_extra.get(servicio, [])
    if not (0 <= indice < len(lista)):
        return None
    e = lista[indice]
    s = live_state
    if servicio == "vmix":
        return {"ip": e.get("ip", ""), "port": e.get("port", "8088"),
                "user": e.get("user") or s.get("vmix_user", ""),
                "pass": e.get("pass") or s.get("vmix_pass", "")}
    if servicio == "reso":
        return {"ip": e.get("ip", ""), "port": e.get("port", "7000")}
    return {"ip": e.get("ip", ""), "port": e.get("port", "4455"),
            "password": e.get("password", "")}

def _marcar_test(label, texto, color):
    try:
        label.text = texto
        label.style(f'color: {color};')
    except Exception:
        pass

def test_destino_extra(servicio, indice, result_label, test_btn):
    """Prueba la conexión de UNA PC extra, sin tocar a las demás."""
    def run():
        _marcar_test(result_label, "⏳ Probando...", 'rgba(255,255,255,0.7)')
        try:
            test_btn.props('disabled')
        except Exception:
            pass
        try:
            dest = _destino_extra(servicio, indice)
            if not dest or not str(dest.get("ip", "")).strip():
                _marcar_test(result_label, "✗ Falta la IP", _C_BAD)
            elif servicio == "vmix":
                r = requests.get(f"http://{dest['ip']}:{dest['port']}/api/", timeout=3, auth=_vmix_auth(dest))
                # Respondió: si estaba marcada como caída, vuelve a usarse ya mismo.
                _vmix_marcar_ok(dest)
                if r.status_code == 200:
                    _marcar_test(result_label, "✓ vMix conectado", _C_OK)
                elif r.status_code == 401:
                    _marcar_test(result_label, "✗ 401: pide usuario/clave", _C_BAD)
                    aviso_401.set()
                else:
                    _marcar_test(result_label, f"✗ HTTP {r.status_code}", _C_BAD)
            elif servicio == "reso":
                try:
                    r = requests.get(f"http://{dest['ip']}:{RESOLUME_REST_PORT}/api/v1/composition", timeout=3)
                    _marcar_test(result_label, "✓ Resolume (REST)" if r.status_code == 200 else f"✗ REST HTTP {r.status_code}",
                                 _C_OK if r.status_code == 200 else _C_WARN)
                except requests.exceptions.ConnectionError:
                    try:
                        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                        sock.settimeout(1)
                        sock.sendto(b"", (dest["ip"], int(dest["port"])))
                        sock.close()
                        _marcar_test(result_label, "~ Puerto OSC alcanzable", _C_WARN)
                    except Exception:
                        _marcar_test(result_label, "✗ Host inalcanzable", _C_BAD)
            else:  # obs
                if not OBS_AVAILABLE:
                    _marcar_test(result_label, "✗ obsws-python no instalado", _C_BAD)
                else:
                    cl = obsws.ReqClient(host=dest["ip"], port=int(dest["port"]),
                                         password=dest.get("password", ""), timeout=3)
                    version = cl.get_version()
                    cl.base_client.ws.close()
                    _marcar_test(result_label, f"✓ OBS {version.obs_version}", _C_OK)
        except requests.exceptions.ConnectionError:
            _marcar_test(result_label, "✗ Sin respuesta — verificar IP/puerto", _C_BAD)
        except requests.exceptions.Timeout:
            _marcar_test(result_label, "✗ Timeout — no responde", _C_BAD)
        except Exception as ex:
            msg = str(ex)
            if "password" in msg.lower() or "auth" in msg.lower():
                _marcar_test(result_label, "✗ Password incorrecto", _C_BAD)
            else:
                _marcar_test(result_label, f"✗ {msg[:40]}", _C_BAD)
        finally:
            try:
                test_btn.props(remove='disabled')
            except Exception:
                pass
    threading.Thread(target=run, daemon=True).start()

# ─── PRUEBA DE TODOS LOS DESTINOS DE UNA ─────────────────────────────
# Botón del Inicio: manda un texto de prueba a TODAS las PCs activas de los
# servicios encendidos (no espera a que Holyrics cambie) y muestra qué contestó
# cada una. En vMix, además, revisa que los Inputs y Campos configurados existan
# de verdad en ese vMix: es la forma de ver por qué un envío "no hace nada".
_C_NEUTRO = "rgba(255,255,255,0.65)"


def _texto_prueba(base):
    """Texto de prueba identificable: el que escriba más la hora, con segundos."""
    return f"{(base or 'PRUEBA HOLYBRIDGE').strip()} {time.strftime('%H:%M:%S')}"


def _vmix_entradas(destino, auth):
    """Lee el XML de /api/ (sin parámetros): qué Inputs y Campos tiene ese vMix.

    Devuelve (entradas, fallo), donde entradas es {título de Input -> {Campos}}.
    None en entradas significa que no se pudo leer (ahí va el motivo en fallo).
    """
    try:
        r = sesion_vmix.get(f"http://{destino['ip']}:{destino['port']}/api/",
                            timeout=VMIX_TIMEOUT, auth=auth)
    except requests.exceptions.Timeout:
        return None, "no respondió (timeout)"
    except Exception as ex:
        return None, f"sin conexión ({str(ex)[:30]})"
    if r.status_code == 401:
        return None, "pide clave (401 del Web Controller)"
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}"
    entradas = {}
    try:
        # Con ElementTree y los bytes (no str): el XML de vMix trae su propia
        # declaración de encoding. Ojo: NO sirve un parser HTML acá, porque para
        # HTML <input> es una etiqueta vacía y perdería los Campos de adentro.
        raiz = ET.fromstring(r.content)
        for nodo in raiz.iter():
            if str(nodo.tag).split('}')[-1].lower() != "input":
                continue
            titulo = nodo.get("title")
            if titulo is None:
                continue
            campos = {h.get("name") for h in nodo.iter() if h.get("name")}
            entradas.setdefault(titulo, set()).update(campos)
    except Exception as ex:
        return None, f"XML ilegible ({str(ex)[:30]})"
    return entradas, ""


def _vmix_campos_usados(s):
    """Pares (Input, Campo, sección) que usa una PC según su configuración."""
    pares = []
    for base, etiqueta in (("biblia", "BIBLIA"), ("letras", "LETRAS"), ("texto", "TEXTO")):
        pares.append((s.get(f"vmix_input_{base}", ""), s.get(f"vmix_field_{base}", ""), etiqueta))
        if s.get("vmix_multi", False):
            pares.append((s.get(f"vmix_input_{base}2", ""), s.get(f"vmix_field_{base}2", ""),
                          f"{etiqueta} salida 2"))
    if s.get("vmix_split_biblia", False):
        pares.append((s.get("vmix_input_versiculo", ""), s.get("vmix_field_versiculo", ""),
                      "VERSÍCULO"))
        pares.append((s.get("vmix_input_cita", ""), s.get("vmix_field_cita", ""), "CITA"))
    return [p for p in pares if str(p[0]).strip() or str(p[1]).strip()]


def _parecido(nombre, opciones):
    """Busca el nombre entre las opciones ignorando mayúsculas y espacios de sobra."""
    if not nombre:
        return ""
    limpio = str(nombre).strip().casefold()
    for o in opciones:
        if str(o).strip().casefold() == limpio:
            return o
    return ""


def _vmix_revisar_nombres(entradas, s):
    """Inputs/Campos configurados que NO existen en ese vMix (lista de avisos)."""
    avisos = []
    for inp, fld, etiqueta in _vmix_campos_usados(s):
        campos = entradas.get(str(inp))
        if campos is None:
            parecido = _parecido(inp, entradas.keys())
            avisos.append(f"no existe el Input «{inp}» ({etiqueta})"
                          + (f" — ¿quisiste «{parecido}»?" if parecido else ""))
            continue
        if str(fld) and str(fld) not in campos:
            parecido = _parecido(fld, campos)
            avisos.append(f"el Input «{inp}» no tiene el Campo «{fld}» ({etiqueta})"
                          + (f" — ¿quisiste «{parecido}»?" if parecido else ""))
    return avisos


def _pintar_prueba(panel, resumen, lineas):
    """Vuelca el resultado de la prueba en el panel del Inicio (HTML con colores)."""
    colores = {"ok": _C_OK, "aviso": _C_WARN, "error": _C_BAD}
    partes = [f'<div style="color: rgba(255,255,255,0.5);">{html.escape(resumen)}</div>']
    for estado, quien, detalle in lineas:
        partes.append(
            f'<div style="margin-top:2px; white-space:normal;">'
            f'<span style="color: rgba(255,255,255,0.85);">{html.escape(quien)}</span>'
            f'<span style="color: {colores.get(estado, _C_NEUTRO)};"> — {html.escape(detalle)}</span>'
            f'</div>')
    try:
        panel.content = "".join(partes)
    except Exception:
        pass


def probar_destinos(texto_base, panel, boton):
    """Manda el texto de prueba a cada PC activa y cuenta qué contestó cada una."""
    def run():
        inicio = time.time()
        texto = _texto_prueba(texto_base)
        try:
            boton.props('disabled')
        except Exception:
            pass
        _pintar_prueba(panel, f"⏳ Probando a las {time.strftime('%H:%M:%S')}...", [])
        lineas = []
        # Se escribe en la sección del tipo que Holyrics está mostrando (o BIBLIA).
        seccion = {"BIBLE": "biblia", "MUSIC": "letras", "TEXT": "texto"}.get(
            ui_state.get("tipo", ""), "biblia")
        etiqueta_seccion = seccion.upper()

        # ── vMix: conexión + nombres + escritura real ──
        if vmix_active():
            for destino in _vmix_targets():
                quien = f"vMix {_vmix_etiqueta(destino)}"
                auth = _vmix_auth(destino)
                s = destino.get("cfg") or live_state
                entradas, fallo = _vmix_entradas(destino, auth)
                if entradas is None:
                    lineas.append(("error", quien, f"✗ {fallo}"))
                    continue
                inp = str(s.get(f"vmix_input_{seccion}") or "")
                fld = str(s.get(f"vmix_field_{seccion}") or "")
                try:
                    r = sesion_vmix.get(f"http://{destino['ip']}:{destino['port']}/api/",
                                        params={"Function": "SetText", "Input": inp,
                                                "SelectedName": fld, "Value": texto},
                                        timeout=VMIX_TIMEOUT, auth=auth)
                    if r.status_code == 401:
                        aviso_401.set()
                        lineas.append(("error", quien, "✗ 401: el Web Controller pide usuario/clave"))
                        continue
                    if r.status_code != 200:
                        lineas.append(("error", quien, f"✗ HTTP {r.status_code} al escribir"))
                        continue
                except requests.exceptions.Timeout:
                    lineas.append(("error", quien, "✗ no respondió (timeout)"))
                    continue
                except Exception as ex:
                    lineas.append(("error", quien, f"✗ sin conexión ({str(ex)[:30]})"))
                    continue
                _vmix_marcar_ok(destino)   # respondió: se destraba si estaba caída
                avisos = _vmix_revisar_nombres(entradas, s) if entradas else []
                escrito = f"✓ escribió «{fld or inp}» ({etiqueta_seccion})"
                if avisos:
                    lineas.append(("aviso", quien, escrito + " · " + " · ".join(avisos)))
                elif not entradas:
                    lineas.append(("aviso", quien, escrito
                                   + " · no pude leer los nombres de los Inputs de ese vMix"))
                else:
                    n_inp = len(entradas)
                    lineas.append(("ok", quien, escrito
                                   + f" · {n_inp} Input{'s' if n_inp != 1 else ''} y sus Campos, todos los nombres configurados existen"))

        # ── Resolume: el texto va por OSC (UDP, sin respuesta); el REST confirma si está vivo ──
        if resolume_active():
            for destino in _reso_targets():
                quien = f"Resolume {destino['ip']}:{destino['port']}"
                s = destino.get("cfg") or live_state
                try:
                    r = requests.get(f"http://{destino['ip']}:{RESOLUME_REST_PORT}/api/v1/composition",
                                     timeout=3)
                    vivo = r.status_code == 200
                    rest = "REST OK" if vivo else f"REST HTTP {r.status_code}"
                except Exception:
                    vivo, rest = False, "el REST no responde"
                capa = s.get(f"reso_layer_{seccion}", "")
                clip = s.get(f"reso_clip_{seccion}", "")
                direccion = (f"/composition/layers/{capa}/clips/{clip}/video/source/"
                             f"{s.get('reso_efecto', 'blocktextgenerator')}/"
                             f"{s.get('reso_param', 'text/params/lines')}")
                if not str(capa).strip() and not str(clip).strip():
                    lineas.append(("aviso", quien, f"⚠ no hay capa/clip configurado para {etiqueta_seccion}"))
                    continue
                try:
                    SimpleUDPClient(destino["ip"], int(destino["port"])).send_message(direccion, texto)
                    lineas.append(("ok" if vivo else "aviso", quien,
                                   f"{'✓' if vivo else '~'} OSC a capa {capa}/clip {clip} "
                                   f"({etiqueta_seccion}) · {rest}"
                                   + ("" if vivo else " (UDP no confirma: revisá el clip a mano)")))
                except Exception as ex:
                    lineas.append(("error", quien, f"✗ no pude mandar el OSC ({str(ex)[:30]})"))

        # ── OBS: el WebSocket contesta, así que se puede confirmar de verdad ──
        if obs_active():
            for destino in _obs_targets():
                quien = f"OBS {destino['ip']}:{destino['port']}"
                if not OBS_AVAILABLE:
                    lineas.append(("error", quien, "✗ obsws-python no está instalado"))
                    continue
                s = destino.get("cfg") or live_state
                fuente = str(s.get(f"obs_source_{seccion}") or "")
                cl = None
                try:
                    cl = obsws.ReqClient(host=destino["ip"], port=int(destino["port"]),
                                         password=destino.get("password", ""), timeout=3)
                    version = cl.get_version().obs_version
                    try:
                        cl.get_input_settings(name=fuente)
                        existe = bool(fuente)
                    except Exception:
                        existe = False
                    if not fuente:
                        lineas.append(("aviso", quien, f"⚠ OBS {version}: no hay fuente configurada "
                                                       f"para {etiqueta_seccion}"))
                    elif not existe:
                        lineas.append(("aviso", quien, f"⚠ OBS {version}: no existe la fuente "
                                                       f"«{fuente}» ({etiqueta_seccion})"))
                    else:
                        cl.set_input_settings(name=fuente, settings={"text": texto}, overlay=True)
                        lineas.append(("ok", quien, f"✓ OBS {version} · escribió «{fuente}» "
                                                     f"({etiqueta_seccion})"))
                except Exception as ex:
                    msg = str(ex)
                    bajo = msg.lower()
                    if "password" in bajo or "auth" in bajo:
                        msg = "la clave del WebSocket no es la correcta"
                    elif ("10061" in msg or "refused" in bajo or "connect" in bajo
                          or "timed out" in bajo):
                        msg = (f"no responde en {destino['ip']}:{destino['port']} "
                               "(¿OBS abierto? ¿WebSocket Server habilitado?)")
                    lineas.append(("error", quien, f"✗ {msg[:70]}"))
                finally:
                    try:
                        cl.base_client.ws.close()
                    except Exception:
                        pass

        if not lineas:
            lineas = [("aviso", "Sin destinos", "no hay ningún servicio encendido (vMix / Resolume / OBS)")]
        transcurrido = time.time() - inicio
        resumen = (f"Prueba {time.strftime('%H:%M:%S')} · texto: «{texto}» · "
                   f"{len(lineas)} destino{'s' if len(lineas) != 1 else ''} · {transcurrido:.1f} s")
        _pintar_prueba(panel, resumen, lineas)
        try:
            boton.props(remove='disabled')
        except Exception:
            pass
    threading.Thread(target=run, daemon=True).start()


# ─── ENVÍO EN ORDEN (que no se pisen) ────────────────────────────────
class Enviador:
    """Serializa los envíos de un destino en un único hilo.

    - vMix recibe los SetText uno detrás del otro (antes se cruzaban).
    - Resolume no mezcla dos procesos de fade.
    - OBS reutiliza una sola conexión WebSocket.
    Además descarta los estados intermedios: si mientras un envío está en curso
    llegan varios cambios, sólo se manda el último.
    """

    def __init__(self, nombre, accion):
        self._nombre = nombre
        self._accion = accion      # recibe (texto, tipo) o None para limpiar
        self._lock = threading.Lock()
        self._pendiente = None
        self._hay_pendiente = False
        self._hilo = None

    def enviar(self, trabajo):
        with self._lock:
            self._pendiente = trabajo
            self._hay_pendiente = True
            if self._hilo is None or not self._hilo.is_alive():
                self._hilo = threading.Thread(target=self._bucle, daemon=True,
                                              name=f"enviar-{self._nombre}")
                self._hilo.start()

    def _bucle(self):
        while True:
            with self._lock:
                if not self._hay_pendiente:
                    return
                trabajo, self._pendiente = self._pendiente, None
                self._hay_pendiente = False
            try:
                self._accion(trabajo)
            except Exception as ex:
                print(f"⚠️ Error enviando a {self._nombre}: {ex}")

enviador_vmix = Enviador("vmix", lambda t: limpiar_vmix() if t is None else enviar_vmix(*t))
enviador_resolume = Enviador("resolume", lambda t: limpiar_resolume() if t is None else enviar_resolume(*t))
enviador_obs = Enviador("obs", lambda t: limpiar_obs() if t is None else enviar_obs(*t))

# ─── WORKER ──────────────────────────────────────────────────────────
def intervalo_actual():
    """Intervalo de sondeo en segundos, tolerante a valores vacíos o inválidos.

    Necesario porque el campo "Pool (s)" se aplica en caliente mientras se escribe.
    """
    try:
        valor = float(str(live_state.get("intervalo", "0.5")).strip().replace(",", "."))
    except (TypeError, ValueError):
        return 0.5
    return valor if valor >= 0.05 else 0.05

def _leer_via_api():
    """Respaldo: lee el texto actual por el API Server de Holyrics.

    Sólo se usa si hay un token cargado (el puerto por defecto es 8091).
    Devuelve el texto, o None si no está configurado o falla.
    """
    token = str(live_state.get("holyrics_api_token", "") or "").strip()
    if not token:
        return None
    ip = live_state.get("holyrics_ip", "127.0.0.1")
    puerto = str(live_state.get("holyrics_api_port", "") or "").strip() or "8091"
    try:
        r = requests.post(f"http://{ip}:{puerto}/api/GetCPInfo?token={token}",
                          json={}, timeout=3)
        if r.status_code == 200:
            data = r.json()
            if data.get("status") == "ok":
                return data.get("data", {}).get("text", "")
    except Exception:
        pass
    return None

def worker():
    """Hilo único y permanente: sondea Holyrics sólo mientras el bridge está encendido.

    Antes se creaba un hilo nuevo en cada START; si el STOP anterior aún no había
    terminado, el START quedaba en nada. Ahora encender/apagar sólo cambia un evento.
    """
    global ultimo_texto, ultimo_texto_raw, ultimo_tipo_enviado
    print("🔄 Worker iniciado")
    while True:
        try:
            ndi_sincronizar()   # el switch NDI manda, aunque el bridge esté en STOP
        except Exception:
            pass
        if not bridge_on.is_set():
            time.sleep(0.15)
            continue
        try:
            ip = live_state.get("holyrics_ip", "127.0.0.1")
            port = live_state.get("holyrics_port", "80")
            # Fuente principal: la vista de Holyrics.
            texto_raw, tipo, error_http = None, "", None
            try:
                r = sesion_holyrics.get(f"http://{ip}:{port}/view/text.json", timeout=3)
                if r.status_code == 200:
                    data = r.json()
                    texto_raw = data["map"].get("text", "")
                    tipo = data["map"].get("type", "")
                else:
                    error_http = r.status_code
            except Exception:
                error_http = "conn"
            # Respaldo: API Server de Holyrics (sólo si hay token configurado).
            if texto_raw is None:
                api_texto = _leer_via_api()
                if api_texto is not None:
                    texto_raw, tipo, error_http = api_texto, "API", None
            if texto_raw is not None:
                # Sólo parseamos y comparamos cuando Holyrics cambió algo: antes se
                # corría BeautifulSoup en cada sondeo (5 veces por segundo) de gusto.
                if texto_raw != ultimo_texto_raw or tipo != ultimo_tipo_enviado:
                    ultimo_texto_raw = texto_raw
                    texto_completo, versiculo, cita = parse_holyrics_text(texto_raw)
                    texto = texto_completo
                    ultimo_texto = texto
                    ultimo_tipo_enviado = tipo
                    ui_state["versiculo"] = versiculo
                    ui_state["cita"] = cita
                    ui_state["texto"] = texto
                    ui_state["texto_raw"] = texto_raw
                    ui_state["tipo"] = tipo
                    ui_state["status_msg"] = f"MODO: {tipo}" if texto else "— SIN SEÑAL —"
                    if not texto:
                        if resolume_active(): enviador_resolume.enviar(None)
                        if vmix_active(): enviador_vmix.enviar(None)
                        if obs_active(): enviador_obs.enviar(None)
                    else:
                        if vmix_active(): enviador_vmix.enviar((texto, tipo))
                        if resolume_active(): enviador_resolume.enviar((texto, tipo))
                        if obs_active(): enviador_obs.enviar((texto, tipo))
                    # Estadística del Home: contamos cada cambio realmente enviado.
                    ui_state["envios"] = ui_state.get("envios", 0) + 1
                    ui_state["ultima"] = time.strftime("%H:%M:%S")
                ui_state["dot"] = "on"
            else:
                if ultimo_texto != "":
                    ultimo_texto = ultimo_texto_raw = ""
                    ultimo_tipo_enviado = None
                    if resolume_active(): enviador_resolume.enviar(None)
                    if vmix_active(): enviador_vmix.enviar(None)
                    if obs_active(): enviador_obs.enviar(None)
                # Sin conexión no dejamos el último versículo en pantalla: el
                # operador vería un texto viejo como si estuviera al aire.
                ui_state["texto"] = ui_state["tipo"] = ""
                ui_state["versiculo"] = ui_state["cita"] = ""
                ui_state["texto_raw"] = ""
                ui_state["dot"] = "off"
                ui_state["status_msg"] = ("Error de conexión - Revisar Holyrics"
                                          if error_http == "conn"
                                          else f"Error HTTP {error_http}")
        except Exception:
            if ultimo_texto != "":
                ultimo_texto = ultimo_texto_raw = ""
                ultimo_tipo_enviado = None
                if resolume_active(): enviador_resolume.enviar(None)
                if vmix_active(): enviador_vmix.enviar(None)
                if obs_active(): enviador_obs.enviar(None)
            ui_state["texto"] = ui_state["tipo"] = ""
            ui_state["versiculo"] = ui_state["cita"] = ""
            ui_state["texto_raw"] = ""
            ui_state["dot"] = "off"
            ui_state["status_msg"] = "Error de conexión - Revisar Holyrics"
        # Conectado sondeamos al ritmo configurado; si Holyrics no responde no lo
        # martillamos: esperamos un poco más antes de reintentar.
        time.sleep(intervalo_actual() if ui_state["dot"] == "on"
                   else min(2.0, intervalo_actual() * 5))

def toggle_bridge(state, silent=False):
    global running, worker_thread
    running = bool(state)
    if state:
        _autoarm_listo.set()
    else:
        # STOP manual: el auto-arranque no debe pelearse con el operador.
        _autoarm_cancelado.set()
    if state:
        bridge_on.set()
        ui_state["inicio_ts"] = time.time()
        if worker_thread is None or not worker_thread.is_alive():
            with worker_lock:
                if worker_thread is None or not worker_thread.is_alive():
                    worker_thread = threading.Thread(target=worker, daemon=True, name="worker-holyrics")
                    worker_thread.start()
        if not silent:
            ui.notify("Bridge Online", type="positive")
        else:
            print("✅ Bridge Online (auto-start)", flush=True)
    else:
        bridge_on.clear()
        # El punto vuelve a gris (apagado): si no, queda verde/rojo con el estado
        # del último sondeo y parece que el bridge sigue andando.
        ui_state["dot"] = "off"
        ui_state["status_msg"] = "Bridge apagado"
        if not silent:
            ui.notify("Bridge Offline", type="warning")
        else:
            print("⏹ Bridge Offline (auto-stop)")

# ─── INTERFAZ ────────────────────────────────────────────────────────
# ─── CADA PC ES UNA TARJETA ──────────────────────────────────────────
# La PC principal y cada PC extra tienen UNA tarjeta con TODA su configuración:
# conexión, secciones de envío (BIBLIA / LETRAS / TEXTO), split versículo/cita y
# su propio test. Las tarjetas de una sección van una al lado de la otra y se
# achican hasta que la cuarta ya no entra (ver .hb-grid en el CSS).
# En las PC extra un campo vacío hereda el valor de la PC principal.
_EXTRA_CONN = {
    "vmix": [("nombre", "Nombre", False), ("ip", "IP", False), ("port", "Port", False),
             ("user", "Usuario", False), ("pass", "Clave", True)],
    "reso": [("nombre", "Nombre", False), ("ip", "IP", False), ("port", "Puerto OSC", False),
             ("efecto", "Efecto OSC", False), ("param", "Parámetro", False), ("fade", "Fade (s)", False)],
    "obs":  [("nombre", "Nombre", False), ("ip", "IP", False), ("port", "WS Port", False),
             ("password", "Password", True)],
}
# Secciones de envío de cada servicio: (título, campos, campos de la salida 2)
_EXTRA_PARAMS = {
    "vmix": [("BIBLIA", [("input_biblia", "Input"), ("field_biblia", "Campo")],
                       [("input_biblia2", "Input"), ("field_biblia2", "Campo")]),
             ("LETRAS", [("input_letras", "Input"), ("field_letras", "Campo")],
                       [("input_letras2", "Input"), ("field_letras2", "Campo")]),
             ("TEXTO", [("input_texto", "Input"), ("field_texto", "Campo")],
                       [("input_texto2", "Input"), ("field_texto2", "Campo")])],
    "reso": [("BIBLIA", [("layer_biblia", "Layer"), ("clip_biblia", "Clip")],
                       [("layer_biblia2", "Layer"), ("clip_biblia2", "Clip")]),
             ("LETRAS", [("layer_letras", "Layer"), ("clip_letras", "Clip")],
                       [("layer_letras2", "Layer"), ("clip_letras2", "Clip")]),
             ("TEXTO", [("layer_texto", "Layer"), ("clip_texto", "Clip")],
                       [("layer_texto2", "Layer"), ("clip_texto2", "Clip")])],
    "obs":  [("BIBLIA", [("source_biblia", "Fuente")], [("source_biblia2", "Fuente")]),
             ("LETRAS", [("source_letras", "Fuente")], [("source_letras2", "Fuente")]),
             ("TEXTO", [("source_texto", "Fuente")], [("source_texto2", "Fuente")])],
}
# Split versículo/cita: (etiqueta, campos)
_EXTRA_SPLIT = {
    "vmix": [("VERSÍCULO →", [("input_versiculo", "Input"), ("field_versiculo", "Campo")]),
             ("CITA →", [("input_cita", "Input"), ("field_cita", "Campo")])],
    "reso": [("VERSÍCULO →", [("layer_versiculo", "Layer"), ("clip_versiculo", "Clip")]),
             ("CITA →", [("layer_cita", "Layer"), ("clip_cita", "Clip")])],
    "obs":  [("VERSÍCULO →", [("source_versiculo", "Fuente")]),
             ("CITA →", [("source_cita", "Fuente")])],
}
_EXTRA_TITULO = {"vmix": "vMIX", "reso": "RESOLUME", "obs": "OBS"}
_MONO = 'font-family: Share Tech Mono; font-size:9px; letter-spacing:2px;'
_GRID_CAMPOS = 'display:grid; grid-template-columns:repeat(auto-fit, minmax(110px,1fr)); gap:6px; width:100%;'

def _tarjeta_pc(servicio, indice=None):
    """Tarjeta de UNA PC: la principal (indice=None) o una extra.

    Devuelve (tarjeta, {clave de configuración: control}). Los controles de la
    principal se devuelven para enlazarlos con el guardado de la sesión; los de
    las extra guardan solos en su propia fila.
    """
    global vmix_ip, vmix_port, vmix_user, vmix_pass, vmix_multi_toggle, vmix_split_toggle
    global reso_ip, reso_port, reso_efecto, reso_param, reso_fade
    global resolume_multi_toggle, reso_split_toggle
    global obs_ip, obs_port, obs_password, obs_salida_unica_toggle, obs_multi_toggle, obs_split_toggle

    principal = indice is None
    pref = _PREF[servicio]
    fila = {} if principal else destinos_extra[servicio][indice]
    controles = {}

    def val(clave):
        """Valor guardado de ESTA PC (sin heredar)."""
        if principal:
            return str(live_state.get(pref + clave, "") or "")
        return str(fila.get(clave, "") or "")

    def marcado(clave):
        """Estado de un interruptor: en la extra, vacío = hereda de la principal."""
        if principal:
            return bool(live_state.get(pref + clave, False))
        v = fila.get(clave, None)
        if v in (None, ""):
            return bool(live_state.get(pref + clave, False))
        return bool(v)

    def campo(clave, etiqueta, password=False):
        """Input de un campo de esta PC (vacío en una extra = hereda)."""
        if principal:
            inp = ui.input(etiqueta, value=val(clave), password=password)
            inp.props('dark filled dense').classes('w-full').on_value_change(save_config)
            controles[pref + clave] = inp
            return inp
        heredado = "" if clave == "nombre" else str(live_state.get(pref + clave, "") or "")
        inp = ui.input(etiqueta, value=val(clave), password=password,
                       placeholder=heredado or None)
        inp.props('dark filled dense').classes('w-full')
        inp.on_value_change(lambda e, s=servicio, ix=indice, k=clave: _guardar_extra(s, ix, k, e.value))
        return inp

    def interruptor(clave, etiqueta):
        sw = ui.switch(etiqueta, value=marcado(clave))
        if principal:
            sw.on_value_change(save_config)
            controles[pref + clave] = sw
        else:
            sw.on_value_change(lambda e, s=servicio, ix=indice, k=clave:
                               _guardar_extra(s, ix, k, bool(e.value)))
        return sw

    def rejilla(campos):
        with ui.element('div').style(_GRID_CAMPOS):
            for clave, etiqueta in campos:
                campo(clave, etiqueta)

    with ui.card().classes('glass p-3').style(
            'display:flex; flex-direction:column; gap:8px; min-width:0;') as card:
        # ── Cabecera: título + prendido/apagado (+ borrar si es una PC extra)
        with ui.row().classes('w-full justify-between items-center').style('flex-wrap:nowrap;'):
            with ui.column().classes('gap-0').style('min-width:0;'):
                titulo = _EXTRA_TITULO[servicio] + ("" if principal else f" · PC {indice + 1}")
                ui.label(titulo).classes('card-title').style(
                    'color: rgba(0,160,255,0.9); white-space:nowrap;')
                ui.label("PC PRINCIPAL" if principal else "PC EXTRA").style(
                    _MONO + ('color: rgba(255,255,255,0.45);' if principal
                             else 'color: rgba(0,220,150,0.8);'))
            with ui.row().classes('items-center gap-1').style('flex-wrap:nowrap;'):
                if principal:
                    sw_on = ui.switch("", value=bool(live_state.get(pref + "enabled", False)))
                    sw_on.on_value_change(save_config)
                    controles[pref + "enabled"] = sw_on
                    sw_on.tooltip(f"Prender/apagar el envío a {_EXTRA_TITULO[servicio]}")
                else:
                    sw_on = ui.switch("", value=bool(fila.get("enabled", True))).props('dense')
                    sw_on.tooltip("Prender/apagar esta PC")
                    sw_on.on_value_change(lambda e, s=servicio, ix=indice: (
                        _guardar_extra(s, ix, "enabled", bool(e.value)),
                        _refrescar_extras(_cliente_actual())))
                    ui.button(icon='delete', on_click=lambda s=servicio, ix=indice: _borrar_extra(s, ix)) \
                        .props('flat dense color=red-5').tooltip("Quitar esta PC")
        ui.separator().style('border-color: rgba(255,255,255,0.07); margin: 2px 0;')

        # ── Nombre + modos de salida
        campo("nombre", "Nombre de la PC")
        fila_multi = ui.row().classes('w-full justify-between items-center').style('flex-wrap:nowrap;')
        with fila_multi:
            ui.label("Múltiples Inputs").style(
                'font-family: Rajdhani; font-size:12px; font-weight:600; '
                'color: rgba(0,220,150,0.85); white-space:nowrap;')
            multi_sw = interruptor("multi", "")
        if servicio == "vmix":
            vmix_multi_toggle = multi_sw if principal else vmix_multi_toggle
        elif servicio == "reso":
            resolume_multi_toggle = multi_sw if principal else resolume_multi_toggle
        else:
            obs_multi_toggle = multi_sw if principal else obs_multi_toggle

        unica_sw = None
        if servicio == "obs":
            with ui.row().classes('w-full justify-between items-center').style('flex-wrap:nowrap;'):
                ui.label("Salida única").style(
                    'font-family: Rajdhani; font-size:12px; font-weight:600; '
                    'color: rgba(180,140,255,0.9); white-space:nowrap;')
                unica_sw = interruptor("salida_unica", "")
                obs_salida_unica_toggle = unica_sw if principal else obs_salida_unica_toggle
            fila_multi.bind_visibility_from(unica_sw, 'value', backward=lambda v: not v)

        # ── Conexión
        ui.label("CONEXIÓN").classes('section-label')
        rejilla([(c, e) for c, e, _p in _EXTRA_CONN[servicio]])
        if principal and servicio == "vmix":
            ui.label("Si vMix tiene clave en Web Controller, cargala acá").style(
                _MONO + 'color: rgba(255,180,80,0.75); white-space: normal; letter-spacing:0;')
        res_lbl = ui.label("").style(_MONO + 'min-height:14px; letter-spacing:0;')
        btn_test = ui.button("⚡ Test conexión").props('flat dense').classes('w-full').style(
            'font-family: Rajdhani; font-size:12px; color: rgba(0,180,255,0.8);')
        if principal:
            _probar = {"vmix": test_vmix, "reso": test_resolume, "obs": test_obs}[servicio]
            btn_test.on_click(lambda b=btn_test, l=res_lbl, f=_probar: f(l, b))
        else:
            btn_test.on_click(lambda s=servicio, ix=indice, l=res_lbl, b=btn_test:
                              test_destino_extra(s, ix, l, b))

        # ── Secciones de envío (OBS: la fuente única reemplaza a las tres)
        bloque_unica = ui.column().classes('w-full').style('gap:6px;')
        bloque_normal = ui.column().classes('w-full').style('gap:10px;')
        if unica_sw is not None:
            bloque_unica.bind_visibility_from(unica_sw, 'value')
            bloque_normal.bind_visibility_from(unica_sw, 'value', backward=lambda v: not v)

        with bloque_unica:
            if servicio == "obs":
                ui.label("FUENTE ÚNICA").classes('section-label')
                rejilla([("source_unica", "Fuente")])

        with bloque_normal:
            for titulo_seccion, campos, campos2 in _EXTRA_PARAMS[servicio]:
                with ui.column().classes('w-full').style('gap:6px; min-width:0;'):
                    ui.label(titulo_seccion).classes('section-label')
                    rejilla(campos)
                    with ui.column().classes('w-full multi-section').style('gap:6px;') \
                            .bind_visibility_from(multi_sw, 'value'):
                        ui.label("+ SALIDA 2").style(
                            _MONO + 'color: rgba(0,220,150,0.6);')
                        rejilla(campos2)
                    if titulo_seccion == "BIBLIA":
                        with ui.row().classes('w-full justify-between items-center') \
                                .style('margin-top:6px; flex-wrap:nowrap;'):
                            ui.label("Split Versículo/Cita").style(
                                'font-family: Rajdhani; font-size:11px; font-weight:600; '
                                'color: rgba(255,180,0,0.85); white-space:nowrap;')
                            split_sw = interruptor("split_biblia", "")
                            if principal:
                                if servicio == "vmix":
                                    vmix_split_toggle = split_sw
                                elif servicio == "reso":
                                    reso_split_toggle = split_sw
                                else:
                                    obs_split_toggle = split_sw
                        with ui.column().classes('w-full').style(
                                'border-left:2px solid rgba(255,180,0,0.3); '
                                'padding-left:8px; gap:6px;') \
                                .bind_visibility_from(split_sw, 'value'):
                            for etiqueta_split, campos_split in _EXTRA_SPLIT[servicio]:
                                ui.label(etiqueta_split).style(
                                    _MONO + 'color: rgba(255,180,0,0.6);')
                                rejilla(campos_split)

        # ── Pie: agregar otra PC (solo en la principal) + ayuda de herencia
        if principal:
            ui.button(f"➕ Agregar PC {_EXTRA_TITULO[servicio]}", on_click=lambda s=servicio: _agregar_extra(s)) \
                .props('flat dense').classes('w-full').style(
                'font-family: Rajdhani; font-size:12px; color: rgba(0,220,150,0.9);')
        else:
            ui.label("Vacío = hereda el valor de la PC principal").style(
                _MONO + 'color: rgba(255,255,255,0.4); white-space: normal; letter-spacing:0;')

    # Guardamos los controles de la principal en las globales que usa el resto
    if principal:
        if servicio == "vmix":
            vmix_ip, vmix_port = controles.get("vmix_ip"), controles.get("vmix_port")
            vmix_user, vmix_pass = controles.get("vmix_user"), controles.get("vmix_pass")
        elif servicio == "reso":
            reso_ip, reso_port = controles.get("reso_ip"), controles.get("reso_port")
            reso_efecto, reso_param = controles.get("reso_efecto"), controles.get("reso_param")
            reso_fade = controles.get("reso_fade")
        else:
            obs_ip, obs_port = controles.get("obs_ip"), controles.get("obs_port")
            obs_password = controles.get("obs_password")
    return card, controles

def _tarjeta_extra_nueva(servicio):
    """Datos iniciales de una PC extra nueva (los campos vacíos heredan)."""
    base = {"enabled": True, "nombre": "", "ip": "", "port": ""}
    if servicio == "obs":
        base["password"] = ""
    return base

def _agregar_extra(servicio):
    destinos_extra.setdefault(servicio, []).append(_tarjeta_extra_nueva(servicio))
    persistir({f"{servicio}_extras": json.dumps(destinos_extra[servicio], ensure_ascii=False)})
    _refrescar_extras()
    ui.notify(f"PC de {_EXTRA_TITULO[servicio]} agregada", type="positive")

def _borrar_extra(servicio, indice):
    lista = destinos_extra.get(servicio, [])
    if 0 <= indice < len(lista):
        lista.pop(indice)
    persistir({f"{servicio}_extras": json.dumps(lista, ensure_ascii=False)})
    _refrescar_extras()

def _guardar_extra(servicio, indice, clave, valor):
    """Guarda un campo de una PC extra (sin reconstruir tarjetas: no corta el foco)."""
    lista = destinos_extra.get(servicio, [])
    if not (0 <= indice < len(lista)):
        return
    if lista[indice].get(clave) == valor:
        return
    lista[indice][clave] = valor
    persistir({f"{servicio}_extras": json.dumps(lista, ensure_ascii=False)})

def main_page():
    global holyrics_ip, holyrics_port, intervalo, auto_start_toggle
    # status_dot, tipo_label y preview_text son PROPIOS de esta sesión: si fueran
    # globales, con dos dispositivos conectados el timer de uno escribiría en los
    # controles del otro.
    global app_port_input

    # Estado PROPIO de esta sesión: cada dispositivo conectado tiene el suyo.
    campos = {}            # clave de config -> control de ESTA sesión
    nav = {"pagina": "home"}   # página visible de ESTA sesión

    ui.add_head_html('''
    <style>
      @import url('https://fonts.googleapis.com/css2?family=Rajdhani:wght@400;600;700&family=Share+Tech+Mono&display=swap');
      body { background: radial-gradient(ellipse at 20% 20%, #0d1b3e 0%, #050d1a 50%, #0a0a0f 100%) !important; background-attachment: fixed !important; font-family: "Rajdhani", sans-serif !important; min-height: 100vh; overflow-x: hidden; }
      body::before { content: ""; position: fixed; inset: 0; background: radial-gradient(circle at 15% 50%, rgba(0,120,255,0.06) 0%, transparent 50%), radial-gradient(circle at 85% 30%, rgba(0,200,150,0.05) 0%, transparent 45%); pointer-events: none; z-index: 0; }
      .q-layout, .q-page-container, .q-page { background: transparent !important; }
      .glass { background: rgba(255,255,255,0.04) !important; backdrop-filter: blur(24px) !important; -webkit-backdrop-filter: blur(24px) !important; border: 1px solid rgba(255,255,255,0.08) !important; box-shadow: 0 8px 40px rgba(0,0,0,0.5), inset 0 1px 0 rgba(255,255,255,0.06) !important; border-radius: 16px !important; }
      .glass-header { background: rgba(10,18,35,0.85) !important; backdrop-filter: blur(30px) !important; border-bottom: 1px solid rgba(255,255,255,0.1) !important; box-shadow: 0 4px 30px rgba(0,0,0,0.6) !important; }
      .glass-preview { background: rgba(0,0,0,0.35) !important; backdrop-filter: blur(20px) !important; border: 1px solid rgba(255,255,255,0.06) !important; box-shadow: inset 0 2px 20px rgba(0,0,0,0.4) !important; border-radius: 16px !important; }
      .section-label { font-family: "Share Tech Mono", monospace !important; font-size: 10px !important; letter-spacing: 3px !important; text-transform: uppercase !important; color: rgba(255,255,255,0.65) !important; padding: 4px 0 8px 0; border-bottom: 1px solid rgba(255,255,255,0.1); margin-bottom: 8px; width: 100%; }
      .card-title { font-family: "Rajdhani", sans-serif !important; font-size: 13px !important; font-weight: 700 !important; letter-spacing: 4px !important; text-transform: uppercase !important; }
      .status-dot-on { background: #00ff88 !important; box-shadow: 0 0 12px #00ff88, 0 0 30px rgba(0,255,136,0.3) !important; }
      .status-dot-off { background: #ff2244 !important; box-shadow: 0 0 8px rgba(255,34,68,0.4) !important; }
      .status-dot-idle { background: rgba(255,255,255,0.25) !important; box-shadow: none !important; }
      .preview-text { font-family: "Rajdhani", sans-serif !important; font-size: 1.5rem !important; font-weight: 600 !important; line-height: 1.3 !important; color: white !important; text-shadow: 0 0 30px rgba(100,180,255,0.2); overflow-wrap: anywhere; }
      .mono { font-family: "Share Tech Mono", monospace !important; }
      .nicegui-input .q-field__control { background: rgba(255,255,255,0.05) !important; border-radius: 8px !important; }
      .nicegui-input .q-field__native { color: #e2e8f0 !important; }
      .q-field--filled .q-field__control:before { background: rgba(255,255,255,0.04) !important; }
      .multi-section { border-left: 2px solid rgba(0,200,150,0.3); padding-left: 10px; margin-top: 4px; }
      .q-drawer { background: rgba(10,18,35,0.92) !important; backdrop-filter: blur(24px) !important; }
      .menu-item { border-radius: 10px; transition: background .15s ease; margin-bottom: 2px; }
      .menu-item:hover { background: rgba(255,255,255,0.06); }
      .menu-item.menu-activo { background: rgba(0,200,150,0.16); box-shadow: inset 0 0 0 1px rgba(0,220,150,0.4); }
      .menu-logo { display:flex; align-items:center; justify-content:center; width:34px; height:34px; }
      .menu-logo-chip { display:flex; align-items:center; justify-content:center; width:34px; height:34px; background:#f2f4f8; border-radius:8px; padding:3px; }
      /* Grilla de las páginas: cada PC es una tarjeta del mismo ancho. La fila
         se reparte entre las tarjetas que hay (1 a 4) y todas se achican con la
         ventana; recién cuando una tarjeta quedaría muy angosta, las columnas
         bajan y las demás pasan a la fila de abajo. Nunca hay una más grande
         que otra: todas las de una sección comparten columnas de 1fr. */
      .hb-grid { display: grid; gap: 14px; align-items: stretch; width: 100%; grid-template-columns: minmax(0, 1fr); }
      .hb-grid > *, .hb-grid .glass { min-width: 0; }
      .hb-c2 { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .hb-c3 { grid-template-columns: repeat(3, minmax(0, 1fr)); }
      .hb-c4 { grid-template-columns: repeat(4, minmax(0, 1fr)); }
      /* Comparador del Inicio: Holyrics crudo a la izquierda, lo que sale a la
         derecha. Se apilan cuando la ventana es angosta. */
      .hb-2col { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; width: 100%; }
      .hb-caja { min-width: 0; background: rgba(0,0,0,0.22); border: 1px solid rgba(255,255,255,0.07); border-radius: 12px; padding: 10px 12px; }
      .hb-crudo { font-family: "Share Tech Mono", monospace !important; font-size: 11px !important; line-height: 1.5 !important; color: rgba(255,255,255,0.6) !important; white-space: pre-wrap; overflow-wrap: anywhere; max-height: 190px; overflow-y: auto; }
      @media (max-width: 900px) { .hb-2col { grid-template-columns: minmax(0, 1fr); } }
      @media (max-width: 1500px) { .hb-c4 { grid-template-columns: repeat(3, minmax(0, 1fr)); } }
      @media (max-width: 1150px) { .hb-c4, .hb-c3 { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
      @media (max-width: 820px)  { .hb-c2, .hb-c3, .hb-c4 { grid-template-columns: minmax(0, 1fr); } }
    </style>
    ''')
    ui.query('body').style('color: white;')

    # ─── BARRA SUPERIOR (siempre visible) ────────────────────────────────
    with ui.header().classes('glass-header').style('padding: 8px 16px;'):
        with ui.row().classes('w-full items-center justify-between').style('flex-wrap: wrap; gap: 12px;'):
            with ui.row().classes('items-center gap-3').style('flex-wrap: nowrap;'):
                menu_btn = ui.button(icon='menu').props('flat round dense').style('color: white;')
                ui.label("HolyBridge Pro").style('font-family: Rajdhani; font-size: 22px; font-weight: 700; letter-spacing: 1.5px; line-height:1;')
            with ui.row().classes('items-center gap-3').style('flex-wrap: wrap; justify-content: flex-end;'):
                def guardar_sesion(notificar=True):
                    # Lee TODOS los controles de ESTA sesión y guarda sólo lo que cambió.
                    cambios = {}
                    for _k, _c in campos.items():
                        if _c is None:
                            continue
                        try:
                            cambios[_k] = _normalizar(_k, _c.value)
                        except Exception:
                            continue
                    _aplicar_cambios(cambios)
                    if notificar:
                        ui.notify(f"Configuración guardada en {os.path.basename(CONFIG_PATH)}", type="positive")
                def guardar_todo():
                    guardar_sesion()
                def iniciar_bridge():
                    guardar_sesion(notificar=False)
                    toggle_bridge(True, silent=False)
                app_port_input = ui.input("Puerto", value=config["app_port"]).props('dark dense filled').classes('w-20').on_value_change(save_config).tooltip("Puerto de la GUI: se aplica al reiniciar")
                status_dot = ui.element('div').classes('w-3 h-3 rounded-full status-dot-idle').tooltip("Verde: Holyrics conectado · Rojo: sin conexión · Gris: bridge apagado")
                dot_label = ui.label("APAGADO").classes('mono').style('font-size:10px; letter-spacing:2px; color: rgba(255,255,255,0.45);')
                ui.button("GUARDAR", on_click=guardar_todo).props('color=blue-8 icon=save dense').style('font-family: Rajdhani; font-weight:700; letter-spacing:2px;')
                btn_start = ui.button("START", on_click=iniciar_bridge).props('color=teal-8 icon=play_arrow dense').style('font-family: Rajdhani; font-weight:700; letter-spacing:2px;')
                btn_stop = ui.button("STOP", on_click=lambda: toggle_bridge(False, silent=False)).props('color=red-9 icon=stop dense').style('font-family: Rajdhani; font-weight:700; letter-spacing:2px;')

    # ─── MENÚ LATERAL ────────────────────────────────────────────────────
    with ui.left_drawer(value=True, bordered=True).props('width=240').classes('glass') as drawer:
        ui.label("SECCIONES").classes('section-label')
        botones_menu = {}
        # clave, etiqueta, logo (None = ícono), ícono, ¿fondo claro para el logo?
        _items_menu = (("home", "Inicio", None, "home", False),
                       ("vmix", "vMix", "/assets/vmix.png", None, True),
                       ("reso", "Resolume", "/assets/resolume.svg", None, False),
                       ("obs", "OBS", "/assets/obs.png", None, True),
                       ("cinetica", "Letras cinéticas", None, "auto_awesome", False))
        for _clave, _etiqueta, _logo, _icono, _fondo in _items_menu:
            _fila = ui.row().classes('w-full items-center gap-3 menu-item').style('cursor:pointer; padding:8px 10px;')
            with _fila:
                if _logo:
                    with ui.element('div').classes('menu-logo-chip' if _fondo else 'menu-logo'):
                        ui.image(_logo).props('fit=contain').style('width:28px; height:28px;')
                else:
                    with ui.element('div').classes('menu-logo'):
                        ui.icon(_icono).style('font-size:22px; color: rgba(255,255,255,0.85);')
                ui.label(_etiqueta).style('font-family: Rajdhani; font-size:15px; letter-spacing:1px;')
            _fila.on('click', lambda e=None, c=_clave: _ir(c))
            botones_menu[_clave] = _fila

        # Letras cinéticas es una sección más: el panel se abre DENTRO de esta
        # página (no en otra pestaña) y se sirve por el mismo puerto de la GUI.
        ui.separator().style('border-color: rgba(255,255,255,0.08); margin: 10px 0;')
        ui.label("Cada PC tiene su tarjeta: conexión, textos y test.").style(
            'font-family: Share Tech Mono; font-size:9px; color: rgba(255,255,255,0.45); white-space: normal;')
    menu_btn.on_click(drawer.toggle)

    # ─── PÁGINAS ─────────────────────────────────────────────────────────
    # Inicio: grilla de tarjetas. vMix / Resolume / OBS: una tarjeta por PC,
    # todas del mismo formato, una al lado de la otra (ver .hb-grid).
    with ui.column().classes('w-full p-4').style('width:100%; margin: 0 auto; align-items: stretch; gap: 12px;'):
        home_box = ui.element('div').classes('w-full hb-grid hb-c4')
        vmix_box = ui.element('div').classes('w-full hb-grid')
        reso_box = ui.element('div').classes('w-full hb-grid')
        obs_box  = ui.element('div').classes('w-full hb-grid')

        # ================= LETRAS CINÉTICAS =================
        # Se embebe el panel del bridge (/config) servido por el proxy: mismo
        # origen, mismo puerto de la GUI, sin pestañas nuevas ni puertos extra.
        cinetica_box = ui.element('div').classes('w-full')
        with cinetica_box:
            with ui.row().classes('w-full items-center gap-3').style('padding: 0 2px 8px;'):
                ui.label("LETRAS CINÉTICAS").classes('card-title').style('color: rgba(120,255,190,0.9);')
                ui.label("Efectos, tipografías y resaltados del overlay web y de NDI.").style(
                    'font-family: Share Tech Mono; font-size:10px; color: rgba(255,255,255,0.5); white-space: normal;')
                ui.space()

                def _recargar_panel():
                    ui.run_javascript(
                        "var f=document.getElementById('hb-cinetica');"
                        "if(f){f.src='/config?r='+Date.now();}")

                ui.button("RECARGAR", on_click=_recargar_panel).props('color=teal-8 icon=refresh dense').style(
                    'font-family: Rajdhani; font-weight:700; letter-spacing:2px;')
                ui.button("ABRIR EN PESTAÑA", on_click=lambda: ui.navigate.to('/config', new_tab=True)).props(
                    'color=blue-8 icon=open_in_new dense').style(
                    'font-family: Rajdhani; font-weight:700; letter-spacing:2px;')
            ui.element('iframe').props('src="/config" frameborder="0"').style(
                'width:100%; height:calc(100vh - 168px); min-height:520px; border:1px solid rgba(255,255,255,0.08);'
                'border-radius:14px; background:#070b14; display:block;').props('id=hb-cinetica')

        def _card(clases='glass p-4'):
            """Tarjeta de la grilla: todas iguales y con la misma separación interna."""
            return ui.card().classes(clases).style('display:flex; flex-direction:column; gap:8px;')

        def _sw_case(evento, otra):
            """MAYÚSCULAS y Minúsculas son excluyentes: prender una apaga la otra."""
            if evento.value and otra.value:
                otra.value = False
            save_config(evento)

        # ================= INICIO =================
        with home_box:
            with _card():
                ui.label("HOLYRICS").classes('card-title').style('color: rgba(0,160,255,0.9);')
                ui.separator().style('border-color: rgba(255,255,255,0.07); margin: 8px 0;')
                holyrics_ip = ui.input("IP", value=config["holyrics_ip"]).props('dark filled dense').classes('w-full').on_value_change(save_config)
                holyrics_port = ui.input("Port", value=config["holyrics_port"]).props('dark filled dense').classes('w-full').on_value_change(save_config)
                intervalo = ui.input("Pool (s)", value=config["intervalo"]).props('dark filled dense').classes('w-full').on_value_change(save_config).tooltip("Segundos entre consultas a Holyrics (mínimo 0.05)")
                with ui.expansion("API Server (respaldo)").classes('w-full').style('font-family: Rajdhani; font-size:11px;'):
                    ui.label("Si la vista de Holyrics falla, el texto se lee por el API Server.").style('font-family: Share Tech Mono; font-size:9px; color: rgba(255,255,255,0.5); white-space: normal;')
                    holyrics_api_port = ui.input("API Port", value=config["holyrics_api_port"]).props('dark filled dense').classes('w-full').on_value_change(save_config).tooltip("Puerto del API Server de Holyrics (por defecto 8091). Vacío = desactivado")
                    holyrics_api_token = ui.input("API Token", value=config["holyrics_api_token"], password=True).props('dark filled dense').classes('w-full').on_value_change(save_config).tooltip("Token creado en Holyrics → Settings → API Server")
                ui.separator().style('border-color: rgba(255,255,255,0.07); margin: 8px 0;')
                detect_result = ui.label("").style('font-family: Share Tech Mono; font-size:10px; min-height:16px;')
                detect_btn = ui.button("🔍 Auto-detectar Holyrics", on_click=lambda: autodetect_holyrics(holyrics_port.value, detect_result, detect_btn)).props('flat dense').classes('w-full').style('font-family: Rajdhani; font-size:12px; color: rgba(0,180,255,0.8); margin-top:4px;')
                ui.separator().style('border-color: rgba(255,255,255,0.07); margin: 8px 0;')
                auto_start_toggle = ui.switch("Auto-Start", value=config["auto_start"]).on_value_change(save_config)
                ui.separator().style('border-color: rgba(255,255,255,0.07); margin: 8px 0;')
                ndi_switch = ui.switch("NDI · letras cinéticas", value=config["ndi_enabled"]).on_value_change(save_config).tooltip("Emite la letra por NDI con los efectos y resaltados del panel de Letras cinéticas")
                ndi_status = ui.label("").classes('mono').style('font-size:9px; color: rgba(255,255,255,0.5); white-space: normal;')
                ui.timer(1.5, lambda: ndi_status.set_text(
                    ("● emitiendo como Kinetic Lyrics" if ndi_estado() == "on" else "○ NDI detenido")
                    + ((" · " + _ndi_error) if _ndi_error else "")))

            with _card():
                ui.label("FORMATO DEL TEXTO").classes('card-title').style('color: rgba(180,140,255,0.9);')
                ui.separator().style('border-color: rgba(255,255,255,0.07); margin: 8px 0;')
                ui.label("Se aplica al texto que sale hacia vMix, Resolume y OBS.").style('font-family: Share Tech Mono; font-size:9px; color: rgba(255,255,255,0.5); white-space: normal;')
                texto_mayusculas_switch = ui.switch("Todo en MAYÚSCULAS", value=config["texto_mayusculas"]).style('font-family: Rajdhani; font-size:13px;').tooltip("Manda el texto tal cual llega de Holyrics pero todo en mayúsculas")
                texto_minusculas_switch = ui.switch("Minúsculas respetando nombres propios", value=config["texto_minusculas"]).style('font-family: Rajdhani; font-size:13px;').tooltip("Pone todo en minúsculas, pero deja los nombres propios y arranca cada frase y cada renglón con mayúscula")
                texto_sin_version_switch = ui.switch("Quitar la versión de la Biblia", value=config["texto_sin_version"]).style('font-family: Rajdhani; font-size:13px;').tooltip('Ej: "Juan 3:16 (RVR1960)", "Juan 3:16 RVR60" y "Juan 3:16 - RV1960" pasan a "Juan 3:16"')
                texto_sin_salto_switch = ui.switch("Sin salto de línea (texto → cita)", value=config["texto_sin_salto"]).style('font-family: Rajdhani; font-size:13px;').tooltip("Une el texto del versículo y la cita en un solo renglón")
                texto_mayusculas_switch.on_value_change(lambda e: _sw_case(e, texto_minusculas_switch))
                texto_minusculas_switch.on_value_change(lambda e: _sw_case(e, texto_mayusculas_switch))
                texto_sin_version_switch.on_value_change(save_config)
                texto_sin_salto_switch.on_value_change(save_config)

            with _card():
                ui.label("ESTADO Y ESTADÍSTICAS").classes('card-title').style('color: rgba(0,220,150,0.9);')
                ui.separator().style('border-color: rgba(255,255,255,0.07); margin: 8px 0;')
                def _stat(titulo):
                    with ui.column().classes('gap-0'):
                        ui.label(titulo).classes('mono').style('font-size:9px; letter-spacing:2px; color: rgba(255,255,255,0.45); white-space: normal;')
                        return ui.label("—").style('font-family: Rajdhani; font-size:16px; font-weight:600;')
                with ui.element('div').style('display:grid; grid-template-columns: repeat(auto-fit, minmax(130px,1fr)); gap:12px; width:100%;'):
                    stat_conexion = _stat("CONEXIÓN")
                    stat_modo = _stat("ESTADO / MODO")
                    stat_envios = _stat("CAMBIOS ENVIADOS")
                    stat_ultima = _stat("ÚLTIMO CAMBIO")
                    stat_uptime = _stat("ENCENDIDO")
                    stat_destinos = _stat("DESTINOS ACTIVOS")

            # ── COMPARADOR: QUÉ LLEGA DE HOLYRICS / QUÉ SALE HACIA LAS PCs ──
            # Dos cajas lado a lado para revisar antes de salir al aire: a la
            # izquierda el texto tal cual lo manda Holyrics (con sus etiquetas),
            # a la derecha el texto ya formateado que se envía a vMix/Resolume/OBS.
            with _card('glass-preview p-5').style('grid-column: 1 / -1;'):
                with ui.row().classes('w-full items-center justify-between').style('gap:10px; flex-wrap:wrap;'):
                    tipo_label = ui.label("— SIN SEÑAL —").classes('mono').style('font-size:10px; letter-spacing:3px; color: rgba(255,255,255,0.3);')
                    dest_label = ui.label("").classes('mono').style('font-size:9px; letter-spacing:2px; color: rgba(255,255,255,0.4);')
                with ui.element('div').classes('hb-2col').style('margin-top:6px;'):
                    with ui.element('div').classes('hb-caja'):
                        ui.label("HOLYRICS · TAL CUAL LLEGA").classes('section-label')
                        crudo_text = ui.label("").classes('hb-crudo')
                    with ui.element('div').classes('hb-caja'):
                        ui.label("SALE ASÍ · FORMATEADO").classes('section-label')
                        preview_text = ui.label("").classes('preview-text').style('min-height: 60px; white-space: pre-line;')
                split_label = ui.label("").classes('mono').style('font-size:10px; letter-spacing:1px; color: rgba(180,140,255,0.85); white-space: pre-line; margin-top: 4px;')
                # ── PRUEBA CONTRA TODAS LAS PCs, SIN ESPERAR A HOLYRICS ──
                # Manda un texto de prueba a cada PC activa y cuenta qué contestó:
                # de paso revisa que los Inputs/Campos configurados existan en ese
                # vMix (o si la fuente existe en ese OBS).
                ui.separator().style('border-color: rgba(255,255,255,0.07); margin: 10px 0 8px 0;')
                # sanitize=False porque el HTML lo armamos nosotros (y el texto que
                # escribe el usuario va escapado). Esta versión de NiceGUI lo exige.
                prueba_panel = ui.html("", sanitize=False).classes('w-full').style('font-family: "Share Tech Mono", monospace; font-size:11px; line-height:1.6;')
                with ui.row().classes('w-full items-center').style('gap:10px; flex-wrap:wrap;'):
                    prueba_input = ui.input("Texto de prueba", value="PRUEBA HOLYBRIDGE").props('dark dense filled').style('min-width: 230px;')
                    prueba_btn = ui.button("PROBAR DESTINOS", icon='bolt',
                                           on_click=lambda: probar_destinos(prueba_input.value, prueba_panel, prueba_btn)).props('color=orange-8 dense').style('font-family: Rajdhani; font-weight:700; letter-spacing:2px;')
                    ui.label("Escribe el texto en cada PC activa y muestra qué contestó cada una.").style('font-family: Share Tech Mono; font-size:9px; color: rgba(255,255,255,0.5); white-space: normal;')

        # ================= vMIX / RESOLUME / OBS =================
        # Cada servicio dibuja: la tarjeta de la PC principal y una tarjeta por
        # cada PC extra, todas iguales y una al lado de la otra.
        def pintar_pcs(servicio, slot):
            slot.clear()
            nuevos = {}
            with slot:
                _carta, ctr = _tarjeta_pc(servicio)
                nuevos.update(ctr)
                for _i in range(len(destinos_extra.get(servicio, []))):
                    _tarjeta_pc(servicio, _i)
            # Una columna por PC, hasta 4: la fila siempre se reparte entera.
            _n = min(1 + len(destinos_extra.get(servicio, [])), 4)
            slot.classes(add=f'hb-c{_n}', remove='hb-c2 hb-c3 hb-c4')
            for _k, _c in nuevos.items():
                try:
                    _c._hb_key = _k
                except Exception:
                    pass
            campos.update(nuevos)
            UI_FIELDS.update(nuevos)

        # ── FOOTER DE LAS SECCIONES ──
        with ui.row().classes('w-full justify-center items-center gap-2').style('margin-top: 4px; padding: 12px 0 6px 0; border-top: 1px solid rgba(255,255,255,0.08);'):
            ui.label("HolyBridge Pro").style('font-family: Rajdhani; font-size:12px; letter-spacing:2px; color: rgba(255,255,255,0.55);')
            ui.label("v6.5.3").classes('mono').style('font-size:11px; letter-spacing:2px; color: rgba(0,220,150,0.85);')

    # ─── NAVEGACIÓN ENTRE PÁGINAS ────────────────────────────────────────
    def _mostrar_pagina():
        actual = nav["pagina"]
        home_box.set_visibility(actual == "home")
        vmix_box.set_visibility(actual == "vmix")
        reso_box.set_visibility(actual == "reso")
        obs_box.set_visibility(actual == "obs")
        cinetica_box.set_visibility(actual == "cinetica")
        for _k2, _b2 in botones_menu.items():
            if _k2 == actual:
                _b2.classes(add='menu-activo')
            else:
                _b2.classes(remove='menu-activo')

    def _ir(destino):
        nav["pagina"] = destino
        _mostrar_pagina()
        # En pantalla chica el menú hamburguesa quedaba abierto tapando la
        # página nueva: se cierra al elegir una sección.
        try:
            drawer.value = False
        except Exception:
            pass

    # Dibujamos las tarjetas de las tres secciones: la PC principal + las extra.
    def _pintar_secciones():
        pintar_pcs("vmix", vmix_box)
        pintar_pcs("reso", reso_box)
        pintar_pcs("obs", obs_box)

    _pintar_secciones()
    _mostrar_pagina()
    _cid_pcs = _cliente_actual()
    if _cid_pcs is not None:
        # Cuando otra PC agrega/borra/prende una PC, esta sesión se redibuja sola.
        _extra_slots[_cid_pcs] = _pintar_secciones

    # ─── PERSISTENCIA ────────────────────────────────────────────────────
    # Enlaza cada control de ESTA sesión con su clave de configuración. El
    # atributo _hb_key deja que save_config guarde exactamente el control que
    # cambió, aunque haya varios dispositivos conectados a la vez.
    # (Los controles de las PCs extra se enlazan solos en _tarjeta_pc.)
    campos.update(
        holyrics_ip=holyrics_ip, holyrics_port=holyrics_port,
        holyrics_api_port=holyrics_api_port, holyrics_api_token=holyrics_api_token,
        intervalo=intervalo,
        auto_start=auto_start_toggle, ndi_enabled=ndi_switch, app_port=app_port_input,
        texto_mayusculas=texto_mayusculas_switch,
        texto_minusculas=texto_minusculas_switch,
        texto_sin_version=texto_sin_version_switch,
        texto_sin_salto=texto_sin_salto_switch,
    )
    for _k, _c in campos.items():
        try:
            _c._hb_key = _k
        except Exception:
            pass
    UI_FIELDS.update(campos)   # referencia para el tray y el diálogo del 401

    # Dejamos las listas de PCs extra en el JSON para que el archivo quede completo.
    persistir({f"{s}_extras": json.dumps(destinos_extra[s], ensure_ascii=False) for s in destinos_extra})

    # Refresco en vivo: si otro dispositivo guarda algo, esta sesión se actualiza sola.
    def _refrescar(cambios):
        for _k, _v in cambios.items():
            _c = campos.get(_k)
            if _c is None:
                continue
            try:
                _c.value = _normalizar(_k, _v)
            except Exception:
                pass
    _cid = _cliente_actual()
    if _cid is not None:
        _sesiones[_cid] = _refrescar
        ui.context.client.on_delete(
            lambda *_: (_sesiones.pop(_cid, None), _extra_slots.pop(_cid, None)))

    # ─── ESTADO EN VIVO DEL HOME ─────────────────────────────────────────
    # El worker deja todo en ui_state, pero NADA lo volcaba a la pantalla: el
    # punto de la barra nunca salía de "off" (siempre rojo) y las tarjetas de
    # estado quedaban en "—" para siempre, aunque el bridge estuviera enviando.
    # Este timer los mantiene al día, en cada sesión, leyendo ui_state.
    _pintado = {"dot": None, "barra": None, "stats": None, "texto": None, "crudo": None,
                "destino": None, "split": None}

    # BIBLE/MUSIC/TEXT son los únicos tipos que viajan a las PCs: en Holyrics
    # cualquier otro tipo (avisos, contador, etc.) no se envía a ningún lado.
    _SECCION_TIPO = {"BIBLE": "BIBLIA", "MUSIC": "LETRAS", "TEXT": "TEXTO"}

    def _formato_activo():
        """Resumen de los switches de formato que están encendidos."""
        partes = []
        if live_state.get("texto_mayusculas"):
            partes.append("MAYÚSCULAS")
        if live_state.get("texto_minusculas"):
            partes.append("minúsculas")
        if live_state.get("texto_sin_version"):
            partes.append("sin versión")
        if live_state.get("texto_sin_salto"):
            partes.append("sin salto")
        return " + ".join(partes) if partes else "sin cambios de formato"

    def _split_activo():
        """Servicios encendidos que mandan versículo y cita por separado."""
        activos = []
        if vmix_active() and live_state.get("vmix_split_biblia"):
            activos.append("vMix")
        if resolume_active() and live_state.get("reso_split_biblia"):
            activos.append("Resolume")
        # Con "salida única" OBS junta versículo y cita en una sola fuente: no es
        # una salida dividida, así que no se anuncia como tal.
        if obs_active() and live_state.get("obs_split_biblia") and not live_state.get("obs_salida_unica"):
            activos.append("OBS")
        return activos

    def _dd_hms(segundos):
        segundos = int(max(0, segundos))
        return f"{segundos // 3600:02d}:{(segundos % 3600) // 60:02d}:{segundos % 60:02d}"

    def _destinos_activos():
        lista = []
        for clave, clave_bool, etiqueta in (("vmix", "vmix_enabled", "vMix"),
                                            ("reso", "resolume_enabled", "Resolume"),
                                            ("obs", "obs_enabled", "OBS")):
            if live_state.get(clave_bool, False):
                pcs = 1 + len(destinos_extra.get(clave, []))
                lista.append(f"{etiqueta}×{pcs}")
        return "  ".join(lista)

    def _pintar_estado():
        encendido = bridge_on.is_set()
        conectado = encendido and ui_state.get("dot") == "on"
        texto = ui_state.get("texto") or ""
        estado = "on" if conectado else ("off" if encendido else "idle")
        if estado != _pintado["dot"]:
            status_dot.classes(add=f"status-dot-{estado}",
                               remove="status-dot-on status-dot-off status-dot-idle")
            # Los botones muestran quién manda ahora: START apagado ya encendido
            # y STOP apagado cuando el bridge está detenido.
            if encendido:
                btn_start.disable()
                btn_stop.enable()
            else:
                btn_start.enable()
                btn_stop.disable()
            _pintado["dot"] = estado
        barra = "ONLINE" if conectado else ("SIN SEÑAL" if encendido else "APAGADO")
        if barra != _pintado["barra"]:
            color_barra = ("rgba(0,255,136,0.95)" if conectado else
                           "rgba(255,75,95,0.95)" if encendido else "rgba(255,255,255,0.45)")
            dot_label.set_text(barra)
            dot_label.style(f"font-size:10px; letter-spacing:2px; color: {color_barra};")
            _pintado["barra"] = barra
        # ── tarjetas de estadísticas ──
        if encendido:
            conexion = "Conectado" if conectado else "Sin conexión"
            color_conexion = "rgba(0,255,136,0.95)" if conectado else "rgba(255,75,95,0.95)"
            # El worker y los enviadores escriben avisos en status_msg ("vMix ...
            # sin conexión", "Error HTTP 401"...). Si hay un aviso se muestra ese
            # (es lo que el operador necesita ver); si no, el modo real.
            msg = ui_state.get("status_msg") or ""
            hay_aviso = bool(msg) and not msg.startswith("MODO:") and "SIN SEÑAL" not in msg
            if hay_aviso:
                modo = msg
            elif texto:
                modo = f"MODO: {ui_state.get('tipo') or '—'}"
            else:
                modo = "— SIN SEÑAL —"
            uptime = _dd_hms(time.time() - (ui_state.get("inicio_ts") or time.time()))
        else:
            conexion, color_conexion = "Bridge apagado", "rgba(255,255,255,0.45)"
            modo = "PRESIONÁ START"
            uptime = "—"
        firma = (conexion, modo, str(ui_state.get("envios", 0)),
                 ui_state.get("ultima") or "—", uptime, _destinos_activos() or "ninguno")
        if firma != _pintado["stats"]:
            stat_conexion.set_text(firma[0])
            stat_conexion.style("font-family: Rajdhani; font-size:16px; font-weight:600; "
                                f"color: {color_conexion}; white-space: normal;")
            stat_modo.set_text(firma[1])
            stat_modo.style("font-family: Rajdhani; font-size:13px; font-weight:600; "
                            "white-space: normal; overflow-wrap: anywhere;")
            stat_envios.set_text(firma[2])
            stat_ultima.set_text(firma[3])
            stat_uptime.set_text(firma[4])
            stat_destinos.set_text(firma[5])
            _pintado["stats"] = firma
        # ── comparador: crudo de Holyrics vs lo que sale formateado ──
        if not encendido:
            aviso, color_aviso = "BRIDGE APAGADO — PRESIONÁ START", "rgba(255,255,255,0.35)"
        elif not conectado:
            aviso = (ui_state.get("status_msg") or "SIN CONEXIÓN").upper()
            color_aviso = "rgba(255,75,95,0.9)"
        elif texto:
            aviso = f"MODO: {ui_state.get('tipo') or '—'}"
            color_aviso = "rgba(0,255,136,0.7)"
        else:
            aviso, color_aviso = "— SIN SEÑAL —", "rgba(255,255,255,0.3)"
        firma_txt = (aviso, texto)
        if firma_txt != _pintado["texto"]:
            tipo_label.set_text(aviso)
            tipo_label.style(f"font-size:10px; letter-spacing:3px; color: {color_aviso}; "
                             "white-space: normal;")
            preview_text.set_text(texto)
            _pintado["texto"] = firma_txt
        crudo = ui_state.get("texto_raw") or ""
        if crudo != _pintado["crudo"]:
            crudo_text.set_text(crudo or "— sin datos de Holyrics —")
            _pintado["crudo"] = crudo
        tipo_actual = ui_state.get("tipo") or ""
        if encendido and conectado and tipo_actual:
            seccion = _SECCION_TIPO.get(tipo_actual)
            destino_txt = (f"SE ENVÍA A: {seccion} · {_formato_activo()}" if seccion
                           else f"TIPO «{tipo_actual}»: NO SE ENVÍA A NINGUNA PC")
        else:
            destino_txt = ""
        if destino_txt != _pintado["destino"]:
            dest_label.set_text(destino_txt)
            _pintado["destino"] = destino_txt
        # Salida dividida: qué texto va a cada campo cuando el switch está puesto.
        divisores = _split_activo() if (encendido and conectado and tipo_actual == "BIBLE") else []
        pie = ("SALIDA DIVIDIDA (" + ", ".join(divisores) + ")\n"
               f"VERSÍCULO: {ui_state.get('versiculo') or '—'}\n"
               f"CITA: {ui_state.get('cita') or '—'}") if divisores else ""
        if pie != _pintado["split"]:
            split_label.set_text(pie)
            split_label.set_visibility(bool(pie))
            _pintado["split"] = pie

    _pintar_estado()
    ui.timer(0.5, _pintar_estado)

    # ─── CLAVE DE vMix CUANDO DEVUELVE 401 ───────────────────────────────
    with ui.dialog() as dlg_401, ui.card().classes('glass p-5').style('min-width:430px; max-width:90vw;'):
        ui.label("vMix rechazó la conexión (HTTP 401)").classes('card-title').style('color: rgba(255,120,120,0.95);')
        ui.label("El Web Controller de vMix tiene contraseña. Escribila para conectarte; "
                 "también podés dejarla vacía en vMix (Settings → Web Controller).").style(
            'font-family: Share Tech Mono; font-size:10px; color: rgba(255,255,255,0.6); white-space: normal;')
        pass_401 = ui.input("Clave de vMix", password=True).props('dark filled dense').classes('w-full')

        def _guardar_401():
            _aplicar_cambios({"vmix_pass": pass_401.value or ""})
            dlg_401.close()
            ui.notify("Clave de vMix guardada", type="positive")
            if ui_state["texto"] and vmix_active():
                enviador_vmix.enviar((ui_state["texto"], ui_state["tipo"]))

        with ui.row().classes('w-full justify-end gap-2'):
            ui.button("Cancelar", on_click=dlg_401.close).props('flat dense')
            ui.button("Guardar y reintentar", on_click=_guardar_401).props('color=teal-8 dense')

    def chequear_401():
        if aviso_401.is_set():
            aviso_401.clear()
            dlg_401.open()

    ui.timer(0.5, chequear_401)

# ─── SYSTEM TRAY ──────────────────────────────────────────────────────
_status_cache = {"valor": None, "ts": 0.0}

def get_holyrics_status():
    """Estado de Holyrics para el menú del tray, con caché corta.

    Sin caché, cada vez que se abría el menú el hilo del tray esperaba hasta 1 s.
    """
    ahora = time.time()
    if _status_cache["valor"] and ahora - _status_cache["ts"] < 2.0:
        return _status_cache["valor"]
    try:
        ip = holyrics_ip.value if holyrics_ip else config["holyrics_ip"]
        port = holyrics_port.value if holyrics_port else config["holyrics_port"]
        r = requests.get(f"http://{ip}:{port}/view/text.json", timeout=1.0)
        estado = "✓ Conectado" if r.status_code == 200 else f"✗ HTTP {r.status_code}"
    except Exception:
        estado = "✗ Sin conexión"
    _status_cache["valor"] = estado
    _status_cache["ts"] = time.time()
    return estado

def tray_open_gui(icon, item):
    webbrowser.open(f"http://localhost:{APP_PORT}")

def tray_toggle_vmix(icon, item):
    activo = not vmix_active()
    set_vmix_active(activo)
    guardar_clave("vmix_enabled", activo)
    if not activo: enviador_vmix.enviar(None)   # la limpieza va por la misma cola
    icon.update_menu()

def tray_toggle_resolume(icon, item):
    activo = not resolume_active()
    set_resolume_active(activo)
    guardar_clave("resolume_enabled", activo)
    if not activo: enviador_resolume.enviar(None)
    icon.update_menu()

def tray_toggle_obs(icon, item):
    activo = not obs_active()
    set_obs_active(activo)
    guardar_clave("obs_enabled", activo)
    if not activo: enviador_obs.enviar(None)
    icon.update_menu()

def revisar_puerto():
    """Devuelve 'libre', 'instancia' (ya corre este mismo bridge) u 'ocupado' (otro programa)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.3)
            if sock.connect_ex(("127.0.0.1", APP_PORT)) != 0:
                return "libre"
        try:
            html_pagina = requests.get(f"http://127.0.0.1:{APP_PORT}/", timeout=2).text
        except Exception:
            return "ocupado"
        return "instancia" if "Holyrics Bridge" in html_pagina else "ocupado"
    except Exception:
        return "ocupado"

def tray_exit(icon, item):
    global running
    running = False
    guardar_desde_ui()
    try:
        ndi_detener()
    except Exception:
        pass
    try:
        app.shutdown()
    except:
        pass
    try:
        icon.visible = False
    except:
        pass
    icon.stop()
    time.sleep(0.5)
    os._exit(0)

def setup_tray():
    estado_puerto = revisar_puerto()
    if estado_puerto == "instancia":
        # Doble clic en el .exe estando ya abierto: mostramos la ventana que ya existe
        print("⚠️ Holyrics Bridge ya está abierto: abro la ventana existente")
        webbrowser.open(f"http://localhost:{APP_PORT}")
        return
    if estado_puerto == "ocupado":
        print(f"⚠️ El puerto {APP_PORT} lo está usando otro programa: cambiá 'Puerto' en la GUI"
              f" (o cerrá ese programa) antes de abrir el bridge")

    # Icono
    image = None
    base_path = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    for ico_path in [
        os.path.join(base_path, "HolyBridge.ico"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "HolyBridge.ico"),
    ]:
        if os.path.exists(ico_path):
            try:
                img = Image.open(ico_path)
                image = img.convert("RGBA").resize((64, 64), Image.LANCZOS)
                break
            except:
                pass
    if image is None:
        image = Image.new('RGB', (64, 64), color=(0, 100, 200))

    menu = pystray.Menu(
        pystray.MenuItem(lambda item: f"Status: {get_holyrics_status()}", lambda item: None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Abrir GUI", tray_open_gui),
        pystray.MenuItem("vMix Activo", tray_toggle_vmix, checked=lambda item: vmix_active()),
        pystray.MenuItem("Resolume Activo", tray_toggle_resolume, checked=lambda item: resolume_active()),
        pystray.MenuItem("OBS Activo", tray_toggle_obs, checked=lambda item: obs_active()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Salir", tray_exit)
    )

    global tray_icon
    icon = pystray.Icon("HolyBridgePro", image, "HolyBridge Pro", menu=menu)
    tray_icon = icon
    tray_thread = threading.Thread(target=icon.run_detached, daemon=True)
    tray_thread.start()

    # Reconciliamos el registro en cada arranque: si el .exe se movió o se
    # renombró, la entrada Run apuntaba a una ruta muerta y el autoarranque
    # "no hacía nada". Con el switch apagado se limpia una entrada vieja.
    set_windows_autostart(bool(config["auto_start"]))

    # Arranque solo: destinos configurados + Holyrics + un destino que responda.
    threading.Thread(target=autoarm_worker, daemon=True, name="autoarm").start()

    if config["auto_start"]:
        def delayed_autostart():
            time.sleep(2.0)
            toggle_bridge(True, silent=True)
        threading.Thread(target=delayed_autostart, daemon=True).start()

    ui.run(
        title="HolyBridge Pro",
        port=APP_PORT,
        reload=False,
        dark=True,
        show=False,
        native=False,
        root=main_page,
        favicon=os.path.join(base_path, "HolyBridge.ico") if os.path.exists(os.path.join(base_path, "HolyBridge.ico")) else None
    )

if __name__ == "__main__":
    setup_tray()
