@echo off
title Letras Cineticas - NDI
rem Lanza el emisor NDI con Python 3.9 (es la version donde anda NDI).
rem Ejemplo:  run-ndi.bat --effect bounce --size 140 --hl "paz,cristo"

py -3.9 "%~dp0kinetic_ndi.py" %*

if errorlevel 1 (
  echo.
  echo No se pudo iniciar. Si falta algo, ejecuta:
  echo    py -3.9 -m pip install -r "%~dp0requirements-3.9.txt"
  pause
)
