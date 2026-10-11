"""Os czasu odczytow baterii - warstwa danych.

Ten modul zajmuje sie wylacznie przechowywaniem i porzadkowaniem historii.
Cala matematyka przewidywania siedzi w runtime_estimator.py, ktory dostaje
z tutejszej migawki gotowe fakty: probki biezacej sesji i zamkniete cykle.

Dlaczego JSON, a nie SQLite
---------------------------
Przy zapisie dopiero na zmiane o punkt procentowy jedno urzadzenie generuje
okolo stu rekordow na pelny cykl rozladowania, a w pliku trzymamy biezaca sesje
i podsumowania kilku ostatnich cykli - to kilkaset rekordow, nie miliony.
SQLite dodalby zaleznosc i odebral mozliwosc zajrzenia do danych zwyklym
edytorem, nie dajac nic w zamian. Bylby wart zachodu przy wielu procesach
piszacych rownolegle albo przy zapisie kazdego odczytu bez filtrowania.

Kiedy zapisujemy
----------------
Zapis na dysk nastepuje tylko wtedy, gdy odczyt naprawde cos wnosi:

  - zmienil sie stan ladowania (to granica cyklu, zawsze warta zapisu),
  - poziom zmienil sie o co najmniej MIN_LEVEL_DELTA punktu procentowego,
  - to pierwszy odczyt urzadzenia,
  - albo naroslo co najmniej ACTIVE_SAVE_SECONDS czasu wlaczenia przy
    niezmienionym poziomie (inaczej po restarcie ginelyby godziny, w ktorych
    bateria stala w miejscu).

Czas wlaczenia, nie zegar scienny
---------------------------------
Czas wlaczenia to suma odcinkow miedzy kolejnymi udanymi odczytami, o ile
przerwa nie przekracza MAX_ONLINE_GAP. Wylaczenie sprzetu, zamkniecie programu
albo sen komputera daje duza luke i wypada z licznika - dzieki temu tempo
rozladowania nie jest zanizane przez godziny, w ktorych urzadzenie spalo.

Jedna bateria, kilka lacz
-------------------------
Ta sama mysz na dongle'u, na kablu i po Bluetooth to trzy rozne PID-y i trzy
rozne nazwy w Windows, ale jedna bateria. Historie prowadzimy wiec per bateria
(klucz modelu z katalogu), inaczej przelaczenie myszy na Bluetooth zaczynaloby
sesje od nowa: moment odlaczenia kabla zostalby po drugiej stronie, a licznik
czasu wlaczenia i zuzytych procent nie mialby od czego liczyc. Po Bluetooth
Windows w ogole nie zglasza ladowania, wiec osobna historia BT nigdy nie
zobaczylaby poczatku sesji i "nieznane" zostaloby tam na zawsze.

Tempa jednak mieszac nie wolno: producent podaje dla Aeroxa 180 h na Bluetooth
wobec 80 h na 2.4 GHz. Dlatego kazda probka i kazdy cykl nosi swoje lacze, czas
wlaczenia narasta osobno dla kazdego z nich, a szacunek czasu pracy bierze pod
uwage tylko to, co zmierzone na laczu uzywanym w tej chwili. Licznik "Wlaczone"
i zuzyte procenty opisuja bateria jako calosc, wiec sumuja wszystkie lacza.

Format pliku (battery_timeline.json)
------------------------------------
    {
      "version": 5,
      "devices": {
        "<klucz baterii>": {
          "samples": [[timestamp, poziom, czy_laduje, lacze], ...],
          "cycles":  [[start, koniec, poz_start, poz_koniec, active_s, lacze], ...],
          "charge_end": timestamp albo null,
          "observed_start": true/false,
          "active": {"<lacze>": sekundy},
          "measured_drop": {"<lacze>": punkty},
          "last_seen": timestamp albo null,
          "active_saved": float
        }
      }
    }

"samples" to wylacznie biezaca sesja - od ostatniego odlaczenia kabla do teraz.
Zamkniete sesje zapisujemy jako "cycles". Dlugosc cyklu to czas wlaczenia,
nie roznica znacznikow sciennych - te ostanie zostaja jako granice kalendarzowe.

"charge_end" to dokladny timestamp momentu, w ktorym ladowanie przeszlo
z true na false. "observed_start" mowi, czy ten moment naprawde widzielismy -
gdy program wystartowal przy urzadzeniu juz pracujacym na baterii, czasu od
naladowania po prostu nie znamy i nie wolno go udawac.
"""

import json
import math
import os
import time
from typing import NamedTuple

import device_catalog
import paths

# Pliki leza w %APPDATA%, a nie obok programu - patrz paths.py. Katalog obok
# spakowanego .exe jest tymczasowy i znika przy zamknieciu programu.
TIMELINE_PATH = paths.data_file("battery_timeline.json")

# Plik w starym formacie (pary [czas, procent], bez stanu ladowania). Przy
# pierwszym uruchomieniu przenosimy z niego probki, zeby nie stracic pomiaru,
# ktory zbieral sie godzinami.
LEGACY_HISTORY_PATH = paths.data_file("battery_history.json")

FORMAT_VERSION = 5

# Lacze przypisywane odczytom, ktore go nie podaly (stare pliki, testy). Dongle
# jest tu rozsadnym domyslem: to jedyne lacze, ktore ma kazdy bezprzewodowy
# model z katalogu.
DEFAULT_LINK = device_catalog.LINK_24

# ---------------------------------------------------------------------------
# PROGI
# ---------------------------------------------------------------------------

# Minimalna zmiana poziomu, ktora trafia na dysk.
MIN_LEVEL_DELTA = 1

# Wzrost poziomu bez zgloszonego ladowania. Urzadzenia raportuja poziom
# skokowo i potrafia sie o punkt "cofnac", ale wzrost wiekszy niz ten oznacza,
# ze urzadzenie bylo ladowane, gdy nie patrzylismy - na przyklad przy
# wylaczonym programie albo na laczu Bluetooth, ktore ladowania nie zglasza.
CHARGE_TOLERANCE = 3

# To samo, ale dla pierwszego odczytu po zmianie lacza. Kazde lacze mierzy
# bateria wlasna miarka: mysz na dongle'u raportuje co 5 punktow, a Windows
# po Bluetooth potrafi pokazac zupelnie inna liczbe dla tego samego ogniwa.
# Bez luzniejszego progu samo przelaczenie myszy na Bluetooth wygladaloby jak
# potajemne naladowanie i kasowaloby trwajaca sesje.
LINK_SWITCH_TOLERANCE = 15

# Odsiewanie bledynch odczytow z sensora. Progu nie da sie oprzec na samym
# tempie: Aerox raportuje baterie w krokach po 5 punktow, wiec legalny krok
# miedzy dwoma odczytami co minute to chwilowe 300 punktow na godzine.
# Dlatego odrzucamy tylko duzy spadek w krotkim czasie - zadne z tych urzadzen
# nie jest w stanie stracic 25 punktow w kwadrans.
GLITCH_MIN_DROP = 25
GLITCH_MAX_ELAPSED_SECONDS = 900

# Kiedy zamknieta sesja zasluguje na miejsce w historii. Cykl z 100% na 95%
# nie mowi nic o tempie, a zanizylby srednia.
MIN_CYCLE_DROP = 10
MIN_CYCLE_HOURS = 0.5

# Ile zamknietych cykli pamietamy i ile probek trzymamy dla biezacej sesji.
MAX_CYCLES = 10
MAX_SAMPLES = 400

# Najdluzsza przerwa miedzy odczytami, ktora jeszcze liczymy jako czas
# wlaczenia. Monitor odpytuje co 60 s; trzy cykle daja zapas na chwilowe
# opoznienie. Wieksza luka to wylaczenie sprzetu, sen PC albo zamkniety program.
MAX_ONLINE_GAP = 180.0

# Przy plaskim poziomie zapisujemy active_seconds co tyle sekund narostu -
# inaczej restart programu gubilby godziny, w ktorych bateria nie drgnela.
ACTIVE_SAVE_SECONDS = 300.0


# ---------------------------------------------------------------------------
# STRUKTURY
# ---------------------------------------------------------------------------


class Sample(NamedTuple):
    """Jeden odczyt na osi czasu, wraz z laczem, na ktorym go zrobiono."""

    timestamp: float
    level: int
    charging: bool
    link: str = DEFAULT_LINK


def _finite_number(value):
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


def _valid_link(value):
    return isinstance(value, str) and bool(value.strip())


def sample_from_entry(entry, link=DEFAULT_LINK):
    """Buduje Sample z wpisu na dysku - obsluguje stary format bez lacza."""
    if not isinstance(entry, (list, tuple)) or len(entry) < 3:
        raise ValueError("invalid battery sample")
    if not _finite_number(entry[0]) or entry[0] < 0:
        raise ValueError("invalid sample timestamp")
    if not _finite_number(entry[1]) or not 0 <= entry[1] <= 100:
        raise ValueError("invalid battery level")
    if not isinstance(entry[2], (bool, int)) or entry[2] not in (0, 1):
        raise ValueError("invalid charging flag")
    link = entry[3] if len(entry) >= 4 else link
    if not _valid_link(link):
        raise ValueError("invalid battery link")
    return Sample(float(entry[0]), int(entry[1]), bool(entry[2]), str(link))


class Cycle(NamedTuple):
    """Zamkniety pomiar rozladowania, do ponownego ladowania.

    "active_seconds" to czas, w ktorym monitor widzial urzadzenie online.
    Znaczniki start/end zostaja jako granice kalendarzowe, ale tempo bierze
    sie wylacznie z active_seconds.

    Poczatek moze byc pierwsza obserwacja juz rozladowywanej baterii:
    do zmierzenia tempa wystarczy jej obserwowany spadek i czas aktywnosci.
    "link" wskazuje lacze tego pomiaru. Sesja obejmujaca kilka lacz daje
    osobny cykl dla kazdego z wystarczajacym zmierzonym spadkiem i czasem.
    """

    start: float
    end: float
    start_level: int
    end_level: int
    active_seconds: float
    link: str = DEFAULT_LINK

    @property
    def hours(self):
        return max(0.0, self.active_seconds / 3600.0)

    @property
    def drop(self):
        return self.start_level - self.end_level

    @property
    def rate(self):
        """Tempo rozladowania w punktach procentowych na godzine."""
        if self.hours <= 0:
            return None
        return self.drop / self.hours


def cycle_from_entry(entry, link=DEFAULT_LINK):
    """Buduje Cycle z wpisu na dysku - obsluguje stare formaty bez czasu i lacza."""
    if not isinstance(entry, (list, tuple)) or len(entry) < 4:
        raise ValueError("invalid battery cycle")
    if not all(_finite_number(value) for value in entry[:4]):
        raise ValueError("invalid cycle values")
    start = float(entry[0])
    end = float(entry[1])
    start_level = int(entry[2])
    end_level = int(entry[3])
    if start < 0 or end < start or not 0 <= end_level < start_level <= 100:
        raise ValueError("invalid cycle boundaries")
    if len(entry) >= 5:
        if not _finite_number(entry[4]) or entry[4] <= 0:
            raise ValueError("invalid cycle duration")
        active = float(entry[4])
    else:
        # Stare cykle znaly tylko zegar scienny - to nadal lepsze niz wyrzucenie.
        active = max(0.0, end - start)
    if len(entry) >= 6:
        link = entry[5]
    if active <= 0 or not _valid_link(link):
        raise ValueError("invalid cycle duration or link")
    return Cycle(start, end, start_level, end_level, active, link)


class DeviceState(NamedTuple):
    """Migawka stanu jednej baterii, gotowa do policzenia szacunku.

    "active_by_link" to czas wlaczenia w rozbiciu na lacza; narasta przy kazdym
    udanym odczycie online. Liczniki pokazywane uzytkownikowi ("Wlaczone",
    zuzyte procenty) sumuja wszystkie lacza, bo opisuja jedna bateria. Tempo
    rozladowania patrzy juz tylko na lacze biezace ("link"), bo na Bluetooth
    ta sama bateria schodzi kilka razy wolniej niz na dongle'u.
    """

    samples: tuple
    cycles: tuple
    charge_end: float
    observed_start: bool
    now: float
    active_by_link: dict
    link: str = DEFAULT_LINK
    measured_drop_by_link: dict = None

    @property
    def level(self):
        return self.samples[-1].level if self.samples else None

    @property
    def charging(self):
        return bool(self.samples[-1].charging) if self.samples else False

    @property
    def active_seconds(self):
        """Czas wlaczenia calej baterii, bez wzgledu na uzywane lacze."""
        return sum(max(0.0, value) for value in self.active_by_link.values())

    @property
    def link_cycles(self):
        """Zamkniete cykle zmierzone na laczu uzywanym teraz."""
        return tuple(cycle for cycle in self.cycles if cycle.link == self.link)

    @property
    def link_hours(self):
        """Ile godzin biezacej sesji uplynelo na laczu uzywanym teraz."""
        return max(0.0, self.active_by_link.get(self.link, 0.0) / 3600.0)

    @property
    def link_drop(self):
        """Ile punktow ubylo w biezacej sesji na laczu uzywanym teraz.

        Licznik obejmuje tylko odczyty tego samego lacza podczas obserwacji
        online. Roznica miarek po zmianie lacza oraz spadek podczas dlugiej
        przerwy nie maja zmierzonego czasu i nie sa pomiarem tempa.
        Niewielkie wzrosty odejmujemy, by szum sensora nie udawal zuzycia.
        """
        if self.measured_drop_by_link is not None:
            return max(0, self.measured_drop_by_link.get(self.link, 0))
        return max(0, _sample_drop_totals(self.samples).get(self.link, 0))

    @property
    def session_start(self):
        """Timestamp poczatku biezacej sesji rozladowania, albo None."""
        if self.charge_end is not None:
            return self.charge_end
        return self.samples[0].timestamp if self.samples else None

    @property
    def session_hours(self):
        """Ile godzin urzadzenie bylo widziane online w biezacej sesji."""
        if self.session_start is None and not self.samples:
            return None
        return max(0.0, self.active_seconds / 3600.0)

    @property
    def hours_since_charge(self):
        """Czas wlaczenia od ostatniego naladowania.

        Zwraca None, gdy momentu odlaczenia kabla nie widzielismy - wtedy
        jedyna szczera odpowiedz jest "nie wiem".
        """
        if not self.observed_start or self.charge_end is None:
            return None
        return max(0.0, self.active_seconds / 3600.0)

    @property
    def session_drop(self):
        """O ile punktow spadl poziom w biezacej sesji."""
        if len(self.samples) < 2:
            return 0
        return self.samples[0].level - self.samples[-1].level

    @property
    def drop_since_charge(self):
        """Ile punktow procentowych ubylo od ostatniego naladowania.

        Zwraca None dokladnie w tych samych sytuacjach co hours_since_charge:
        gdy nie widzielismy momentu odlaczenia kabla. Bez tego pierwsza probka
        nie jest poziomem po naladowaniu, a poziomem z chwili, w ktorej program
        zaczal patrzec - i licznik pokazywalby zuzycie od wlaczenia programu,
        podpisane jako zuzycie od naladowania.

        Wynik nie schodzi ponizej zera. Urzadzenia potrafia sie o punkt "cofnac"
        w gore bez ladowania, a "ubylo -1%" nie znaczy nic sensownego.
        """
        if not self.observed_start or self.charge_end is None:
            return None
        return max(0, self.session_drop)


# ---------------------------------------------------------------------------
# OS CZASU
# ---------------------------------------------------------------------------


class Timeline:
    """Przechowuje historie odczytow i rozpoznaje granice cykli ladowania."""

    def __init__(self, path=TIMELINE_PATH, legacy_path=LEGACY_HISTORY_PATH):
        self._path = path
        previous = self._read_json(self._path)
        self._data = self._load(legacy_path, previous)
        self._dirty = False
        self._heartbeat_dirty = False
        self._clock_ticks = {}

        # Odczyty odrzucone jako bledne, czekajace na potwierdzenie kolejnym
        # odczytem. Trzymane tylko w pamieci - po restarcie po prostu
        # sprawdzimy je ponownie.
        self._pending = {}

        # Migracja licznikow zyje inaczej tylko w pamieci, dopoki
        # poziom baterii sie nie ruszy albo nie narosnie ACTIVE_SAVE_SECONDS.
        # Przy plaskim poziomie plik na dysku zostawalby w starym formacie przez
        # godziny, a restart gubilby juz naliczony czas wlaczenia - zapisujemy
        # od razu po podniesieniu formatu.
        if self._needs_format_rewrite(previous):
            self._save()

    # -- wejscie ------------------------------------------------------------

    def record(self, key, level, charging, now=None, link=DEFAULT_LINK):
        """Dopisuje odczyt i zwraca aktualna migawke stanu baterii.

        "key" to stabilny identyfikator baterii - dzis jest nim klucz modelu
        z katalogu, wspolny dla wszystkich lacz tego samego urzadzenia.
        "link" mowi, ktorym laczem przyszedl ten odczyt.
        """
        implicit_clock = now is None
        clock_tick = time.monotonic() if implicit_clock else None
        now = time.time() if implicit_clock else now
        if not _finite_number(now) or now < 0 or not _finite_number(level) or not 0 <= level <= 100:
            return self.state(key)
        level = int(level)
        charging = bool(charging)
        link = str(link or DEFAULT_LINK)

        device = self._device(key)
        samples = device["samples"]
        previous = sample_from_entry(samples[-1]) if samples else None

        # Opozniony odczyt nie moze cofnac ostatniego kontaktu i naliczyc
        # ponownie juz zaobserwowanego odcinka czasu.
        last_seen = device.get("last_seen")
        latest = max(previous.timestamp, last_seen or 0) if previous else last_seen
        elapsed = now - last_seen if last_seen is not None else None
        if implicit_clock and latest is not None:
            tick_before = self._clock_ticks.get(key)
            if tick_before is not None and last_seen is not None:
                elapsed = max(0.0, clock_tick - tick_before)
                # Czas aktywnosci mierzy zegar monotoniczny. Cofniecie UTC
                # nie moze zatrzymac odczytow ani przestawic kolejnosci probek.
                now = max(now, latest + elapsed)
            else:
                now = max(now, latest)
        elif latest is not None and now < latest:
            return self.state(key, now=now, link=link)

        changed = self._apply(key, device, previous, level, charging, now, link, elapsed)
        if implicit_clock:
            self._clock_ticks[key] = clock_tick
        else:
            self._clock_ticks.pop(key, None)
        self._heartbeat_dirty = self._heartbeat_dirty or last_seen != device.get("last_seen")
        self._dirty = self._dirty or changed
        if self._dirty:
            self._save()

        return self.state(key, now=now, link=link)

    def state(self, key, now=None, link=DEFAULT_LINK):
        """Zwraca migawke stanu bez dopisywania czegokolwiek."""
        now = time.time() if now is None else now
        link = str(link or DEFAULT_LINK)

        device = self._data["devices"].get(key)
        if device is None:
            return DeviceState((), (), None, False, now, {}, link)

        return DeviceState(
            samples=tuple(sample_from_entry(entry) for entry in device["samples"]),
            cycles=tuple(cycle_from_entry(entry) for entry in device["cycles"]),
            charge_end=device["charge_end"],
            observed_start=bool(device["observed_start"]),
            now=now,
            active_by_link=dict(device.get("active") or {}),
            link=link,
            measured_drop_by_link=dict(device.get("measured_drop") or {}),
        )

    def devices(self):
        return list(self._data["devices"])

    def mark_offline(self, key):
        """Przerywa heartbeat po potwierdzonym braku odpowiedzi urzadzenia.

        Sesja i pomiary zostaja; nastepny odczyt zaczyna nowy odcinek online,
        nawet jesli brak odpowiedzi trwal krocej niz MAX_ONLINE_GAP.
        """
        device = self._data["devices"].get(key)
        if device is None:
            return
        if device["last_seen"] is not None:
            device["last_seen"] = None
            self._heartbeat_dirty = True
        if device.get("rise") is not None:
            device["rise"] = None
            self._heartbeat_dirty = True
        self._clock_ticks.pop(key, None)
        self._pending.pop(key, None)

    def flush(self):
        """Utrwala koncowe heartbeaty przy zamknieciu monitora.

        Wywoluje je ten sam watek, ktory zapisuje odczyty. Zwraca False przy
        bledzie dysku; niezapisane zmiany zostaja w pamieci do ponownej proby.
        """
        pending_time = any(
            sum(device["active"].values()) != device["active_saved"]
            for device in self._data["devices"].values()
        )
        return self._save() if self._dirty or self._heartbeat_dirty or pending_time else True

    # -- logika przejsc -----------------------------------------------------

    def _apply(self, key, device, previous, level, charging, now, link, elapsed):
        """Nanosi odczyt na os czasu. Zwraca True, gdy trzeba zapisac plik."""
        if previous is None:
            # Pierwszy odczyt urzadzenia. Jesli trwa ladowanie, sesja
            # rozladowania jeszcze sie nie zaczela; jesli nie trwa, to jej
            # poczatku nie widzielismy.
            device["charge_end"] = None
            device["observed_start"] = False
            device["samples"] = [[now, level, int(charging), link]]
            self._reset_active(device, now)
            return True

        if previous.charging and not charging:
            # Kabel odlaczony. To jest dokladnie ten moment, ktorego szukamy:
            # przejscie ladowania z true na false. Od niego liczymy czas pracy.
            observed = elapsed is not None and 0 <= elapsed <= MAX_ONLINE_GAP
            device["charge_end"] = now if observed else None
            device["observed_start"] = observed
            device["samples"] = [[now, level, 0, link]]
            self._reset_active(device, now)
            self._pending.pop(key, None)
            return True

        if not previous.charging and charging:
            # Ladowanie ruszylo, wiec biezaca sesja rozladowania sie zamknela.
            self._close_cycle(device, previous)
            device["charge_end"] = None
            device["observed_start"] = False
            device["samples"] = [[now, level, 1, link]]
            self._reset_active(device, now)
            self._pending.pop(key, None)
            return True

        if charging:
            # W trakcie ladowania interesuje nas tylko biezacy poziom.
            device["last_seen"] = now
            return self._append_if_moved(device, previous, level, charging, now, link)

        return self._apply_discharge(key, device, previous, level, now, link, elapsed)

    def _apply_discharge(self, key, device, previous, level, now, link, elapsed):
        """Odczyt w trakcie pracy na baterii - tutaj mieszka odsiewanie bledow."""
        delta = level - previous.level
        switched = link != previous.link
        baseline = device.get("rise")
        if (switched or elapsed is None or elapsed > MAX_ONLINE_GAP
                or (baseline is not None and level <= baseline["level"])):
            baseline = None
            device["rise"] = None
        if delta > 0 and not switched and baseline is None and elapsed is not None and 0 <= elapsed <= MAX_ONLINE_GAP:
            # Jesli wzrost okaze sie ladowaniem, odzyskamy pomiar sprzed
            # pierwszego wzrostu, bez doliczania czasu ladowania do rozladowania.
            baseline = {
                "link": link, "level": previous.level,
                "last_seen": device["last_seen"],
                "active": dict(device["active"]),
                "measured_drop": dict(device["measured_drop"]),
            }
            device["rise"] = baseline

        # Po zmianie lacza wzrost poziomu o kilka punktow jest normalny: to ta
        # sama bateria zmierzona inna miarka. Dopiero skok wiekszy niz
        # LINK_SWITCH_TOLERANCE znaczy, ze sprzet naprawde zostal naladowany.
        tolerance = LINK_SWITCH_TOLERANCE if switched else CHARGE_TOLERANCE

        rise = delta
        if baseline is not None:
            rise = max(rise, level - baseline["level"])
        if delta > 0 and not switched:
            # Kilka malych wzrostow tez moze byc ladowaniem po Bluetooth.
            # Granica lacza przerywa porownanie roznych miarek.
            for entry in reversed(device["samples"]):
                if entry[3] != link:
                    break
                rise = max(rise, level - entry[1])
        if rise > tolerance:
            # Poziom wyraznie wzrosl, choc urzadzenie nie zglasza ladowania.
            # Zostalo naladowane bez naszej wiedzy: zamykamy stary cykl, ale
            # nowej sesji nie mozemy datowac, bo nie wiemy, kiedy sie zaczela.
            self._close_cycle(device, previous, use_rise=True)
            device["charge_end"] = None
            device["observed_start"] = False
            device["samples"] = [[now, level, 0, link]]
            self._reset_active(device, now)
            self._pending.pop(key, None)
            return True

        drop = -delta
        if drop >= GLITCH_MIN_DROP and elapsed is not None and elapsed < GLITCH_MAX_ELAPSED_SECONDS and not switched:
            # Spadek fizycznie niemozliwy w tym czasie. Nie zapisujemy go, ale
            # zapamietujemy: gdy kolejny odczyt potwierdzi podobna wartosc, to
            # nie byl blad sensora i wtedy ja przyjmiemy. Przy zmianie lacza tej
            # bramki nie stosujemy - roznica bierze sie z miarki, nie z sensora,
            # a odrzucanie odczytow zatrzymaloby monitor na starej wartosci.
            confirmed = self._pending.get(key)
            if confirmed is None or abs(confirmed[1] - level) > CHARGE_TOLERANCE or now <= confirmed[0]:
                self._pending[key] = (now, level)
                # Urzadzenie odpowiedzialo - czas wlaczenia i tak sie liczy.
                return self._touch_active(device, now, link, elapsed)

            self._pending.pop(key, None)

        else:
            self._pending.pop(key, None)

        # Roznica poziomu ma zmierzony czas tylko wtedy, gdy urzadzenie bylo
        # stale widoczne na tym samym laczu. Po przerwie zachowujemy nowy
        # poziom, lecz nie dzielimy spadku offline przez same godziny online.
        if not switched and elapsed is not None and 0 <= elapsed <= MAX_ONLINE_GAP:
            measured = device.setdefault("measured_drop", {})
            # Zachowujemy takze ujemne saldo. Przyciecie kazdego wzrostu do
            # zera sprawiloby, ze 80->81->80 tworzyloby fikcyjny spadek.
            measured[link] = measured.get(link, 0) - delta
        level_changed = self._append_if_moved(device, previous, level, False, now, link)
        active_changed = self._touch_active(device, now, previous.link, elapsed)
        return level_changed or active_changed

    def _append_if_moved(self, device, previous, level, charging, now, link):
        """Dopisuje probke, gdy zmienil sie poziom albo lacze.

        Zmiana lacza sama w sobie jest faktem wartym zapisania nawet przy tym
        samym poziomie: od tej chwili czas i spadek naliczaja sie na inne konto,
        a bez granicy w probkach nie dalo by sie ich rozdzielic.
        """
        if abs(level - previous.level) < MIN_LEVEL_DELTA and link == previous.link:
            return False

        device["samples"].append([now, level, int(charging), link])
        self._trim(device)
        return True

    @staticmethod
    def _reset_active(device, now):
        """Zeruje akumulatory czasu wlaczenia na granicy sesji."""
        device["active"] = {}
        device["measured_drop"] = {}
        device["rise"] = None
        device["last_seen"] = now
        device["active_saved"] = 0.0

    @staticmethod
    def _touch_active(device, now, link, elapsed):
        """Nalicza czas wlaczenia od poprzedniego odczytu, na konto tego lacza.

        Zwraca True, gdy naroslo tyle, ze warto zejsc na dysk - przy plaskim
        poziomie to jedyny sposob, zeby restart nie zgubil godzin obserwacji.
        """
        active = device.setdefault("active", {})
        if elapsed is not None and 0 < elapsed <= MAX_ONLINE_GAP:
            active[link] = float(active.get(link) or 0.0) + elapsed

        device["last_seen"] = now

        total = sum(max(0.0, value) for value in active.values())
        saved = float(device.get("active_saved") or 0.0)
        if total - saved >= ACTIVE_SAVE_SECONDS:
            return True
        return False

    def _close_cycle(self, device, last, use_rise=False):
        """Zapisuje zamknieta sesje jako cykl, jesli cokolwiek z niej wynika."""
        samples = device["samples"]
        # measured_drop i active opisuja wspolny, zaobserwowany fragment.
        # Nie musimy znac momentu odlaczenia kabla, by zmierzyc tempo.
        if not samples:
            return

        baseline = device.get("rise") if use_rise else None
        measurement = baseline or device
        active = measurement.get("active") or {}
        if not active:
            return

        first = sample_from_entry(samples[0])
        end = baseline["last_seen"] if baseline else max(last.timestamp, device.get("last_seen") or last.timestamp)
        drops = measurement.get("measured_drop") or {}
        for link, seconds in active.items():
            drop = max(0, min(100, drops.get(link, 0)))
            first_level = next((entry[1] for entry in samples if entry[3] == link), first.level)
            # W cyklu mieszanym lub z luka poziomy opisuja tylko zmierzony
            # ubytek, a nie roznice kalibracji lacz czy zuzycie offline.
            start_level = max(first_level, drop)
            cycle = Cycle(first.timestamp, end, start_level, start_level - drop, seconds, link)
            if cycle.drop >= MIN_CYCLE_DROP and cycle.hours >= MIN_CYCLE_HOURS:
                device["cycles"].append(list(cycle))
        device["cycles"] = device["cycles"][-MAX_CYCLES:]

    def _trim(self, device):
        """Ogranicza liczbe probek, zachowujac pierwsza.

        Pierwsza probka wyznacza poziom startowy sesji, wiec nie wolno jej
        wyrzucic - bez niej spadek sesji liczylby sie z coraz krotszego odcinka.
        """
        samples = device["samples"]
        if len(samples) <= MAX_SAMPLES:
            return
        device["samples"] = [samples[0]] + samples[-(MAX_SAMPLES - 1):]

    # -- dysk ---------------------------------------------------------------

    def _device(self, key):
        devices = self._data["devices"]
        if key not in devices:
            devices[key] = {
                "samples": [],
                "cycles": [],
                "charge_end": None,
                "observed_start": False,
                "active": {},
                "measured_drop": {},
                "rise": None,
                "last_seen": None,
                "active_saved": 0.0,
            }
        return devices[key]

    def _load(self, legacy_path, data):
        if isinstance(data, dict) and isinstance(data.get("devices"), dict):
            return self._sanitize(data)

        migrated = self._migrate_legacy(legacy_path)
        if migrated is not None:
            return migrated

        return {"version": FORMAT_VERSION, "devices": {}}

    @staticmethod
    def _read_json(path):
        try:
            with open(path, "r", encoding="utf-8-sig") as handle:
                return json.load(handle)
        except (OSError, UnicodeError, ValueError):
            return None

    def _sanitize(self, data):
        """Odsiewa uszkodzone wpisy i sprowadza plik do biezacego formatu.

        Tutaj tez laduja stare historie prowadzone osobno dla kazdego lacza:
        wpisy "Aerox 5 Wireless" i "Aerox 5 WL (Bluetooth)" to jedna bateria,
        wiec schodza sie pod wspolnym kluczem katalogowym.
        """
        clean = {}
        for key, device in data["devices"].items():
            if not isinstance(device, dict):
                continue

            battery_key, link = _battery_identity(key)
            record = self._sanitize_device(device, link)

            existing = clean.get(battery_key)
            clean[battery_key] = record if existing is None else _merge_devices(existing, record)

        return {"version": FORMAT_VERSION, "devices": clean}

    @staticmethod
    def _sanitize_device(device, link):
        """Porzadkuje jeden wpis. "link" obowiazuje tam, gdzie plik go nie podal."""
        samples = []
        for entry in _entries(device.get("samples")):
            try:
                samples.append(list(sample_from_entry(entry, link)))
            except (TypeError, ValueError, OverflowError):
                continue
        samples.sort(key=lambda entry: entry[0])
        reconstructed_drop = _sample_drop_totals(tuple(sample_from_entry(entry) for entry in samples))
        if len(samples) > MAX_SAMPLES:
            samples = [samples[0]] + samples[-(MAX_SAMPLES - 1):]

        cycles = []
        for entry in _entries(device.get("cycles")):
            try:
                cycles.append(list(cycle_from_entry(entry, link)))
            except (TypeError, ValueError, OverflowError):
                continue
        cycles.sort(key=lambda entry: entry[1])

        charge_end = device.get("charge_end")
        charge_end = float(charge_end) if _finite_number(charge_end) and charge_end >= 0 else None
        observed = device.get("observed_start") is True or device.get("observed_start") == 1
        if not samples or charge_end is None or charge_end > samples[-1][0]:
            charge_end = None
            observed = False

        active = device.get("active")
        if isinstance(active, dict):
            active = {
                name: max(0.0, float(value))
                for name, value in active.items()
                if _valid_link(name) and _finite_number(value)
            }
        elif _finite_number(device.get("active_seconds")):
            # Plik sprzed rozbicia na lacza: caly zebrany czas nalezy do lacza,
            # na ktorym ten wpis byl prowadzony.
            active = {link: max(0.0, float(device["active_seconds"]))}
        else:
            # Plik jeszcze starszy, bez licznika czasu wlaczenia: przyblizamy go
            # sciennym czasem sesji, zeby nie wyzerowac trwajacego pomiaru.
            active = {link: _legacy_active_seconds(samples, charge_end, observed)}

        total = sum(active.values())
        if not math.isfinite(total):
            active = {}
            total = 0.0
        measured = device.get("measured_drop")
        if isinstance(measured, dict):
            measured = {
                name: max(-100, min(100, int(value)))
                for name, value in measured.items()
                if _valid_link(name) and _finite_number(value)
            }
        else:
            # Stary format nie zapisywal granic przerw offline. Zachowujemy
            # odzyskiwalny pomiar, a nowe odczyty juz liczymy bez tych luk.
            measured = reconstructed_drop

        last_seen = device.get("last_seen")
        if "last_seen" in device and last_seen is None:
            # Jawnie zapisany brak heartbeatu oznacza urzadzenie offline.
            pass
        elif not _finite_number(last_seen) or last_seen < 0:
            last_seen = samples[-1][0] if samples else None
        else:
            last_seen = max(float(last_seen), samples[-1][0] if samples else 0)

        # Wszystko co wczytano jest juz utrwalone. Deadline kolejnego zapisu
        # liczymy od rzeczywiscie zapisanych godzin, nie starego progu.
        saved = total

        if samples and samples[-1][2]:
            charge_end = None
            observed = False
            active = {}
            measured = {}
            saved = 0.0

        rise = _sanitize_rise(device.get("rise"), samples, last_seen, active)

        return {
            "samples": samples,
            "cycles": cycles[-MAX_CYCLES:],
            "charge_end": charge_end,
            "observed_start": observed,
            "active": active,
            "measured_drop": measured,
            "rise": rise,
            "last_seen": last_seen,
            "active_saved": saved,
        }

    def _migrate_legacy(self, legacy_path):
        """Przenosi probki ze starego battery_history.json.

        Stary format kasowal historie przy ladowaniu, wiec to, co w nim zostalo,
        jest dokladnie biezaca sesja rozladowania - mozna ja przejac wprost.
        Stanu ladowania nie zapisywal, dlatego poczatku sesji nie uznajemy za
        zaobserwowany.
        """
        legacy = self._read_json(legacy_path)
        if not isinstance(legacy, dict) or not legacy:
            return None

        devices = {}
        for key, samples in legacy.items():
            if not isinstance(samples, list):
                continue
            battery_key, link = _battery_identity(key)
            converted = []
            for entry in samples:
                if not isinstance(entry, list) or len(entry) != 2:
                    continue
                try:
                    converted.append(list(sample_from_entry([*entry, 0, link])))
                except (TypeError, ValueError, OverflowError):
                    continue
            converted.sort(key=lambda entry: entry[0])
            if not converted:
                continue
            active = _legacy_active_seconds(converted, None, False)
            record = {
                "samples": converted if len(converted) <= MAX_SAMPLES else [converted[0]] + converted[-(MAX_SAMPLES - 1):],
                "cycles": [],
                "charge_end": None,
                "observed_start": False,
                "active": {link: active},
                "measured_drop": _sample_drop_totals(tuple(sample_from_entry(entry) for entry in converted)),
                "rise": None,
                "last_seen": converted[-1][0],
                "active_saved": active,
            }
            existing = devices.get(battery_key)
            devices[battery_key] = record if existing is None else _merge_devices(existing, record)

        if not devices:
            return None

        return {"version": FORMAT_VERSION, "devices": devices}

    def _save(self):
        """Zapisuje plik przez podmiane, zeby przerwanie nie zostawilo ruiny."""
        temporary = self._path + ".tmp"
        payload = {"version": FORMAT_VERSION, "devices": {
            key: {**device, "active_saved": sum(device["active"].values())}
            for key, device in self._data["devices"].items()
        }}
        try:
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, allow_nan=False)
            os.replace(temporary, self._path)
            for key, device in payload["devices"].items():
                self._data["devices"][key]["active_saved"] = device["active_saved"]
            self._dirty = False
            self._heartbeat_dirty = False
            return True
        except (OSError, ValueError):
            # Brak zapisu nie moze wywrocic monitora - stracimy tylko dokladnosc
            # szacowania po restarcie.
            try:
                os.remove(temporary)
            except OSError:
                pass
            self._dirty = True
            return False

    @staticmethod
    def _needs_format_rewrite(previous):
        """Czy plik na dysku wymaga natychmiastowego zapisu po migracji formatu."""
        if not isinstance(previous, dict):
            return False
        if previous.get("version") != FORMAT_VERSION:
            return True
        devices = previous.get("devices")
        if not isinstance(devices, dict):
            return True
        for device in devices.values():
            if not isinstance(device, dict):
                continue
            if not isinstance(device.get("active"), dict):
                return True
            if not isinstance(device.get("measured_drop"), dict):
                return True
        return False


def _entries(value):
    return value if isinstance(value, list) else []


def _sanitize_rise(value, samples, last_seen, active):
    """Sprawdza zapisany punkt odniesienia dla potencjalnego ladowania."""
    if not isinstance(value, dict) or not samples or samples[-1][2]:
        return None
    link, level, moment = value.get("link"), value.get("level"), value.get("last_seen")
    end = last_seen if last_seen is not None else samples[-1][0]
    if (not _valid_link(link) or link != samples[-1][3]
            or not _finite_number(level) or not 0 <= level <= samples[-1][1]
            or not _finite_number(moment) or not 0 <= moment <= end):
        return None
    counters = {}
    for name in ("active", "measured_drop"):
        raw = value.get(name)
        if not isinstance(raw, dict):
            return None
        if any(not _valid_link(key) or not _finite_number(amount)
               or (amount < 0 if name == "active" else abs(amount) > 100)
               for key, amount in raw.items()):
            return None
        counters[name] = dict(raw)
        if not math.isfinite(sum(raw.values())):
            return None
        if name == "active" and any(amount > active.get(key, 0) for key, amount in raw.items()):
            return None
    return {"link": link, "level": int(level), "last_seen": float(moment), **counters}


def _sample_drop_totals(samples):
    """Odzyskuje netto spadek ze starszych probek, bez skokow miedzy laczami."""
    totals = {}
    for previous, current in zip(samples, samples[1:]):
        if previous.link == current.link:
            totals[previous.link] = totals.get(previous.link, 0) + previous.level - current.level
    return totals


def _battery_identity(key):
    """Zwraca (klucz baterii, lacze) dla klucza wczytanego z pliku.

    Klucz katalogowy zostaje bez zmian. Stare pliki kluczowane nazwa modelu
    tlumaczymy na klucz katalogowy, zapamietujac przy okazji lacze, ktorego ta
    nazwa dotyczyla - inaczej po aktualizacji cykle zmierzone na Bluetooth
    weszlyby do szacunku dla dongle'a.
    """
    if device_catalog.by_key(key) is not None:
        return key, DEFAULT_LINK

    battery_key, link = device_catalog.resolve_battery(key)
    if battery_key is None:
        # Sprzet spoza katalogu (np. nieznana nazwa z Bluetooth). Nie ma go
        # z czym scalic, wiec zostaje przy swojej nazwie jako kluczu.
        return key, DEFAULT_LINK

    return battery_key, link


def _merge_devices(first, second):
    """Scala dwa stare wpisy tej samej baterii - po jednym na lacze.

    Cykle zabieramy z obu, bo kazdy jest zamknietym pomiarem swojego lacza.
    Z sesji zostaje jedna: ta z zaobserwowanym odlaczeniem kabla, o ile
    swiezsze odczyty z drugiego wpisu do niej pasuja (sa pozniejsze i nie
    pokazuja naladowanej baterii). Tak wlasnie wygladaja te dane w praktyce -
    gdy mysz nie odpowie na dongle'u, ten sam poziom przychodzi po Bluetooth,
    wiec oba wpisy opisuja jedna sesje rozdzielona tylko przez klucz w pliku.

    Probek i czasu wlaczenia z odrzuconego wpisu nie doklejamy: bez znajomosci
    granic sesji nie ma pewnosci, czy nie pochodza sprzed ladowania. Licznik
    startuje wiec od tego, co wiadomo na pewno, i rosnie dalej normalnie.
    """
    newer, older = (
        (first, second) if _last_moment(first) >= _last_moment(second) else (second, first)
    )

    continuing = _continues_session(older, newer)
    merged = dict(older if continuing else newer)
    if continuing:
        # Znany poczatek sesji zachowujemy, lecz poziom i ostatni kontakt
        # musza pochodzic z najnowszego odczytu, takze po drugim laczu.
        boundary = _last_moment(older)
        samples = list(older["samples"])
        samples.extend(entry for entry in newer["samples"] if entry[0] >= boundary)
        samples.sort(key=lambda entry: entry[0])
        if len(samples) > MAX_SAMPLES:
            samples = [samples[0]] + samples[-(MAX_SAMPLES - 1):]
        merged["samples"] = samples
        merged["last_seen"] = _last_moment(newer)

    cycles = [list(entry) for entry in dict.fromkeys(tuple(entry) for entry in older["cycles"] + newer["cycles"])]
    cycles.sort(key=lambda entry: entry[1])
    merged["cycles"] = cycles[-MAX_CYCLES:]
    return merged


def _continues_session(older, newer):
    """Czy swiezsze odczyty naleza jeszcze do sesji zapisanej w starszym wpisie."""
    if not older.get("observed_start") or older.get("charge_end") is None:
        return False

    samples = newer.get("samples") or []
    if not samples:
        return True
    if samples[-1][2]:
        return False
    if newer.get("observed_start") and newer.get("charge_end") != older["charge_end"]:
        return False

    if samples[-1][0] < float(older["charge_end"]):
        # Caly swiezszy wpis jest sprzed ostatniego ladowania - nie ma czego ciagnac.
        return False

    known = older.get("samples") or []
    if not known:
        return True

    # Poziom wyzszy niz ostatnio widziany znaczy, ze miedzy wpisami bylo
    # ladowanie; roznica w granicach miarki lacza jeszcze o niczym nie swiadczy.
    return samples[-1][1] <= known[-1][1] + LINK_SWITCH_TOLERANCE


def _last_moment(device):
    """Ostatni znany moment kontaktu z urzadzeniem, do porownania dwoch wpisow."""
    last_seen = device.get("last_seen")
    if isinstance(last_seen, (int, float)):
        return float(last_seen)
    samples = device.get("samples") or []
    return float(samples[-1][0]) if samples else 0.0


def _legacy_active_seconds(samples, charge_end, observed_start):
    """Przybliza czas wlaczenia ze starych danych, ktore znaly tylko zegar scienny.

    Stary format nie mial heartbeatu, wiec jedyne co da sie odzyskac to rozpietosc
    znacznikow probek (albo od charge_end do ostatniej). To nadal moze zawierac
    krotkie snny sprzetu miedzy zmianami poziomu - od migracji licznik rosnie juz
    wylacznie odcinkami miedzy udanymi odczytami (MAX_ONLINE_GAP).
    """
    if not samples:
        return 0.0
    if observed_start and charge_end is not None:
        return max(0.0, samples[-1][0] - float(charge_end))
    return max(0.0, samples[-1][0] - samples[0][0])


_default_timeline = None


def default_timeline():
    """Wspolna os czasu dla calego procesu, tworzona przy pierwszym uzyciu."""
    global _default_timeline
    if _default_timeline is None:
        _default_timeline = Timeline()
    return _default_timeline


if __name__ == "__main__":
    # Szybki wglad w zebrane dane: python battery_timeline.py
    timeline = default_timeline()
    names = timeline.devices()
    if not names:
        print("Os czasu jest pusta - monitor nie zapisal jeszcze zadnych odczytow.")

    for name in names:
        state = timeline.state(name)
        print(f"\n{name}")
        print(f"  poziom teraz:      {state.level}%" + (" (ladowanie)" if state.charging else ""))
        print(f"  probek w sesji:    {len(state.samples)}")
        print(f"  czas wlaczenia:    {state.active_seconds / 3600.0:.1f} h")

        for link, seconds in sorted(state.active_by_link.items()):
            display = device_catalog.LINK_NAMES.get(link, link)
            print(f"    {display:<10} {seconds / 3600.0:.1f} h")

        since = state.hours_since_charge
        print(f"  od naladowania:    {since:.1f} h" if since is not None else
              "  od naladowania:    nieznane (nie widzielismy odlaczenia kabla)")

        if not state.cycles:
            print("  zamkniete cykle:   brak")
        for cycle in state.cycles:
            display = device_catalog.LINK_NAMES.get(cycle.link, cycle.link)
            print(
                f"  cykl: {cycle.start_level}% -> {cycle.end_level}% "
                f"w {cycle.hours:.1f} h ({cycle.rate:.2f} pkt/h, {display})"
            )
