@echo off
title Refractory Capture Server
cd /d "%~dp0"
echo.
echo  Iniciando servidor de captura...
echo  Mantene esta ventana abierta mientras uses la app del celular.
echo.
python server.py --host 0.0.0.0 --port 5005
pause
