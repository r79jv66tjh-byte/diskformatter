"""DiskFormatter – Laufwerke formatieren und vorher sicher mit Nullen überschreiben.

Startet sich bei Bedarf per UAC mit Administratorrechten neu. Mit --no-elevate bleibt es
ohne Adminrechte (nur Ansicht).
"""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from diskformatter import elevate, ps  # noqa: E402


def main():
    if not ps.is_admin() and "--no-elevate" not in sys.argv:
        exe = sys.executable
        if exe.lower().endswith("python.exe") and os.path.exists(exe[:-10] + "pythonw.exe"):
            exe = exe[:-10] + "pythonw.exe"
        try:
            elevate.run_elevated(exe, [str(Path(__file__).resolve()), "--no-elevate"],
                                 cwd=str(HERE), wait=False, show=1)
            return
        except OSError:
            pass   # UAC abgelehnt -> ohne Adminrechte weiter (nur Ansicht)
    from diskformatter.gui import main as gui_main
    gui_main()


if __name__ == "__main__":
    main()
