"""Budowanie BatteryMonitor.exe.

Uruchomienie:
    python build.py           - zbuduj
    python build.py --clean   - zbuduj od zera, ignorujac cache PyInstallera

Skrypt istnieje zamiast samego "pyinstaller BatteryMonitor.spec" z dwoch powodow:
sprawdza, czy sa wszystkie zaleznosci i czy ikona jest zbudowana (bez niej
PyInstaller przerywa z bledem, ktory nie mowi wprost, czego brakuje), oraz
wypisuje na koniec sciezke i rozmiar gotowego pliku.
"""

import os
import shutil
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))

SPEC = os.path.join(_HERE, "BatteryMonitor.spec")
ICON = os.path.join(_HERE, "assets", "icon.ico")
RESULT = os.path.join(_HERE, "dist", "BatteryMonitor.exe")

# Moduly, bez ktorych program sie nie zbuduje albo nie uruchomi.
REQUIRED = ("PyInstaller", "customtkinter", "pystray", "PIL", "requests", "hid")


def check_dependencies():
    missing = []
    for name in REQUIRED:
        try:
            __import__(name)
        except ImportError:
            missing.append(name)

    if missing:
        print("Brak modulow: " + ", ".join(missing))
        print("Zainstaluj je: python -m pip install -r requirements.txt pyinstaller")
        return False

    return True


def check_icon():
    """Ikona musi istniec przed budowaniem - PyInstaller nie zbuduje bez niej."""
    if os.path.isfile(ICON):
        return True

    print(f"Brak ikony {ICON}.")
    print("Zbuduj ja najpierw: python make_icons.py")
    return False


def main():
    if not check_dependencies() or not check_icon():
        return 1

    command = [sys.executable, "-m", "PyInstaller", SPEC, "--noconfirm"]
    if "--clean" in sys.argv:
        command.append("--clean")
        # Katalog build trzyma cache analizy importow; przy zmianie zaleznosci
        # potrafi zostawic stary wynik.
        shutil.rmtree(os.path.join(_HERE, "build"), ignore_errors=True)

    print("Budowanie: " + " ".join(command))
    result = subprocess.run(command, cwd=_HERE, check=False)
    if result.returncode != 0:
        print("PyInstaller zakonczyl sie bledem.")
        return result.returncode

    if not os.path.isfile(RESULT):
        print(f"Budowanie sie udalo, ale nie ma pliku {RESULT}.")
        return 1

    size_mb = os.path.getsize(RESULT) / (1024 * 1024)
    print()
    print(f"Gotowe: {RESULT}  ({size_mb:.1f} MB)")
    print("Skopiuj ten plik gdziekolwiek chcesz - dane trafiaja do %APPDATA%.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
