"""Odczyt rzeczywistego poziomu baterii urzadzen SteelSeries przez interfejs HID.

Podzial obowiazkow: **co** istnieje opisuje device_catalog.py (modele, PID-y,
lacza), a **jak** z tym rozmawiac - rodziny protokolow w tym pliku. Dodanie
modelu, ktory uzywa juz znanego protokolu, to wiec jeden wpis w katalogu bez
dotykania kodu odczytu.

Zrodla protokolow
-----------------
  - sluchawki Arctis: HeadsetControl, lib/devices/steelseries_arctis_*.hpp
    (do wersji 3.1.0 byly to pliki .c w src/devices/),
  - myszki Aerox, Prime, Rival: rivalcfg, rivalcfg/devices/*.py.

Odczyt jest wylacznie pasywny - wysylamy komende zapytania o status i czytamy
odpowiedz. Nie zmieniamy zadnych ustawien urzadzenia.

Czego NIE ma i dlaczego
-----------------------
Klawiatury Apex nie maja tu ani jednego wpisu. Zaden publiczny projekt - ani
OpenRGB, ani apex-tux, ani steelseriesgg-rs, ani sterownik hid-steelseries
w jadrze Linuksa - nie zna komendy odczytu baterii dla klawiatur SteelSeries,
a GameSense SDK nie udostepnia baterii zadnym zdarzeniem ani endpointem.
Wyslanie zgadywanego opcodu na interfejs vendorowy klawiatury z pamiecia profili
to realne ryzyko, wiec te modele siedza w katalogu bez protokolu.

Trzy rzeczy, ktore roznia rodziny miedzy soba
---------------------------------------------
1. **Skala poziomu baterii wystepuje w pieciu odmianach**: gotowy procent, krok
   0-4, krok 0-8, numer kroku po 5 punktow (myszki) oraz surowy zakres
   0x64-0x9A (Arctis 9). Dlatego kazda rodzina ma wlasny parser, nawet gdy dzieli
   komende z inna - `0x00 0xb0` wysylaja Nova 7, Nova 5 i Arctis 7+, a odpowiedz
   parsuje sie w trzech roznych ukladach bajtow.
2. **Echo opcodu nie zawsze istnieje.** W protokole Rivali bajt zerowy odpowiedzi
   to sam poziom baterii, wiec nie ma tam czego dopasowywac.
3. **Numer interfejsu i usage page roznia sie** i nie zawsze sa znane. Rival 650
   siedzi na interfejsie 0, Arctis 7 na 5, Nova Pro na 4. Dla czterech modeli
   usage page nie jest potwierdzone w zadnym zrodle, wiec musi byc opcjonalne.
"""

import re
import time

import hid

import device_catalog
from device_catalog import LINK_24, LINK_WIRED

VENDOR_ID = 0x1038

# Ile czasu czekamy na raport bedacy odpowiedzia na nasze zapytanie.
_QUERY_TIMEOUT_SECONDS = 0.5

# Odbiorniki nadaja takze spontaniczne raporty. Oproznienie ich kolejki musi
# miec limit, inaczej stale aktywny interfejs moze zatrzymac caly monitor.
_DRAIN_TIMEOUT_SECONDS = 0.05
_MAX_DRAIN_REPORTS = 256


def _scale(value, raw_min, raw_max):
    """Przelicza surowa wartosc na procenty, przycinajac ja najpierw do zakresu.

    Przycinanie jest istotne dla rodzin, w ktorych dolna granica nie jest zerem:
    Arctis 9 raportuje 0x64-0x9A, wiec bez przyciecia odczyt ponizej 0x64 dalby
    wynik ujemny. HeadsetControl dodal ten clamp przy przejsciu na C++ i to jest
    wersja, ktora tu odtwarzam.
    """
    value = max(raw_min, min(raw_max, value))
    return (value - raw_min) * 100 // (raw_max - raw_min)


# ---------------------------------------------------------------------------
# Parsery odpowiedzi
#
# Kazdy zwraca (procent, czy_laduje) albo None, gdy urzadzenie jest wylaczone,
# poza zasiegiem, albo gdy odpowiedz nie ma sensu.
# ---------------------------------------------------------------------------

_AEROX_CHARGING_FLAG = 0b10000000


def _parse_aerox(pid, data):
    """Myszki Aerox i Prime: poziom zakodowany w drugim bajcie.

    Wartosc surowa to numer kroku - rivalcfg przelicza ja wzorem (krok - 1) * 5.
    Gdy mysz jest wylaczona, dongle zwraca 0xff, co daje 630% i wypada poza
    zakres. rivalcfg nie ma na to osobnego warunku, tylko wlasnie ten filtr
    zakresu, i ten sam mechanizm chroni tu przed przypadkowym raportem.
    """
    del pid
    if len(data) < 2:
        return None

    raw = data[1]
    charging = bool(raw & _AEROX_CHARGING_FLAG)
    percent = ((raw & ~_AEROX_CHARGING_FLAG) - 1) * 5

    if not 0 <= percent <= 100:
        return None

    return percent, charging


def _parse_rival(pid, data):
    """Myszki Rival: procent wprost w bajcie zerowym, ladowanie w drugim.

    Zupelnie inny uklad niz w rodzinie Aerox - tu poziom nie jest krokiem i nie
    dzieli bajtu z flaga ladowania.
    """
    del pid
    if len(data) < 3:
        return None

    percent = data[0]
    if not 0 <= percent <= 100:
        return None

    return percent, bool(data[2])


# Warianty Nova 7, ktore raportuja baterie w pieciu krokach (0-4). Pozostale
# podaja procent bezposrednio. Wyboru NIE da sie zrobic po tresci odpowiedzi -
# zalezy wylacznie od PID-u, a ten sam egzemplarz po aktualizacji firmware ze
# stycznia 2026 zmienia PID i przechodzi na procenty.
_NOVA_7_DISCRETE_PIDS = {0x2202, 0x2206, 0x220A, 0x223A, 0x227A, 0x22A4}


def _parse_nova_7(pid, data):
    """Arctis Nova 7, 7P, 7X: [echo, ?, bateria, status, chatmix...]."""
    if len(data) < 4:
        return None

    status = data[3]
    if status == 0x00:
        # Dongle odpowiada, ale sluchawki sa wylaczone albo poza zasiegiem.
        return None

    if pid in _NOVA_7_DISCRETE_PIDS:
        if not 0 <= data[2] <= 4:
            return None
        percent = _scale(data[2], 0, 4)
    else:
        percent = min(100, data[2])

    if not 0 <= percent <= 100:
        return None

    # 0x01 i 0x02 oznaczaja ladowanie, pozostale wartosci normalna prace.
    return percent, status in (0x01, 0x02)


def _parse_nova_5(pid, data):
    """Arctis Nova 5 i 5X: ta sama komenda co Nova 7, inny uklad odpowiedzi.

    Tutaj stan lacza jest w bajcie 1, poziom w bajcie 3 (juz w procentach),
    a ladowanie w bajcie 4. Pomylenie tego z ukladem Nova 7 dalo by poziom
    odczytany z bajtu statusu.
    """
    del pid
    if len(data) < 16:
        return None

    if data[1] == 0x02:
        return None

    percent = data[3]
    if not 0 <= percent <= 100:
        return None

    return percent, data[4] == 0x01


def _parse_nova_pro(pid, data):
    """Arctis Nova Pro Wireless: poziom w bajcie 6 w skali 0-8."""
    del pid
    if len(data) < 16:
        return None

    status = data[15]
    if status == 0x01:
        # Sluchawki odlozone albo wylaczone; stacja bazowa odpowiada dalej.
        return None
    if status not in (0x02, 0x08):
        # Nieznany stan - lepiej zglosic brak danych niz zgadywac.
        return None

    if not 0 <= data[6] <= 8:
        return None
    return _scale(data[6], 0, 8), status == 0x02


def _parse_arctis_7_plus(pid, data):
    """Arctis 7+ (oraz warianty PS5 i Xbox): krok 0-4 w bajcie 2.

    Trzeci uklad odpowiedzi na komende 0x00 0xb0: offline w bajcie 1, poziom
    w bajcie 2, ladowanie w bajcie 3.
    """
    del pid
    if len(data) < 4:
        return None

    if data[1] == 0x01:
        return None

    if not 0 <= data[2] <= 4:
        return None
    return _scale(data[2], 0, 4), data[3] == 0x01


def _parse_arctis_7(pid, data):
    """Arctis 7 i Arctis Pro 2019: procent wprost w bajcie 2.

    Ten model nie raportuje ani ladowania, ani stanu wylaczenia - HeadsetControl
    jawnie wylacza obie sciezki. Wylaczone sluchawki poznamy tylko po tym, ze
    odczyt nie dojdzie.
    """
    del pid
    if len(data) < 3:
        return None

    percent = min(100, data[2])
    if not 0 <= percent <= 100:
        return None

    return percent, False


def _parse_arctis_1(pid, data):
    """Arctis 1 Wireless oraz Arctis 7P i 7X (bez plusa): procent w bajcie 3.

    Ladowanie nie jest raportowane - w HeadsetControl stoi w tym miejscu jawny
    komentarz "no charging for this device".
    """
    del pid
    if len(data) < 4:
        return None

    if data[2] == 0x01:
        return None

    percent = min(100, data[3])
    if not 0 <= percent <= 100:
        return None

    return percent, False


def _parse_arctis_9(pid, data):
    """Arctis 9: surowy zakres 0x64-0x9A w bajcie 3, ladowanie w bajcie 4.

    Jedyna rodzina z taka skala. Stan wylaczenia nie jest wykrywany - w kodzie
    zrodlowym warunek offline porownuje bajt statusu z 0xff, co nigdy nie
    zachodzi, wiec ta sciezka jest martwa.
    """
    del pid
    if len(data) < 5:
        return None

    return _scale(data[3], 0x64, 0x9A), data[4] == 0x01


def _parse_arctis_pro_wireless(pid, data):
    """Arctis Pro Wireless: jeden bajt odpowiedzi, krok 0-4.

    Ladowanie nie jest raportowane. Stan wylaczenia sprawdza osobne, wczesniejsze
    zapytanie (patrz "status_request" w rodzinie).
    """
    del pid
    if not data or not 0 <= data[0] <= 4:
        return None

    return _scale(data[0], 0, 4), False


# ---------------------------------------------------------------------------
# Rodziny protokolow
# ---------------------------------------------------------------------------
#
# Znaczenie pol:
#   interface      - numer interfejsu HID, na ktorym urzadzenie przyjmuje
#                    komendy konfiguracyjne. To nie ten sam interfejs, ktory
#                    przenosi zwykle zdarzenia myszy czy audio.
#   usage_page     - dodatkowe zawezenie wyboru sciezki HID. None znaczy, ze
#                    nie jest potwierdzone w zadnym zrodle i nie filtrujemy po
#                    nim - inaczej odcielibysmy modele, ktore rivalcfg
#                    identyfikuje samym numerem interfejsu i ktore dzialaja.
#   requests       - komenda per lacze. Pierwszy bajt to numer raportu HID.
#                    Ta sama mysz oczekuje innego bajtu na kablu niz na
#                    dongle'u: rivalcfg doklada bezprzewodowo flage 0x40.
#                    Brak wpisu dla lacza znaczy, ze tam odczyt nie dziala.
#   write_length   - do ilu bajtow dopelnic komende zerami. None zostawia ja
#                    tak, jak jest.
#   read_length    - ile bajtow probujemy odczytac.
#   echoes_opcode  - czy odpowiedz zaczyna sie echem opcodu, czyli ostatniego
#                    bajtu wyslanej komendy. Odbiornik nadaje wlasne raporty
#                    kilkadziesiat razy na sekunde, a SteelSeries GG odpytuje
#                    urzadzenie rownolegle, wiec echo pozwala odrzucic obcy
#                    raport. Oczekiwany bajt bierzemy z komendy, a nie z
#                    rodziny, bo ta sama mysz pyta o baterie innym opcodem na
#                    kablu niz na dongle'u. Nie wszedzie echo istnieje: w
#                    protokole Rivali bajt zerowy to sam poziom baterii.
#   status_request - dla protokolu dwuetapowego: zapytanie o obecnosc
#                    urzadzenia, zadawane przed pytaniem o baterie.

PROTOCOL_FAMILIES = {
    # -- myszki ------------------------------------------------------------
    "aerox": {
        "interface": 3,
        "usage_page": 0xFFC0,
        "requests": {
            LINK_24: (0x00, 0x92 | 0x40),
            LINK_WIRED: (0x00, 0x92),
        },
        "read_length": 64,
        "minimum_length": 2,
        # 0xff jest znana odpowiedzia wylaczonej myszy; inne wartosci poza
        # krokami 1-21 nie powinny zaslaniac kolejnego prawidlowego raportu.
        "valid_response": lambda data: data[1] == 0xFF or 1 <= (data[1] & 0x7F) <= 21,
        "echoes_opcode": True,
        "parse": _parse_aerox,
    },
    "rival": {
        "interface": 3,
        # rivalcfg identyfikuje te modele samym numerem interfejsu; usage page
        # nie wystepuje w zadnym ze sprawdzonych zrodel.
        "usage_page": None,
        "requests": {
            LINK_24: (0x00, 0xAA, 0x01),
            LINK_WIRED: (0x00, 0xAA, 0x01),
        },
        "read_length": 3,
        "minimum_length": 3,
        # Bajt zerowy odpowiedzi to poziom baterii, wiec nie ma tu echa opcodu.
        "echoes_opcode": False,
        "parse": _parse_rival,
    },
    "rival_650": {
        # Jedyny model z listy, ktory nie uzywa interfejsu 3.
        "interface": 0,
        "usage_page": None,
        "requests": {
            LINK_24: (0x00, 0xAA, 0x01),
            LINK_WIRED: (0x00, 0xAA, 0x01),
        },
        "read_length": 3,
        "minimum_length": 3,
        "echoes_opcode": False,
        "parse": _parse_rival,
    },
    # -- sluchawki ---------------------------------------------------------
    "nova_7": {
        "interface": 3,
        "usage_page": 0xFFC0,
        "requests": {LINK_24: (0x00, 0xB0)},
        "read_length": 64,
        "minimum_length": 4,
        "echoes_opcode": True,
        "parse": _parse_nova_7,
    },
    "nova_5": {
        "interface": 3,
        "usage_page": 0xFFC0,
        "requests": {LINK_24: (0x00, 0xB0)},
        "read_length": 64,
        "minimum_length": 16,
        # HeadsetControl czyta tu pierwszy raport, jaki przyjdzie. Nie mam tego
        # sprzetu, wiec nie zakladam echa - blednie zalozone odcieloby odczyt
        # calkowicie, a bez niego najwyzej trafimy w obcy raport, ktory odsieje
        # walidacja zakresu.
        "echoes_opcode": False,
        "parse": _parse_nova_5,
    },
    "nova_pro": {
        "interface": 4,
        "usage_page": None,
        "requests": {LINK_24: (0x06, 0xB0)},
        # Ta rodzina wysyla pelny 31-bajtowy raport, nie dwa bajty.
        "write_length": 31,
        "read_length": 128,
        "minimum_length": 16,
        "echoes_opcode": False,
        "parse": _parse_nova_pro,
    },
    "arctis_7_plus": {
        "interface": 3,
        "usage_page": 0xFFC0,
        "requests": {LINK_24: (0x00, 0xB0)},
        "read_length": 64,
        "minimum_length": 4,
        "echoes_opcode": False,
        "parse": _parse_arctis_7_plus,
    },
    "arctis_7": {
        "interface": 5,
        "usage_page": None,
        "requests": {LINK_24: (0x06, 0x18)},
        "read_length": 8,
        "minimum_length": 3,
        "echoes_opcode": False,
        "parse": _parse_arctis_7,
    },
    "arctis_1": {
        "interface": 3,
        # Jedyna rodzina z ta usage page.
        "usage_page": 0xFF43,
        "requests": {LINK_24: (0x06, 0x12)},
        "read_length": 8,
        "minimum_length": 4,
        "echoes_opcode": False,
        "parse": _parse_arctis_1,
    },
    "arctis_9": {
        "interface": 0,
        "usage_page": None,
        "requests": {LINK_24: (0x00, 0x20)},
        "read_length": 12,
        "minimum_length": 5,
        "echoes_opcode": False,
        "parse": _parse_arctis_9,
    },
    "arctis_pro_wireless": {
        "interface": 0,
        "usage_page": None,
        # Jedyny protokol dwuetapowy: najpierw pytamy, czy sluchawki sa w ogole
        # obecne, i tylko wtedy pytamy o poziom baterii.
        "status_request": (0x41, 0xAA),
        "status_length": 2,
        "status_offline": lambda data: bool(data) and data[0] == 0x02,
        # Odpowiedz czytamy do 2 bajtow, ale znaczenie ma tylko bajt zerowy.
        "status_online": lambda data: bool(data) and data[0] == 0x04,
        "status_valid": lambda data: data[0] in (0x02, 0x04),
        "requests": {LINK_24: (0x40, 0xAA)},
        "write_length": 31,
        "read_length": 1,
        "minimum_length": 1,
        "echoes_opcode": False,
        "parse": _parse_arctis_pro_wireless,
    },
}


def _plan_for(pid):
    """Zwraca (urzadzenie, lacze, rodzina, komenda) dla PID-u, albo None."""
    device, link = device_catalog.find_by_pid(pid)
    if device is None:
        return None

    family = PROTOCOL_FAMILIES.get(device.protocol)
    if family is None:
        return None

    request = family["requests"].get(link)
    if request is None:
        # Model jest znany, ale na tym laczu nie wiemy, o co zapytac.
        return None

    return device, link, family, request


# ---------------------------------------------------------------------------
# Warstwa HID
# ---------------------------------------------------------------------------


def _find_path(pid, interface, usage_page):
    """Zwraca sciezke HID do interfejsu vendorowego danego urzadzenia.

    Windows czasem podaje interface_number rowny -1; wtedy numer odczytujemy
    z segmentu mi_XX sciezki USB. Nie wysylamy komendy do pierwszego dowolnego
    interfejsu: moze on przenosic zwykle zdarzenia myszy lub klawiatury.
    """
    entries = list(hid.enumerate(VENDOR_ID, pid))
    if not entries:
        return None

    def interface_for(entry):
        number = entry.get("interface_number")
        if isinstance(number, int) and number >= 0:
            return number
        path = entry.get("path", b"")
        text = path.decode("ascii", errors="ignore") if isinstance(path, bytes) else str(path)
        match = re.search(r"(?:[&#])mi_([0-9a-f]{2})(?:[&#]|$)", text, re.IGNORECASE)
        return int(match[1], 16) if match else None

    def matches_usage(entry):
        return usage_page is None or entry.get("usage_page") == usage_page

    for entry in entries:
        if interface_for(entry) == interface and matches_usage(entry):
            return entry["path"]

    # Usage page zastapi numer tylko wtedy, gdy numeru naprawde nie znamy.
    # Jawnie inny numer lub usage page nie jest kandydatem do zgadywania.
    for entry in entries:
        if usage_page is not None and matches_usage(entry) and interface_for(entry) is None:
            return entry["path"]

    return None


def _write(handle, request, write_length):
    payload = bytes(request)
    if write_length and len(payload) < write_length:
        # Czesc rodzin oczekuje pelnego raportu dopelnionego zerami.
        payload = payload + bytes(write_length - len(payload))
    # Backend Windows dopelnia raport do OutputReportByteLength i zwraca
    # dlugosc po dopelnieniu. Wiekszy wynik jest wiec poprawnym zapisem.
    if handle.write(payload) < len(payload):
        raise OSError("Incomplete HID query write")


def _query(
    handle, request, read_length, echoes_opcode=False, write_length=None, minimum_length=1,
    cancel_event=None, valid_response=None,
):
    """Wysyla zapytanie i zwraca raport bedacy na nie odpowiedzia.

    Gdy protokol echem potwierdza komende, czekamy na raport zaczynajacy sie
    opcodem, czyli ostatnim bajtem tego, co wyslalismy - to odsiewa raporty,
    ktore odbiornik nadaje sam z siebie kilkadziesiat razy na sekunde. Gdy echa
    nie ma, bierzemy pierwszy raport o wymaganej minimalnej dlugosci.
    Raporty za krotkie nie koncza proby przed wlasciwa odpowiedzia.
    """
    expected = request[-1] if echoes_opcode else None

    def cancelled():
        return cancel_event is not None and cancel_event.is_set()

    if cancelled():
        return None
    handle.set_nonblocking(1)

    drain_deadline = time.monotonic() + _DRAIN_TIMEOUT_SECONDS
    for _ in range(_MAX_DRAIN_REPORTS):
        if cancelled():
            return None
        if not handle.read(read_length):
            break
        if time.monotonic() >= drain_deadline:
            return None
    else:
        # Nie wiemy, czy stary raport z kolejki odpowiadalby naszemu pytaniu.
        # Konczymy bez wysylania, zamiast uznac stara baterie za nowy pomiar.
        return None

    if cancelled():
        return None
    _write(handle, request, write_length)

    deadline = time.monotonic() + _QUERY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if cancelled():
            return None
        data = handle.read(read_length)
        if cancelled():
            return None
        if not data:
            if cancel_event is None:
                time.sleep(0.005)
            elif cancel_event.wait(0.005):
                return None
            continue
        if (len(data) >= minimum_length and (expected is None or data[0] == expected)
                and (valid_response is None or valid_response(data))):
            return data

    return None


def read_battery(pid, cancel_event=None):
    """Zwraca (procent, czy_laduje) dla urzadzenia o danym PID, albo None.

    None oznacza, ze urzadzenie jest wylaczone, poza zasiegiem, albo ze nie
    udalo sie otworzyc interfejsu.
    """
    if cancel_event is not None and cancel_event.is_set():
        return None
    plan = _plan_for(pid)
    if plan is None:
        return None

    _, _, family, request = plan

    handle = None
    try:
        path = _find_path(pid, family["interface"], family["usage_page"])
        if path is None or (cancel_event is not None and cancel_event.is_set()):
            return None
        handle = hid.device()
        handle.open_path(path)
        return _read_opened(handle, pid, family, request, cancel_event=cancel_event)
    except (OSError, IOError):
        return None
    finally:
        if handle is not None:
            try:
                handle.close()
            except (OSError, IOError):
                # Odlaczenie USB przy sprzataniu nie uniewaznia wyniku odczytu.
                pass


def _read_opened(handle, pid, family, request, cancel_event=None):
    """Zadaje pytania na otwartym uchwycie i parsuje odpowiedz."""
    write_length = family.get("write_length")

    status_request = family.get("status_request")
    if status_request is not None:
        # Protokol dwuetapowy: bez tego pytania nie odroznimy wylaczonych
        # sluchawek od naladowanych, bo samo zapytanie o baterie nie zawiera
        # informacji o obecnosci urzadzenia.
        status = None
        for _ in range(2):
            status = _query(
                handle,
                status_request,
                family["status_length"],
                write_length=write_length,
                cancel_event=cancel_event,
                valid_response=family["status_valid"],
            )
            if status is not None:
                break
        if status is None or not family["status_online"](status):
            return None

    # Pierwsze zapytanie po otwarciu interfejsu czasem przepada, dlatego
    # ponawiamy je raz przed uznaniem urzadzenia za niedostepne.
    for _ in range(2):
        data = _query(
            handle,
            request,
            family["read_length"],
            echoes_opcode=family["echoes_opcode"],
            write_length=write_length,
            minimum_length=family.get("minimum_length", 1),
            cancel_event=cancel_event,
            valid_response=family.get("valid_response"),
        )
        if data:
            return family["parse"](pid, data)

    return None


def discover_devices():
    """Zwraca liste podlaczonych urzadzen, ktorych protokol znamy.

    Wykrywa obecnosc odbiornika USB - nie oznacza to, ze samo urzadzenie jest
    wlaczone. O tym decyduje dopiero wynik read_battery().

    Kazdy wpis ma "key" modelu z katalogu oraz "link", bo ta sama mysz zglasza
    sie pod innym PID-em na kablu i na dongle'u.
    """
    found = []
    seen_pids = set()

    for entry in hid.enumerate(VENDOR_ID, 0):
        pid = entry["product_id"]
        if pid in seen_pids:
            continue

        plan = _plan_for(pid)
        if plan is None:
            continue

        device, link, _, _ = plan
        seen_pids.add(pid)
        found.append(
            {
                "pid": pid,
                "key": device.key,
                "link": link,
                "name": device.display_name(link),
                "model": device.name,
                "label": device.label,
                "rated_hours": device.rated_for(link),
            }
        )

    return found


if __name__ == "__main__":
    # Szybka diagnostyka: python battery_hid.py
    devices = discover_devices()
    if not devices:
        print("Nie znaleziono obslugiwanych urzadzen SteelSeries.")
    for entry in devices:
        result = read_battery(entry["pid"])
        if result is None:
            state = "brak danych (urzadzenie wylaczone?)"
        else:
            percent, charging = result
            state = f"{percent}%" + (" (ladowanie)" if charging else "")
        print(f"{entry['name']:<34} PID 0x{entry['pid']:04x}  {state}")
