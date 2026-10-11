"""Katalog urzadzen SteelSeries - jedno zrodlo prawdy o tym, co program zna.

Tu mieszka tozsamosc sprzetu: nazwa, kategoria, ekran, lacza i identyfikatory USB.
Jak z danym urzadzeniem rozmawiac, opisuja rodziny protokolow w battery_hid.py -
katalog wskazuje je tylko z nazwy, dzieki czemu dodanie modelu obslugujacego
istniejacy protokol to jeden wpis, a nie zmiana w kodzie odczytu.

Dlaczego "model" i "lacze" to dwie osobne rzeczy
------------------------------------------------
Jedno fizyczne urzadzenie zglasza sie pod roznymi Product ID zaleznie od tego,
jak jest podlaczone - mysz na kablu ma inny PID niz ta sama mysz na dongle'u.
Dla programu to jednak ta sama bateria i ta sama historia, wiec katalog trzyma
PID-y pogrupowane per lacze wewnatrz jednego wpisu modelu. Bez tego moment
odlaczenia kabla wypadalby miedzy dwie osobne tozsamosci i czas pracy od
ostatniego naladowania nigdy by sie nie policzyl.

Pole "protocol"
---------------
Puste pole znaczy: model istnieje, jest na liscie, ale odczytu baterii nie mamy
potwierdzonego w zadnym zrodle. Takie pozycje sa widoczne w oknie wyboru jako
nieobslugiwane, zamiast udawac, ze dzialaja. Wpisanie tu zgadywanego PID-u
byloby gorsze niz brak wpisu: program wyslalby komende do niewlasciwego
urzadzenia tego samego producenta.
"""

from typing import NamedTuple

# Kategorie - decyduja o grupowaniu w oknie wyboru sprzetu. Same stale, bez
# nazw do wyswietlenia: te sa w i18n.py pod kluczami "category.<stala>", zeby
# nazwa grupy nie byla zapisana w dwoch miejscach naraz.
KEYBOARD = "keyboard"
MOUSE = "mouse"
HEADSET = "headset"

CATEGORY_ORDER = (KEYBOARD, MOUSE, HEADSET)

# Lacza. Rozdzielone, bo ten sam model ma inny PID i inny czas pracy w kazdym
# z nich, a na kablu dodatkowo raportuje ladowanie.
LINK_24 = "24"
LINK_WIRED = "wired"
LINK_BT = "bt"

LINK_NAMES = {
    LINK_24: "2.4 GHz",
    LINK_WIRED: "kabel",
    LINK_BT: "Bluetooth",
}

# Rozdzielczosc ekranu OLED. GameSense przyjmuje ja w kluczu "device-type"
# jako "screened-128x40", wiec inny ekran wymagalby innego klucza.
SCREEN_128X40 = "128x40"


class Device(NamedTuple):
    """Jeden model sprzetu.

    key         - stabilny identyfikator, ktory trafia do settings.json.
                  Nie zmieniac po wydaniu: uzytkownik stracilby swoj wybor.
    name        - nazwa handlowa, pokazywana w oknie.
    category    - KEYBOARD / MOUSE / HEADSET.
    label       - krotka etykieta na ekran OLED, bez polskich znakow.
    screen      - rozdzielczosc ekranu albo "" gdy model go nie ma.
    wireless    - czy model ma baterie, ktora w ogole da sie monitorowac.
    protocol    - nazwa rodziny protokolu w battery_hid, albo "" gdy nieznany.
    pids        - {lacze: frozenset(Product ID)}.
    rated_hours - {lacze: katalogowy czas pracy w godzinach}.
    bt_names    - nazwy, pod ktorymi Windows pokazuje ten model po Bluetooth.
                  Rzadko pokrywaja sie z nazwa handlowa ("Aerox 5 WL" zamiast
                  "Aerox 5 Wireless"), a po Bluetooth nie ma PID-u, po ktorym
                  mozna by rozpoznac model - zostaje tylko nazwa.
    """

    key: str
    name: str
    category: str
    label: str
    screen: str = ""
    wireless: bool = False
    protocol: str = ""
    pids: dict = {}
    rated_hours: dict = {}
    bt_names: tuple = ()

    @property
    def has_screen(self):
        return bool(self.screen)

    @property
    def battery_supported(self):
        """Czy potrafimy odczytac baterie tego modelu.

        Sam protokol nie wystarczy - bez PID-ow nie ma jak znalezc urzadzenia.
        """
        return bool(self.protocol and self.pids)

    def link_for_pid(self, pid):
        """Zwraca lacze, na ktorym urzadzenie zglasza sie pod danym PID."""
        for link, pids in self.pids.items():
            if pid in pids:
                return link
        return None

    def rated_for(self, link):
        return self.rated_hours.get(link)

    def display_name(self, link=None):
        """Nazwa z dopiskiem lacza, np. "Aerox 5 Wireless (2.4 GHz)".

        Lacze jest w nazwie, bo bateria schodzi na kazdym z nich innym tempem -
        bez tego dopisku nie bylo by widac, skad wzial sie szacunek czasu pracy.
        """
        if link is None or link not in LINK_NAMES:
            return self.name
        return f"{self.name} ({LINK_NAMES[link]})"


# ---------------------------------------------------------------------------
# KATALOG
# ---------------------------------------------------------------------------
#
# Kolejnosc wpisow jest kolejnoscia w oknie wyboru sprzetu.
#
# UWAGA co do PID-ow: wpisane sa tylko wartosci potwierdzone w zrodlach open
# source (rivalcfg, HeadsetControl, OpenRGB) albo zweryfikowane na zywym
# sprzecie. Modele bez potwierdzonych danych maja puste "pids" i "protocol".

_CATALOG = (
    # -- Klawiatury z ekranem OLED, bez baterii -----------------------------
    Device(
        key="apex_pro",
        name="Apex Pro",
        category=KEYBOARD,
        label="KLAWIATURA",
        screen=SCREEN_128X40,
    ),
    Device(
        key="apex_pro_tkl",
        name="Apex Pro TKL",
        category=KEYBOARD,
        label="KLAWIATURA",
        screen=SCREEN_128X40,
    ),
    Device(
        key="apex_7",
        name="Apex 7",
        category=KEYBOARD,
        label="KLAWIATURA",
        screen=SCREEN_128X40,
    ),
    Device(
        key="apex_7_tkl",
        name="Apex 7 TKL",
        category=KEYBOARD,
        label="KLAWIATURA",
        screen=SCREEN_128X40,
    ),
    Device(
        key="apex_5",
        name="Apex 5",
        category=KEYBOARD,
        label="KLAWIATURA",
        screen=SCREEN_128X40,
    ),

    # -- Klawiatury bezprzewodowe ------------------------------------------
    #
    # Zadna z nich nie ma protokolu odczytu baterii i nie jest to przeoczenie.
    # Sprawdzone zostaly OpenRGB, apex-tux, steelseriesgg-rs, rivalcfg oraz
    # sterownik hid-steelseries z jadra Linuksa - zaden nie zna komendy baterii
    # dla klawiatur SteelSeries, a GameSense SDK nie udostepnia jej zadnym
    # zdarzeniem. Klawiatura z ekranem pokazuje wiec baterie myszy i sluchawek,
    # ale swojej wlasnej nie zna.
    Device(
        key="apex_pro_tkl_wireless",
        name="Apex Pro TKL Wireless",
        category=KEYBOARD,
        label="KLAWIATURA",
        screen=SCREEN_128X40,
        wireless=True,
    ),
    Device(
        key="apex_pro_mini_wireless",
        name="Apex Pro Mini Wireless",
        category=KEYBOARD,
        label="KLAWIATURA",
        # Ten model nie ma ekranu OLED, mimo ze jest w tej samej linii co Apex
        # Pro. Okno wyboru ostrzega o tym, bo bez klawiatury z ekranem program
        # nie ma gdzie wyswietlac.
        screen="",
        wireless=True,
    ),

    # -- Myszki bezprzewodowe ----------------------------------------------
    Device(
        key="aerox_3_wireless",
        name="Aerox 3 Wireless",
        category=MOUSE,
        label="MYSZ",
        wireless=True,
        protocol="aerox",
        pids={
            LINK_24: frozenset({0x1838, 0x1878}),
            LINK_WIRED: frozenset({0x183A, 0x187A}),
        },
        # Jedyna myszka, dla ktorej SteelSeries podaje rozbicie na lacza
        # w osobnym artykule pomocy technicznej.
        rated_hours={LINK_24: 80.0, LINK_BT: 200.0},
    ),
    Device(
        key="aerox_5_wireless",
        name="Aerox 5 Wireless",
        category=MOUSE,
        label="MYSZ",
        wireless=True,
        protocol="aerox",
        # Potwierdzone na tym komputerze: 0x1852 odpowiada na dongle'u.
        pids={
            LINK_24: frozenset({0x1852, 0x185C, 0x1860}),
            LINK_WIRED: frozenset({0x1854, 0x185E, 0x1862}),
        },
        rated_hours={LINK_24: 80.0, LINK_BT: 180.0},
        # Nazwa zaobserwowana na tym komputerze w liscie urzadzen Bluetooth.
        bt_names=("Aerox 5 WL",),
    ),
    Device(
        key="aerox_9_wireless",
        name="Aerox 9 Wireless",
        category=MOUSE,
        label="MYSZ",
        wireless=True,
        protocol="aerox",
        pids={
            LINK_24: frozenset({0x1858, 0x1874}),
            LINK_WIRED: frozenset({0x185A, 0x1876}),
        },
        # Producent podaje jedna zbiorcza liczbe "do 180 h", ktora u Aeroxa 5
        # odpowiada Bluetoothowi. Wartosci dla 2.4 GHz nie zgaduje - bez niej
        # program po prostu poczeka na wlasny pomiar.
        rated_hours={LINK_BT: 180.0},
    ),
    Device(
        key="prime_wireless",
        name="Prime Wireless",
        category=MOUSE,
        label="MYSZ",
        wireless=True,
        protocol="aerox",
        pids={
            LINK_24: frozenset({0x1840}),
            LINK_WIRED: frozenset({0x1842}),
        },
        # Ten model nie ma Bluetootha.
        rated_hours={LINK_24: 100.0},
    ),
    Device(
        key="prime_mini_wireless",
        name="Prime Mini Wireless",
        category=MOUSE,
        label="MYSZ",
        wireless=True,
        protocol="aerox",
        # Uwaga: 0x184D to "Prime Mini" - inny, przewodowy produkt, ktory
        # baterii nie ma wcale. Nie wolno go tu dopisac.
        pids={
            LINK_24: frozenset({0x1848}),
            LINK_WIRED: frozenset({0x184A}),
        },
        rated_hours={LINK_24: 100.0},
    ),
    Device(
        key="rival_3_wireless",
        name="Rival 3 Wireless",
        category=MOUSE,
        label="MYSZ",
        wireless=True,
        protocol="rival",
        # Brak PID-u kablowego nie jest luka w danych, a wlasciwoscia sprzetu:
        # ta mysz chodzi na dwoch bateriach AAA i nie ma portu ladowania.
        # 0x1872 to wersja Gen 2 - ten sam protokol.
        pids={LINK_24: frozenset({0x1830, 0x1872})},
        rated_hours={LINK_24: 400.0},
    ),
    Device(
        key="rival_650_wireless",
        name="Rival 650 Wireless",
        category=MOUSE,
        label="MYSZ",
        wireless=True,
        # Osobna rodzina, bo to jedyny model siedzacy na interfejsie 0.
        protocol="rival_650",
        pids={
            LINK_24: frozenset({0x1726}),
            LINK_WIRED: frozenset({0x172B}),
        },
        rated_hours={LINK_24: 24.0},
    ),

    # -- Sluchawki bezprzewodowe -------------------------------------------
    Device(
        key="arctis_nova_pro_wireless",
        name="Arctis Nova Pro Wireless",
        category=HEADSET,
        label="SLUCHAWKI",
        wireless=True,
        protocol="nova_pro",
        # PID-y stacji bazowej - same sluchawki nie maja PID-u widocznego
        # dla komputera.
        pids={LINK_24: frozenset({0x12E0, 0x12E5})},
        # 22 h na jedna baterie; ten model ma dwie wymienne.
        rated_hours={LINK_24: 22.0},
    ),
    Device(
        key="arctis_nova_7",
        name="Arctis Nova 7",
        category=HEADSET,
        label="SLUCHAWKI",
        wireless=True,
        protocol="nova_7",
        # Wszystkie warianty Nova 7, 7P i 7X. Potwierdzone na tym komputerze:
        # 0x227e (Gen 2). Czesc PID-ow raportuje baterie w krokach 0-4, czesc
        # w procentach - podzial jest w battery_hid._NOVA_7_DISCRETE_PIDS,
        # bo zalezy wylacznie od PID-u i nie da sie go poznac z odpowiedzi.
        pids={
            LINK_24: frozenset({
                0x2202, 0x22A1, 0x227E, 0x2206, 0x2258, 0x229E,
                0x22AD, 0x223A, 0x22A9, 0x227A, 0x22A4, 0x22A5,
                # Warianty 7P: 0x220a przed aktualizacja firmware ze stycznia
                # 2026, 0x22a7 po niej.
                0x220A, 0x22A7,
            }),
        },
        # Dane dla Gen 2, czyli egzemplarza z tego komputera. Pierwsza
        # generacja ma katalogowo 38 h przy 2.4 GHz.
        rated_hours={LINK_24: 54.0, LINK_BT: 42.0},
    ),
    Device(
        key="arctis_nova_5",
        name="Arctis Nova 5",
        category=HEADSET,
        label="SLUCHAWKI",
        wireless=True,
        # Ta sama komenda co Nova 7, ale zupelnie inny uklad odpowiedzi -
        # dlatego osobna rodzina, a nie wspolna.
        protocol="nova_5",
        # 0x2264 to nowsza rewizja 5X, dla ktorej protokol nie jest
        # potwierdzony. Swiadomie jej tu nie ma.
        pids={LINK_24: frozenset({0x2232, 0x2253})},
        rated_hours={LINK_24: 60.0},
    ),
    Device(
        key="arctis_nova_4",
        name="Arctis Nova 4",
        category=HEADSET,
        label="SLUCHAWKI",
        wireless=True,
        # Ten model nie wystepuje w HeadsetControl w zadnej postaci: brak pliku
        # urzadzenia, brak wpisu w rejestrze, brak wiersza w dokumentacji.
        # Nie znamy wiec ani jednego PID-u i nie ma czego zgadywac.
    ),
    Device(
        key="arctis_pro_wireless",
        name="Arctis Pro Wireless",
        category=HEADSET,
        label="SLUCHAWKI",
        wireless=True,
        protocol="arctis_pro_wireless",
        pids={LINK_24: frozenset({0x1290})},
        # Monitorujemy jedna wlozona baterie: 10 h. Reklamowane 20 h to suma
        # obu akumulatorow; uzycie jej tutaj podwajaloby przewidywany czas.
        # https://steelseries.com/press/52-steelseries-launches-arctis-pro-headset-line-first-high-fidelity-and-high
        rated_hours={LINK_24: 10.0},
    ),
    Device(
        key="arctis_9",
        name="Arctis 9",
        category=HEADSET,
        label="SLUCHAWKI",
        wireless=True,
        protocol="arctis_9",
        # Wariant 9X nie wystepuje w zadnym zrodle - jest tylko ten jeden PID.
        pids={LINK_24: frozenset({0x12C2})},
        rated_hours={LINK_24: 20.0},
    ),
    Device(
        key="arctis_7",
        name="Arctis 7",
        category=HEADSET,
        label="SLUCHAWKI",
        wireless=True,
        protocol="arctis_7",
        # HeadsetControl obsluguje tez 0x1252 (Arctis Pro) i 0x1280 (GameDAC)
        # tym protokolem, ale to sluchawki przewodowe bez baterii. Nie sa wiec
        # modelami monitora baterii, nawet jesli odpowiadaja na te sama komende.
        # https://support.steelseries.com/hc/en-us/articles/360001702631
        pids={LINK_24: frozenset({0x1260, 0x12AD})},
        rated_hours={LINK_24: 24.0},
    ),
    Device(
        key="arctis_7_plus",
        name="Arctis 7+ (7P+, 7X+)",
        category=HEADSET,
        label="SLUCHAWKI",
        wireless=True,
        # Trzeci uklad odpowiedzi na komende 0x00 0xb0, obok Nova 7 i Nova 5.
        protocol="arctis_7_plus",
        pids={LINK_24: frozenset({0x220E, 0x2212, 0x2216, 0x2236})},
        rated_hours={LINK_24: 30.0},
    ),
    Device(
        key="arctis_7p_7x",
        name="Arctis 7P / 7X",
        category=HEADSET,
        label="SLUCHAWKI",
        wireless=True,
        # Wbrew nazwie te modele nie dziela protokolu z Arctis 7 ani z 7+,
        # tylko z Arctis 1 Wireless. To nie pomylka, a uklad zrodel.
        protocol="arctis_1",
        pids={LINK_24: frozenset({0x12D5, 0x12D7})},
    ),
    Device(
        key="arctis_1_wireless",
        name="Arctis 1 Wireless",
        category=HEADSET,
        label="SLUCHAWKI",
        wireless=True,
        protocol="arctis_1",
        pids={LINK_24: frozenset({0x12B3, 0x12B6})},
        rated_hours={LINK_24: 20.0},
    ),
)

_BY_KEY = {device.key: device for device in _CATALOG}


# ---------------------------------------------------------------------------
# WYSZUKIWANIE
# ---------------------------------------------------------------------------


def all_devices():
    return _CATALOG


def by_key(key):
    return _BY_KEY.get(key)


def by_category(category):
    return tuple(device for device in _CATALOG if device.category == category)


def find_by_pid(pid):
    """Zwraca (urzadzenie, lacze) dla danego Product ID, albo (None, None)."""
    for device in _CATALOG:
        link = device.link_for_pid(pid)
        if link is not None:
            return device, link
    return None, None


def find_by_bt_name(reported_name):
    """Dopasowuje nazwe urzadzenia Bluetooth z Windows do modelu z katalogu.

    Zwraca None, gdy nazwy nie znamy. Dopasowanie jest po nazwie handlowej
    i jawnej liscie aliasow,
    a nie po podobienstwie tekstu: "Arctis 7" jest przedrostkiem "Arctis 7P",
    wiec zgadywanie po fragmencie wybieraloby zly model i przypisywalo mu obcy
    katalogowy czas pracy.
    """
    if not isinstance(reported_name, str):
        return None
    normalized = reported_name.strip().casefold()
    for device in _CATALOG:
        if not device.wireless:
            continue
        for alias in (device.name,) + device.bt_names:
            if alias.casefold() == normalized:
                return device
    return None


_LINK_BY_DISPLAY_NAME = {display: link for link, display in LINK_NAMES.items()}


def resolve_battery(reported_name):
    """Mapuje nazwe urzadzenia na (klucz baterii, lacze), albo (None, None).

    Sluzy przeniesieniu starych historii, ktore kluczowane byly nazwa modelu
    zamiast kluczem katalogowym - i to osobno dla kazdego lacza, bo mysz po
    Bluetooth Windows nazywa inaczej niz ta sama mysz na dongle'u. Nazwy
    dopasowujemy dokladnie, tak samo jak find_by_bt_name i z tego samego
    powodu: "Arctis 7" jest przedrostkiem "Arctis 7P".
    """
    text = (reported_name or "").strip()
    if not text:
        return None, None

    link = None
    if text.endswith(")") and "(" in text:
        head, _, tail = text.rpartition("(")
        display = tail[:-1].strip()
        if display in _LINK_BY_DISPLAY_NAME:
            link = _LINK_BY_DISPLAY_NAME[display]
            text = head.strip()

    normalized = text.casefold()
    for device in _CATALOG:
        if device.name.casefold() == normalized:
            return device.key, link or LINK_24

    known = find_by_bt_name(text)
    if known is not None:
        return known.key, link or LINK_BT

    return None, None


def monitorable():
    """Modele, ktorych baterie potrafimy odczytac."""
    return tuple(device for device in _CATALOG if device.battery_supported)


def screens():
    """Modele wyposazone w ekran OLED."""
    return tuple(device for device in _CATALOG if device.has_screen)


if __name__ == "__main__":
    # Przeglad katalogu: python device_catalog.py
    for category in CATEGORY_ORDER:
        print(f"\n{category.upper()}")
        for device in by_category(category):
            marks = []
            if device.has_screen:
                marks.append(f"ekran {device.screen}")
            if device.wireless:
                marks.append("bateria: " + ("tak" if device.battery_supported else "brak protokolu"))
            print(f"  {device.name:<26} {', '.join(marks)}")
