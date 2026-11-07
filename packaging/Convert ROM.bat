@echo off
rem Drag .gb, .gbc or .zip ROMs onto this file: GAME.gba is written next to each (gbforge convert).
if "%~1"=="" goto ask
"%~dp0gbforge.exe" convert %*
goto end
:ask
echo Drag a Game Boy ROM (.gb, .gbc or .zip) onto "Convert ROM.bat",
echo or drag it into this window now and press Enter.
set /p "ROM=> "
if not defined ROM goto end
"%~dp0gbforge.exe" convert %ROM%
:end
echo.
pause
