# -*- mode: python ; coding: utf-8 -*-
# ─── HolyBridgePro-v6.5.4 ────────────────────────────────────────────────
# Compilar con:
#   python -m PyInstaller HolyBridgeProv6.5.4.spec --noconfirm
#
# v6.5 — salida NDI de las letras cinéticas:
#   * Switch "NDI · letras cinéticas" en la tarjeta HOLYRICS (Inicio): arranca
#     y para el emisor desde la GUI, con estado en vivo.
#   * El emisor (ndi/kinetic_ndi.py) toma texto del bridge y la configuración
#     del panel de Letras cinéticas: mismo efecto, tipografía, case y modo de
#     resaltado que la vista web.
#   * Se lanza con Python 3.9 (donde anda NDI) como proceso aparte.
#   * v6.4.3 — autoarranque con Windows, ítem de menú de Letras cinéticas.
#
# - La carpeta de NiceGUI se resuelve con "import nicegui" (sin rutas fijas).
# - No se empaqueta ninguna credencial: la app sólo habla con Holyrics,
#   vMix, Resolume y OBS por HTTP/OSC locales.
# - Un solo archivo (.exe), sin consola, con icono.
import os

import nicegui  # sirve para ubicar los archivos del frontend de NiceGUI

datas = [
    (os.path.dirname(nicegui.__file__), 'nicegui'),
    ('HolyBridge.ico', '.'),
    ('assets', 'assets'),   # logos de vMix / Resolume / OBS que usa el menú
]

a = Analysis(
    ['HolyBridgeProV6.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='HolyBridgeProv6.5.4',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['HolyBridge.ico'],
)
