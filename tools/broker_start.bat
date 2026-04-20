@echo off
REM Start Mosquitto broker for SSM project
REM Run from the SSM project root directory

set SSM_ROOT=%~dp0..
echo Starting Mosquitto broker...
echo TCP:       mqtt://localhost:1883  (for ESP32)
echo WebSocket: ws://localhost:9001    (for Phone PWA)
echo.

"D:\software\mosquitto\mosquitto.exe" -c "%SSM_ROOT%\broker\mosquitto.conf" -v
