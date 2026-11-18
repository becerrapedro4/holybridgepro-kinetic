# HolyBridge Pro — Kinetic

Letras cinéticas en vivo desde **Holyrics**, con dos salidas idénticas: overlay web
para OBS / vMix y emisor **NDI** (RGBA con alpha).

- Bridge Node con push por SSE: lee `GET /view/text.json` de Holyrics cada 60 ms.
- Overlay con 18 animaciones, 8 tipografías + fuentes instalables, switch de
  MAYÚSCULAS/minúsculas/Camel Case, 7 modos de resaltado y ajuste automático al cuadro.
- Panel de configuración en `/config`, accesible desde el celular u otra PC de la red.
- Emisor NDI compilado (`ndi/HolyBridgeNDI.exe`): mismos efectos, tipografías y
  resaltados que la web, sin necesidad de Python instalado.
- GUI de escritorio (NiceGUI + PyInstaller) con switch de NDI y autoarranque.

## Uso rápido

1. Instalar con `dist/HolyBridge-6.5.2-Setup.exe` (o descomprimir el portable).
2. Doble clic en **HolyBridge**.
3. OBS / vMix → fuente navegador → `http://127.0.0.1:7333/` (1920×1080, fondo transparente).
4. Panel: `http://127.0.0.1:7333/config`.
5. NDI: switch **NDI · letras cinéticas** → fuente `Kinetic Lyrics` en OBS/vMix.

La IP de Holyrics se configura en `config.json` (`holyrics.ip`) o con la variable
`HOLYRICS_IP`. El bridge escucha en `0.0.0.0:7333` (`host`/`port` en `config.json`,
o `BRIDGE_HOST` / `BRIDGE_PORT`).

## Estructura

```
server.js              bridge HTTP + SSE + panel + subida de fuentes
lib/holyrics.js        sondeo de Holyrics y armado del texto
public/                overlay (index.html, overlay.css/js) y panel (/config)
ndi/kinetic_ndi.py     emisor NDI (Pillow + NDIlib, Python 3.9)
ndi/HolyBridgeNDI.exe  emisor NDI ya compilado
holybridge-src/        GUI de escritorio (NiceGUI) y .spec de PyInstaller
installer/             script de Inno Setup del instalador
tools/                 demo, medición de latencia y de fluidez
```

## Desarrollo

```bash
node server.js                  # bridge en http://127.0.0.1:7333
node tools/demo.js              # texto de prueba
node tools/medir-fluidez.mjs "http://127.0.0.1:7333/?effect=pop"   # FPS por CDP
py -3.9 ndi/kinetic_ndi.py --preview salida.png --text "PAZ"
```

Compilar el emisor NDI:

```bash
py -3.9 -m PyInstaller --onefile --name HolyBridgeNDI --collect-all NDIlib \
  --collect-all numpy --hidden-import numpy ndi/kinetic_ndi.py
```

Compilar el instalador (Inno Setup 7):

```bash
"C:\Program Files\Inno Setup 7\ISCC.exe" installer\HolyBridgeSetup.iss
```

Los paquetes grandes (`portable/`, `dist/`, `*.zip`) no se versionan porque superan
el límite de 100 MB por archivo de GitHub.
