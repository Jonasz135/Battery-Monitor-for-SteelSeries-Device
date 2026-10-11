"""Budowanie BatteryMonitor.exe.

Uruchomienie:
    python build.py           - zbuduj
    python build.py --clean   - zbuduj od zera, ignorujac cache PyInstallera
    python build.py --distpath dist/updated --workpath build/updated
                             - zbuduj osobno, gdy dotychczasowy .exe dziala

Skrypt istnieje zamiast samego "pyinstaller BatteryMonitor.spec" z dwoch powodow:
sprawdza, czy sa wszystkie zaleznosci i czy ikona jest zbudowana (bez niej
PyInstaller przerywa z bledem, ktory nie mowi wprost, czego brakuje), oraz
wypisuje na koniec sciezke i rozmiar gotowego pliku.
"""

import os
import argparse
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
        except (ImportError, OSError):
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clean", action="store_true", help="ignore PyInstaller cache")
    parser.add_argument("--distpath", default="dist", help="output directory relative to the project")
    parser.add_argument("--workpath", default="build", help="build cache directory relative to the project")
    arguments = parser.parse_args(argv)
    if not check_dependencies() or not check_icon():
        return 1

    destination = os.path.abspath(os.path.join(_HERE, arguments.distpath))
    work = os.path.abspath(os.path.join(_HERE, arguments.workpath))
    result_path = os.path.join(destination, "BatteryMonitor.exe")
    command = [
        sys.executable, "-m", "PyInstaller", SPEC, "--noconfirm",
        "--distpath", destination, "--workpath", work,
    ]
    if arguments.clean:
        command.append("--clean")
        # PyInstaller czysci wlasny cache. Nie usuwamy calego katalogu build,
        # ktory moze zawierac tez inne pliki uzytkownika.

    print("Budowanie: " + " ".join(command))
    result = subprocess.run(command, cwd=_HERE, check=False)
    if result.returncode != 0:
        print("PyInstaller zakonczyl sie bledem.")
        return result.returncode

    if not os.path.isfile(result_path):
        print(f"Budowanie sie udalo, ale nie ma pliku {result_path}.")
        return 1

    size_mb = os.path.getsize(result_path) / (1024 * 1024)
    print()
    print(f"Gotowe: {result_path}  ({size_mb:.1f} MB)")
    print("Skopiuj ten plik gdziekolwiek chcesz - dane trafiaja do %APPDATA%.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
