"""Testy osi czasu i kalkulatora przewidywania czasu pracy.

Uruchomienie:  python test_battery_runtime.py

Bez pytest i bez sprzetu - kazdy test podstawia wlasne znaczniki czasu, wiec
czterdziestogodzinny cykl rozladowania sprawdza sie w milisekundach. Dane ida
do katalogu tymczasowego, prawdziwa historia nie jest ruszana.

Dlugi odcinek "online" trzeba symulowac heartbeatami (advance_online): czas
sesji to suma przerw miedzy odczytami, a nie roznica znacznikow sciennych.
"""

import json
import os
import shutil
import sys
import tempfile

import battery_timeline
import device_catalog
import runtime_estimator

HOUR = 3600.0
STEP = 60.0

LINK_24 = device_catalog.LINK_24
LINK_BT = device_catalog.LINK_BT

_failures = []


def check(condition, description, detail=""):
    if condition:
        print(f"  OK   {description}")
    else:
        print(f"  BLAD {description}" + (f" -> {detail}" if detail else ""))
        _failures.append(description)


def close_to(value, expected, tolerance=0.01):
    return value is not None and abs(value - expected) <= tolerance


def advance_online(
    timeline, key, level, charging, start, end, step=STEP, end_level=None, link=LINK_24
):
    """Symuluje urzadzenie widziane online od start do end.

    Heartbeaty co "step" sekund utrzymuja czas wlaczenia. Na znaczniku "end"
    ustawiany jest end_level (domyslnie ten sam co level). Zaklada, ze w "start"
    jest juz wpisany odczyt (np. odlaczenie kabla) - pierwszy heartbeat idzie
    w start+step, zeby nie dublowac tego samego znacznika.
    """
    if end_level is None:
        end_level = level

    moment = start + step
    while moment < end - 1e-9:
        timeline.record(key, level, charging, now=moment, link=link)
        moment += step

    return timeline.record(key, end_level, charging, now=end, link=link)


class Workspace:
    """Katalog tymczasowy z osia czasu, sprzatany po sobie."""

    def __init__(self):
        self.directory = tempfile.mkdtemp(prefix="battery_test_")

    def timeline(self, legacy=None):
        return battery_timeline.Timeline(
            path=os.path.join(self.directory, "timeline.json"),
            legacy_path=legacy or os.path.join(self.directory, "brak.json"),
        )

    def write_legacy(self, payload):
        path = os.path.join(self.directory, "legacy.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        return path

    def cleanup(self):
        shutil.rmtree(self.directory, ignore_errors=True)


def counting_saves(timeline):
    """Podmienia zapis na liczacy, zeby sprawdzic, jak czesto schodzimy na dysk."""
    counter = {"writes": 0}
    original = timeline._save

    def counted():
        counter["writes"] += 1
        original()

    timeline._save = counted
    return counter


# ---------------------------------------------------------------------------
# 1. STRUKTURA DANYCH I POLITYKA ZAPISU
# ---------------------------------------------------------------------------


def test_record_layout(workspace):
    print("\n1a. Rekord zawiera czas, poziom i stan ladowania")
    timeline = workspace.timeline()

    state = timeline.record("Mysz", 80, False, now=1000.0)
    sample = state.samples[-1]

    check(sample.timestamp == 1000.0, "timestamp zapisany")
    check(sample.level == 80, "poziom zapisany")
    check(sample.charging is False, "stan ladowania zapisany")

    charging = timeline.record("Mysz", 81, True, now=1100.0)
    check(charging.samples[-1].charging is True, "ladowanie odnotowane jako True")


def test_write_throttling(workspace):
    print("\n1b. Zapis tylko wtedy, gdy odczyt cos wnosi")
    timeline = workspace.timeline()
    counter = counting_saves(timeline)

    timeline.record("Mysz", 80, False, now=0.0)
    check(counter["writes"] == 1, "pierwszy odczyt zapisany", f"zapisow: {counter['writes']}")

    # Dwie minuty bez zmiany poziomu - za malo, by ACTIVE_SAVE_SECONDS zmusil zapis.
    for minute in range(1, 3):
        timeline.record("Mysz", 80, False, now=minute * 60.0)
    check(
        counter["writes"] == 1,
        "krotka obserwacja bez zmiany poziomu nie rusza dysku",
        f"zapisow: {counter['writes']}",
    )

    # Po pieciu minutach naroslego czasu wlaczenia schodzimy na dysk, zeby
    # restart nie zgubil godzin plaskiego poziomu.
    timeline.record("Mysz", 80, False, now=5 * 60.0)
    check(
        counter["writes"] == 2,
        "narosly czas wlaczenia schodzi na dysk",
        f"zapisow: {counter['writes']}",
    )

    timeline.record("Mysz", 79, False, now=700.0)
    check(counter["writes"] == 3, "spadek o punkt zapisany", f"zapisow: {counter['writes']}")

    timeline.record("Mysz", 79, True, now=800.0)
    check(
        counter["writes"] == 4,
        "zmiana stanu ladowania zapisana mimo tego samego poziomu",
        f"zapisow: {counter['writes']}",
    )

    state = timeline.state("Mysz")
    check(len(state.samples) == 1, "start ladowania rozpoczyna nowa sesje")


def test_round_trip(workspace):
    print("\n1c. Dane przetrwaja restart programu")
    timeline = workspace.timeline()
    timeline.record("Mysz", 100, True, now=0.0)
    timeline.record("Mysz", 100, False, now=1 * HOUR)
    advance_online(timeline, "Mysz", 100, False, 1 * HOUR, 11 * HOUR, end_level=90)

    reopened = workspace.timeline()
    state = reopened.state("Mysz", now=11 * HOUR)

    check(len(state.samples) == 2, "probki wczytane z pliku")
    check(close_to(state.charge_end, 1 * HOUR), "moment odlaczenia kabla wczytany")
    check(state.observed_start is True, "informacja o zaobserwowanym starcie wczytana")
    check(
        close_to(state.active_seconds, 10 * HOUR, 60),
        "czas wlaczenia przetrwal restart",
        f"{state.active_seconds}",
    )


# ---------------------------------------------------------------------------
# 2. SLEDZENIE CYKLI LADOWANIA
# ---------------------------------------------------------------------------


def test_charge_end_detection(workspace):
    print("\n2a. Wykrycie momentu, w ktorym ladowanie przeszlo z True na False")
    timeline = workspace.timeline()

    timeline.record("Mysz", 60, True, now=0.0)
    timeline.record("Mysz", 80, True, now=2 * HOUR)
    state = timeline.record("Mysz", 100, False, now=5 * HOUR)

    check(close_to(state.charge_end, 5 * HOUR), "timestamp odlaczenia kabla", f"{state.charge_end}")
    check(state.observed_start is True, "przejscie uznane za zaobserwowane")
    check(close_to(state.hours_since_charge, 0.0), "czas pracy startuje od zera")

    later = advance_online(timeline, "Mysz", 100, False, 5 * HOUR, 17 * HOUR, end_level=97)
    check(close_to(later.hours_since_charge, 12.0, 0.02), "czas pracy liczony na biezaco")


def test_charge_end_unknown_when_not_observed(workspace):
    print("\n2b. Nieznany czas pracy, gdy odlaczenia kabla nie widzielismy")
    timeline = workspace.timeline()

    # Program wystartowal przy urzadzeniu juz pracujacym na baterii.
    state = timeline.record("Mysz", 70, False, now=0.0)
    check(state.hours_since_charge is None, "czas od naladowania nieznany, a nie zmyslony")
    check(state.charge_end is None, "brak momentu odlaczenia kabla")

    later = advance_online(timeline, "Mysz", 70, False, 0.0, 5 * HOUR, end_level=65)
    check(later.hours_since_charge is None, "nadal nieznany po kolejnych odczytach")
    check(close_to(later.session_hours, 5.0, 0.02), "ale dlugosc obserwacji jest znana")


def test_drop_since_charge(workspace):
    print("\n2e. Licznik zuzytych procent od naladowania")
    timeline = workspace.timeline()

    timeline.record("Mysz", 100, True, now=0.0)
    state = timeline.record("Mysz", 100, False, now=1 * HOUR)
    check(state.drop_since_charge == 0, "zaraz po odlaczeniu kabla nic nie ubylo")

    state = advance_online(timeline, "Mysz", 100, False, 1 * HOUR, 10 * HOUR, end_level=82)
    check(state.drop_since_charge == 18, "ubylo 18 punktow", f"{state.drop_since_charge}")

    # Urzadzenia potrafia sie o punkt "cofnac" w gore bez ladowania. Ujemny
    # ubytek nie znaczy nic sensownego, wiec licznik zatrzymuje sie na zerze.
    fresh = workspace.timeline()
    fresh.record("Sluchawki", 60, True, now=0.0)
    fresh.record("Sluchawki", 60, False, now=1 * HOUR)
    bumped = fresh.record("Sluchawki", 62, False, now=1 * HOUR + 60)
    check(bumped.drop_since_charge == 0, "wzrost poziomu nie daje ujemnego ubytku")


def test_drop_unknown_when_charge_not_observed(workspace):
    print("\n2f. Bez zaobserwowanego naladowania nie ma czego liczyc")
    timeline = workspace.timeline()

    # Program wystartowal przy urzadzeniu juz pracujacym na baterii: pierwsza
    # probka to poziom z chwili, gdy zaczelismy patrzec, a nie poziom po
    # naladowaniu. Roznica wzgledem niej to zuzycie od startu programu, co jest
    # zupelnie inna liczba niz zuzycie od naladowania.
    timeline.record("Mysz", 70, False, now=0.0)
    state = advance_online(timeline, "Mysz", 70, False, 0.0, 5 * HOUR, end_level=55)

    check(state.drop_since_charge is None, "ubytek nieznany, a nie policzony od startu")
    check(state.session_drop == 15, "ale spadek w obserwowanej sesji jest znany")
    check(
        state.hours_since_charge is None,
        "oba pola milcza w tej samej sytuacji",
    )


def test_cycle_closed_on_charge(workspace):
    print("\n2c. Ladowanie zamyka cykl i zapisuje go w historii")
    timeline = workspace.timeline()

    timeline.record("Mysz", 100, True, now=0.0)
    timeline.record("Mysz", 100, False, now=1 * HOUR)
    advance_online(timeline, "Mysz", 100, False, 1 * HOUR, 41 * HOUR, end_level=20)
    state = timeline.record("Mysz", 21, True, now=41.5 * HOUR)

    check(len(state.cycles) == 1, "cykl dopisany do historii", f"cykli: {len(state.cycles)}")
    if state.cycles:
        cycle = state.cycles[0]
        check(cycle.start_level == 100 and cycle.end_level == 20, "poziomy graniczne cyklu")
        check(close_to(cycle.hours, 40.0, 0.02), "dlugosc cyklu to czas wlaczenia", f"{cycle.hours}")
        check(close_to(cycle.active_seconds, 40 * HOUR, 60), "active_seconds w cyklu")
        check(close_to(cycle.rate, 2.0, 0.02), "tempo cyklu w punktach na godzine", f"{cycle.rate}")


def test_short_cycle_ignored(workspace):
    print("\n2d. Cykl bez tresci nie trafia do historii")
    timeline = workspace.timeline()

    timeline.record("Mysz", 100, True, now=0.0)
    timeline.record("Mysz", 100, False, now=1 * HOUR)
    advance_online(timeline, "Mysz", 100, False, 1 * HOUR, 3 * HOUR, end_level=96)
    state = timeline.record("Mysz", 97, True, now=3.5 * HOUR)

    check(len(state.cycles) == 0, "spadek o 4 punkty odrzucony jako zbyt maly")


def test_offline_gap_excluded_from_active_time(workspace):
    print("\n2g. Przerwa z wylaczonym sprzetem nie wchodzi do licznika")
    timeline = workspace.timeline()

    timeline.record("Mysz", 100, True, now=0.0)
    timeline.record("Mysz", 100, False, now=0.0)
    # Dwie godziny online.
    advance_online(timeline, "Mysz", 100, False, 0.0, 2 * HOUR, end_level=95)

    # Osiem godzin bez odczytu (sprzet wylaczony / program zamkniety).
    later = timeline.record("Mysz", 90, False, now=10 * HOUR)

    check(
        close_to(later.hours_since_charge, 2.0, 0.02),
        "licznik pokazuje 2 h, nie 10 h",
        f"{later.hours_since_charge}",
    )
    check(
        later.active_seconds < 2.5 * HOUR,
        "luka nie zwiekszyla active_seconds",
        f"{later.active_seconds}",
    )


def test_large_gap_does_not_count(workspace):
    print("\n2h. Luka powyzej MAX_ONLINE_GAP nie nalicza czasu")
    timeline = workspace.timeline()

    timeline.record("Mysz", 100, False, now=0.0)
    timeline.record("Mysz", 100, False, now=0.0)
    timeline.record("Mysz", 100, False, now=60.0)
    # Przerwa wieksza niz prog - jakby urzadzenie zniknelo.
    gap = battery_timeline.MAX_ONLINE_GAP + 1
    state = timeline.record("Mysz", 99, False, now=60.0 + gap)

    check(
        close_to(state.active_seconds, 60.0, 1),
        "naliczona tylko pierwsza minuta",
        f"{state.active_seconds}",
    )


# ---------------------------------------------------------------------------
# 3. ALGORYTM SZACOWANIA
# ---------------------------------------------------------------------------


def test_session_only_estimate(workspace):
    print("\n3a. Szacunek z samej biezacej sesji")
    timeline = workspace.timeline()

    timeline.record("Mysz", 100, True, now=0.0)
    timeline.record("Mysz", 100, False, now=0.0)
    state = advance_online(timeline, "Mysz", 100, False, 0.0, 10 * HOUR, end_level=80)

    result = runtime_estimator.estimate(state)

    check(result.source == runtime_estimator.SOURCE_SESSION, "zrodlo: biezaca sesja")
    check(close_to(result.rate, 2.0, 0.02), "tempo 20 punktow w 10 godzin", f"{result.rate}")
    check(close_to(result.hours, 40.0, 0.5), "pozostalo 80 punktow przy 2 pkt/h", f"{result.hours}")
    check(runtime_estimator.format_hours(result.hours, result.source) == "40h", "napis 40h")


def test_weighted_blend(workspace):
    print("\n3b. Srednia wazona: 70 procent biezaca sesja, 30 procent historia")
    timeline = workspace.timeline()

    # Cykl historyczny: 100% -> 20% w 40 godzin, czyli 2,0 pkt/h.
    timeline.record("Sluchawki", 100, True, now=0.0)
    timeline.record("Sluchawki", 100, False, now=0.0)
    advance_online(timeline, "Sluchawki", 100, False, 0.0, 40 * HOUR, end_level=20)
    timeline.record("Sluchawki", 21, True, now=41 * HOUR)

    # Biezaca sesja: 100% -> 90% w 10 godzin, czyli 1,0 pkt/h.
    timeline.record("Sluchawki", 100, False, now=50 * HOUR)
    state = advance_online(timeline, "Sluchawki", 100, False, 50 * HOUR, 60 * HOUR, end_level=90)

    result = runtime_estimator.estimate(state)

    expected_rate = 0.7 * 1.0 + 0.3 * 2.0
    check(result.source == runtime_estimator.SOURCE_BLENDED, "zrodlo: obie skladowe")
    check(close_to(result.rate, expected_rate, 0.05), f"tempo {expected_rate} pkt/h", f"{result.rate}")
    check(close_to(result.hours, 90 / expected_rate, 1.0), "pozostaly czas z tempa mieszanego")
    check(runtime_estimator.format_hours(result.hours, result.source) == "69h", "napis 69h")


def test_history_only_estimate(workspace):
    print("\n3c. Szacunek z samej historii, gdy biezaca sesja jest za krotka")
    timeline = workspace.timeline()

    timeline.record("Mysz", 100, True, now=0.0)
    timeline.record("Mysz", 100, False, now=0.0)
    advance_online(timeline, "Mysz", 100, False, 0.0, 25 * HOUR, end_level=50)
    timeline.record("Mysz", 51, True, now=26 * HOUR)

    # Nowa sesja ma dopiero 10 minut i zerowy spadek - sama nic nie mowi.
    timeline.record("Mysz", 100, False, now=30 * HOUR)
    state = timeline.record("Mysz", 100, False, now=30 * HOUR + 600)

    result = runtime_estimator.estimate(state)

    check(result.source == runtime_estimator.SOURCE_HISTORY, "zrodlo: historia")
    check(close_to(result.rate, 2.0, 0.02), "tempo z cyklu 50 punktow w 25 godzin", f"{result.rate}")
    check(close_to(result.hours, 50.0, 0.5), "pozostaly czas 100 / 2,0", f"{result.hours}")


def test_history_weighted_by_duration(workspace):
    print("\n3d. Dlugi cykl wazy w historii wiecej niz krotki")
    timeline = workspace.timeline()

    # Cykl dlugi: 80 punktow w 40 godzin (2,0 pkt/h).
    timeline.record("Mysz", 100, True, now=0.0)
    timeline.record("Mysz", 100, False, now=0.0)
    advance_online(timeline, "Mysz", 100, False, 0.0, 40 * HOUR, end_level=20)
    timeline.record("Mysz", 21, True, now=41 * HOUR)

    # Cykl krotki: 20 punktow w 2 godziny (10,0 pkt/h).
    timeline.record("Mysz", 100, False, now=50 * HOUR)
    advance_online(timeline, "Mysz", 100, False, 50 * HOUR, 52 * HOUR, end_level=80)
    state = timeline.record("Mysz", 81, True, now=53 * HOUR)

    rate = runtime_estimator.history_rate(state.cycles)

    # Srednia arytmetyczna temp dalaby 6,0 pkt/h. Wazona czasem daje
    # 100 punktow na 42 godziny, czyli 2,38 - blizej tego, jak urzadzenie
    # zachowuje sie na dluzsza mete.
    check(close_to(rate, 100 / 42, 0.05), "tempo wazone czasem, nie arytmetyczne", f"{rate}")


def test_flat_level_extends_estimate(workspace):
    print("\n3e. Godziny bez zmiany poziomu wydluzaja szacunek")
    timeline = workspace.timeline()

    timeline.record("Mysz", 100, True, now=0.0)
    timeline.record("Mysz", 100, False, now=0.0)
    state = advance_online(timeline, "Mysz", 100, False, 0.0, 10 * HOUR, end_level=90)
    first = runtime_estimator.estimate(state)

    # Kolejne 10 godzin online bez zmiany poziomu - heartbeatami, nie skokiem.
    later = advance_online(timeline, "Mysz", 90, False, 10 * HOUR, 20 * HOUR, end_level=90)
    second = runtime_estimator.estimate(later)

    check(close_to(first.rate, 1.0, 0.02), "tempo po 10 godzinach", f"{first.rate}")
    check(close_to(second.rate, 0.5, 0.02), "tempo po 20 godzinach", f"{second.rate}")
    check(second.hours > first.hours, "szacunek wydluzony", f"{first.hours} -> {second.hours}")


def test_rounding_to_whole_hours(workspace):
    print("\n3f. Wynik zaokraglony do pelnych godzin")
    del workspace

    check(runtime_estimator.format_hours(13.6) == "14h", "13,6 h to 14h")
    check(runtime_estimator.format_hours(14.4) == "14h", "14,4 h to 14h")
    check(runtime_estimator.format_hours(1.0) == "1h", "rowna godzina")
    check(
        runtime_estimator.format_hours(0.4) == "<1h",
        "ponizej godziny nie pokazuje 0h",
        runtime_estimator.format_hours(0.4),
    )
    check(
        runtime_estimator.format_hours(54.0, runtime_estimator.SOURCE_RATED) == "~54h",
        "dane katalogowe z tylda",
    )
    check(runtime_estimator.format_hours(None) == "", "brak danych to pusty napis")


def test_drop_formatting(workspace):
    print("\n3g. Napis licznika ubytku")
    del workspace

    check(runtime_estimator.format_drop(18) == "-18%", "ubytek ze znakiem minus")
    check(runtime_estimator.format_drop(100) == "-100%", "pelne rozladowanie")
    # Zero i brak danych daja pusty napis, bo "-0%" zajmowaloby miejsce na
    # ekranie 128x40, nie mowiac niczego.
    check(runtime_estimator.format_drop(0) == "", "zero to pusty napis")
    check(runtime_estimator.format_drop(None) == "", "brak danych to pusty napis")


def test_hours_per_point(workspace):
    print("\n3h. Sredni czas pracy na jednym procencie")
    timeline = workspace.timeline()

    timeline.record("Mysz", 100, True, now=0.0)
    timeline.record("Mysz", 100, False, now=0.0)
    state = advance_online(timeline, "Mysz", 100, False, 0.0, 10 * HOUR, end_level=80)
    result = runtime_estimator.estimate(state)

    # 20 punktow w 10 godzin to 2 pkt/h, czyli pol godziny na punkt. Ta sama
    # informacja co tempo, tylko odwrocona - dlatego liczymy ja z gotowego
    # Estimate.rate, a nie drugi raz z osi czasu.
    per_point = runtime_estimator.hours_per_point(result.rate)
    check(close_to(per_point, 0.5, 0.02), "pol godziny na punkt", f"{per_point}")
    check(
        runtime_estimator.format_per_point(per_point) == "30 min",
        "wynik w minutach, nie w pelnych godzinach",
        runtime_estimator.format_per_point(per_point),
    )


def test_per_point_formatting(workspace):
    print("\n3i. Napis czasu na 1 procent")
    del workspace

    # Ponizej godziny liczymy w minutach: zaokraglenie do pelnych godzin -
    # jak w czasie pozostalym - zamienialoby caly wskaznik w "<1h".
    check(runtime_estimator.format_per_point(0.25) == "15 min", "kwadrans")
    check(
        runtime_estimator.format_per_point(0.54) == "32 min",
        "zaokraglenie do pelnych minut",
        runtime_estimator.format_per_point(0.54),
    )
    check(runtime_estimator.format_per_point(0.008) == "<1 min", "ponizej minuty")
    check(
        runtime_estimator.format_per_point(2.25) == "2 h 15 min",
        "powyzej godziny z minutami",
        runtime_estimator.format_per_point(2.25),
    )
    check(
        runtime_estimator.format_per_point(1.8, runtime_estimator.SOURCE_RATED)
        == "~1 h 48 min",
        "dane katalogowe z tylda",
        runtime_estimator.format_per_point(1.8, runtime_estimator.SOURCE_RATED),
    )
    check(runtime_estimator.format_per_point(None) == "", "brak danych to pusty napis")

    # Ladujace sie urzadzenie nie ma tempa rozladowania, wiec nie ma czego
    # dzielic - i nie wolno tu wpasc w dzielenie przez zero.
    check(runtime_estimator.hours_per_point(None) is None, "brak tempa to brak wyniku")
    check(runtime_estimator.hours_per_point(0) is None, "zerowe tempo nie dzieli przez zero")
    check(
        runtime_estimator.hours_per_point(0.0001) == runtime_estimator.MAX_PER_POINT_HOURS,
        "znikome tempo przyciete do gornej granicy",
    )


# ---------------------------------------------------------------------------
# 4. PRZYPADKI BRZEGOWE
# ---------------------------------------------------------------------------


def test_no_history_falls_back_to_rated(workspace):
    print("\n4a. Brak historii: wartosc katalogowa producenta")
    timeline = workspace.timeline()

    # Wartosc katalogowa bierzemy z katalogu urzadzen, wiec test sprawdza takze
    # to, czy dane sa tam wpisane i czy siegamy po nie dla wlasciwego lacza.
    nova = device_catalog.by_key("arctis_nova_7")
    rated = nova.rated_for(device_catalog.LINK_24)
    check(close_to(rated, 54.0), "katalog zna czas pracy Arctis Nova 7", f"{rated}")

    state = timeline.record(nova.name, 100, False, now=0.0)
    result = runtime_estimator.estimate(state, rated)

    check(result.source == runtime_estimator.SOURCE_RATED, "zrodlo: dane katalogowe")
    check(close_to(result.hours, 54.0), "54 godziny z karty produktu", f"{result.hours}")
    check(result.measured is False, "wynik oznaczony jako niezmierzony")
    check(
        runtime_estimator.format_hours(result.hours, result.source) == "~54h",
        "tylda odroznia szacunek katalogowy",
    )

    # Osobne urzadzenie, tez bez historii, ale z bateria w polowie. Tego samego
    # urzadzenia nie da sie tu uzyc: spadek ze 100% na 50% w ciagu minuty
    # zostalby slusznie odrzucony jako niemozliwy odczyt.
    half = timeline.record("Inny egzemplarz", 50, False, now=0.0)
    check(
        close_to(runtime_estimator.estimate(half, rated).hours, 27.0),
        "katalog przeliczony proporcjonalnie do poziomu",
        f"{runtime_estimator.estimate(half, rated).hours}",
    )

    bluetooth = nova.rated_for(device_catalog.LINK_BT)
    check(
        bluetooth is not None and bluetooth < rated,
        "Bluetooth ma wlasny, krotszy czas katalogowy",
        f"{bluetooth}",
    )


def test_unknown_device_without_history(workspace):
    print("\n4b. Nieznane urzadzenie bez historii i bez danych katalogowych")
    timeline = workspace.timeline()

    state = timeline.record("Cos Zupelnie Nowego", 77, False, now=0.0)
    result = runtime_estimator.estimate(state)

    check(result.hours is None, "brak szacunku zamiast liczby z sufitu")
    check(runtime_estimator.format_hours(result.hours, result.source) == "", "pusty napis")


def test_charging_returns_no_estimate(workspace):
    print("\n4c. Urzadzenie w trakcie ladowania nie dostaje szacunku")
    timeline = workspace.timeline()

    timeline.record("Mysz", 100, True, now=0.0)
    timeline.record("Mysz", 100, False, now=0.0)
    advance_online(timeline, "Mysz", 100, False, 0.0, 10 * HOUR, end_level=80)
    state = timeline.record("Mysz", 81, True, now=11 * HOUR)

    result = runtime_estimator.estimate(state)

    check(result.hours is None, "brak liczby godzin")
    check(result.source is None, "brak zrodla")
    check(
        runtime_estimator.runtime_text(state) == runtime_estimator.charging_text(),
        "napis informuje o ladowaniu",
    )


def test_sensor_glitch_rejected(workspace):
    print("\n4d. Nienaturalny skok baterii odrzucony")
    timeline = workspace.timeline()

    timeline.record("Mysz", 100, True, now=0.0)
    timeline.record("Mysz", 100, False, now=0.0)
    advance_online(timeline, "Mysz", 100, False, 0.0, 10 * HOUR, end_level=90)

    # Spadek z 90% na 10% w ciagu sekundy - fizycznie niemozliwy.
    state = timeline.record("Mysz", 10, False, now=10 * HOUR + 1)
    check(state.level == 90, "bledny odczyt pominiety", f"poziom: {state.level}")
    check(len(state.samples) == 2, "bledny odczyt nie trafil na os czasu")

    # Ten sam poziom w kolejnym odczycie - to nie byl blad sensora.
    confirmed = timeline.record("Mysz", 10, False, now=10 * HOUR + 120)
    check(confirmed.level == 10, "potwierdzony odczyt przyjety", f"poziom: {confirmed.level}")


def test_large_drop_over_long_gap_accepted(workspace):
    print("\n4e. Duzy spadek po dlugiej przerwie jest prawdziwy")
    timeline = workspace.timeline()

    timeline.record("Mysz", 100, True, now=0.0)
    timeline.record("Mysz", 100, False, now=0.0)

    # Komputer byl wylaczony przez dobe, urzadzenie pracowalo dalej.
    # Poziom przyjmujemy, ale doby nie wliczamy do czasu wlaczenia.
    state = timeline.record("Mysz", 30, False, now=24 * HOUR)
    check(state.level == 30, "spadek o 70 punktow w dobe przyjety", f"poziom: {state.level}")
    check(
        state.active_seconds < HOUR,
        "doba offline nie weszla do active_seconds",
        f"{state.active_seconds}",
    )


def test_five_percent_steps_accepted(workspace):
    print("\n4f. Krok co 5 punktow nie jest traktowany jako blad")
    timeline = workspace.timeline()

    timeline.record("Aerox", 100, True, now=0.0)
    timeline.record("Aerox", 100, False, now=0.0)

    # Aerox raportuje baterie w krokach po 5 punktow, co minute miedzy odczytami
    # daje chwilowe 300 punktow na godzine - progu nie wolno oprzec na tempie.
    state = timeline.record("Aerox", 95, False, now=60.0)
    check(state.level == 95, "krok o 5 punktow przyjety", f"poziom: {state.level}")


def test_charge_without_flag_starts_new_session(workspace):
    print("\n4g. Naladowanie bez zgloszonego ladowania konczy sesje")
    timeline = workspace.timeline()

    timeline.record("Mysz", 100, True, now=0.0)
    timeline.record("Mysz", 100, False, now=0.0)
    advance_online(timeline, "Mysz", 100, False, 0.0, 40 * HOUR, end_level=20)

    # Bluetooth nie zglasza ladowania - poziom po prostu skacze w gore.
    state = timeline.record("Mysz", 100, False, now=50 * HOUR)

    check(len(state.cycles) == 1, "poprzedni cykl zamkniety", f"cykli: {len(state.cycles)}")
    check(state.level == 100, "nowy poziom przyjety")
    check(len(state.samples) == 1, "nowa sesja zaczyna sie od jednej probki")
    check(state.hours_since_charge is None, "czas od naladowania nieznany, bo nie byl widoczny")


def test_corrupted_file_survives(workspace):
    print("\n4h. Uszkodzony plik nie wywraca monitora")
    path = os.path.join(workspace.directory, "timeline.json")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("{to nie jest poprawny JSON")

    timeline = battery_timeline.Timeline(path=path, legacy_path="brak.json")
    state = timeline.record("Mysz", 50, False, now=0.0)

    check(state.level == 50, "monitor dziala dalej na czystej historii")


def test_legacy_migration(workspace):
    print("\n4i. Przeniesienie historii ze starego formatu")
    legacy = workspace.write_legacy(
        {"Arctis Nova 7": [[0.0, 100], [10 * HOUR, 90]]}
    )

    timeline = battery_timeline.Timeline(
        path=os.path.join(workspace.directory, "nowy.json"), legacy_path=legacy
    )
    state = timeline.state("arctis_nova_7", now=10 * HOUR)

    check(len(state.samples) == 2, "probki przeniesione", f"probek: {len(state.samples)}")
    check(state.observed_start is False, "poczatek sesji nie jest uznany za zaobserwowany")

    result = runtime_estimator.estimate(state)
    check(
        result.source == runtime_estimator.SOURCE_SESSION,
        "pomiar z przeniesionych danych dziala od razu",
        f"{result.source}",
    )
    check(close_to(result.rate, 1.0), "tempo 10 punktow w 10 godzin", f"{result.rate}")


def test_format_rewrite_on_load(workspace):
    print("\n4j. Stary plik dostaje licznik czasu przy starcie, bez czekania na zmiane poziomu")
    path = os.path.join(workspace.directory, "timeline.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "version": 2,
                "devices": {
                    "Mysz": {
                        "samples": [[0.0, 100, 0], [2 * HOUR, 90, 0]],
                        "cycles": [],
                        "charge_end": 0.0,
                        "observed_start": True,
                    }
                },
            },
            handle,
        )

    battery_timeline.Timeline(path=path, legacy_path=os.path.join(workspace.directory, "brak.json"))

    with open(path, "r", encoding="utf-8") as handle:
        saved = json.load(handle)

    check(
        saved.get("version") == battery_timeline.FORMAT_VERSION,
        "format podniesiony do biezacej wersji",
        f"{saved.get('version')}",
    )
    device = saved["devices"]["Mysz"]
    check(isinstance(device.get("active"), dict), "licznik czasu zapisany od razu")
    check(
        close_to(sum(device["active"].values()), 2 * HOUR, 1),
        "migracja zachowuje rozpietosc sesji",
        f"{device.get('active')}",
    )
    check(runtime_estimator.format_elapsed(0.0) == "0 min", "zero to 0 min, nie 1 min")


def test_links_share_one_battery(workspace):
    print("\n5a. Przelaczenie z 2.4 GHz na Bluetooth nie gubi licznikow")
    timeline = workspace.timeline()

    timeline.record("aerox_5_wireless", 100, True, now=0.0, link=LINK_24)
    timeline.record("aerox_5_wireless", 100, False, now=0.0, link=LINK_24)
    advance_online(
        timeline, "aerox_5_wireless", 100, False, 0.0, 3 * HOUR, end_level=80, link=LINK_24
    )

    # Mysz przelaczona na Bluetooth: inna nazwa w Windows, brak informacji
    # o ladowaniu, poziom raportowany wlasna miarka.
    switched = timeline.record("aerox_5_wireless", 85, False, now=3 * HOUR + 120, link=LINK_BT)

    check(
        switched.hours_since_charge is not None,
        "licznik czasu wlaczenia dziala dalej po Bluetooth",
        f"{switched.hours_since_charge}",
    )
    check(
        close_to(switched.hours_since_charge, 3.0, 0.1),
        "licznik pokazuje trzy godziny zebrane na dongle'u",
        f"{switched.hours_since_charge}",
    )
    check(switched.drop_since_charge == 15, "zuzycie liczone dalej", f"{switched.drop_since_charge}")
    check(switched.observed_start is True, "moment odlaczenia kabla nie przepadl")

    later = advance_online(
        timeline, "aerox_5_wireless", 85, False, 3 * HOUR + 120, 4 * HOUR + 120, link=LINK_BT
    )
    check(
        close_to(later.active_by_link.get(LINK_24, 0.0) / HOUR, 3.0, 0.1),
        "czas na dongle'u zapisany osobno",
        f"{later.active_by_link}",
    )
    check(
        close_to(later.active_by_link.get(LINK_BT, 0.0) / HOUR, 1.0, 0.1),
        "czas na Bluetooth zapisany osobno",
        f"{later.active_by_link}",
    )
    check(close_to(later.session_hours, 4.0, 0.1), "licznik sumuje oba lacza")


def test_rate_measured_per_link(workspace):
    print("\n5b. Tempo z jednego lacza nie zanieczyszcza szacunku dla drugiego")
    timeline = workspace.timeline()

    timeline.record("aerox_5_wireless", 100, True, now=0.0, link=LINK_24)
    timeline.record("aerox_5_wireless", 100, False, now=0.0, link=LINK_24)
    # Dziesiec godzin na dongle'u, 20 punktow: 2 pkt/h.
    on_dongle = advance_online(
        timeline, "aerox_5_wireless", 100, False, 0.0, 10 * HOUR, end_level=80, link=LINK_24
    )

    check(close_to(on_dongle.link_drop, 20), "spadek przypisany dongle'owi", f"{on_dongle.link_drop}")
    fast = runtime_estimator.estimate(on_dongle)
    check(close_to(fast.rate, 2.0, 0.05), "tempo dongle'a", f"{fast.rate}")

    # Dziesiec godzin na Bluetooth, 5 punktow: 0,5 pkt/h.
    on_bt = advance_online(
        timeline, "aerox_5_wireless", 80, False, 10 * HOUR, 20 * HOUR, end_level=75, link=LINK_BT
    )

    check(close_to(on_bt.link_drop, 5), "spadek Bluetooth liczony osobno", f"{on_bt.link_drop}")
    check(
        close_to(on_bt.active_by_link.get(LINK_24, 0.0) / HOUR, 10.0, 0.1),
        "godziny dongle'a nie weszly do Bluetootha",
        f"{on_bt.active_by_link}",
    )
    slow = runtime_estimator.estimate(on_bt)
    check(close_to(slow.rate, 0.5, 0.05), "tempo Bluetootha liczone z jego wlasnych danych", f"{slow.rate}")
    check(slow.hours > fast.hours, "na Bluetooth zostaje wiecej godzin pracy")


def test_link_switch_is_not_a_secret_charge(workspace):
    print("\n5c. Skok poziomu przy zmianie lacza nie kasuje sesji")
    timeline = workspace.timeline()

    timeline.record("aerox_5_wireless", 100, True, now=0.0, link=LINK_24)
    timeline.record("aerox_5_wireless", 100, False, now=0.0, link=LINK_24)
    advance_online(
        timeline, "aerox_5_wireless", 100, False, 0.0, 2 * HOUR, end_level=70, link=LINK_24
    )

    # Windows po Bluetooth pokazuje 80% tam, gdzie dongle pokazywal 70%.
    switched = timeline.record("aerox_5_wireless", 80, False, now=2 * HOUR + 60, link=LINK_BT)
    check(switched.observed_start is True, "sesja przetrwala skok o 10 punktow")
    check(
        close_to(switched.hours_since_charge, 2.0, 0.1),
        "licznik czasu nie wrocil do zera",
        f"{switched.hours_since_charge}",
    )

    # Skok o 30 punktow to juz nie roznica miarek, tylko naladowany sprzet.
    charged = timeline.record("aerox_5_wireless", 100, False, now=3 * HOUR, link=LINK_24)
    check(charged.observed_start is False, "prawdziwe naladowanie nadal zamyka sesje")


def test_old_per_link_history_is_merged(workspace):
    print("\n5d. Stare, osobne historie lacz schodza sie pod jednym kluczem")
    path = os.path.join(workspace.directory, "timeline.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "version": 3,
                "devices": {
                    "Aerox 5 Wireless": {
                        "samples": [[10 * HOUR, 80, 0]],
                        "cycles": [[0.0, 9 * HOUR, 100, 40, 9 * HOUR]],
                        "charge_end": 9 * HOUR,
                        "observed_start": True,
                        "active_seconds": 1 * HOUR,
                        "last_seen": 10 * HOUR,
                        "active_saved": 1 * HOUR,
                    },
                    "Aerox 5 WL (Bluetooth)": {
                        "samples": [[2 * HOUR, 60, 0]],
                        "cycles": [[0.0, 2 * HOUR, 90, 60, 2 * HOUR]],
                        "charge_end": None,
                        "observed_start": False,
                        "active_seconds": 2 * HOUR,
                        "last_seen": 2 * HOUR,
                        "active_saved": 2 * HOUR,
                    },
                },
            },
            handle,
        )

    timeline = battery_timeline.Timeline(
        path=path, legacy_path=os.path.join(workspace.directory, "brak.json")
    )

    check(timeline.devices() == ["aerox_5_wireless"], "jeden wpis zamiast dwoch", f"{timeline.devices()}")

    state = timeline.state("aerox_5_wireless", now=10 * HOUR, link=LINK_24)
    check(state.observed_start is True, "sesja z zaobserwowanym odlaczeniem kabla zachowana")
    check(close_to(state.session_hours, 1.0, 0.01), "czas wlaczenia z tej sesji")
    check(len(state.cycles) == 2, "cykle z obu lacz zachowane", f"cykli: {len(state.cycles)}")
    check(len(state.link_cycles) == 1, "szacunek dla dongle'a widzi tylko swoj cykl")

    on_bt = timeline.state("aerox_5_wireless", now=10 * HOUR, link=LINK_BT)
    check(len(on_bt.link_cycles) == 1, "cykl Bluetooth przypisany do Bluetootha")
    if on_bt.link_cycles:
        check(close_to(on_bt.link_cycles[0].rate, 15.0, 0.1), "tempo cyklu BT nietkniete")
    check(
        on_bt.hours_since_charge is not None,
        "po przelaczeniu na Bluetooth licznik nadal ma od czego liczyc",
    )


def test_stale_session_is_not_resurrected(workspace):
    print("\n5e. Sesja sprzed ladowania nie wraca do zycia przy scalaniu")
    path = os.path.join(workspace.directory, "timeline.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "version": 3,
                "devices": {
                    # Stara sesja z kablem: skonczyla sie dwa dni temu.
                    "Aerox 5 Wireless": {
                        "samples": [[0.0, 40, 0]],
                        "cycles": [],
                        "charge_end": 0.0,
                        "observed_start": True,
                        "active_seconds": 1 * HOUR,
                        "last_seen": 1 * HOUR,
                        "active_saved": 1 * HOUR,
                    },
                    # Swiezszy wpis pokazuje pelna bateria, czyli po drodze bylo
                    # ladowanie, ktorego nie widzielismy.
                    "Aerox 5 WL (Bluetooth)": {
                        "samples": [[48 * HOUR, 100, 0]],
                        "cycles": [],
                        "charge_end": None,
                        "observed_start": False,
                        "active_seconds": 0.5 * HOUR,
                        "last_seen": 48 * HOUR,
                        "active_saved": 0.5 * HOUR,
                    },
                },
            },
            handle,
        )

    timeline = battery_timeline.Timeline(
        path=path, legacy_path=os.path.join(workspace.directory, "brak.json")
    )
    state = timeline.state("aerox_5_wireless", now=48 * HOUR, link=LINK_BT)

    check(state.observed_start is False, "stara sesja odrzucona, a nie doklejona")
    check(state.level == 100, "poziom z najswiezszego odczytu", f"{state.level}")
    check(state.hours_since_charge is None, "czas od naladowania uczciwie nieznany")


def main():
    tests = [
        test_record_layout,
        test_write_throttling,
        test_round_trip,
        test_charge_end_detection,
        test_charge_end_unknown_when_not_observed,
        test_drop_since_charge,
        test_drop_unknown_when_charge_not_observed,
        test_cycle_closed_on_charge,
        test_short_cycle_ignored,
        test_offline_gap_excluded_from_active_time,
        test_large_gap_does_not_count,
        test_session_only_estimate,
        test_weighted_blend,
        test_history_only_estimate,
        test_history_weighted_by_duration,
        test_flat_level_extends_estimate,
        test_rounding_to_whole_hours,
        test_drop_formatting,
        test_hours_per_point,
        test_per_point_formatting,
        test_no_history_falls_back_to_rated,
        test_unknown_device_without_history,
        test_charging_returns_no_estimate,
        test_sensor_glitch_rejected,
        test_large_drop_over_long_gap_accepted,
        test_five_percent_steps_accepted,
        test_charge_without_flag_starts_new_session,
        test_corrupted_file_survives,
        test_legacy_migration,
        test_format_rewrite_on_load,
        test_links_share_one_battery,
        test_rate_measured_per_link,
        test_link_switch_is_not_a_secret_charge,
        test_old_per_link_history_is_merged,
        test_stale_session_is_not_resurrected,
    ]

    for test in tests:
        workspace = Workspace()
        try:
            test(workspace)
        finally:
            workspace.cleanup()

    print()
    if _failures:
        print(f"NIEPOWODZENIA: {len(_failures)}")
        for description in _failures:
            print(f"  - {description}")
        return 1

    print(f"Wszystkie sprawdzenia przeszly ({len(tests)} testow).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
