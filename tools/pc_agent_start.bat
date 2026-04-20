@echo off
cd /d "%~dp0..\agents\pc_agent"

:: 检查 uv 是否安装
where uv >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 uv，请先安装：
    echo   powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
    pause
    exit /b 1
)

:: 创建虚拟环境（已存在则跳过）
if not exist ".venv" (
    echo [PC Agent] 创建虚拟环境...
    uv venv .venv
)

:: 安装依赖
echo [PC Agent] 同步依赖...
uv pip install -r requirements.txt

:: 启动
echo [PC Agent] 启动 LangGraph 多智能体...
echo.
uv run python main.py
pause
