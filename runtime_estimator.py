"""Kalkulator przewidywanego czasu pracy na baterii - warstwa matematyki.

Modul nie dotyka dysku i nie wie, skad biora sie dane. Dostaje migawke stanu
z battery_timeline.py, czyta z niej fakty i zwraca liczbe godzin. Dzieki temu
da sie go testowac na danych z palca, bez zadnego sprzetu i bez plikow.

Algorytm
--------
Przewidywane tempo rozladowania to srednia wazona dwoch skladnikow:

  1. Tempo biezacej sesji (waga WEIGHT_SESSION) - ile punktow na godzine
     urzadzenie traci od chwili odlaczenia kabla do teraz. To najlepsze zrodlo,
     bo opisuje dzisiejszy sposob uzywania: jasnosc podswietlenia, czestotliwosc
     odpytywania, glosnosc.
  2. Tempo historyczne (waga WEIGHT_HISTORY) - srednia z kilku ostatnich
     zamknietych cykli. Stabilizuje wynik, gdy biezaca sesja jest jeszcze krotka
     albo trafila na nietypowy fragment uzytkowania.

Pozostaly czas to poziom baterii podzielony przez to tempo. Wynik podajemy
w pelnych godzinach - przy tempie mierzonym w punktach na godzine i baterii
raportowanej co 5 punktow minuty bylyby pozorna dokladnoscia.

Gdy zadnego pomiaru jeszcze nie ma, wchodzi wartosc katalogowa producenta -
podawana z zewnatrz, z device_catalog.py - oznaczana tylda (na przyklad "~54h"),
zeby bylo widac, ze to jeszcze nie pomiar tego konkretnego egzemplarza.

Czas na jeden procent
---------------------
To samo tempo odwrocone: zamiast "ile punktow na godzine" mowi "ile czasu na
jednym punkcie". Ta sama informacja, ale czytelniejsza dla czlowieka - "35 min
na 1%" od razu daje wyobrazenie, ile zostalo z osiemdziesieciu procent, podczas
gdy "1.7 pkt/h" wymaga dzielenia w glowie. Liczba idzie tylko do okna: na
ekranie 128x40 nie ma dla niej miejsca obok procentu, czasu pracy i ubytku.
"""

import math
from typing import NamedTuple

import i18n

# ---------------------------------------------------------------------------
# WAGI I PROGI
# ---------------------------------------------------------------------------

# Wagi sredniej wazonej. Biezaca sesja wazy wiecej, bo opisuje aktualny sposob
# uzywania urzadzenia, a nie sredni z ostatnich dni.
WEIGHT_SESSION = 0.7
WEIGHT_HISTORY = 0.3

# Ile ostatnich zamknietych cykli wchodzi do tempa historycznego.
HISTORY_CYCLES = 5

# Kiedy wierzymy tempu biezacej sesji. Aerox raportuje baterie w krokach po
# 5 punktow, wiec mniejszy spadek nie mowi nic pewnego o tempie, a pol godziny
# to minimum, przy ktorym jeden krok nie rozjezdza wyniku.
MIN_SESSION_DROP = 5
MIN_SESSION_HOURS = 0.5

# Gorne ograniczenie wyniku. Bardzo powolne rozladowanie na krotkiej probce
# potrafi dac tysiace godzin - taka liczba nic nie wnosi, a rozsadza uklad ekranu.
MAX_ESTIMATE_HOURS = 999

# Gorne ograniczenie czasu na jeden procent. Ta sama granica co dla calego
# szacunku: powyzej niej liczba przestaje cokolwiek znaczyc.
MAX_PER_POINT_HOURS = MAX_ESTIMATE_HOURS

# Zrodla wyniku, w kolejnosci od najbardziej do najmniej wiarygodnego.
SOURCE_BLENDED = "blended"
SOURCE_SESSION = "session"
SOURCE_HISTORY = "history"
SOURCE_RATED = "rated"


class Estimate(NamedTuple):
    """Wynik szacowania.

    hours   - pozostaly czas pracy w godzinach, albo None gdy nie da sie policzyc
    source  - skad wynik pochodzi (jedna ze stalych SOURCE_*), albo None
    rate    - przewidywane tempo w punktach procentowych na godzine, albo None
    """

    hours: float
    source: str
    rate: float

    @property
    def measured(self):
        """Czy wynik opiera sie na pomiarze, a nie na danych producenta."""
        return self.source in (SOURCE_BLENDED, SOURCE_SESSION, SOURCE_HISTORY)


# ---------------------------------------------------------------------------
# CZYSTE FUNKCJE TEMPA
# ---------------------------------------------------------------------------


def _finite(value):
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


def rate_from(drop, hours):
    """Tempo w punktach na godzine z zaobserwowanego spadku, albo None.

    Zwraca None takze przy spadku zerowym lub ujemnym: urzadzenie, ktore nic
    nie traci, nie pozwala niczego przewidziec.
    """
    if not _finite(hours) or hours <= 0:
        return None
    if not _finite(drop) or drop <= 0:
        return None
    rate = drop / hours
    return rate if _finite(rate) and rate > 0 else None


def session_rate(drop, hours):
    """Tempo biezacej sesji, ale tylko gdy probka jest dosc duza, by cos znaczyc."""
    if not _finite(drop) or not _finite(hours):
        return None
    if drop < MIN_SESSION_DROP or hours < MIN_SESSION_HOURS:
        return None
    return rate_from(drop, hours)


def history_rate(cycles, limit=HISTORY_CYCLES):
    """Srednie tempo z ostatnich zamknietych cykli.

    Liczymy je jako suma spadkow przez suma czasow, a nie jako srednia
    arytmetyczna temp poszczegolnych cykli. Inaczej dwugodzinny cykl wazylby
    tyle samo co czterdziestogodzinny, choc mowi o urzadzeniu znacznie mniej.
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        return None
    recent = [cycle for cycle in cycles if rate_from(cycle.drop, cycle.hours) is not None][-limit:]
    if not recent:
        return None

    total_drop = sum(cycle.drop for cycle in recent)
    total_hours = sum(cycle.hours for cycle in recent)
    return rate_from(total_drop, total_hours)


def blend_rates(from_session, from_history):
    """Laczy oba tempa w jedno. Zwraca (tempo, zrodlo).

    Gdy dostepne jest tylko jedno tempo, bierzemy je w calosci. Mieszanie go
    z zerem zanizyloby albo zawyzyloby wynik zaleznie od tego, ktorego brakuje,
    zamiast po prostu oprzec sie na tym, co wiemy.
    """
    from_session = from_session if _finite(from_session) and from_session > 0 else None
    from_history = from_history if _finite(from_history) and from_history > 0 else None
    if from_session is not None and from_history is not None:
        blended = WEIGHT_SESSION * from_session + WEIGHT_HISTORY * from_history
        return blended, SOURCE_BLENDED
    if from_session is not None:
        return from_session, SOURCE_SESSION
    if from_history is not None:
        return from_history, SOURCE_HISTORY
    return None, None


# ---------------------------------------------------------------------------
# SZACOWANIE
# ---------------------------------------------------------------------------


def estimate(state, rated=None):
    """Liczy pozostaly czas pracy na podstawie migawki z osi czasu.

    "state" jest tylko czytany - ten modul nigdy nie zapisuje historii.
    "rated" to katalogowy czas pracy w godzinach, brany z device_catalog.py
    i uzywany dopiero wtedy, gdy nie ma zadnego wlasnego pomiaru.

    Urzadzenie w trakcie ladowania nie dostaje szacunku. Tempo rozladowania
    z poprzedniej sesji nic wtedy nie mowi, a poziom rosnie, wiec dzielenie
    przez tempo spadku bylo by bez sensu.
    """
    if state is None or state.charging:
        return Estimate(None, None, None)

    level = state.level
    if not _finite(level) or not 0 <= level <= 100:
        return Estimate(None, None, None)

    # Oba tempa biora tylko to, co zmierzone na laczu uzywanym w tej chwili.
    # Ta sama bateria schodzi na Bluetooth kilka razy wolniej niz na dongle'u,
    # wiec wspolna srednia myliloby sie na obu laczach naraz.
    rate, source = blend_rates(
        session_rate(state.link_drop, state.link_hours),
        history_rate(state.link_cycles),
    )

    if rate is not None:
        hours = min(level / rate, MAX_ESTIMATE_HOURS)
        return Estimate(hours, source, rate)

    # Nie ma jeszcze zadnego pomiaru - zostaja dane producenta, przeliczone
    # proporcjonalnie do biezacego poziomu.
    if _finite(rated) and rated > 0:
        hours = min(rated * (level / 100.0), MAX_ESTIMATE_HOURS)
        return Estimate(hours, SOURCE_RATED, rate_from(100.0, rated))

    return Estimate(None, None, None)


# ---------------------------------------------------------------------------
# CZAS NA JEDEN PROCENT
# ---------------------------------------------------------------------------


def hours_per_point(rate):
    """Ile godzin urzadzenie dziala na jednym punkcie procentowym baterii.

    Bierze gotowe tempo z Estimate.rate, a nie migawke stanu - liczenie go
    drugi raz dawaloby dokladnie te sama wartosc, tylko z ryzykiem, ze kiedys
    rozjedzie sie z ta, na ktorej opiera sie czas pracy pokazany obok.
    """
    if not _finite(rate) or rate <= 0:
        return None

    return min(1.0 / rate, MAX_PER_POINT_HOURS)


# ---------------------------------------------------------------------------
# FORMATOWANIE
# ---------------------------------------------------------------------------


def format_per_point(hours, source=None):
    """Czas na jeden procent, w postaci do okna.

    Tu jednostka sa minuty, nie godziny. Typowa mysz schodzi punkt na kilkanascie
    do kilkudziesieciu minut, wiec zaokraglenie do pelnych godzin - jak w czasie
    pozostalym - zamienialoby caly ten wskaznik w "<1h" i nic by nie mowilo.
    """
    if not _finite(hours) or hours < 0:
        return ""

    prefix = "~" if source == SOURCE_RATED else ""
    minutes = hours * 60

    if minutes < 1:
        # Ponizej minuty na punkt: bateria schodzi w oczach, dokladna liczba
        # sekund nic tu nie zmienia.
        return f"{prefix}<1 min"

    if hours < 1:
        return f"{prefix}{int(round(minutes))} min"

    return f"{prefix}{format_elapsed(hours)}"


def format_hours(hours, source=None):
    """Sklada napis do rogu ekranu, zaokraglony do pelnych godzin.

    Ponizej godziny pokazujemy "<1h", a nie "0h" - zero sugerowaloby, ze
    urzadzenie juz sie wylaczylo, a chodzi o ostatnie minuty pracy.
    """
    if not _finite(hours) or hours < 0:
        return ""

    prefix = "~" if source == SOURCE_RATED else ""

    if hours < 1:
        return f"{prefix}<1h"

    return f"{prefix}{int(round(hours))}h"


def format_elapsed(hours):
    """Czas pracy od naladowania, do pokazania w oknie.

    Tutaj minuty maja sens: to zmierzony czas, ktory naprawde uplynal, a nie
    przewidywanie. Wynik jest wiec dokladniejszy niz sam szacunek.
    """
    if not _finite(hours) or hours < 0:
        return ""

    if hours < 1:
        minutes = int(round(hours * 60))
        if minutes <= 0:
            return "0 min"
        return f"{minutes} min"

    whole = int(hours)
    minutes = int(round((hours - whole) * 60))
    if minutes == 60:
        whole += 1
        minutes = 0

    if minutes == 0:
        return f"{whole} h"
    return f"{whole} h {minutes:02d} min"


def format_drop(points):
    """Ile procent ubylo od naladowania, w postaci na ekran OLED.

    Zero zwracamy jako pusty napis. "-0%" tuz po odlaczeniu kabla nie wnosi nic,
    a ekran 128x40 jest za ciasny, zeby trzymac na nim napis bez tresci.
    """
    if not _finite(points) or points <= 0:
        return ""

    return f"-{int(points)}%"


def charging_text():
    """Napis zamiast czasu pracy, gdy urzadzenie sie laduje.

    Krotki, bo musi sie zmiescic w rogu ekranu 128x40: "LAD" po polsku, "CHG"
    po angielsku. Funkcja, a nie stala, bo jezyk mozna zmienic bez restartu.
    """
    return i18n.t("oled.charging")


def runtime_text(state, rated=None):
    """Gotowy napis do prawego gornego rogu ekranu OLED."""
    if state is not None and state.charging:
        return charging_text()

    result = estimate(state, rated)
    return format_hours(result.hours, result.source)
