@echo off
rem Builds release\ (app + installer scripts), LyricsOverlay-Setup.zip and LyricsOverlay-Setup.exe.
setlocal
cd /d "%~dp0"
python lyrics_overlay.py --selftest || exit /b 1
python lyrics_overlay.py --make-icon icon.ico || exit /b 1
python -m PyInstaller --noconfirm --clean --windowed --name LyricsOverlay --icon icon.ico ^
  --collect-submodules winrt lyrics_overlay.py || exit /b 1
if exist release rmdir /s /q release
mkdir release
xcopy /e /i /q dist\LyricsOverlay release\app >nul
rem trim Qt parts this app never loads (~45 MB): software OpenGL, QML/Quick, PDF, virtual keyboard, network
set Q=release\app\_internal\PySide6
del /q %Q%\opengl32sw.dll %Q%\Qt6Quick*.dll %Q%\Qt6Qml*.dll %Q%\Qt6Pdf.dll %Q%\Qt6VirtualKeyboard*.dll ^
  %Q%\Qt6Network.dll %Q%\plugins\imageformats\qpdf.dll 2>nul
for %%d in (translations plugins\platforminputcontexts plugins\networkinformation plugins\tls) do (
  if exist %Q%\%%d rmdir /s /q %Q%\%%d
)
for %%f in (Install.cmd install.ps1 uninstall.ps1) do copy /y installer\%%f release >nul
python lyrics_overlay.py --version > release\version.txt
powershell -NoProfile -Command "Compress-Archive -Force -Path release\* -DestinationPath LyricsOverlay-Setup.zip"
rem single-file installer: the zip embedded in a small exe that runs install.ps1
python -m PyInstaller --noconfirm --onefile --windowed --name LyricsOverlay-Setup --icon "%~dp0icon.ico" ^
  --add-data "%~dp0LyricsOverlay-Setup.zip;." --distpath . --workpath build\setup --specpath build\setup ^
  installer\setup_exe.py || exit /b 1
echo.
echo Built: LyricsOverlay-Setup.exe, LyricsOverlay-Setup.zip and release\
