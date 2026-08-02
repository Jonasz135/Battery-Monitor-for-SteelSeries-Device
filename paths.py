"""Gdzie program trzyma swoje pliki - dane uzytkownika i zasoby wbudowane.

Ten modul istnieje z powodu, ktory pojawia sie dopiero po spakowaniu programu
do pliku .exe. PyInstaller w trybie jednoplikowym rozpakowuje zawartosc do
katalogu tymczasowego i **usuwa go przy zamknieciu programu**. Katalog obok
"pliku programu" nie jest wiec miejscem, w ktorym cokolwiek przetrwa: historia
baterii zbierana godzinami przepadalaby przy kazdym zamknieciu okna, a program
po restarcie zaczynalby szacowanie od zera.

Dlatego dane i zasoby maja tu dwie osobne sciezki:

  - **dane uzytkownika** (historia baterii, wybor sprzetu) ida do
    %APPDATA%\\BatteryMonitor. Ten katalog jest wlasnoscia uzytkownika, wiec
    zapis nie wymaga uprawnien administratora nawet wtedy, gdy program lezy
    w Program Files.
  - **zasoby tylko do czytania** (ikona, logo) czytamy z katalogu, do ktorego
    PyInstaller je rozpakowal, a w wersji uruchamianej ze zrodel - z katalogu
    projektu.

Ta sama sciezka danych obowiazuje w obu trybach uruchomienia. Gdyby wersja ze
zrodel trzymala dane obok skryptu, a wersja spakowana w %APPDATA%, to przejscie
na .exe wygladaloby jak utrata calej historii.
"""

import os
import shutil
import sys

# Nazwa katalogu w %APPDATA%. Bez spacji, bo trafia tez do wpisu w rejestrze
# i do sciezek w komunikatach.
APP_DIR_NAME = "BatteryMonitor"

_SOURCE_DIR = os.path.dirname(os.path.abspath(__file__))

_data_dir = None


def is_frozen():
    """Czy program dziala jako spakowany .exe, a nie jako skrypt Pythona."""
    return bool(getattr(sys, "frozen", False))


def program_path():
    """Sciezka, ktora trzeba uruchomic, by wystartowac ten program.

    W wersji spakowanej to sam .exe. W wersji ze zrodel - interpreter Pythona
    wraz ze skryptem, bo sciezka do app.py nie jest wykonywalna sama z siebie.
    Uzywa tego wpis autostartu.
    """
    if is_frozen():
        return (sys.executable,)

    # pythonw.exe zamiast python.exe: ten sam interpreter, ale bez okna konsoli
    # migajacego przy kazdym starcie systemu.
    interpreter = sys.executable
    windowless = os.path.join(os.path.dirname(interpreter), "pythonw.exe")
    if os.path.isfile(windowless):
        interpreter = windowless

    return (interpreter, os.path.join(_SOURCE_DIR, "app.py"))


def data_dir():
    """Katalog na dane uzytkownika, utworzony przy pierwszym uzyciu.

    Gdy %APPDATA% jest niedostepne albo nie da sie tam pisac, wracamy do
    katalogu programu. Utrata historii jest mniej szkodliwa niz program, ktory
    nie chce sie uruchomic.
    """
    global _data_dir
    if _data_dir is not None:
        return _data_dir

    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    candidate = os.path.join(base, APP_DIR_NAME)

    try:
        os.makedirs(candidate, exist_ok=True)
        _data_dir = candidate
    except OSError:
        _data_dir = _SOURCE_DIR

    return _data_dir


def data_file(name):
    """Pelna sciezka pliku danych, z jednorazowym przejeciem starej lokalizacji.

    Wczesniejsze wersje trzymaly dane obok skryptu. Gdy taki plik tam jeszcze
    lezy, a w nowym miejscu go nie ma, kopiujemy go - inaczej przejscie na wersje
    spakowana wygladaloby jak utrata calej zebranej historii. Kopiujemy, a nie
    przenosimy, zeby powrot do starszej wersji programu wciaz mial swoje dane.
    """
    target = os.path.join(data_dir(), name)

    if not os.path.exists(target):
        _adopt_from_source(name, target)

    return target


def _adopt_from_source(name, target):
    source = os.path.join(_SOURCE_DIR, name)

    if os.path.abspath(source) == os.path.abspath(target):
        # Zapasowa sciezka danych pokrywa sie z katalogiem programu.
        return
    if not os.path.isfile(source):
        return

    try:
        shutil.copy2(source, target)
    except OSError:
        # Brak kopii oznacza tylko utrate historii, wiec nie przerywamy startu.
        pass


def resource_path(*parts):
    """Sciezka do zasobu dolaczonego do programu (ikona, logo).

    PyInstaller rozpakowuje dolaczone pliki do katalogu wskazanego przez
    sys._MEIPASS; przy uruchomieniu ze zrodel tym katalogiem jest projekt.
    """
    base = getattr(sys, "_MEIPASS", _SOURCE_DIR)
    return os.path.join(base, *parts)


if __name__ == "__main__":
    # python paths.py - podglad, gdzie program bedzie szukal swoich plikow.
    print(f"tryb:        {'.exe (spakowany)' if is_frozen() else 'zrodla'}")
    print(f"dane:        {data_dir()}")
    print(f"zasoby:      {resource_path('assets')}")
    print("polecenie:   " + " ".join(f'"{part}"' for part in program_path()))
