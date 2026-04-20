@echo off
REM Start HTTP server for Phone PWA development
REM Access from phone: http://<your-laptop-IP>:8080

set SSM_ROOT=%~dp0..
echo Starting PWA development server on port 8080...
echo.
echo Find your laptop IP:
ipconfig | findstr "IPv4"
echo.
echo Open on phone: http://10.193.37.44:8080
echo Press Ctrl+C to stop.
echo.

cd /d "%SSM_ROOT%\agents\phone"
call D:\software\Anaconda\condabin\conda.bat activate base
python -m http.server 8080
pause
