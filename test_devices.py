"""Testy katalogu urzadzen, ustawien i trybu deklaracji.

Uruchomienie:  python test_devices.py

Bez pytest i bez sprzetu. Najwazniejsze sa tu testy spojnosci katalogu: wpisuje
sie go recznie, a jedna literowka w Product ID wyslalaby komende konfiguracyjna
do niewlasciwego urzadzenia tego samego producenta.
"""

import os
import shutil
import sys
import tempfile

import autostart
import battery_hid
import device_catalog
import monitor_service
import paths
import settings as app_settings
import single_instance

_failures = []


def check(condition, description, detail=""):
    if condition:
        print(f"  OK   {description}")
    else:
        print(f"  BLAD {description}" + (f" -> {detail}" if detail else ""))
        _failures.append(description)


# ---------------------------------------------------------------------------
# 1. SPOJNOSC KATALOGU
# ---------------------------------------------------------------------------


def test_unique_keys():
    print("\n1a. Klucze modeli sa unikalne")
    keys = [device.key for device in device_catalog.all_devices()]
    duplicates = {key for key in keys if keys.count(key) > 1}

    check(not duplicates, "brak powtorzonych kluczy", f"{duplicates}")
    check(len(keys) > 0, "katalog nie jest pusty")
    check(
        all(key == key.lower() and " " not in key for key in keys),
        "klucze sa w jednym formacie, bez spacji",
    )


def test_unique_pids():
    print("\n1b. Jeden Product ID nie wskazuje dwoch modeli")
    owners = {}
    collisions = []

    for device in device_catalog.all_devices():
        for link, pids in device.pids.items():
            for pid in pids:
                previous = owners.get(pid)
                if previous is not None:
                    collisions.append((hex(pid), previous, f"{device.key}/{link}"))
                owners[pid] = f"{device.key}/{link}"

    check(not collisions, "brak kolizji PID-ow miedzy modelami", f"{collisions}")


def test_pid_never_in_two_links():
    print("\n1c. Ten sam PID nie jest jednoczesnie kablowy i bezprzewodowy")
    problems = []
    for device in device_catalog.all_devices():
        links = list(device.pids)
        for index, link in enumerate(links):
            for other in links[index + 1:]:
                shared = device.pids[link] & device.pids[other]
                if shared:
                    problems.append((device.key, link, other, [hex(p) for p in shared]))

    # Lacze decyduje o tresci komendy, wiec PID w dwoch laczach oznacza, ze
    # program wysylalby raz jedna, raz druga komende do tego samego urzadzenia.
    check(not problems, "kazdy PID nalezy do jednego lacza", f"{problems}")


def test_protocols_exist():
    print("\n1d. Kazdy protokol z katalogu ma implementacje")
    missing = [
        (device.key, device.protocol)
        for device in device_catalog.all_devices()
        if device.protocol and device.protocol not in battery_hid.PROTOCOL_FAMILIES
    ]
    check(not missing, "brak odwolan do nieistniejacych protokolow", f"{missing}")


def test_every_link_has_request():
    print("\n1e. Dla kazdego lacza z PID-ami wiadomo, o co zapytac")
    missing = []
    for device in device_catalog.monitorable():
        family = battery_hid.PROTOCOL_FAMILIES[device.protocol]
        for link in device.pids:
            if link not in family["requests"]:
                missing.append((device.key, link))

    # Bez komendy dla danego lacza odczyt cicho zwracalby brak danych, a wpis
    # w katalogu sugerowalby, ze urzadzenie jest obslugiwane.
    check(not missing, "kazde lacze z PID-ami ma komende", f"{missing}")


def test_pid_lookup():
    print("\n1f. Wyszukiwanie po PID zwraca model i lacze")
    device, link = device_catalog.find_by_pid(0x1852)
    check(device is not None and device.key == "aerox_5_wireless", "0x1852 to Aerox 5 Wireless")
    check(link == device_catalog.LINK_24, "0x1852 to lacze bezprzewodowe", f"{link}")

    wired, wired_link = device_catalog.find_by_pid(0x1854)
    check(
        wired is not None and wired.key == "aerox_5_wireless",
        "0x1854 to ta sama mysz",
    )
    check(wired_link == device_catalog.LINK_WIRED, "0x1854 to lacze kablowe", f"{wired_link}")

    unknown, unknown_link = device_catalog.find_by_pid(0xFFFF)
    check(unknown is None and unknown_link is None, "nieznany PID nie jest do niczego dopasowany")


def test_screens():
    print("\n1g. Ekrany OLED sa oznaczone zgodnie z rzeczywistoscia")
    mini = device_catalog.by_key("apex_pro_mini_wireless")
    check(mini is not None and not mini.has_screen, "Apex Pro Mini Wireless nie ma ekranu")

    apex_pro = device_catalog.by_key("apex_pro")
    check(apex_pro is not None and apex_pro.has_screen, "Apex Pro ma ekran")
    check(
        apex_pro.screen == device_catalog.SCREEN_128X40,
        "ekran ma rozdzielczosc obslugiwana przez renderer",
        f"{apex_pro.screen}",
    )

    resolutions = {device.screen for device in device_catalog.screens()}
    # Renderer i handler GameSense sa napisane pod 128x40. Inna rozdzielczosc
    # w katalogu wymagalaby innego klucza "device-type" w SDK.
    check(
        resolutions == {device_catalog.SCREEN_128X40},
        "wszystkie ekrany w katalogu to 128x40",
        f"{resolutions}",
    )


def test_categories_and_labels():
    print("\n1h. Kategorie i etykiety nadaja sie do wyswietlenia")
    for device in device_catalog.all_devices():
        if device.category not in device_catalog.CATEGORY_ORDER:
            check(False, f"znana kategoria: {device.key}", device.category)
            return
        if not device.label.isascii() or device.label != device.label.upper():
            # OLED nie gwarantuje poprawnego rysowania polskich znakow.
            check(False, f"etykieta bez znakow diakrytycznych: {device.key}", device.label)
            return

    check(True, "wszystkie kategorie sa znane")
    check(True, "wszystkie etykiety sa wielkimi literami ASCII")


def test_display_name():
    print("\n1i. Nazwa zawiera lacze, bo od niego zalezy czas pracy")
    aerox = device_catalog.by_key("aerox_5_wireless")

    check(aerox.display_name(device_catalog.LINK_24) == "Aerox 5 Wireless (2.4 GHz)", "2.4 GHz")
    check(aerox.display_name(device_catalog.LINK_WIRED) == "Aerox 5 Wireless (kabel)", "kabel")
    check(aerox.display_name(None) == "Aerox 5 Wireless", "bez lacza sama nazwa")


def test_rated_hours_differ_between_links():
    print("\n1j. Czas katalogowy jest przypisany do lacza")
    aerox = device_catalog.by_key("aerox_5_wireless")
    on_dongle = aerox.rated_for(device_catalog.LINK_24)
    on_bluetooth = aerox.rated_for(device_catalog.LINK_BT)

    check(close(on_dongle, 80.0), "2.4 GHz: 80 h", f"{on_dongle}")
    check(close(on_bluetooth, 180.0), "Bluetooth: 180 h", f"{on_bluetooth}")
    check(aerox.rated_for("cos") is None, "nieznane lacze nie ma czasu katalogowego")


def close(value, expected):
    return value is not None and abs(value - expected) < 0.01


def test_bt_name_lookup():
    print("\n1k. Rozpoznawanie modelu po nazwie z Bluetooth")
    found = device_catalog.find_by_bt_name("Aerox 5 WL")
    check(found is not None and found.key == "aerox_5_wireless", "Aerox 5 WL to Aerox 5 Wireless")

    spaced = device_catalog.find_by_bt_name("  aerox 5 wl  ")
    check(spaced is not None, "dopasowanie nie zalezy od spacji i wielkosci liter")

    check(device_catalog.find_by_bt_name("Aerox") is None, "sam fragment nazwy nie wystarcza")
    check(device_catalog.find_by_bt_name("Cos obcego") is None, "obca nazwa nie jest dopasowana")


# ---------------------------------------------------------------------------
# 2. USTAWIENIA
# ---------------------------------------------------------------------------


class Workspace:
    def __init__(self):
        self.directory = tempfile.mkdtemp(prefix="devices_test_")

    def path(self, name="settings.json"):
        return os.path.join(self.directory, name)

    def cleanup(self):
        shutil.rmtree(self.directory, ignore_errors=True)


def test_settings_default_is_automatic(workspace):
    print("\n2a. Bez pliku ustawien dziala tryb automatyczny")
    config = app_settings.Settings(workspace.path())

    check(config.automatic is True, "tryb automatyczny")
    check(config.declared == (), "pusta lista zadeklarowanych")


def test_settings_round_trip(workspace):
    print("\n2b. Wybor sprzetu przetrwa restart")
    config = app_settings.Settings(workspace.path())
    config.set_declared(["aerox_5_wireless", "arctis_nova_7"])

    reopened = app_settings.Settings(workspace.path())
    check(
        reopened.declared == ("aerox_5_wireless", "arctis_nova_7"),
        "kolejnosc i zawartosc zachowane",
        f"{reopened.declared}",
    )
    check(reopened.automatic is False, "tryb deklaracji")
    check(reopened.is_declared("arctis_nova_7") is True, "sprawdzenie pojedynczego modelu")
    check(reopened.is_declared("apex_5") is False, "niezadeklarowany model")


def test_settings_deduplicates(workspace):
    print("\n2c. Powtorzony wybor nie duplikuje karty")
    config = app_settings.Settings(workspace.path())
    config.set_declared(["aerox_5_wireless", "aerox_5_wireless", "arctis_nova_7"])

    check(
        config.declared == ("aerox_5_wireless", "arctis_nova_7"),
        "duplikat usuniety, kolejnosc zachowana",
        f"{config.declared}",
    )


def test_settings_empty_returns_to_automatic(workspace):
    print("\n2d. Wyczyszczenie listy wraca do trybu automatycznego")
    config = app_settings.Settings(workspace.path())
    config.set_declared(["aerox_5_wireless"])
    config.set_declared([])

    check(config.automatic is True, "znow tryb automatyczny")
    check(app_settings.Settings(workspace.path()).automatic is True, "tak samo po ponownym wczytaniu")


def test_settings_survives_corruption(workspace):
    print("\n2e. Uszkodzony plik ustawien nie wywraca programu")
    path = workspace.path()
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("{to nie jest poprawny JSON")

    config = app_settings.Settings(path)
    check(config.automatic is True, "start w trybie automatycznym")

    config.set_declared(["apex_5"])
    check(app_settings.Settings(path).declared == ("apex_5",), "zapis dziala po naprawie")


def test_settings_ignores_junk(workspace):
    print("\n2f. Obce wpisy w pliku sa pomijane")
    path = workspace.path()
    with open(path, "w", encoding="utf-8") as handle:
        handle.write('{"declared": ["aerox_5_wireless", 42, null, "arctis_nova_7"]}')

    config = app_settings.Settings(path)
    check(
        config.declared == ("aerox_5_wireless", "arctis_nova_7"),
        "zostaly tylko poprawne klucze",
        f"{config.declared}",
    )


# ---------------------------------------------------------------------------
# 3. TRYB DEKLARACJI
# ---------------------------------------------------------------------------


def test_offline_entry_for_declared_device():
    print("\n3a. Zadeklarowane, ale niewykryte urzadzenie ma wpis")
    entries = monitor_service._offline_entries([], ("aerox_5_wireless",), lambda message: None)

    check(len(entries) == 1, "jeden wpis", f"{len(entries)}")
    if entries:
        entry = entries[0]
        check(entry["online"] is False, "oznaczone jako niedostepne")
        check(entry["supported"] is True, "odczyt baterii jest obslugiwany")
        check(entry["percent"] is None, "brak poziomu baterii zamiast zera")
        check(entry["label"] == "MYSZ", "etykieta z katalogu")
        check(entry["runtime"] == "", "brak szacunku czasu pracy")


def test_no_offline_entry_when_already_read():
    print("\n3b. Wykryte urzadzenie nie dostaje drugiego wpisu")
    readings = [{"key": "aerox_5_wireless", "online": True}]
    entries = monitor_service._offline_entries(
        readings, ("aerox_5_wireless",), lambda message: None
    )

    check(entries == [], "brak duplikatu karty")


def test_no_offline_entries_in_automatic_mode():
    print("\n3c. Tryb automatyczny nie wymysla kart")
    entries = monitor_service._offline_entries([], (), lambda message: None)

    # Bez deklaracji brak odpowiedzi znaczy tylko tyle, ze niczego nie znalezlismy.
    check(entries == [], "brak wpisow bez deklaracji")


def test_wired_keyboard_has_no_battery_card():
    print("\n3d. Klawiatura przewodowa nie dostaje karty baterii")
    entries = monitor_service._offline_entries([], ("apex_pro",), lambda message: None)

    check(entries == [], "Apex Pro jest ekranem, nie urzadzeniem z bateria")


def test_unsupported_device_is_marked():
    print("\n3e. Model bez protokolu jest oznaczony, a nie podany jako wylaczony")
    # Arctis Nova 4 to jedyne sluchawki z listy, ktorych nie zna zadne zrodlo -
    # nie mamy dla nich ani PID-u, ani komendy.
    entries = monitor_service._offline_entries([], ("arctis_nova_4",), lambda message: None)

    check(len(entries) == 1, "wpis istnieje", f"{len(entries)}")
    if entries:
        check(entries[0]["supported"] is False, "oznaczony jako nieobslugiwany")
        check(entries[0]["online"] is False, "i jako niedostepny")


def test_unknown_declared_key_is_ignored():
    print("\n3f. Nieznany klucz w ustawieniach jest pomijany")
    entries = monitor_service._offline_entries([], ("cos_czego_nie_ma",), lambda message: None)

    # Klucz moze zostac w pliku po usunieciu modelu z katalogu.
    check(entries == [], "brak wpisu i brak wyjatku")


# ---------------------------------------------------------------------------
# 4. PARSERY ODPOWIEDZI
# ---------------------------------------------------------------------------
#
# Mam pod reka dwa urzadzenia z katalogu, a rodzin protokolow jest jedenascie.
# Dla pozostalych to jedyna dostepna weryfikacja: skladam odpowiedz taka, jaka
# opisuja zrodla, i sprawdzam, czy parser czyta wlasciwe bajty we wlasciwej
# skali. Blad przesuniecia o jeden bajt daje tu poziom baterii odczytany
# z bajtu statusu - liczbe wygladajaca sensownie i zupelnie nieprawdziwa.


def frame(length, **positions):
    """Buduje odpowiedz o danej dlugosci z wartosciami na wskazanych pozycjach."""
    data = [0] * length
    for index, value in positions.items():
        data[int(index[1:])] = value
    return data


def test_parse_aerox():
    print("\n4a. Myszki Aerox i Prime: numer kroku po 5 punktow")
    parse = battery_hid._parse_aerox

    check(parse(0x1852, [0xD2, 0x15]) == (100, False), "krok 21 to pelne 100%")
    check(parse(0x1852, [0xD2, 0x0B]) == (50, False), "krok 11 to 50%")
    check(parse(0x1852, [0xD2, 0x95]) == (100, True), "najstarszy bit oznacza ladowanie")
    # Wylaczona mysz zwraca 0xff, co po przeliczeniu daje 630%.
    check(parse(0x1852, [0xD2, 0xFF]) is None, "wartosc poza zakresem to brak danych")
    check(parse(0x1852, [0xD2]) is None, "za krotka odpowiedz to brak danych")


def test_parse_rival():
    print("\n4b. Myszki Rival: procent w bajcie zerowym, bez echa")
    parse = battery_hid._parse_rival

    check(parse(0x1830, [50, 0, 0]) == (50, False), "poziom brany wprost")
    check(parse(0x1830, [50, 0, 1]) == (50, True), "ladowanie w trzecim bajcie")
    check(parse(0x1830, [200, 0, 0]) is None, "ponad 100% to brak danych")
    check(parse(0x1830, [50, 0]) is None, "za krotka odpowiedz to brak danych")


def test_parse_nova_7_depends_on_pid():
    print("\n4c. Nova 7: ta sama odpowiedz znaczy co innego zaleznie od PID-u")
    parse = battery_hid._parse_nova_7
    discrete = frame(4, b2=4, b3=0x03)

    # 0x2202 raportuje krokami 0-4, 0x227e w procentach. Tej roznicy nie da sie
    # poznac z tresci odpowiedzi - stad jawna lista PID-ow w battery_hid.
    check(parse(0x2202, discrete) == (100, False), "0x2202: krok 4 to 100%")
    check(parse(0x227E, discrete) == (4, False), "0x227e: te same bajty to 4%")

    check(parse(0x227E, frame(4, b2=73, b3=0x03)) == (73, False), "procent brany wprost")
    check(parse(0x2202, frame(4, b2=2, b3=0x03)) == (50, False), "krok 2 to 50%")
    check(parse(0x227E, frame(4, b2=80, b3=0x01)) == (80, True), "status 0x01 to ladowanie")
    check(parse(0x227E, frame(4, b2=80, b3=0x02)) == (80, True), "status 0x02 tez to ladowanie")
    check(parse(0x227E, frame(4, b2=80, b3=0x00)) is None, "status 0x00 to wylaczone sluchawki")


def test_parse_nova_5():
    print("\n4d. Nova 5: ta sama komenda co Nova 7, inne bajty")
    parse = battery_hid._parse_nova_5

    check(parse(0x2232, frame(16, b1=0x08, b3=64, b4=0x00)) == (64, False), "poziom z bajtu 3")
    check(parse(0x2232, frame(16, b1=0x08, b3=64, b4=0x01)) == (64, True), "ladowanie z bajtu 4")
    check(parse(0x2232, frame(16, b1=0x02, b3=64)) is None, "bajt 1 rowny 0x02 to offline")
    check(parse(0x2232, frame(8, b3=64)) is None, "za krotka odpowiedz to brak danych")


def test_nova_families_read_different_bytes():
    print("\n4e. Trzy rodziny dziela komende 0x00 0xb0 i musza sie roznic")
    # Gdyby ktos kiedys zechcial je scalic w jedna rodzine: ta sama ramka daje
    # trzy rozne odpowiedzi, bo poziom baterii siedzi w innym bajcie i w innej
    # skali. To jest powod, dla ktorego sa osobne.
    shared = frame(16, b1=0x08, b2=2, b3=60, b4=0x00)

    nova_7 = battery_hid._parse_nova_7(0x2202, shared)
    nova_5 = battery_hid._parse_nova_5(0x2232, shared)
    seven_plus = battery_hid._parse_arctis_7_plus(0x220E, shared)

    check(nova_7 == (50, False), "Nova 7 czyta krok z bajtu 2", f"{nova_7}")
    check(nova_5 == (60, False), "Nova 5 czyta procent z bajtu 3", f"{nova_5}")
    check(seven_plus == (50, False), "Arctis 7+ czyta krok z bajtu 2", f"{seven_plus}")
    check(nova_5 != nova_7, "wyniki sie roznia, wiec rodzin nie wolno scalic")


def test_parse_nova_pro():
    print("\n4f. Nova Pro: skala 0-8 i trzystanowy bajt zasilania")
    parse = battery_hid._parse_nova_pro

    check(parse(0x12E0, frame(16, b6=8, b15=0x08)) == (100, False), "krok 8 to 100%")
    check(parse(0x12E0, frame(16, b6=4, b15=0x08)) == (50, False), "krok 4 to 50%")
    check(parse(0x12E0, frame(16, b6=4, b15=0x02)) == (50, True), "0x02 to ladowanie z kabla")
    check(parse(0x12E0, frame(16, b6=4, b15=0x01)) is None, "0x01 to sluchawki odlozone")
    check(parse(0x12E0, frame(16, b6=4, b15=0x05)) is None, "nieznany stan to brak danych")


def test_parse_arctis_7_plus():
    print("\n4g. Arctis 7+: offline w bajcie 1, ladowanie w bajcie 3")
    parse = battery_hid._parse_arctis_7_plus

    check(parse(0x220E, frame(4, b2=4)) == (100, False), "krok 4 to 100%")
    check(parse(0x220E, frame(4, b2=1)) == (25, False), "krok 1 to 25%")
    check(parse(0x220E, frame(4, b2=2, b3=0x01)) == (50, True), "ladowanie z bajtu 3")
    check(parse(0x220E, frame(4, b1=0x01, b2=2)) is None, "offline z bajtu 1")


def test_parse_arctis_7_and_1():
    print("\n4h. Arctis 7 i Arctis 1: procent wprost, bez informacji o ladowaniu")
    check(battery_hid._parse_arctis_7(0x1260, frame(8, b2=88)) == (88, False), "Arctis 7: bajt 2")
    check(
        battery_hid._parse_arctis_7(0x1260, frame(8, b2=200)) == (100, False),
        "Arctis 7: wartosc powyzej 100 przycieta",
    )

    parse_1 = battery_hid._parse_arctis_1
    check(parse_1(0x12B3, frame(8, b3=88)) == (88, False), "Arctis 1: bajt 3")
    check(parse_1(0x12B3, frame(8, b2=0x01, b3=88)) is None, "Arctis 1: offline z bajtu 2")
    # Oba modele nie raportuja ladowania - kabel podlaczymy, a program pokaze
    # tylko rosnacy poziom baterii.
    check(parse_1(0x12B3, frame(8, b3=88))[1] is False, "ladowanie nigdy nie jest zglaszane")


def test_parse_arctis_9_clamps():
    print("\n4i. Arctis 9: surowy zakres 0x64-0x9A, z przycieciem")
    parse = battery_hid._parse_arctis_9

    check(parse(0x12C2, frame(5, b3=0x64)) == (0, False), "dolna granica to 0%")
    check(parse(0x12C2, frame(5, b3=0x9A)) == (100, False), "gorna granica to 100%")
    check(parse(0x12C2, frame(5, b3=0x9A, b4=0x01)) == (100, True), "ladowanie z bajtu 4")

    # Bez przyciecia wejscia ten odczyt dalby wartosc ujemna, bo dolna granica
    # nie jest zerem. To wlasnie ta poprawka, ktora HeadsetControl dodal
    # przy przepisywaniu na C++.
    below = parse(0x12C2, frame(5, b3=0x10))
    check(below == (0, False), "odczyt ponizej zakresu to 0%, nie liczba ujemna", f"{below}")


def test_parse_arctis_pro_wireless():
    print("\n4j. Arctis Pro Wireless: jeden bajt odpowiedzi")
    parse = battery_hid._parse_arctis_pro_wireless

    check(parse(0x1290, [4]) == (100, False), "krok 4 to 100%")
    check(parse(0x1290, [2]) == (50, False), "krok 2 to 50%")
    check(parse(0x1290, []) is None, "pusta odpowiedz to brak danych")

    family = battery_hid.PROTOCOL_FAMILIES["arctis_pro_wireless"]
    # Ten protokol jako jedyny wymaga dwoch zapytan: samo zapytanie o baterie
    # nie mowi, czy sluchawki sa w ogole wlaczone.
    check(family["status_offline"]([0x02]) is True, "bajt 0x02 z zapytania o status to offline")
    check(family["status_offline"]([0x04]) is False, "bajt 0x04 to sluchawki obecne")


def test_expected_echo_follows_request():
    print("\n4k. Oczekiwane echo bierze sie z komendy, nie z rodziny")
    family = battery_hid.PROTOCOL_FAMILIES["aerox"]
    wireless = family["requests"][device_catalog.LINK_24]
    wired = family["requests"][device_catalog.LINK_WIRED]

    # Ta sama mysz pyta o baterie innym opcodem na kablu niz na dongle'u.
    # Echo przypisane do rodziny odcieloby odczyt na jednym z tych lacz.
    check(wireless[-1] == 0xD2, "bezprzewodowo opcode 0xd2", f"{wireless}")
    check(wired[-1] == 0x92, "na kablu opcode 0x92", f"{wired}")
    check(wireless[-1] != wired[-1], "opcody sa rozne, wiec echo musi byc wyliczane")


def test_families_declare_complete_fields():
    print("\n4l. Kazda rodzina protokolu ma pelny opis")
    required = ("interface", "usage_page", "requests", "read_length", "echoes_opcode", "parse")
    problems = []

    for name, family in battery_hid.PROTOCOL_FAMILIES.items():
        for field in required:
            if field not in family:
                problems.append((name, field))
        if family.get("status_request") is not None:
            # Protokol dwuetapowy potrzebuje jeszcze dlugosci i warunku offline.
            for field in ("status_length", "status_offline"):
                if field not in family:
                    problems.append((name, field))

    check(not problems, "wszystkie rodziny sa opisane w calosci", f"{problems}")


# ---------------------------------------------------------------------------
# 5. PROGRAM, AUTOSTART I POJEDYNCZA INSTANCJA
# ---------------------------------------------------------------------------


def test_data_and_resource_paths():
    print("\n5a. Dane i zasoby maja rozne miejsca")
    data = paths.data_file("battery_timeline.json")

    check(
        os.path.dirname(data) == paths.data_dir(),
        "plik danych lezy w katalogu danych",
        data,
    )
    # W wersji spakowanej katalog obok .exe jest tymczasowy i znika przy
    # zamknieciu, wiec dane nie moga tam trafiac.
    check(
        paths.APP_DIR_NAME in data or paths.data_dir() == os.path.dirname(paths.__file__),
        "katalog danych nalezy do uzytkownika",
        paths.data_dir(),
    )

    icon = paths.resource_path("assets", "icon.ico")
    check(os.path.isfile(icon), "ikona jest tam, gdzie program jej szuka", icon)


def test_program_path_is_runnable():
    print("\n5b. Polecenie uruchamiajace program wskazuje istniejacy plik")
    parts = paths.program_path()

    check(len(parts) >= 1, "polecenie nie jest puste")
    check(os.path.isfile(parts[0]), "pierwszy czlon istnieje", parts[0])

    if not paths.is_frozen():
        # Ze zrodel uruchamiamy interpreter ze skryptem, a nie sam skrypt.
        check(len(parts) == 2, "ze zrodel: interpreter plus skrypt", f"{parts}")
        check(os.path.isfile(parts[1]), "skrypt istnieje", parts[1])
        check(
            os.path.basename(parts[0]).lower() == "pythonw.exe",
            "uzywamy pythonw.exe, zeby nie migala konsola",
            parts[0],
        )


def test_autostart_command():
    print("\n5c. Wpis autostartu jest poprawnie zbudowany")
    command = autostart.command()

    check(command.endswith(autostart.TRAY_ARGUMENT), "konczy sie flaga zasobnika", command)
    check(command.count('"') % 2 == 0, "cudzyslowy sa domkniete")
    # Bez cudzyslowow Windows probowalby uruchomic sam pierwszy czlon sciezki,
    # gdy program lezy w katalogu ze spacja w nazwie.
    check(command.startswith('"'), "sciezka jest w cudzyslowach", command)

    for part in paths.program_path():
        check(f'"{part}"' in command, f"polecenie zawiera {os.path.basename(part)}")


def test_autostart_reports_state_without_changing_it():
    print("\n5d. Sprawdzenie stanu autostartu niczego nie zmienia")
    before = autostart.current_entry()
    enabled = autostart.is_enabled()
    after = autostart.current_entry()

    check(before == after, "odczyt nie modyfikuje rejestru")
    check(enabled == (after is not None), "stan zgadza sie z zawartoscia rejestru")


def test_single_instance_blocks_second_copy():
    print("\n5e. Druga kopia programu nie moze wystartowac")
    original = single_instance.MUTEX_NAME
    original_handle = single_instance._handle

    # Wlasna nazwa blokady, zeby test nie zalezal od tego, czy program wlasnie
    # dziala, i zeby nie zabral blokady prawdziwemu programowi.
    single_instance.MUTEX_NAME = "Local\\BatteryMonitor.Test.SingleInstance"
    single_instance._handle = None

    try:
        first = single_instance.acquire()
        second = single_instance.acquire()

        check(first is True, "pierwszy egzemplarz dostaje blokade")
        # Dwie kopie naraz migotalyby ekranem i nadpisywaly sobie historie.
        check(second is False, "drugi egzemplarz jest odrzucony")
    finally:
        single_instance.MUTEX_NAME = original
        single_instance._handle = original_handle


def main():
    catalog_tests = [
        test_unique_keys,
        test_unique_pids,
        test_pid_never_in_two_links,
        test_protocols_exist,
        test_every_link_has_request,
        test_pid_lookup,
        test_screens,
        test_categories_and_labels,
        test_display_name,
        test_rated_hours_differ_between_links,
        test_bt_name_lookup,
    ]
    settings_tests = [
        test_settings_default_is_automatic,
        test_settings_round_trip,
        test_settings_deduplicates,
        test_settings_empty_returns_to_automatic,
        test_settings_survives_corruption,
        test_settings_ignores_junk,
    ]
    declaration_tests = [
        test_offline_entry_for_declared_device,
        test_no_offline_entry_when_already_read,
        test_no_offline_entries_in_automatic_mode,
        test_wired_keyboard_has_no_battery_card,
        test_unsupported_device_is_marked,
        test_unknown_declared_key_is_ignored,
    ]
    program_tests = [
        test_data_and_resource_paths,
        test_program_path_is_runnable,
        test_autostart_command,
        test_autostart_reports_state_without_changing_it,
        test_single_instance_blocks_second_copy,
    ]
    parser_tests = [
        test_parse_aerox,
        test_parse_rival,
        test_parse_nova_7_depends_on_pid,
        test_parse_nova_5,
        test_nova_families_read_different_bytes,
        test_parse_nova_pro,
        test_parse_arctis_7_plus,
        test_parse_arctis_7_and_1,
        test_parse_arctis_9_clamps,
        test_parse_arctis_pro_wireless,
        test_expected_echo_follows_request,
        test_families_declare_complete_fields,
    ]

    for test in catalog_tests:
        test()

    for test in settings_tests:
        workspace = Workspace()
        try:
            test(workspace)
        finally:
            workspace.cleanup()

    for test in declaration_tests:
        test()

    for test in parser_tests:
        test()

    for test in program_tests:
        test()

    total = (
        len(catalog_tests)
        + len(settings_tests)
        + len(declaration_tests)
        + len(parser_tests)
        + len(program_tests)
    )

    print()
    if _failures:
        print(f"NIEPOWODZENIA: {len(_failures)}")
        for description in _failures:
            print(f"  - {description}")
        return 1

    print(f"Wszystkie sprawdzenia przeszly ({total} testow).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
