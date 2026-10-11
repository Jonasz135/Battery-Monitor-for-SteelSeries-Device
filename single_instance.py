"""Pilnowanie, zeby program dzialal w jednym egzemplarzu.

Po co to jest, na konkretnym przykladzie: gdy autostart uruchomi monitor przy
zalogowaniu, a uzytkownik pozniej kliknie ikone jeszcze raz, dostajemy dwie
kopie tego samego programu. Skutki nie sa teoretyczne:

  - **Ekran klawiatury migocze.** Oba egzemplarze rotuja urzadzenia we wlasnym
    tempie i wysylaja do GameSense konkurencyjne klatki.
  - **Historia baterii sie psuje.** Kazdy egzemplarz trzyma swoja kopie osi czasu
    w pamieci i zapisuje caly plik, wiec ten, ktory zapisze pozniej, nadpisuje
    ustalenia drugiego. Momenty odlaczenia kabla zaczynaja przepadac.
  - **Odczyty HID sie gubia.** Dwa procesy pytaja ten sam interfejs urzadzenia
    naprzemiennie i kazdy moze odebrac odpowiedz na pytanie drugiego.

Mechanizm to nazwany mutex Windows - obiekt jadra, ktory istnieje tak dlugo, jak
trzyma go choc jeden proces. Nie plik blokady, bo plik zostaje po awaryjnym
zamknieciu programu i przy nastepnym starcie wygladalby jak dzialajaca instancja.
Mutex system zwalnia sam, nawet gdy proces zostanie zabity.

Przedrostek "Local\\" ogranicza nazwe do sesji logowania. Dzieki temu dwoch
uzytkownikow przelaczajacych sie szybkim przelaczaniem kont moze miec kazdy swoj
monitor - a to ma sens, bo kazdy z nich ma wlasny wybor sprzetu i wlasna historie.
"""

import ctypes
from ctypes import wintypes
import atexit

import i18n

MUTEX_NAME = "Local\\BatteryMonitor.SingleInstance"

_ERROR_ALREADY_EXISTS = 183

# Przyciski i ikona okna komunikatu (MB_OK, MB_ICONINFORMATION).
_MB_OK = 0x0
_MB_ICONINFORMATION = 0x40

# Uchwyt musi przezyc caly czas dzialania programu. Gdyby zostal odsmiecony,
# system zwolnilby mutex i druga instancja uznalaby, ze pierwszej nie ma.
_handle = None


def _kernel32():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # ctypes domyslnie zwraca c_int. HANDLE jest wskaznikiem i na x64 ma 64 bity.
    kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def acquire():
    """Zajmuje blokade. Zwraca True, gdy to pierwszy egzemplarz programu.

    Gdy samego mutexa nie da sie utworzyc, zwracamy True. Uruchomienie programu
    jest wtedy wazniejsze niz pilnowanie unikalnosci - w najgorszym razie
    uzytkownik zobaczy dwa okna, zamiast nie zobaczyc zadnego.
    """
    global _handle

    try:
        kernel32 = _kernel32()
        handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
        already = ctypes.get_last_error() == _ERROR_ALREADY_EXISTS
    except (AttributeError, OSError):
        return True

    if not handle:
        return True

    if already:
        # Mutex istnieje, wiec zamykamy wlasny uchwyt - inaczej trzymalibysmy
        # blokade po wyjsciu i pierwsza instancja nie zwolnilaby jej nigdy.
        kernel32.CloseHandle(handle)
        return False

    _handle = handle
    return True


def release():
    """Zwalnia nasz uchwyt, takze gdy mainloop skonczyl sie po wyjatku."""
    global _handle
    handle, _handle = _handle, None
    if handle is not None:
        try:
            _kernel32().CloseHandle(handle)
        except (AttributeError, OSError):
            pass


atexit.register(release)


def warn_already_running(title="Battery Monitor"):
    """Mowi uzytkownikowi, gdzie szukac dzialajacego programu.

    Bez tego drugie klikniecie ikony wygladaloby tak, jakby program nie chcial
    sie uruchomic - okno nie pojawia sie, bo pierwsza instancja siedzi
    w zasobniku i nikt jej stamtad nie wolal.

    Komunikat idzie w jezyku systemu, a nie w tym zapisanym w ustawieniach:
    okno pojawia sie przed zbudowaniem interfejsu, wiec ustawienia nie sa jeszcze
    wczytane, a wczytywanie ich tylko dla jednego zdania nie ma sensu.
    """
    try:
        ctypes.windll.user32.MessageBoxW(
            None, i18n.t("instance.running"), title, _MB_OK | _MB_ICONINFORMATION
        )
    except (AttributeError, OSError):
        pass
