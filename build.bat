@echo off
echo Installing dependencies...
pip install -r requirements.txt
echo.
echo Building exe...
pyinstaller --onefile --windowed --name "SysInfoTool" main.py
echo.
echo Done! Check dist\SysInfoTool.exe
pause
