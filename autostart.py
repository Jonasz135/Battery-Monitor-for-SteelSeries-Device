"""Uruchamianie programu razem z Windows.

Wpis trafia do klucza rejestru HKEY_CURRENT_USER, a nie do HKEY_LOCAL_MACHINE
ani do Harmonogramu zadan. Powody, w kolejnosci waznosci:

  - **Nie wymaga uprawnien administratora.** Klucz jest wlasnoscia uzytkownika,
    wiec zaznaczenie checkboxa nie wywoluje okna kontroli konta.
  - **Dotyczy tego uzytkownika, ktory zaznaczyl.** Monitor rysuje po ekranie
    klawiatury i czyta baterie *jego* urzadzen; uruchamianie go na koncie innej
    osoby przy tym samym komputerze nie mialoby sensu.
  - **Wystartuje po zalogowaniu, a nie przy starcie systemu.** SteelSeries GG
    tez startuje z kontem uzytkownika, wiec wczesniejszy start nic by nie dal -
    program czekalby tylko dluzej na serwer GameSense.

Zaleznosc od SteelSeries GG nie jest tu problemem, nawet gdy monitor wystartuje
pierwszy. Serwis co RECONNECT_DELAY_SECONDS ponawia probe polaczenia i przez
caly ten czas normalnie odczytuje baterie - brakuje tylko obrazu na klawiaturze,
do momentu az GG sie podniesie.
"""

import os
import shlex
import winreg

import paths

# Standardowy klucz autostartu uzytkownika w Windows.
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

# Nazwa wpisu. Nie zmieniac po wydaniu: pod stara nazwa zostalby osierocony wpis,
# ktory dalej uruchamialby program, a checkbox pokazywalby, ze autostart jest
# wylaczony.
ENTRY_NAME = "BatteryMonitor"

# Argument, ktorym autostart mowi programowi, ze ma wystartowac do zasobnika.
# Bez niego okno wyskakiwaloby na wierzch przy kazdym zalogowaniu.
TRAY_ARGUMENT = "--tray"


def command():
    """Polecenie, ktore ma sie znalezc w rejestrze.

    Kazda czesc sciezki jest w cudzyslowach, bo program moze lezec w katalogu
    ze spacja w nazwie - bez nich Windows probowalby uruchomic sam pierwszy
    czlon sciezki.
    """
    parts = paths.program_path()
    quoted = " ".join(f'"{part}"' for part in parts)
    return f"{quoted} {TRAY_ARGUMENT}"


def current_entry():
    """Zwraca polecenie zapisane w rejestrze, albo None gdy wpisu nie ma."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, ENTRY_NAME)
            return value if isinstance(value, str) else None
    except FileNotFoundError:
        return None
    except OSError:
        return None


def is_enabled():
    """Czy program jest ustawiony na uruchamianie z Windows."""
    entry = current_entry()
    return isinstance(entry, str) and bool(entry.strip())


def enable(entry=None):
    """Dodaje wpis autostartu. Zwraca True, gdy sie udalo."""
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, ENTRY_NAME, 0, winreg.REG_SZ, command() if entry is None else entry)
        return True
    except OSError:
        return False


def disable():
    """Usuwa wpis autostartu. Zwraca True takze wtedy, gdy wpisu juz nie bylo."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, ENTRY_NAME)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def set_enabled(enabled):
    return enable() if enabled else disable()


def _entry_arguments(entry):
    if not isinstance(entry, str) or not entry.strip():
        return []
    try:
        return [part.strip('"') for part in shlex.split(entry, posix=False)]
    except ValueError:
        return []


def entry_target(entry):
    """Wyciaga z polecenia sciezke programu, ktory ma sie uruchomic.

    Polecenie ma postac '"sciezka" [argumenty]', wiec bierzemy zawartosc
    pierwszej pary cudzyslowow. Gdy ich nie ma - bo wpis powstal recznie -
    zostaje pierwszy czlon rozdzielony spacja, co dla sciezki ze spacja da
    wynik bledny, ale tylko na tyle, ze uznamy wpis za nieaktualny.
    """
    arguments = _entry_arguments(entry)
    return arguments[0] if arguments and arguments[0] else None


def _entry_targets(entry):
    """Sprawdza takze skrypt Pythona, bo istnienie interpretera nie wystarcza."""
    target = entry_target(entry)
    if not target:
        return ()
    targets = [target]
    executable = os.path.basename(target).lower()
    if executable.startswith("python") and executable.endswith(".exe"):
        arguments = _entry_arguments(entry)
        for argument in arguments[1:]:
            if argument.lower().endswith((".py", ".pyw")):
                targets.append(argument)
                break
    return targets


def repair_if_stale():
    """Poprawia wpis autostartu, gdy prowadzi do pliku, ktorego juz nie ma.

    Sytuacja jest zwyczajna: uzytkownik zaznaczyl autostart, a potem przeniosl
    albo skasowal plik programu. Wpis zostaje, wiec checkbox pokazuje wlaczony
    autostart, ale przy zalogowaniu nic sie nie uruchamia - i nie ma zadnego
    sladu, dlaczego.

    Warunkiem jest **brak pliku**, a nie sama roznica wzgledem obecnego
    polecenia. Gdyby wystarczyla roznica, jedno uruchomienie ze zrodel
    przestawialoby autostart z .exe na "pythonw app.py" - a to nie jest to,
    o co ktokolwiek prosil.

    Zwraca True, gdy wpis zostal podmieniony.
    """
    existing = current_entry()
    if existing is None or existing == command():
        return False

    targets = _entry_targets(existing)
    if targets and all(os.path.isfile(target) for target in targets):
        # Wpis prowadzi do dzialajacego programu - byc moze innej kopii, ale to
        # wybor uzytkownika, nie usterka.
        if TRAY_ARGUMENT not in _entry_arguments(existing)[1:]:
            return enable(existing.strip() + " " + TRAY_ARGUMENT)
        return False

    return enable()


if __name__ == "__main__":
    # python autostart.py - podglad stanu bez zmieniania czegokolwiek.
    print(f"wpis w rejestrze: {current_entry() or 'brak'}")
    print(f"powinien byc:     {command()}")
