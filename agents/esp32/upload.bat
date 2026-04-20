@echo off
REM ============================================================
REM  SSM ESP32 — Upload all MicroPython files via mpremote
REM  Requires: pip install mpremote
REM  Or use Thonny File Manager (see README below)
REM ============================================================

echo SSM ESP32 MicroPython Uploader
echo.

REM Auto-detect COM port (adjust if needed)
set PORT=COM7

REM Check if mpremote is available
where mpremote >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] mpremote not found.
    echo Install it with: pip install mpremote
    echo Or upload manually via Thonny File Manager ^(see instructions below^)
    echo.
    goto thonny_instructions
)

echo Uploading to ESP32 on %PORT%...
echo.

mpremote connect %PORT% cp config.py         :config.py
mpremote connect %PORT% cp ism.py            :ism.py
mpremote connect %PORT% cp bsm.py            :bsm.py
mpremote connect %PORT% cp mqtt_client.py    :mqtt_client.py
mpremote connect %PORT% cp trigger_map.py    :trigger_map.py
mpremote connect %PORT% cp local_rules.py    :local_rules.py
mpremote connect %PORT% cp agent_manifest.py :agent_manifest.py
mpremote connect %PORT% cp boot.py           :boot.py
mpremote connect %PORT% cp main.py           :main.py

echo.
echo Upload complete! Resetting ESP32...
mpremote connect %PORT% reset

goto end

:thonny_instructions
echo ============================================================
echo  THONNY 手动上传步骤（不使用 mpremote）:
echo ============================================================
echo  1. 打开 Thonny，连接 ESP32（右下角选择解释器：MicroPython ESP32）
echo  2. 菜单 View → Files，打开文件面板
echo  3. 左侧面板导航到 SSM\agents\esp32\
echo  4. 按顺序上传以下文件（右键 → Upload to /）:
echo     config.py
echo     ism.py
echo     bsm.py
echo     mqtt_client.py
echo     trigger_map.py
echo     local_rules.py
echo     agent_manifest.py
echo     boot.py
echo     main.py
echo  5. 上传完成后按 ESP32 上的 Reset 按钮，或 Thonny 菜单 Run → Stop/Restart
echo ============================================================

:end
