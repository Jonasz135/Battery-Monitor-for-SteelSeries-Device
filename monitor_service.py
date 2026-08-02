"""Serwis monitorujacy baterie i zasilajacy ekran OLED, gotowy do pracy w watku.

Cala logika, ktora wczesniej siedziala w blokujacej petli skryptu konsolowego,
jest tutaj opakowana w klase dajaca sie uruchomic i zatrzymac z zewnatrz.
Dzieki temu ten sam kod obsluguje interfejs okienkowy (app.py) i tryb
konsolowy (battery_oled.py).

Trzy zasady, na ktorych opiera sie wspolpraca z GUI:

  - Pauzy sa przerywalne. Zamiast time.sleep uzywamy Event.wait w krotkich
    odcinkach, wiec zamkniecie okna konczy prace natychmiast, a nie po
    doczekaniu do konca 4-sekundowego cyklu.
  - Stan udostepniamy jako migawke (get_snapshot), ktora interfejs odpytuje
    w swoim tempie. Klatki powstaja 10 razy na sekunde i przepychanie kazdej
    z nich przez kolejke tylko po to, by narysowac najnowsza, byloby marnotrawstwem.
  - Komunikaty ida przez wywolanie zwrotne, wiec konsola je wypisuje,
    a okno wrzuca do panelu logu.

Serwis nie sklada gotowych napisow dla interfejsu. Stan wychodzi z niego jako
klucz tlumaczenia plus parametry ("service.connected" + adres), a nie jako
"Polaczono (127.0.0.1:1234)". Napis powstaje dopiero w oknie, w chwili
rysowania - dzieki temu przelaczenie jezyka odswieza tez stopke i nie zostawia
w niej zdania w poprzednim jezyku do najblizszej zmiany stanu.
"""

import json
import os
import threading
import time

import requests

import battery_bluetooth
import battery_hid
import battery_timeline
import device_catalog
import i18n
import oled_render
import runtime_estimator
import settings as app_settings

# ---------------------------------------------------------------------------
# 1. KONFIGURACJA
# ---------------------------------------------------------------------------

# Nazwy "game" i "event" moga zawierac wylacznie wielkie A-Z, cyfry 0-9,
# myslnik i podkreslnik - to wymog GameSense SDK.
GAME = "BATTERY_MONITOR"
EVENT = "BATTERY"

GAME_DISPLAY_NAME = "Battery Monitor"
DEVELOPER = "Local"

# Jak czesto naprawde odpytujemy urzadzenia o poziom baterii.
BATTERY_POLL_SECONDS = 60

# Jak czesto wysylamy zdarzenie na ekran, przelaczajac sie na kolejne urzadzenie.
# Krotszy niz 15 s timeout GameSense, wiec kazda wysylka dziala tez jak keepalive.
SCREEN_ROTATE_SECONDS = 4

# Ile czekac przed ponowna proba, gdy GG jest wylaczone lub nie odpowiada.
RECONNECT_DELAY_SECONDS = 10

# Liczba klatek na sekunde animacji paska podczas ladowania. Kazda klatka to
# jedno zdarzenie z 640 bajtami obrazu, wiec nie ma sensu iść wyżej. Wzor
# przesuwa sie o jeden piksel na klatke, czyli ta wartosc jest wprost predkoscia
# animacji w pikselach na sekunde.
ANIMATION_FPS = 10

# Ikona pokazywana obok zdarzenia w interfejsie GG. W SDK nie ma ikony baterii,
# 16 to "Lightning" - najblizsze znaczeniowo. Swiadomie NIE uzywamy "icon-id"
# w samej klatce ekranu, bo ikona zajmuje 32 z 128 pikseli szerokosci i
# zepsulaby uklad "nazwa + procent" oraz pasek postepu.
EVENT_ICON_ID = 16

# Rozdzielczosc ekranu Apex Pro / Apex Pro TKL / Apex 7. To wartosc klucza
# "device-type"; strefa ("zone") dla wszystkich urzadzen z ekranem to "one".
SCREEN_DEVICE_TYPE = "screened-128x40"
SCREEN_ZONE = "one"

HTTP_TIMEOUT_SECONDS = 3

# Najdluzszy odcinek, na jaki zasypiamy jednorazowo. Krotki, bo od niego zalezy,
# jak szybko serwis reaguje na zatrzymanie i na zadanie odswiezenia.
_TICK_SECONDS = 0.05


def no_data_label():
    """Napis na ekran, gdy zadne urzadzenie nie zwraca danych (wszystkie wylaczone).

    Funkcja, a nie stala, bo jezyk mozna zmienic w trakcie dzialania programu.
    Stala zapamietalaby napis z chwili importu modulu.
    """
    return i18n.t("oled.no_data")


# ---------------------------------------------------------------------------
# 2. ODNAJDYWANIE SERWERA GAMESENSE
# ---------------------------------------------------------------------------


class GameSenseUnavailable(RuntimeError):
    """SteelSeries GG nie jest uruchomione albo nie da sie go zlokalizowac.

    Niesie klucz tlumaczenia i parametry, nie gotowy napis. Wyjatek moze
    poczekac w stopce okna kilka minut, a przez ten czas uzytkownik moze
    przelaczyc jezyk - napis musi wiec powstawac przy wyswietlaniu, nie
    przy podnoszeniu wyjatku.
    """

    def __init__(self, key, **params):
        super().__init__(key)
        self.key = key
        self.params = params

    def __str__(self):
        return i18n.t(self.key, **self.params)


def find_server_address():
    """Zwraca adres serwera GameSense w formacie "host:port".

    SteelSeries Engine przy starcie tworzy plik coreProps.json z adresem, na
    ktorym nasluchuje. Brak tego pliku oznacza, ze GG nie dziala i nie ma sensu
    wysylac zdarzen.

    Komunikaty bledow trafiaja wprost do stopki okna, dlatego mowia, co zrobic,
    a nie tylko co sie nie udalo. Sam odczyt baterii idzie przez HID i nie ma
    z GameSense nic wspolnego - bez GG traci sie wylacznie obraz na klawiaturze.
    """
    program_data = os.environ.get("PROGRAMDATA")
    if not program_data:
        raise GameSenseUnavailable("service.no_programdata")

    core_props_path = os.path.join(
        program_data, "SteelSeries", "SteelSeries Engine 3", "coreProps.json"
    )

    if not os.path.isfile(core_props_path):
        raise GameSenseUnavailable("service.gg_missing")

    try:
        with open(core_props_path, "r", encoding="utf-8") as handle:
            core_props = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise GameSenseUnavailable("service.coreprops_unreadable", error=exc) from exc

    # Plik zawiera tez "encryptedAddress" i "ggEncryptedAddress" - to kanaly
    # szyfrowane, ktorych zwykle JSON API nie uzywa. Nas interesuje "address".
    address = core_props.get("address")
    if not address:
        raise GameSenseUnavailable("service.coreprops_no_address")

    return address


# ---------------------------------------------------------------------------
# 3. KLIENT API GAMESENSE
# ---------------------------------------------------------------------------


class GameSenseClient:
    """Cienka warstwa nad endpointami HTTP serwera GameSense."""

    def __init__(self, address):
        self.address = address
        self.base_url = f"http://{address}"
        self.session = requests.Session()

    def _post(self, endpoint, payload):
        """Wysyla payload jako JSON i podnosi wyjatek przy odpowiedzi != 2xx."""
        response = self.session.post(
            f"{self.base_url}{endpoint}",
            json=payload,
            timeout=HTTP_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return response

    def register_metadata(self):
        """Rejestruje aplikacje, by w GG pokazywala sie pod przyjazna nazwa.

        "deinitialize_timer_length_ms" podnosi domyslny 15-sekundowy timeout do
        maksymalnych 60 s - to zabezpieczenie na wypadek, gdyby rotacja ekranu
        na chwile stanela.
        """
        self._post(
            "/game_metadata",
            {
                "game": GAME,
                "game_display_name": GAME_DISPLAY_NAME,
                "developer": DEVELOPER,
                "deinitialize_timer_length_ms": 60000,
            },
        )

    def bind_screen_handler(self):
        """Konfiguruje ekran OLED jako obraz rysowany przez nas.

        Uzywamy trybu bitmapowego, a nie tekstowych linii, bo API JSON nie
        pozwala umiescic tekstu w konkretnym miejscu ekranu - a czas pracy ma
        byc w prawym gornym rogu. Przy "has-text": false SDK oczekuje surowych
        pikseli w "image-data"; tutaj podajemy pusta klatke jako domyslna,
        a prawdziwe klatki lecą w danych kazdego zdarzenia.

        "value_optional": true jest konieczne, bo tresc bierze sie z danych
        kontekstowych, a nie z "value" - bez tego Engine cache'owalby wartosc
        i pomijal odswiezenie, gdy procent sie nie zmienil.
        """
        self._post(
            "/bind_game_event",
            {
                "game": GAME,
                "event": EVENT,
                "min_value": 0,
                "max_value": 100,
                "icon_id": EVENT_ICON_ID,
                "value_optional": True,
                "handlers": [
                    {
                        "device-type": SCREEN_DEVICE_TYPE,
                        "zone": SCREEN_ZONE,
                        "mode": "screen",
                        "datas": [
                            {
                                "has-text": False,
                                "image-data": [0] * oled_render.FRAME_BYTES,
                            }
                        ],
                    }
                ],
            },
        )

    def send_frame(self, image_data, percent):
        """Wysyla gotowa klatke bitmapy na ekran.

        Klucz "image-data-128x40" w danych kontekstowych nadpisuje domyslny
        obraz z bindowania - tak SDK pozwala animowac ekran z aplikacji.
        """
        self._post(
            "/game_event",
            {
                "game": GAME,
                "event": EVENT,
                "data": {
                    "value": percent,
                    "frame": {"image-data-128x40": image_data},
                },
            },
        )


# ---------------------------------------------------------------------------
# 4. ODCZYT BATERII
# ---------------------------------------------------------------------------


def _offline_entries(readings, declared, log):
    """Wpisy dla zadeklarowanych modeli, ktore sie nie zglosily.

    Powstaja tylko w trybie deklaracji, bo tylko wtedy wiemy, ze uzytkownik dane
    urzadzenie ma. W trybie automatycznym braku odpowiedzi nie da sie odroznic od
    braku urzadzenia i wymyslanie kart byloby zgadywaniem.
    """
    if not declared:
        return []

    seen = {reading["key"] for reading in readings if reading.get("key")}
    entries = []

    for key in declared:
        if key in seen:
            continue

        device = device_catalog.by_key(key)
        # Klawiatury przewodowe sa na liscie jako ekran, na ktorym wyswietlamy -
        # nie maja baterii, wiec nie maja czego pokazywac na wlasnej karcie.
        if device is None or not device.wireless:
            continue

        entries.append(
            {
                "name": device.name,
                "model": device.name,
                "key": device.key,
                "label": device.label,
                "rated_hours": None,
                "percent": None,
                "charging": False,
                "online": False,
                "supported": device.battery_supported,
                "runtime": "",
                "runtime_source": None,
                "rate_per_hour": None,
                "per_point_hours": None,
                "since_charge": "",
                "drop_since_charge": None,
            }
        )
        log(
            i18n.t(
                "log.device_off" if device.battery_supported else "log.device_unsupported",
                name=device.name,
            )
        )

    return entries


def read_all_batteries(log=None, timeline=None, declared=()):
    """Zbiera odczyty z obu mozliwych drog: HID (dongle albo kabel) oraz Bluetooth.

    To samo urzadzenie moze byc widoczne jednoczesnie na oba sposoby - mysz
    sparowana po Bluetooth i podlaczona przez dongle zglasza sie dwa razy.
    Odczyt przez HID ma wtedy pierwszenstwo, bo to aktywne lacze i tylko on
    podaje informacje o ladowaniu.

    "declared" to klucze modeli zadeklarowanych przez uzytkownika. Pusta lista
    znaczy tryb automatyczny: bierzemy wszystko, co odpowie. Gdy lista jest
    niepusta, odpytujemy tylko te modele, a kazdy z nich zawsze trafia do wyniku -
    wylaczony jako wpis z "online": False. Bez tego nie dalo by sie odroznic
    urzadzenia wylaczonego od takiego, ktorego uzytkownik nie ma.
    """
    log = log or (lambda message: None)

    by_model = {}
    used_labels = set()

    for device in battery_hid.discover_devices():
        if declared and device["key"] not in declared:
            continue

        result = battery_hid.read_battery(device["pid"])
        if result is None:
            log(i18n.t("log.device_no_data", name=device["name"]))
            continue

        percent, charging = result
        # Pasek postepu wymaga liczby calkowitej z zakresu 0-100.
        percent = max(0, min(100, int(percent)))

        # Jedna mysz moze odpowiedziec na dwoch PID-ach naraz: kablowym
        # i bezprzewodowym. Skoro oba wpisy dziela teraz jedna os czasu,
        # dwa sprzeczne odczyty w jednym cyklu odpytywania wygladalyby jak
        # migotanie ladowania. Pierwszenstwo ma ten, ktory zglasza ladowanie,
        # bo tylko wpis kablowy wie o nim prawde.
        existing = by_model.get(device["model"])
        if existing is not None and not (charging and not existing["charging"]):
            log(i18n.t("log.device_duplicate", name=device["name"], percent=percent))
            continue

        by_model[device["model"]] = {
            "name": device["name"],
            "model": device["model"],
            "key": device["key"],
            "label": device["label"],
            "rated_hours": device["rated_hours"],
            "percent": percent,
            "charging": charging,
            "online": True,
            "supported": True,
        }
        used_labels.add(device["label"])
        log(
            i18n.t("log.device_value", name=device["name"], percent=percent)
            + (i18n.t("log.device_charging_suffix") if charging else "")
        )

    readings = list(by_model.values())

    for device in battery_bluetooth.read_all():
        if declared and device["key"] not in declared:
            continue
        if device["label"] in used_labels:
            log(
                i18n.t(
                    "log.device_bt_duplicate",
                    name=device["name"],
                    percent=device["percent"],
                )
            )
            continue
        device["online"] = True
        device["supported"] = True
        readings.append(device)
        used_labels.add(device["label"])
        log(i18n.t("log.device_value", name=device["name"], percent=device["percent"]))

    readings.extend(_offline_entries(readings, declared, log))

    # Dopiero teraz, gdy znamy pelna liste, nanosimy odczyty na os czasu
    # i liczymy szacunki.
    timeline = timeline or battery_timeline.default_timeline()

    for reading in readings:
        if not reading["online"]:
            continue

        # Historie prowadzimy per bateria, a nie per lacze: Aerox na kablu,
        # na dongle'u i po Bluetooth to jedna bateria, wiec musza dzielic os
        # czasu. Inaczej przejscie z ladowania na prace wypadaloby miedzy dwie
        # osobne historie, a przelaczenie myszy na Bluetooth kasowaloby licznik
        # czasu wlaczenia i zuzytych procent. Lacze idzie osobnym parametrem -
        # tempo i cykle sa liczone dla kazdego z nich z osobna.
        state = timeline.record(
            reading["key"] or reading["model"],
            reading["percent"],
            reading["charging"],
            link=reading.get("link") or battery_timeline.DEFAULT_LINK,
        )
        result = runtime_estimator.estimate(state, reading["rated_hours"])

        reading["runtime"] = (
            runtime_estimator.charging_text()
            if reading["charging"]
            else runtime_estimator.format_hours(result.hours, result.source)
        )
        reading["runtime_source"] = result.source
        reading["rate_per_hour"] = result.rate

        # Sredni czas na jeden punkt procentowy. Trzymamy go jako liczbe godzin,
        # a nie gotowy napis: karta urzadzenia formatuje go w swoim jezyku, wiec
        # przelaczenie jezyka nie musi czekac na kolejny odczyt baterii.
        reading["per_point_hours"] = runtime_estimator.hours_per_point(result.rate)

        # Czas pracy od ostatniego naladowania i ubytek procentow znamy tylko
        # wtedy, gdy sami widzielismy moment odlaczenia kabla. Oba pola sa
        # puste dokladnie w tych samych sytuacjach, bo opisuja te sama sesje.
        reading["since_charge"] = runtime_estimator.format_elapsed(state.hours_since_charge)
        reading["drop_since_charge"] = state.drop_since_charge

        if result.hours is not None:
            log(
                i18n.t(
                    "log.runtime",
                    runtime=reading["runtime"],
                    source=result.source,
                    rate=f"{result.rate:.2f}",
                )
            )
            log(
                i18n.t(
                    "log.per_point",
                    duration=runtime_estimator.format_per_point(
                        reading["per_point_hours"], result.source
                    ),
                )
            )
        if reading["since_charge"]:
            used = reading["drop_since_charge"]
            log(
                i18n.t(
                    "log.since_charge",
                    elapsed=reading["since_charge"],
                    used=i18n.t("log.used_suffix", used=used) if used else "",
                )
            )

    return readings


# ---------------------------------------------------------------------------
# 5. SERWIS
# ---------------------------------------------------------------------------


class MonitorService:
    """Odczytuje baterie i rysuje ekran OLED we wlasnym watku.

    Interfejs dla wolajacego:
      start()         - uruchamia watek roboczy,
      stop()          - zatrzymuje go i czeka na zakonczenie,
      refresh_now()   - wymusza natychmiastowy odczyt baterii,
      set_paused()    - wstrzymuje wysylanie na OLED, nie przerywajac odczytow,
      get_snapshot()  - zwraca biezacy stan do wyswietlenia.
    """

    def __init__(self, on_log=None):
        self._on_log = on_log or (lambda message: None)

        # Jedna instancja na serwis: trzyma historie w pamieci i schodzi na dysk
        # tylko wtedy, gdy poziom baterii faktycznie sie zmienil.
        self._timeline = battery_timeline.Timeline()

        self._settings = app_settings.Settings()
        self._declared = self._settings.declared

        self._stop = threading.Event()
        self._refresh = threading.Event()
        self._paused = threading.Event()
        self._thread = None

        self._lock = threading.Lock()
        self._readings = []
        self._last_poll_monotonic = 0.0
        self._snapshot = {
            "connected": False,
            "address": None,
            # Klucz tlumaczenia i jego parametry, nie gotowe zdanie - napis
            # sklada okno przy rysowaniu stopki.
            "status_key": "service.not_started",
            "status_params": {},
            "readings": [],
            "last_poll": None,
            "frame": None,
            "frame_label": "",
            "paused": False,
            "running": False,
        }

    # -- sterowanie ---------------------------------------------------------

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="MonitorService", daemon=True)
        self._set_status("service.starting", running=True)
        self._thread.start()

    def stop(self, timeout=5.0):
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
        self._thread = None
        self._set_status("service.stopped", running=False, connected=False)

    def refresh_now(self):
        self._refresh.set()

    def reload_settings(self):
        """Wczytuje ustawienia od nowa i natychmiast odswieza odczyty.

        Stara liste odczytow porzucamy, bo po zmianie deklaracji karty urzadzen,
        ktorych uzytkownik juz nie zaznaczyl, musza zniknac od razu, a nie dopiero
        przy nastepnym odczycie.
        """
        self._settings = app_settings.Settings()
        self._declared = self._settings.declared
        with self._lock:
            self._readings = []
            self._snapshot["readings"] = []
        self._refresh.set()

    def declared_devices(self):
        return self._declared

    def set_paused(self, paused):
        if paused:
            self._paused.set()
        else:
            self._paused.clear()
        self._update(paused=bool(paused))

    def is_paused(self):
        return self._paused.is_set()

    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    # -- stan ---------------------------------------------------------------

    def get_snapshot(self):
        """Zwraca kopie biezacego stanu - bezpieczna do czytania z innego watku."""
        with self._lock:
            return dict(self._snapshot)

    def _update(self, **fields):
        with self._lock:
            self._snapshot.update(fields)

    def _set_status(self, key, params=None, **fields):
        """Zapisuje stan jako klucz tlumaczenia, ewentualnie z parametrami."""
        self._update(status_key=key, status_params=params or {}, **fields)

    def _log(self, message):
        try:
            self._on_log(message)
        except Exception:
            # Blad w odbiorcy komunikatow nie moze zatrzymac monitora.
            pass

    # -- pauzy przerywalne --------------------------------------------------

    def _sleep(self, seconds):
        """Przerywalna pauza. Zwraca False, gdy zazadano zatrzymania.

        Czekanie w krotkich odcinkach zamiast jednego time.sleep sprawia, ze
        zatrzymanie i przycisk odswiezenia dzialaja od razu, a nie po doczekaniu
        do konca pelnego cyklu ekranu.
        """
        if seconds <= 0:
            return not self._stop.is_set()

        deadline = time.monotonic() + seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            if self._stop.wait(min(remaining, _TICK_SECONDS)):
                return False
            if self._refresh.is_set():
                return True

    # -- odczyt -------------------------------------------------------------

    def _poll_if_due(self):
        """Odczytuje baterie, gdy minal interwal albo gdy ktos zazadal odswiezenia.

        Przy pustej liscie odczytow ponawiamy probe za kazdym razem, zeby
        wlaczenie uspionej myszki bylo widoczne w ciagu sekund, a nie po minucie.
        """
        forced = self._refresh.is_set()
        if forced:
            self._refresh.clear()

        now = time.monotonic()
        due = now - self._last_poll_monotonic >= BATTERY_POLL_SECONDS

        if not (forced or due or not self._readings):
            return False

        self._log(i18n.t("log.poll"))
        self._readings = read_all_batteries(self._log, self._timeline, self._declared)
        self._last_poll_monotonic = time.monotonic()
        self._update(readings=list(self._readings), last_poll=time.time())
        return True

    # -- rysowanie ----------------------------------------------------------

    def _publish(self, frame, label):
        self._update(frame=frame, frame_label=label)

    def _send(self, client, frame, percent):
        """Wysyla klatke, chyba ze monitor jest wstrzymany.

        W pauzie klatki nadal powstaja i trafiaja do podgladu w oknie - wstrzymane
        jest tylko przejmowanie ekranu klawiatury.
        """
        if not self._paused.is_set():
            client.send_frame(frame, percent)

    def _show_static(self, client, label, percent, runtime_text, seconds, used_text=""):
        frame = oled_render.render_frame(label, percent, runtime_text, used_text=used_text)
        self._publish(frame, label)
        self._send(client, frame, percent or 0)
        return self._sleep(seconds)

    def _show_animated(self, client, label, percent, runtime_text, seconds, first_step):
        """Animuje pasek i zwraca numer nastepnej klatki.

        Numer klatki wchodzi i wychodzi, zeby wzor plynnie przechodzil miedzy
        kolejnymi cyklami wyswietlania tego samego urzadzenia, a nie skakal
        do poczatku przy kazdym obrocie petli.
        """
        frame_delay = 1.0 / ANIMATION_FPS
        frame_count = max(1, int(seconds * ANIMATION_FPS))

        # Klatki planujemy na sztywnej osi czasu. Zwykla pauza po kazdym wyslaniu
        # dodawalaby do odstepu czas odpowiedzi Engine, wiec tempo animacji
        # falowaloby razem z obciazeniem maszyny.
        start = time.monotonic()

        for offset in range(frame_count):
            step = first_step + offset
            frame = oled_render.render_frame(
                label, percent, runtime_text, charging=True, step=step
            )
            self._publish(frame, label)
            self._send(client, frame, percent)

            remaining = start + (offset + 1) * frame_delay - time.monotonic()
            if not self._sleep(remaining):
                return first_step + offset + 1
            if self._refresh.is_set():
                return first_step + offset + 1

        return first_step + frame_count

    # -- petle --------------------------------------------------------------

    def _online_readings(self):
        """Odczyty, ktore maja co pokazac na ekranie klawiatury.

        Zadeklarowane, ale wylaczone urzadzenia dostaja karte w oknie - tam ta
        informacja jest przydatna. Na ekranie zabieralyby tylko miejsce w rotacji,
        zaslaniajac urzadzenia, ktore faktycznie pracuja.
        """
        return [reading for reading in self._readings if reading["online"]]

    def _display_loop(self, client):
        """Rotuje urzadzenia na ekranie do zatrzymania albo utraty lacznosci z GG."""
        index = 0
        animation_step = 0

        while not self._stop.is_set():
            if self._poll_if_due():
                index = 0

            readings = self._online_readings()
            if not readings:
                # Wysylamy mimo wszystko, by ekran nie zgasl i by bylo widac,
                # ze monitor dziala.
                if not self._show_static(
                    client, no_data_label(), None, "", SCREEN_ROTATE_SECONDS
                ):
                    return
                continue

            current = readings[index % len(readings)]
            index += 1

            # Renderer sam sklada procent i znak ladowania, wiec etykieta to tylko nazwa.
            # Etykieta w odczycie jest nietlumaczonym tokenem - na ekran idzie
            # przez i18n, zeby po angielsku byl "MOUSE", a nie "MYSZ".
            label = i18n.oled_label(current["label"])
            percent = current["percent"]
            runtime_text = current.get("runtime", "")

            if current["charging"]:
                # W trakcie ladowania licznika ubytku nie pokazujemy: opisuje
                # zakonczona sesje, a poziom wlasnie rosnie.
                animation_step = self._show_animated(
                    client, label, percent, runtime_text, SCREEN_ROTATE_SECONDS, animation_step
                )
            elif not self._show_static(
                client,
                label,
                percent,
                runtime_text,
                SCREEN_ROTATE_SECONDS,
                runtime_estimator.format_drop(current.get("drop_since_charge")),
            ):
                return

    def _idle_loop(self, seconds):
        """Czeka na ponowna probe polaczenia, ale nie przestaje odczytywac baterii.

        Bez tego okno byloby zupelnie puste, dopoki SteelSeries GG nie wystartuje,
        a poziom baterii da sie odczytac niezaleznie od niego.
        """
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not self._stop.is_set():
            self._poll_if_due()
            self._publish_preview_only()
            if not self._sleep(1.0):
                return

    def _publish_preview_only(self):
        """Rysuje klatke do podgladu w oknie, nie wysylajac jej nigdzie."""
        readings = self._online_readings()
        if readings:
            current = readings[0]
            label = i18n.oled_label(current["label"])
            frame = oled_render.render_frame(
                label,
                current["percent"],
                current.get("runtime", ""),
                charging=current["charging"],
                used_text=(
                    ""
                    if current["charging"]
                    else runtime_estimator.format_drop(current.get("drop_since_charge"))
                ),
            )
            self._publish(frame, label)
        else:
            label = no_data_label()
            self._publish(oled_render.render_frame(label, None, ""), label)

    def _run(self):
        """Zewnetrzna petla: odnajduje serwer, rejestruje aplikacje, wznawia po bledzie."""
        while not self._stop.is_set():
            try:
                address = find_server_address()
                client = GameSenseClient(address)
                client.register_metadata()
                client.bind_screen_handler()

                self._set_status(
                    "service.connected",
                    {"address": address},
                    connected=True,
                    address=address,
                )
                self._log(i18n.t("log.connected", address=address))

                # Wraca tylko przez wyjatek albo zatrzymanie - kazdy blad lacznosci
                # oznacza, ze trzeba przejsc rejestracje od nowa, bo GG moglo zostac
                # zrestartowane i nasluchuje juz na innym porcie.
                self._display_loop(client)

            except GameSenseUnavailable as exc:
                self._set_status(exc.key, exc.params, connected=False, address=None)
                self._log(
                    i18n.t("log.retry", message=exc, seconds=RECONNECT_DELAY_SECONDS)
                )
                self._idle_loop(RECONNECT_DELAY_SECONDS)

            except requests.RequestException as exc:
                self._set_status("service.link_error", connected=False, address=None)
                self._log(i18n.t("log.gg_error", error=exc))
                self._idle_loop(RECONNECT_DELAY_SECONDS)

            except Exception as exc:  # ostatnia linia obrony - serwis ma nie umierac
                self._set_status(
                    "service.unexpected_error",
                    {"error": exc},
                    connected=False,
                    address=None,
                )
                self._log(i18n.t("log.unexpected", error=repr(exc)))
                self._idle_loop(RECONNECT_DELAY_SECONDS)

        self._set_status("service.stopped", running=False, connected=False)
