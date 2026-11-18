# -*- mode: python ; coding: utf-8 -*-
# ─── HolyBridgePro-v6.4.3 ───────────────────────────────────────────────
# Compilar con:
#   python -m PyInstaller HolyBridgeProv6.4.3.spec --noconfirm
#
# v6.4.3 — autoarranque corregido:
#   * El switch "Auto-Start" ahora registra la app en
#     HKCU\Software\Microsoft\Windows\CurrentVersion\Run (se abre sola con Windows)
#     además de arrancar el bridge al iniciar.
#   * En cada arranque se reconcilia esa clave: si el .exe se movió o renombró,
#     la ruta muerta se corrige; con el switch apagado se limpia la entrada.
#   * bool("0") dejó de interpretarse como True (el switch ya no puede quedar
#     encendido solo).
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
    name='HolyBridgeProv6.4.3',
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
