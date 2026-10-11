"""Napisy interfejsu w dwoch jezykach: polskim i angielskim.

Jak to jest zrobione
--------------------
Jeden slownik na jezyk, klucze z kropkami grupujace napisy po miejscu, w ktorym
sie pojawiaja (`card.`, `status.`, `picker.`, `log.`, `oled.`). Zaden gotowy
system tlumaczen - gettext wymagalby kompilowania plikow .mo i katalogu locale
obok programu, a przy siedemdziesieciu napisach i dwoch jezykach dokladalby
wiecej obslugi niz daje.

Parametry wchodza przez `str.format`, nie przez skladanie napisow po kawalku.
Kolejnosc slow w zdaniu roznia sie miedzy jezykami, wiec "zostalo {hours}"
musi byc jednym napisem, a nie napisem plus liczba doklejona z boku.

Dlaczego napisy na OLED sa osobno
---------------------------------
Ekran 128x40 dostaje czcionke zbinaryzowana do jednego bitu i nie gwarantuje
polskich znakow diakrytycznych. Napisy dla niego (`oled.*`) musza wiec byc
czystym ASCII w **obu** jezykach - pilnuje tego test w test_i18n.py. W oknie
zadnego takiego ograniczenia nie ma, dlatego ten sam zestaw sluchawek jest na
ekranie podpisany "SLUCHAWKI", a w oknie "SŁUCHAWKI".

Etykiety urzadzen
-----------------
`device_catalog.Device.label` zostaje **nietlumaczonym tokenem** ("MYSZ",
"SLUCHAWKI", "KLAWIATURA"). Logika uzywa go do rozpoznawania urzadzen, wiec
zmiana jezyka nie moze go ruszac - inaczej odsiewanie podwojnych odczytow
w monitor_service przestaloby dzialac po przelaczeniu na angielski. Tlumaczenie
wchodzi dopiero przy wyswietlaniu, przez oled_label() i window_label().
"""

import ctypes
import locale

# Kody jezykow. "auto" nie jest jezykiem, a zyczeniem: rozwija sie do jezyka
# systemu przy kazdym starcie, wiec program nadaza za zmiana ustawien Windows.
POLISH = "pl"
ENGLISH = "en"
AUTO = "auto"

LANGUAGES = (POLISH, ENGLISH)

DEFAULT_LANGUAGE = ENGLISH

# LANG_POLISH z windows.h. GetUserDefaultUILanguage zwraca LANGID, w ktorym
# dolne dziesiec bitow to jezyk glowny, a gorne - wariant regionalny.
_LANG_POLISH = 0x15
_PRIMARY_LANGUAGE_MASK = 0x3FF

# Nazwy do przelacznika w oknie. Kazdy jezyk podpisany w sobie samym, bo
# "angielski" na angielskim interfejsie nikomu nie pomaga.
LANGUAGE_NAMES = {
    POLISH: "PL",
    ENGLISH: "EN",
}

# Token etykiety z katalogu urzadzen -> czlon klucza tlumaczenia.
_LABEL_SLUGS = {
    "MYSZ": "mouse",
    "SLUCHAWKI": "headset",
    "KLAWIATURA": "keyboard",
}


_TRANSLATIONS = {
    POLISH: {
        # -- okno ----------------------------------------------------------
        "app.subtitle": "SteelSeries OLED",
        "section.screen": "EKRAN KLAWIATURY",
        "devices.empty": (
            "Brak aktywnych urządzeń.\n"
            "Włącz mysz lub słuchawki - pojawią się w ciągu kilku sekund."
        ),
        # -- wskaznik stanu w naglowku -------------------------------------
        "status.connecting": "Łączenie...",
        "status.connected": "Połączono",
        "status.no_screen": "Bez ekranu",
        "status.stopped": "Zatrzymano",
        "status.paused": "Wstrzymano",
        # -- stan serwisu, pokazywany w stopce -----------------------------
        "service.not_started": "Nie uruchomiono",
        "service.starting": "Uruchamianie...",
        "service.stopped": "Zatrzymano",
        "service.connected": "Połączono ({address})",
        "service.gg_missing": "SteelSeries GG nie działa — ekran włączy się po jego uruchomieniu.",
        "service.no_programdata": "Brak zmiennej środowiskowej PROGRAMDATA.",
        "service.coreprops_unreadable": "Nie udało się odczytać coreProps.json: {error}",
        "service.coreprops_no_address": "coreProps.json nie zawiera klucza 'address'.",
        "service.link_error": "Błąd łączności z GameSense",
        "service.unexpected_error": "Nieoczekiwany błąd: {error}",
        "footer.last_poll": "Ostatni odczyt: {time}",
        # -- przyciski i przelaczniki --------------------------------------
        "button.refresh": "Odśwież",
        "button.pause": "Pauza",
        "button.resume": "Wznów",
        "button.hardware": "Sprzęt",
        "button.log": "Log",
        "checkbox.autostart": "Uruchamiaj razem z Windows (w tle, w zasobniku)",
        # -- karta urzadzenia ----------------------------------------------
        "card.charging": "Ładowanie",
        "card.estimating": "szacowanie...",
        "card.off": "Wyłączone",
        "card.unsupported": "Brak obsługi",
        "card.offline_hint": "Urządzenie nie odpowiada — wyłączone albo poza zasięgiem",
        "card.unsupported_hint": "Odczyt baterii tego modelu nie jest jeszcze potwierdzony",
        "card.charging_in_progress": "Ładowanie w toku",
        "card.since_charge": "Włączone: {elapsed}",
        "card.since_unknown": "Włączone: nieznane",
        "card.used": "zużyto {used}%",
        "card.per_point": "{duration} na 1%",
        # -- zasobnik systemowy --------------------------------------------
        "tray.show": "Pokaż okno",
        "tray.refresh": "Odśwież",
        "tray.quit": "Zakończ",
        "tray.hidden": "Monitor działa w tle, ekran klawiatury jest odświeżany.",
        "tray.no_devices": "brak aktywnych urządzeń",
        "tray.charging_short": "(ład.)",
        # -- okno wyboru sprzetu -------------------------------------------
        "picker.title": "Wybór sprzętu",
        "picker.heading": "Zaznacz sprzęt, który posiadasz",
        "picker.intro": (
            "Zaznaczone urządzenia mają stałą kartę w oknie — także wtedy, gdy są\n"
            "wyłączone. Nie zaznaczaj nic, aby program sam wykrywał, co jest podłączone."
        ),
        "picker.keyboard_note": (
            "wskazują, na czym wyświetlać; bateria tylko w modelach bezprzewodowych"
        ),
        "picker.note_no_screen": "bez ekranu OLED",
        "picker.note_no_battery": "brak odczytu baterii",
        "picker.warn_no_screen": (
            "{names} nie ma ekranu OLED — program nie ma gdzie wyświetlać.\n"
            "Zaznacz też klawiaturę z ekranem albo zostaw monitorowanie w oknie."
        ),
        "picker.warn_no_battery": "Żadne z zaznaczonych urządzeń nie ma baterii do monitorowania.",
        "picker.warn_unsupported": (
            "Dla zaznaczonych urządzeń nie mamy potwierdzonego odczytu baterii."
        ),
        "picker.clear": "Wyczyść",
        "picker.cancel": "Anuluj",
        "picker.save": "Zapisz",
        # -- kategorie urzadzen --------------------------------------------
        "category.keyboard": "Klawiatury",
        "category.mouse": "Myszki",
        "category.headset": "Słuchawki",
        # -- panel logu ----------------------------------------------------
        "log.poll": "Odczyt baterii:",
        "log.device_value": "  {name}: {percent}%",
        "log.device_charging_suffix": " (ładowanie)",
        "log.device_no_data": "  {name}: brak danych (wyłączone?)",
        "log.device_duplicate": "  {name}: {percent}% - pomijam, to samo urządzenie",
        "log.device_bt_duplicate": "  {name}: {percent}% - pomijam, już odczytane przez HID",
        "log.device_off": "  {name}: wyłączone",
        "log.device_unsupported": "  {name}: brak obsługi odczytu baterii",
        "log.runtime": "    czas pracy: {runtime} ({source}, {rate} pkt/h)",
        "log.since_charge": "    włączone: {elapsed}{used}",
        "log.used_suffix": ", zużyto {used}%",
        "log.per_point": "    czas na 1%: {duration}",
        "log.connected": "Podłączono do GameSense na {address}.",
        "log.retry": "{message} Ponowna próba za {seconds} s.",
        "log.gg_error": "Błąd komunikacji z GameSense: {error}",
        "log.unexpected": "Nieoczekiwany błąd: {error}",
        "log.read_error": "Błąd odczytu ({transport}): {error}",
        "log.settings_failed": "Nie udało się zapisać ustawień. Poprzedni wybór został zachowany.",
        "log.history_failed": "Nie udało się zapisać historii baterii.",
        "log.tray_failed": "Zasobnik jest niedostępny; pokazano okno aplikacji: {error}",
        "log.autostart_repaired": "Poprawiono ścieżkę w autostarcie Windows.",
        "log.autostart_on": "Autostart Windows: włączony.",
        "log.autostart_off": "Autostart Windows: wyłączony.",
        "log.autostart_failed": "Nie udało się zmienić autostartu (odmowa dostępu do rejestru).",
        # Liczba po dwukropku, a nie w zdaniu: "1 urządzeń" byłoby błędem, a pełna
        # odmiana przez liczebniki to za dużo maszynerii na jeden wiersz logu.
        "log.declared": "Zadeklarowany sprzęt: {count}.",
        "log.automatic": "Wybrano wykrywanie automatyczne.",
        "log.language": "Język interfejsu: polski.",
        # -- pojedyncza instancja i konsola --------------------------------
        "instance.running": (
            "Battery Monitor już działa.\n\n"
            "Jego ikona jest w zasobniku systemowym, obok zegara — "
            "kliknij ją, aby otworzyć okno."
        ),
        "console.header": "Monitor baterii SteelSeries - Ctrl+C aby zakończyć.",
        "console.already_running": (
            "Battery Monitor już działa - zamknij go, zanim uruchomisz wersję konsolową."
        ),
        "console.stopping": "Zatrzymywanie...",
        # -- napisy na ekran OLED (tylko ASCII) ----------------------------
        "oled.no_data": "BRAK DANYCH",
        "oled.charging": "LAD",
        "oled.label.mouse": "MYSZ",
        "oled.label.headset": "SLUCHAWKI",
        "oled.label.keyboard": "KLAWIATURA",
        # -- etykiety w oknie (polskie znaki dozwolone) --------------------
        "window.label.mouse": "MYSZ",
        "window.label.headset": "SŁUCHAWKI",
        "window.label.keyboard": "KLAWIATURA",
    },
    ENGLISH: {
        # -- okno ----------------------------------------------------------
        "app.subtitle": "SteelSeries OLED",
        "section.screen": "KEYBOARD SCREEN",
        "devices.empty": (
            "No active devices.\n"
            "Turn on a mouse or headset - it will show up within seconds."
        ),
        # -- wskaznik stanu w naglowku -------------------------------------
        "status.connecting": "Connecting...",
        "status.connected": "Connected",
        "status.no_screen": "No screen",
        "status.stopped": "Stopped",
        "status.paused": "Paused",
        # -- stan serwisu, pokazywany w stopce -----------------------------
        "service.not_started": "Not started",
        "service.starting": "Starting...",
        "service.stopped": "Stopped",
        "service.connected": "Connected ({address})",
        "service.gg_missing": "SteelSeries GG is not running — the screen starts once it does.",
        "service.no_programdata": "The PROGRAMDATA environment variable is missing.",
        "service.coreprops_unreadable": "Could not read coreProps.json: {error}",
        "service.coreprops_no_address": "coreProps.json has no 'address' key.",
        "service.link_error": "GameSense connection error",
        "service.unexpected_error": "Unexpected error: {error}",
        "footer.last_poll": "Last reading: {time}",
        # -- przyciski i przelaczniki --------------------------------------
        "button.refresh": "Refresh",
        "button.pause": "Pause",
        "button.resume": "Resume",
        "button.hardware": "Hardware",
        "button.log": "Log",
        "checkbox.autostart": "Start with Windows (in background, in tray)",
        # -- karta urzadzenia ----------------------------------------------
        "card.charging": "Charging",
        "card.estimating": "estimating...",
        "card.off": "Off",
        "card.unsupported": "Unsupported",
        "card.offline_hint": "Device is not responding — turned off or out of range",
        "card.unsupported_hint": "Battery reading for this model is not confirmed yet",
        "card.charging_in_progress": "Charging in progress",
        "card.since_charge": "On time: {elapsed}",
        "card.since_unknown": "On time: unknown",
        "card.used": "used {used}%",
        "card.per_point": "{duration} per 1%",
        # -- zasobnik systemowy --------------------------------------------
        "tray.show": "Show window",
        "tray.refresh": "Refresh",
        "tray.quit": "Quit",
        "tray.hidden": "The monitor runs in the background, the keyboard screen stays live.",
        "tray.no_devices": "no active devices",
        "tray.charging_short": "(chg.)",
        # -- okno wyboru sprzetu -------------------------------------------
        "picker.title": "Hardware selection",
        "picker.heading": "Select the hardware you own",
        "picker.intro": (
            "Selected devices keep a permanent card in the window — even when they\n"
            "are off. Select nothing to let the program detect what is connected."
        ),
        "picker.keyboard_note": (
            "these pick where to display; battery only on wireless models"
        ),
        "picker.note_no_screen": "no OLED screen",
        "picker.note_no_battery": "no battery reading",
        "picker.warn_no_screen": (
            "{names} has no OLED screen — the program has nowhere to display.\n"
            "Also select a keyboard with a screen, or just watch the window."
        ),
        "picker.warn_no_battery": "None of the selected devices has a battery to monitor.",
        "picker.warn_unsupported": (
            "For the selected devices we have no confirmed battery reading."
        ),
        "picker.clear": "Clear",
        "picker.cancel": "Cancel",
        "picker.save": "Save",
        # -- kategorie urzadzen --------------------------------------------
        "category.keyboard": "Keyboards",
        "category.mouse": "Mice",
        "category.headset": "Headsets",
        # -- panel logu ----------------------------------------------------
        "log.poll": "Battery reading:",
        "log.device_value": "  {name}: {percent}%",
        "log.device_charging_suffix": " (charging)",
        "log.device_no_data": "  {name}: no data (turned off?)",
        "log.device_duplicate": "  {name}: {percent}% - skipping, same device",
        "log.device_bt_duplicate": "  {name}: {percent}% - skipping, already read over HID",
        "log.device_off": "  {name}: off",
        "log.device_unsupported": "  {name}: battery reading not supported",
        "log.runtime": "    runtime: {runtime} ({source}, {rate} pts/h)",
        "log.since_charge": "    on time: {elapsed}{used}",
        "log.used_suffix": ", used {used}%",
        "log.per_point": "    time per 1%: {duration}",
        "log.connected": "Connected to GameSense at {address}.",
        "log.retry": "{message} Retrying in {seconds} s.",
        "log.gg_error": "GameSense communication error: {error}",
        "log.unexpected": "Unexpected error: {error}",
        "log.read_error": "Reading failed ({transport}): {error}",
        "log.settings_failed": "Could not save settings. The previous selection was kept.",
        "log.history_failed": "Could not save battery history.",
        "log.tray_failed": "The system tray is unavailable; the application window was shown: {error}",
        "log.autostart_repaired": "Fixed the path in the Windows autostart entry.",
        "log.autostart_on": "Windows autostart: enabled.",
        "log.autostart_off": "Windows autostart: disabled.",
        "log.autostart_failed": "Could not change autostart (registry access denied).",
        "log.declared": "Declared hardware: {count}.",
        "log.automatic": "Automatic detection selected.",
        "log.language": "Interface language: English.",
        # -- pojedyncza instancja i konsola --------------------------------
        "instance.running": (
            "Battery Monitor is already running.\n\n"
            "Its icon sits in the system tray, next to the clock — "
            "click it to open the window."
        ),
        "console.header": "SteelSeries battery monitor - press Ctrl+C to quit.",
        "console.already_running": (
            "Battery Monitor is already running - close it before starting the console version."
        ),
        "console.stopping": "Stopping...",
        # -- napisy na ekran OLED (tylko ASCII) ----------------------------
        "oled.no_data": "NO DATA",
        "oled.charging": "CHG",
        "oled.label.mouse": "MOUSE",
        "oled.label.headset": "HEADSET",
        "oled.label.keyboard": "KEYBOARD",
        # -- etykiety w oknie ----------------------------------------------
        "window.label.mouse": "MOUSE",
        "window.label.headset": "HEADSET",
        "window.label.keyboard": "KEYBOARD",
    },
}

# Klucze, ktorych tresc trafia na ekran OLED. Test pilnuje, zeby byly czystym
# ASCII w kazdym jezyku - zbinaryzowana czcionka 128x40 nie gwarantuje
# diakrytykow, a "SŁUCHAWKI" wyszloby tam jako "S UCHAWKI".
OLED_KEYS = tuple(
    key for key in _TRANSLATIONS[POLISH] if key.startswith("oled.")
)

_language = DEFAULT_LANGUAGE


# ---------------------------------------------------------------------------
# WYBOR JEZYKA
# ---------------------------------------------------------------------------


def detect():
    """Jezyk interfejsu Windows, zwezony do tego, co program zna.

    Pytamy o jezyk **interfejsu**, a nie o ustawienia regionalne. Ktos moze miec
    polski format daty przy angielskim systemie i odwrotnie; napisy powinny isc
    za jezykiem, w ktorym mowi do niego reszta systemu.
    """
    try:
        langid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
        if langid & _PRIMARY_LANGUAGE_MASK == _LANG_POLISH:
            return POLISH
        return ENGLISH
    except (AttributeError, OSError):
        pass

    # Poza Windows albo gdy API zawiedzie - zostaje ustawienie regionalne.
    try:
        code = (locale.getdefaultlocale()[0] or "").lower()
    except (ValueError, TypeError):
        return DEFAULT_LANGUAGE

    return POLISH if code.startswith("pl") else ENGLISH


def resolve(code):
    """Zamienia zapisane ustawienie na konkretny jezyk.

    "auto" i wszystko nieznane rozwija sie do jezyka systemu, zeby uszkodzony
    albo recznie edytowany plik ustawien nie zostawil programu bez napisow.
    """
    if code in LANGUAGES:
        return code
    return detect()


def set_language(code):
    """Ustawia jezyk napisow. Zwraca faktycznie ustawiony kod."""
    global _language
    _language = resolve(code)
    return _language


def language():
    return _language


def available():
    """Pary (kod, podpis) do przelacznika w oknie."""
    return tuple((code, LANGUAGE_NAMES[code]) for code in LANGUAGES)


# Jezyk startowy bierzemy z Windows. app.py zaraz nadpisze go zapisanym
# ustawieniem, ale pozostale wejscia - tryb konsolowy, skrypty diagnostyczne -
# maja mowic sensownie bez zadnej konfiguracji.
set_language(AUTO)


# ---------------------------------------------------------------------------
# POBIERANIE NAPISOW
# ---------------------------------------------------------------------------


def t(key, **params):
    """Napis pod danym kluczem, z wstawionymi parametrami.

    Nieznany klucz zwraca sam siebie, a nie wyjatek. Brakujace tlumaczenie ma
    byc widoczne w interfejsie jako "card.charging", ale nie ma prawa wywrocic
    okna ani zatrzymac monitora - to napis, nie logika.
    """
    template = _TRANSLATIONS.get(_language, {}).get(key)
    if template is None:
        # Awaryjnie polski: uzupelniany pierwszy, wiec ma najwiecej kluczy.
        template = _TRANSLATIONS[POLISH].get(key)
    if template is None:
        return key

    if not params:
        return template

    try:
        return template.format(**params)
    except (KeyError, IndexError, ValueError):
        # Napis i wywolanie sie rozjechaly. Lepiej pokazac wzorzec niz nic.
        return template


def oled_label(token):
    """Etykieta urzadzenia na ekran OLED, zawsze bez znakow diakrytycznych."""
    slug = _LABEL_SLUGS.get(token)
    return t(f"oled.label.{slug}") if slug else token


def window_label(token):
    """Etykieta urzadzenia w oknie - tu polskie znaki sa dozwolone."""
    slug = _LABEL_SLUGS.get(token)
    return t(f"window.label.{slug}") if slug else token


def keys(code=POLISH):
    """Zbior kluczy danego jezyka - do testu kompletnosci tlumaczen."""
    return set(_TRANSLATIONS[code])


if __name__ == "__main__":
    # python i18n.py - podglad obu jezykow obok siebie.
    print(f"jezyk systemu: {detect()}")
    print(f"kluczy: {len(_TRANSLATIONS[POLISH])}\n")

    for key in sorted(_TRANSLATIONS[POLISH]):
        for code in LANGUAGES:
            set_language(code)
            first_line = t(key).splitlines()[0] if t(key) else ""
            print(f"  {code}  {key:<32} {first_line}")
