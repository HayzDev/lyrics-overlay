"""Single-file installer (LyricsOverlay-Setup.exe): carries LyricsOverlay-Setup.zip and runs its install.ps1.
Built by build.cmd; uses plain Windows message boxes so it stays small."""
import ctypes
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

TITLE = "Lyrics Overlay Setup"
MB_OKCANCEL, MB_ICONERROR, MB_ICONINFORMATION, IDOK = 0x1, 0x10, 0x40, 1


def box(text, flags):
    return ctypes.windll.user32.MessageBoxW(None, text, TITLE, flags)


def main():
    payload = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "LyricsOverlay-Setup.zip"
    if box("Install Lyrics Overlay?\n\nIt installs for your user only (no admin needed), adds a Start menu entry "
           "and starts right away.", MB_OKCANCEL | MB_ICONINFORMATION) != IDOK:
        return
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        with zipfile.ZipFile(payload) as z:
            z.extractall(tmp)
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                            str(Path(tmp) / "install.ps1")],
                           capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
    if r.returncode:
        box("Installation failed.\n\n" + (r.stderr or r.stdout)[-1500:], MB_ICONERROR)
    else:
        box("Lyrics Overlay is installed and running.\n\nSettings are in its tray icon (the green ♪). "
            "You can find it in the Start menu, and uninstall it from Settings > Apps.", MB_ICONINFORMATION)


if __name__ == "__main__":
    main()
