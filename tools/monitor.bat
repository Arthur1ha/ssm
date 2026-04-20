@echo off
echo Monitoring all SSM traffic (ssm/#)...
echo Press Ctrl+C to stop.
echo.
"D:\software\mosquitto\mosquitto_sub.exe" -h 10.193.37.44 -p 1883 -v -t "ssm/#"
pause
