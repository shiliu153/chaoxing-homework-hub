@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist "config.yaml" (
    echo [错误] 未找到 config.yaml，请先执行：
    echo        copy config.example.yaml config.yaml
    pause
    exit /b 1
)
python -m cxreminder run -c config.yaml
pause
