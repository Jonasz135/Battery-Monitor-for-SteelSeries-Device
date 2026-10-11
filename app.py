"""Okno monitora baterii SteelSeries.

Uruchomienie:
    BatteryMonitor.exe          - wersja spakowana, do codziennego uzytku
    BatteryMonitor.exe --tray   - start od razu do zasobnika, bez okna
    python app.py               - ze zrodel, z konsola (widac bledy startu)
    pythonw app.py              - ze zrodel, bez okna konsoli

Cala praca dzieje sie w monitor_service, w osobnym watku. Okno tylko odpytuje
jego migawke stanu i rysuje. Trzy zasady, ktore trzeba tu utrzymac:

  - Watek serwisu nigdy nie dotyka widgetow. Tkinter nie jest bezpieczny
    watkowo, wiec komunikaty ida przez kolejke, a stan przez migawke; jedno
    i drugie odczytuje cykliczne odswiezanie w watku glownym.
  - Akcje z menu zasobnika wracaja przez kolejke odczytywana w glownym watku.
    Takze after() jest wywolaniem Tk i nie wolno go wolac z watku pystray.
  - Zamkniecie okna zwija program do zasobnika, a nie konczy go. Ekran
    klawiatury gasnie kilkanascie sekund po ostatnim zdarzeniu, wiec monitor
    musi dzialac caly czas - wyjscie jest w menu zasobnika.

Jezyk przelacza sie bez restartu. Zaden napis nie jest wiec zapisany w widgecie
"na zawsze": wszystko, co widac, powstaje w retranslate() albo w cyklicznym
odswiezaniu. Widgety z tekstem statycznym trzymamy w self._translatable, zeby
przelacznik nie musial ich szukac po drzewie ani ich pominac.
"""

import argparse
import ctypes
from ctypes import wintypes
import os
import queue
import sys
import threading
import time
import tkinter as tk

import customtkinter as ctk
import pystray
from PIL import Image, ImageOps, ImageTk

import autostart
import device_catalog
import i18n
import monitor_service
import oled_render
import paths
import runtime_estimator
import settings as app_settings
import single_instance

# ---------------------------------------------------------------------------
# WYGLAD
# ---------------------------------------------------------------------------

# Nazwa programu zostaje nieprzetlumaczona w obu jezykach - to nazwa wlasna,
# ta sama w belce okna, w zasobniku i we wpisie autostartu.
APP_TITLE = "Battery Monitor"

WINDOW_WIDTH = 470

# Wysokosc, na jaka rozciagamy okno przed pomiarem zawartosci. Przy zbyt niskim
# oknie grid przycina widgety i wyliczona wysokosc bylaby za mala.
AUTOSIZE_PROBE_HEIGHT = 1000

# Odstep pod ostatnim widgetem - jego wlasne pady jest na zewnatrz pomiaru.
AUTOSIZE_BOTTOM_PAD = 12

# Pomaranczowy w duchu SteelSeries, uzywany jako akcent i kolor ladowania.
ACCENT = "#FF5200"
ACCENT_HOVER = "#D64500"

COLOR_OK = "#3FB950"
COLOR_WARN = "#D9822B"
COLOR_BAD = "#F85149"
COLOR_MUTED = "#8A8A93"
COLOR_TEXT = "#EDEDF0"

APP_BG = "#17171A"
CARD_BG = "#232327"
PANEL_BG = "#1D1D21"

# Progi kolorow paska naladowania.
LEVEL_WARN = 50
LEVEL_LOW = 20

# Kolory podgladu ekranu - prawdziwy OLED Apex Pro swieci bialo, wiec podglad
# tez jest bialy, a nie pomaranczowy jak logo.
OLED_ON = (238, 241, 245)
OLED_OFF = (9, 9, 12)

# Powiekszenie podgladu: 128x40 razy 3 daje 384x120.
PREVIEW_ZOOM = 3

# Co ile milisekund okno odswieza sie z migawki serwisu. 100 ms odpowiada
# 10 klatkom animacji na sekunde, wiec podglad jest tak plynny jak sam OLED.
TICK_MS = 100
TRAY_TICK_MS = 500

# Zasoby czytamy przez paths, bo w wersji spakowanej leza w katalogu, do ktorego
# PyInstaller je rozpakowal, a nie obok pliku programu.
ICON_PATH = paths.resource_path("assets", "icon.ico")
LOGO_PATH = paths.resource_path("assets", "logo.png")

# Identyfikator aplikacji dla Windows. Bez niego pasek zadan grupuje okno pod
# generyczna ikona Pythona zamiast pod nasza.
APP_USER_MODEL_ID = "OLEDbattery.BatteryMonitor"

# Separator czlonow w wierszu podsumowania na karcie urzadzenia.
DETAIL_SEPARATOR = "  ·  "


class _MonitorInfo(ctypes.Structure):
    _fields_ = [
        ("size", wintypes.DWORD),
        ("monitor", wintypes.RECT),
        ("work", wintypes.RECT),
        ("flags", wintypes.DWORD),
    ]


def _monitor_work_area(window):
    """Obszar roboczy aktualnego monitora, w fizycznych pikselach Windows."""
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.MonitorFromWindow.argtypes = (wintypes.HWND, wintypes.DWORD)
        user32.MonitorFromWindow.restype = wintypes.HANDLE
        user32.GetMonitorInfoW.argtypes = (wintypes.HANDLE, ctypes.POINTER(_MonitorInfo))
        user32.GetMonitorInfoW.restype = wintypes.BOOL
        monitor = user32.MonitorFromWindow(window.winfo_id(), 2)
        information = _MonitorInfo()
        information.size = ctypes.sizeof(information)
        if monitor and user32.GetMonitorInfoW(monitor, ctypes.byref(information)):
            area = information.work
            return area.left, area.top, area.right, area.bottom
    except (AttributeError, OSError, tk.TclError):
        pass
    return 0, 0, window.winfo_screenwidth(), window.winfo_screenheight()


def _cancel_pending_callbacks(widget, all_callbacks=False):
    """Tk destroy usuwa polecenia Pythona, ale nie usuwa odpowiadajacych after."""
    try:
        commands = set()
        if not all_callbacks:
            pending_widgets = [widget]
            while pending_widgets:
                owner = pending_widgets.pop()
                commands.update(getattr(owner, "_tclCommands", ()) or ())
                pending_widgets.extend(owner.winfo_children())
        for identifier in widget.tk.call("after", "info"):
            try:
                script = widget.tk.call("after", "info", identifier)[0]
                registered = widget.tk.splitlist(script)
                if all_callbacks or registered and registered[0] in commands:
                    # after_cancel() usuwa tez polecenie Pythona. Moze ono
                    # nalezec do dziecka; jego destroy musialby usunac je drugi
                    # raz. Tutaj anulujemy timer, a polecenie sprzata wlasciciel.
                    widget.tk.call("after", "cancel", identifier)
            except tk.TclError:
                continue
    except (AttributeError, tk.TclError):
        pass


def level_color(percent, charging):
    if charging:
        return ACCENT
    if percent < LEVEL_LOW:
        return COLOR_BAD
    if percent < LEVEL_WARN:
        return COLOR_WARN
    return COLOR_OK


def frame_to_preview(frame, zoom=PREVIEW_ZOOM):
    """Zamienia 640 bajtow klatki OLED na powiekszony, pokolorowany obrazek."""
    mono = oled_render.frame_to_image(frame).convert("L")
    tinted = ImageOps.colorize(mono, black=OLED_OFF, white=OLED_ON)
    return tinted.resize(
        (oled_render.WIDTH * zoom, oled_render.HEIGHT * zoom), Image.NEAREST
    )


# ---------------------------------------------------------------------------
# KARTA URZADZENIA
# ---------------------------------------------------------------------------


class DeviceCard(ctk.CTkFrame):
    """Kafelek jednego urzadzenia: nazwa, procent, czas pracy i pasek."""

    def __init__(self, master, fonts):
        super().__init__(master, fg_color=CARD_BG, corner_radius=10)

        self.grid_columnconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=0)

        self.label = ctk.CTkLabel(
            self, text="", font=fonts["card_label"], text_color=COLOR_TEXT, anchor="w"
        )
        self.label.grid(row=0, column=0, sticky="w", padx=(14, 0), pady=(12, 0))

        self.runtime = ctk.CTkLabel(
            self, text="", font=fonts["card_runtime"], text_color=COLOR_MUTED, anchor="e"
        )
        self.runtime.grid(row=0, column=1, sticky="e", padx=(0, 14), pady=(12, 0))

        self.percent = ctk.CTkLabel(
            self, text="", font=fonts["card_percent"], text_color=COLOR_TEXT, anchor="w"
        )
        self.percent.grid(row=1, column=0, sticky="w", padx=(14, 0))

        self.name = ctk.CTkLabel(
            self, text="", font=fonts["card_name"], text_color=COLOR_MUTED, anchor="e"
        )
        self.name.grid(row=1, column=1, sticky="e", padx=(0, 14))

        self.bar = ctk.CTkProgressBar(
            self, height=8, corner_radius=4, fg_color="#34343A", progress_color=COLOR_OK
        )
        self.bar.grid(row=2, column=0, columnspan=2, sticky="ew", padx=14, pady=(10, 0))

        # Wiersz jest widoczny zawsze, takze gdy czasu nie znamy. Ukrywanie go
        # zmienialoby wysokosc karty w trakcie pracy, a dopasowanie okna liczy
        # sie tylko przy zmianie liczby urzadzen.
        self.since = ctk.CTkLabel(
            self, text="", font=fonts["card_since"], text_color=COLOR_MUTED, anchor="w"
        )
        self.since.grid(row=3, column=0, columnspan=2, sticky="w", padx=14, pady=(8, 12))

    def update_reading(self, reading):
        self.label.configure(text=i18n.window_label(reading["label"]))
        self.name.configure(text=reading.get("name", ""))

        if not reading.get("online", True):
            self._show_offline(reading)
            return

        percent = reading["percent"]
        charging = reading["charging"]

        self.percent.configure(text=f"{percent}%", text_color=COLOR_TEXT)

        if charging:
            self.runtime.configure(text=i18n.t("card.charging"), text_color=ACCENT)
        else:
            runtime = reading.get("runtime")
            # Wyszarzamy to, co nie jest jeszcze twardym pomiarem: brak wyniku
            # oraz szacunek z danych katalogowych, ktory renderer znaczy tylda.
            uncertain = not runtime or runtime.startswith("~")
            self.runtime.configure(
                text=runtime or i18n.t("card.estimating"),
                text_color=COLOR_MUTED if uncertain else COLOR_TEXT,
            )

        self.bar.configure(progress_color=level_color(percent, charging))
        self.bar.set(percent / 100)

        self.since.configure(text=self._since_text(reading))

    def _show_offline(self, reading):
        """Karta zadeklarowanego urzadzenia, ktore sie nie zglosilo.

        Pusty pasek i myslnik zamiast procentu, bo zero wygladaloby jak bateria
        na wyczerpaniu - a o poziomie nie wiemy nic.
        """
        supported = reading.get("supported", True)

        self.percent.configure(text="—", text_color=COLOR_MUTED)
        self.runtime.configure(
            text=i18n.t("card.off") if supported else i18n.t("card.unsupported"),
            text_color=COLOR_MUTED,
        )
        self.bar.configure(progress_color="#34343A")
        self.bar.set(0)
        self.since.configure(
            text=i18n.t("card.offline_hint" if supported else "card.unsupported_hint")
        )

    @staticmethod
    def _since_text(reading):
        """Wiersz podsumowania sesji: czas od naladowania, ubytek, czas na 1%.

        Trzy liczby w jednym wierszu, a nie w trzech: karta ma stala wysokosc
        (patrz komentarz przy self.since), a kazdy dodatkowy wiersz podnosilby
        ja o kilkanascie pikseli razy liczba urzadzen.
        """
        if reading["charging"]:
            # Wszystkie trzy liczby opisuja rozladowywanie. Podczas ladowania
            # zadna nic nie znaczy, bo poziom wlasnie rosnie.
            return i18n.t("card.charging_in_progress")

        elapsed = (
            runtime_estimator.format_elapsed(reading["since_charge_hours"])
            if "since_charge_hours" in reading
            else reading.get("since_charge")
        )
        if elapsed:
            parts = [i18n.t("card.since_charge", elapsed=elapsed)]
        else:
            # Program nie widzial momentu odlaczenia kabla - najczesciej dlatego,
            # ze zostal uruchomiony przy urzadzeniu juz pracujacym na baterii.
            parts = [i18n.t("card.since_unknown")]

        used = reading.get("drop_since_charge")
        if used:
            # Zaraz po odlaczeniu kabla poziom jeszcze nie drgnal. "zuzyto 0%"
            # byloby prawda, ale niczego nie wnosi.
            parts.append(i18n.t("card.used", used=used))

        per_point = runtime_estimator.format_per_point(
            reading.get("per_point_hours"), reading.get("runtime_source")
        )
        if per_point:
            parts.append(i18n.t("card.per_point", duration=per_point))

        return DETAIL_SEPARATOR.join(parts)


# ---------------------------------------------------------------------------
# WYBOR SPRZETU
# ---------------------------------------------------------------------------


class DevicePicker(ctk.CTkToplevel):
    """Okno deklaracji posiadanego sprzetu.

    Brak zaznaczen znaczy tryb automatyczny - dokladnie tak, jak to widzi
    settings.py. Zaden osobny przelacznik trybu nie jest wiec potrzebny i nie ma
    jak rozjechac sie z lista.
    """

    WIDTH = 470
    HEIGHT = 640

    def __init__(self, master, declared, on_save):
        super().__init__(master, fg_color=APP_BG)

        self.title(i18n.t("picker.title"))
        self.resizable(False, False)
        self.transient(master)

        self._on_save = on_save
        self._fonts = master.fonts
        self._vars = {}

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self._build_intro()
        self._build_list(declared)
        self._build_warning()
        self._build_buttons()

        self._place_over(master)
        self._refresh_warning()

        # Ikona ustawiona od razu nie przyjmuje sie w oknie potomnym - CTkToplevel
        # nadpisuje ja przy ustawianiu koloru belki tytulu.
        self.after(250, self._apply_icon)

        self.grab_set()
        self.focus_force()

    # -- budowa -------------------------------------------------------------

    def _build_intro(self):
        wrapper = ctk.CTkFrame(self, fg_color="transparent")
        wrapper.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 8))
        wrapper.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            wrapper,
            text=i18n.t("picker.heading"),
            font=self._fonts["title"],
            text_color=COLOR_TEXT,
            anchor="w",
        ).grid(row=0, column=0, sticky="w")

        ctk.CTkLabel(
            wrapper,
            text=i18n.t("picker.intro"),
            font=self._fonts["subtitle"],
            text_color=COLOR_MUTED,
            anchor="w",
            justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(6, 0))

    def _build_list(self, declared):
        self._list = ctk.CTkScrollableFrame(self, fg_color=PANEL_BG, corner_radius=8)
        self._list.grid(row=1, column=0, sticky="nsew", padx=16)
        self._list.grid_columnconfigure(0, weight=1)

        row = 0
        for category in device_catalog.CATEGORY_ORDER:
            devices = device_catalog.by_category(category)
            if not devices:
                continue

            row = self._add_group_header(category, row)
            for device in devices:
                row = self._add_device_row(device, declared, row)

    def _add_group_header(self, category, row):
        header = ctk.CTkFrame(self._list, fg_color="transparent")
        header.grid(row=row, column=0, sticky="ew", padx=10, pady=(14 if row else 10, 4))
        header.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            header,
            text=i18n.t(f"category.{category}").upper(),
            font=self._fonts["section"],
            text_color=COLOR_MUTED,
            anchor="w",
        ).grid(row=0, column=0, sticky="w")

        if category == device_catalog.KEYBOARD:
            # Klawiatury sa na liscie po to, by dalo sie ostrzec o braku ekranu.
            # Bateria dotyczy tylko modeli bezprzewodowych, wiec bez tego zdania
            # zaznaczenie Apex Pro wygladaloby na obietnice karty baterii.
            ctk.CTkLabel(
                header,
                text=i18n.t("picker.keyboard_note"),
                font=self._fonts["footer"],
                text_color=COLOR_MUTED,
                anchor="w",
            ).grid(row=1, column=0, sticky="w", pady=(2, 0))

        return row + 1

    def _add_device_row(self, device, declared, row):
        holder = ctk.CTkFrame(self._list, fg_color="transparent")
        holder.grid(row=row, column=0, sticky="ew", padx=10)
        holder.grid_columnconfigure(1, weight=1)

        variable = ctk.BooleanVar(value=device.key in declared)
        self._vars[device.key] = variable

        ctk.CTkCheckBox(
            holder,
            text=device.name,
            variable=variable,
            command=self._refresh_warning,
            font=self._fonts["subtitle"],
            text_color=COLOR_TEXT,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            checkbox_width=18,
            checkbox_height=18,
            border_width=2,
        ).grid(row=0, column=0, sticky="w", pady=3)

        note, color = self._note_for(device)
        if note:
            ctk.CTkLabel(
                holder,
                text=note,
                font=self._fonts["footer"],
                text_color=color,
                anchor="e",
            ).grid(row=0, column=1, sticky="e", padx=(8, 0))

        return row + 1

    @staticmethod
    def _note_for(device):
        """Dopisek przy modelu: czego program o nim nie umie albo nie wie.

        Oba zastrzezenia moga dotyczyc jednego modelu naraz - Apex Pro Mini
        Wireless nie ma ekranu i nie ma potwierdzonego odczytu baterii.
        """
        notes = []
        warn = False

        if device.category == device_catalog.KEYBOARD and not device.has_screen:
            notes.append(i18n.t("picker.note_no_screen"))
            warn = True

        if device.wireless and not device.battery_supported:
            # Model jest na liscie, ale nie mamy potwierdzonego protokolu odczytu.
            # Lepiej powiedziec to wprost, niz pozwolic czekac na dane, ktore
            # nigdy nie przyjda.
            notes.append(i18n.t("picker.note_no_battery"))

        return " · ".join(notes), COLOR_WARN if warn else COLOR_MUTED

    def _build_warning(self):
        self._warning = ctk.CTkLabel(
            self,
            text="",
            font=self._fonts["subtitle"],
            text_color=COLOR_WARN,
            anchor="w",
            justify="left",
        )
        self._warning.grid(row=2, column=0, sticky="ew", padx=16, pady=(10, 0))

    def _build_buttons(self):
        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.grid(row=3, column=0, sticky="ew", padx=16, pady=(10, 14))
        buttons.grid_columnconfigure(0, weight=1)

        ctk.CTkButton(
            buttons,
            text=i18n.t("picker.clear"),
            width=88,
            height=30,
            font=self._fonts["button"],
            fg_color="#34343A",
            hover_color="#41414A",
            command=self._clear,
        ).grid(row=0, column=0, sticky="w")

        ctk.CTkButton(
            buttons,
            text=i18n.t("picker.cancel"),
            width=88,
            height=30,
            font=self._fonts["button"],
            fg_color="#34343A",
            hover_color="#41414A",
            command=self.destroy,
        ).grid(row=0, column=1, padx=(0, 8))

        ctk.CTkButton(
            buttons,
            text=i18n.t("picker.save"),
            width=88,
            height=30,
            font=self._fonts["button"],
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            command=self._save,
        ).grid(row=0, column=2)

    def _apply_icon(self):
        if os.path.isfile(ICON_PATH):
            try:
                self.iconbitmap(ICON_PATH)
            except tk.TclError:
                pass

    def _place_over(self, master):
        """Ustawia okno na srodku okna glownego, ale w granicach ekranu."""
        self.update_idletasks()
        # Toplevel startuje na domyslnym monitorze. Docelowy DPI pochodzi z
        # rodzica, zanim Windows zdazy przeskalowac nowe okno po przeniesieniu.
        scaling = ctk.ScalingTracker.get_window_scaling(master)
        left, top, right, bottom = _monitor_work_area(master)
        decoration = round(48 * scaling)
        width = min(self.WIDTH, max(1, int((right - left) / scaling)))
        height = min(self.HEIGHT, max(1, int((bottom - top - decoration) / scaling)))
        physical_width, physical_height = round(width * scaling), round(height * scaling)
        x = master.winfo_x() + (master.winfo_width() - physical_width) // 2
        y = master.winfo_y() + (master.winfo_height() - physical_height) // 2
        x = max(left, min(x, right - physical_width))
        y = max(top, min(y, bottom - physical_height - decoration))
        self.geometry(f"{width}x{height}+{x}+{y}")

    # -- reakcje ------------------------------------------------------------

    def _selected(self):
        return [key for key, variable in self._vars.items() if variable.get()]

    def _refresh_warning(self):
        self._warning.configure(text=self._warning_text(self._selected()))

    @staticmethod
    def _warning_text(selected):
        """Ostrzezenia zalezne od zaznaczenia."""
        devices = [device_catalog.by_key(key) for key in selected]
        devices = [device for device in devices if device is not None]

        keyboards = [device for device in devices if device.category == device_catalog.KEYBOARD]
        if keyboards and not any(device.has_screen for device in keyboards):
            names = ", ".join(device.name for device in keyboards)
            return i18n.t("picker.warn_no_screen", names=names)

        monitored = [device for device in devices if device.wireless]
        if devices and not monitored:
            return i18n.t("picker.warn_no_battery")

        unsupported = [device for device in monitored if not device.battery_supported]
        if monitored and len(unsupported) == len(monitored):
            return i18n.t("picker.warn_unsupported")

        return ""

    def _clear(self):
        for variable in self._vars.values():
            variable.set(False)
        self._refresh_warning()

    def _save(self):
        if self._on_save(self._selected()):
            self.destroy()
        else:
            self._warning.configure(text=i18n.t("log.settings_failed"))

    def destroy(self):
        _cancel_pending_callbacks(self)
        super().destroy()


# ---------------------------------------------------------------------------
# OKNO
# ---------------------------------------------------------------------------


class App(ctk.CTk):
    def __init__(self, start_hidden=False):
        super().__init__(fg_color=APP_BG)

        self.title(APP_TITLE)
        # Wysokosc dobiera sie do liczby urzadzen (patrz _autosize), dlatego okno
        # nie jest skalowalne - inaczej dopasowanie walczyloby z uzytkownikiem.
        self.resizable(False, False)

        self._apply_icon()

        self._log_queue = queue.Queue()
        self._ui_queue = queue.Queue()
        self._last_frame = None
        self._last_readings = None
        self._last_readings_revision = None
        self._last_status = None
        self._last_tooltip = None
        self._last_layout_key = None
        self._last_layout_check = 0.0
        self._preview_image = None
        self._log_visible = False
        self._quitting = False
        self._shutdown_complete = False
        self._shutdown_id = None
        self._hide_requested = False
        self._card_count = None
        self._tick_id = None
        self._picker = None

        # Widgety z napisem statycznym, razem z kluczem tlumaczenia. Przelacznik
        # jezyka przechodzi po tej liscie zamiast szukac ich po drzewie widgetow.
        self._translatable = []

        self.fonts = self._build_fonts()

        # Okno i serwis czytaja ten sam plik, ale kazde ma wlasna instancje:
        # serwis wczytuje ustawienia we wlasnym watku przy reload_settings(),
        # a okno potrzebuje ich do zaznaczen w oknie wyboru.
        self.settings = app_settings.Settings()

        # Jezyk ustawiamy przed zbudowaniem czegokolwiek - inaczej pierwsze
        # napisy powstalyby w jezyku systemu i zmienilyby sie w mgnieniu oka.
        i18n.set_language(self.settings.resolved_language())

        self.service = monitor_service.MonitorService(on_log=self._log_queue.put)

        self.grid_columnconfigure(0, weight=1)

        self._cards = {}
        self._build_header()
        self._build_preview()
        self._build_devices()
        self._build_footer()
        self._build_log()

        self._tray_ready = threading.Event()
        self._tray = None
        self._tray_thread = None
        try:
            self._tray = self._build_tray()
        except Exception as exc:
            self._ui_queue.put(lambda error=str(exc): self._handle_tray_failure(error))
        else:
            self._tray_thread = threading.Thread(target=self._run_tray, name="Tray", daemon=True)

        self.protocol("WM_DELETE_WINDOW", self.hide_to_tray)

        # Dopasowanie przed pierwszym wyswietleniem: CustomTkinter pokazuje okno
        # dopiero w mainloop, wiec uzytkownik nie zobaczy rozmiaru probnego.
        self._autosize()

        if start_hidden:
            # Tu withdraw() dziala inaczej niz gdziekolwiek pozniej: wywolane
            # przed pierwszym pokazaniem okna ustawia w CTk flage, przez ktora
            # mainloop pomija swoje deiconify(). Dzieki temu przy starcie razem
            # z Windows okno nie mignie ani na chwile. Pozniejsze pokazanie idzie
            # przez show_window(), ktore te flage obchodzi.
            self.withdraw()

        # Autostart moze wskazywac stara sciezke, jesli program zostal
        # przeniesiony albo zamieniony ze zrodel na .exe.
        if autostart.repair_if_stale():
            self._log_queue.put(i18n.t("log.autostart_repaired"))

        try:
            self.service.start()
            if self._tray_thread is not None:
                self._tray_thread.start()
            self._tick()
        except BaseException:
            self._quitting = True
            self.wait_for_shutdown()
            raise

    # -- budowa interfejsu --------------------------------------------------

    def _track(self, widget, key):
        """Zapamietuje widget o statycznym napisie i od razu go wpisuje.

        Dzieki temu klucz tlumaczenia wystepuje w kodzie raz - przy tworzeniu
        widgetu - a nie drugi raz w retranslate(). Napis i klucz nie moga sie
        wiec rozjechac po dodaniu kolejnego przycisku.
        """
        widget.configure(text=i18n.t(key))
        self._translatable.append((widget, key))
        return widget

    def _build_fonts(self):
        # CTkFont wymaga istniejacego okna, dlatego czcionki powstaja tutaj,
        # a nie na poziomie modulu.
        family = "Segoe UI"
        return {
            "title": ctk.CTkFont(family=family, size=17, weight="bold"),
            "subtitle": ctk.CTkFont(family=family, size=11),
            "status": ctk.CTkFont(family=family, size=12, weight="bold"),
            "section": ctk.CTkFont(family=family, size=11, weight="bold"),
            "card_label": ctk.CTkFont(family=family, size=14, weight="bold"),
            "card_percent": ctk.CTkFont(family=family, size=26, weight="bold"),
            "card_runtime": ctk.CTkFont(family=family, size=13, weight="bold"),
            "card_name": ctk.CTkFont(family=family, size=11),
            "card_since": ctk.CTkFont(family=family, size=11),
            "button": ctk.CTkFont(family=family, size=12, weight="bold"),
            "footer": ctk.CTkFont(family=family, size=11),
            "log": ctk.CTkFont(family="Consolas", size=11),
        }

    def _apply_icon(self):
        # Kolejnosc ma znaczenie: identyfikator aplikacji trzeba ustawic, zeby
        # pasek zadan pokazal nasza ikone, a nie generyczna ikone Pythona.
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
        except (AttributeError, OSError):
            pass

        if os.path.isfile(ICON_PATH):
            try:
                self.iconbitmap(ICON_PATH)
            except tk.TclError:
                pass

    def _build_header(self):
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 10))
        header.grid_columnconfigure(1, weight=1)

        if os.path.isfile(LOGO_PATH):
            logo = ctk.CTkImage(Image.open(LOGO_PATH), size=(38, 38))
            ctk.CTkLabel(header, image=logo, text="").grid(row=0, column=0, rowspan=2, padx=(0, 12))
            self._logo_image = logo

        ctk.CTkLabel(
            header, text=APP_TITLE, font=self.fonts["title"], text_color=COLOR_TEXT, anchor="w"
        ).grid(row=0, column=1, sticky="sw")

        subtitle = ctk.CTkLabel(
            header,
            text="",
            font=self.fonts["subtitle"],
            text_color=COLOR_MUTED,
            anchor="w",
        )
        subtitle.grid(row=1, column=1, sticky="nw")
        self._track(subtitle, "app.subtitle")

        # Bez tekstu startowego: pierwszy przebieg _update_status wpisze go
        # kilkadziesiat milisekund pozniej, wiec napis na sztywno tylko dublowalby
        # klucz "status.connecting".
        self.status_label = ctk.CTkLabel(
            header, text="", font=self.fonts["status"], text_color=COLOR_MUTED
        )
        self.status_label.grid(row=0, column=2, rowspan=2, sticky="e")

    def _build_preview(self):
        wrapper = ctk.CTkFrame(self, fg_color="transparent")
        wrapper.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 12))
        wrapper.grid_columnconfigure(0, weight=1)

        section = ctk.CTkLabel(
            wrapper,
            text="",
            font=self.fonts["section"],
            text_color=COLOR_MUTED,
            anchor="w",
        )
        section.grid(row=0, column=0, sticky="w", pady=(0, 6))
        self._track(section, "section.screen")

        # Ramka udaje obudowe ekranu, zeby podglad nie zlewal sie z tlem okna.
        bezel = ctk.CTkFrame(
            wrapper, fg_color="#0E0E11", corner_radius=8,
            width=oled_render.WIDTH * PREVIEW_ZOOM + 20,
            height=oled_render.HEIGHT * PREVIEW_ZOOM + 20,
        )
        self._preview_bezel = bezel
        bezel.grid(row=1, column=0)

        # Pusta klatka jako obraz startowy. Etykieta bierze rozmiar z obrazu -
        # opcje width i height sa dla niej liczone w znakach i wierszach tekstu,
        # wiec podanie tam pikseli rozdmuchaloby okno.
        self._preview_image = ImageTk.PhotoImage(
            frame_to_preview([0] * oled_render.FRAME_BYTES)
        )

        # Zwykla etykieta tkinter, a nie CTkImage: podglad zmienia sie 10 razy
        # na sekunde i ImageTk.PhotoImage ma tu wyraznie mniejszy narzut.
        self.preview = tk.Label(
            bezel,
            image=self._preview_image,
            bg="#0E0E11",
            borderwidth=0,
            highlightthickness=0,
        )
        self.preview.pack(padx=10, pady=10)

    def _build_devices(self):
        # Kontener ma kontrolowana wysokosc. Sama zawartosc moze byc dowolnie
        # wysoka, a przy dlugiej deklaracji przewija sie bez ukrywania przyciskow.
        self._devices_container = ctk.CTkFrame(
            self, fg_color="transparent", corner_radius=0, height=1
        )
        self._devices_container.grid(row=2, column=0, sticky="ew", padx=16)
        self._devices_container.grid_propagate(False)
        self._devices_container.grid_rowconfigure(0, weight=1)
        self._devices_container.grid_columnconfigure(0, weight=1)
        self.devices = ctk.CTkScrollableFrame(
            self._devices_container, fg_color="transparent", corner_radius=0, height=1
        )
        self.devices.grid(row=0, column=0, sticky="nsew")
        self.devices.grid_columnconfigure(0, weight=1)

        self.empty_label = ctk.CTkLabel(
            self.devices,
            text="",
            font=self.fonts["subtitle"],
            text_color=COLOR_MUTED,
            justify="center",
        )
        self._track(self.empty_label, "devices.empty")

    def _build_footer(self):
        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.grid(row=3, column=0, sticky="ew", padx=16, pady=(12, 6))
        footer.grid_columnconfigure(3, weight=1)

        self.refresh_button = ctk.CTkButton(
            footer,
            text="",
            width=88,
            height=30,
            font=self.fonts["button"],
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            command=self.refresh_now,
        )
        self.refresh_button.grid(row=0, column=0, padx=(0, 8))
        self._track(self.refresh_button, "button.refresh")

        # Przycisk pauzy jest poza _translatable: jego napis zalezy od stanu,
        # wiec ustawia go _refresh_pause_button, a nie staly klucz.
        self.pause_button = ctk.CTkButton(
            footer,
            text="",
            width=88,
            height=30,
            font=self.fonts["button"],
            fg_color="#34343A",
            hover_color="#41414A",
            command=self.toggle_pause,
        )
        self.pause_button.grid(row=0, column=1, padx=(0, 8))
        self._refresh_pause_button()

        self.hardware_button = ctk.CTkButton(
            footer,
            text="",
            width=88,
            height=30,
            font=self.fonts["button"],
            fg_color="#34343A",
            hover_color="#41414A",
            command=self.open_picker,
        )
        self.hardware_button.grid(row=0, column=2, padx=(0, 8))
        self._track(self.hardware_button, "button.hardware")

        self.log_button = ctk.CTkButton(
            footer,
            text="",
            width=60,
            height=30,
            font=self.fonts["button"],
            fg_color="#34343A",
            hover_color="#41414A",
            command=self.toggle_log,
        )
        self.log_button.grid(row=0, column=4, sticky="e")
        self._track(self.log_button, "button.log")

        self._build_options()

        self.footer_status = ctk.CTkLabel(
            self, text="", font=self.fonts["footer"], text_color=COLOR_MUTED, anchor="w"
        )
        self.footer_status.grid(row=5, column=0, sticky="ew", padx=16, pady=(6, 12))

    def _build_options(self):
        """Wiersz ustawien: autostart po lewej, jezyk po prawej.

        Dwa ustawienia w jednym wierszu, bo oba dotycza calego programu, a nie
        pojedynczego urzadzenia. Przelacznik jezyka jest po prawej, gdzie zostaje
        wolne miejsce po dlugim opisie autostartu.
        """
        options = ctk.CTkFrame(self, fg_color="transparent")
        options.grid(row=4, column=0, sticky="ew", padx=16, pady=(4, 0))
        options.grid_columnconfigure(0, weight=1)

        self.autostart_var = ctk.BooleanVar(value=autostart.is_enabled())
        self.autostart_box = ctk.CTkCheckBox(
            options,
            text="",
            variable=self.autostart_var,
            command=self.toggle_autostart,
            font=self.fonts["footer"],
            text_color=COLOR_MUTED,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            checkbox_width=16,
            checkbox_height=16,
            border_width=2,
        )
        self.autostart_box.grid(row=0, column=0, sticky="w")
        self._track(self.autostart_box, "checkbox.autostart")

        codes = [code for code, _ in i18n.available()]
        self._language_codes = codes

        # Podpisy sa nazwami samych jezykow ("PL", "EN"), a nie tlumaczonymi
        # napisami - przelacznik ma byc czytelny takze dla kogos, kto trafil na
        # jezyk, ktorego nie zna.
        self.language_switch = ctk.CTkSegmentedButton(
            options,
            values=[i18n.LANGUAGE_NAMES[code] for code in codes],
            width=84,
            height=24,
            font=self.fonts["footer"],
            fg_color="#26262B",
            selected_color=ACCENT,
            selected_hover_color=ACCENT_HOVER,
            unselected_color="#26262B",
            unselected_hover_color="#34343A",
            text_color=COLOR_TEXT,
            command=self._on_language_selected,
        )
        self.language_switch.set(i18n.LANGUAGE_NAMES[i18n.language()])
        self.language_switch.grid(row=0, column=1, sticky="e")

    def _build_log(self):
        self.log_box = ctk.CTkTextbox(
            self,
            height=150,
            font=self.fonts["log"],
            fg_color=PANEL_BG,
            text_color=COLOR_MUTED,
            corner_radius=8,
            wrap="none",
        )
        self.log_box.configure(state="disabled")

    # -- zasobnik -----------------------------------------------------------

    def _build_tray(self):
        if os.path.isfile(LOGO_PATH):
            with Image.open(LOGO_PATH) as source:
                image = source.convert("RGBA")
        else:
            image = Image.new("RGB", (64, 64), ACCENT)
        # Napisy jako funkcje, nie napisy: pystray wola je za kazdym otwarciem
        # menu, wiec po zmianie jezyka menu jest juz przetlumaczone i nie trzeba
        # go przebudowywac.
        menu = pystray.Menu(
            pystray.MenuItem(lambda item: i18n.t("tray.show"), self._tray_show, default=True),
            pystray.MenuItem(lambda item: i18n.t("tray.refresh"), self._tray_refresh),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(lambda item: i18n.t("tray.quit"), self._tray_quit),
        )
        return pystray.Icon("battery_monitor", image, APP_TITLE, menu)

    def _run_tray(self):
        error = "The system tray stopped unexpectedly."
        try:
            self._tray.run(setup=self._setup_tray)
        except Exception as exc:
            error = str(exc)
        finally:
            self._tray_ready.clear()
            if not self._quitting:
                self._ui_queue.put(lambda message=error: self._handle_tray_failure(message))

    def _setup_tray(self, icon):
        if self._quitting:
            return
        try:
            icon.visible = True
            if self._quitting:
                icon.visible = False
                return
            self._tray_ready.set()
            if self._hide_requested:
                self._ui_queue.put(self.hide_to_tray)
        except Exception as exc:
            self._ui_queue.put(lambda error=str(exc): self._handle_tray_failure(error))

    def _handle_tray_failure(self, error):
        if self._quitting:
            return
        self._hide_requested = False
        self._tray_ready.clear()
        self._log_queue.put(i18n.t("log.tray_failed", error=error))
        self.protocol("WM_DELETE_WINDOW", self.quit_app)
        self.show_window()

    # Nawet after() dotyka Tk. Callbacki pystray tylko zapisują akcje do kolejki.
    def _tray_show(self, icon=None, item=None):
        if not self._quitting:
            self._ui_queue.put(self.show_window)

    def _tray_refresh(self, icon=None, item=None):
        if not self._quitting:
            self._ui_queue.put(self.refresh_now)

    def _tray_quit(self, icon=None, item=None):
        if not self._quitting:
            self._ui_queue.put(self.quit_app)

    def hide_to_tray(self):
        if not self._tray_ready.is_set():
            # Okno musi zostac dostepne, dopoki ikona nie zostala utworzona.
            self._hide_requested = True
            return
        self._hide_requested = False
        self.withdraw()
        try:
            self._tray.notify(i18n.t("tray.hidden"), APP_TITLE)
        except Exception:
            # Powiadomienia systemowe potrafia byc wylaczone - to nic krytycznego.
            pass

    def show_window(self):
        self._hide_requested = False
        self.deiconify()
        # Przy starcie do zasobnika okno nigdy nie bylo pokazane, a w tym stanie
        # samo deiconify() nie zawsze wystarcza - stan "withdrawn" trzeba
        # zdjac wprost.
        self.state("normal")
        self.lift()
        self.focus_force()
        self._check_display_layout(force=True)
        snapshot = self.service.get_snapshot()
        self._update_status(snapshot)
        self._update_devices(snapshot)
        self._update_preview(snapshot)

    # -- akcje --------------------------------------------------------------

    def refresh_now(self):
        self.service.refresh_now()

    def open_picker(self):
        """Otwiera okno deklaracji sprzetu, pilnujac jednej instancji."""
        if self._picker is not None and self._picker.winfo_exists():
            self._picker.focus_force()
            return

        self._picker = DevicePicker(
            self, declared=self.settings.declared, on_save=self._apply_declaration
        )

    def _apply_declaration(self, keys):
        """Zapisuje wybor i kaze serwisowi odczytac go od nowa."""
        if not self.settings.set_declared(keys):
            self._log_queue.put(i18n.t("log.settings_failed"))
            if not self._log_visible:
                self.toggle_log()
            return False

        # Karty odpowiadaja poprzedniej deklaracji, wiec musza zniknac razem
        # z nia - serwis zbuduje liste na nowo przy najblizszym odczycie.
        for card in self._cards.values():
            card.destroy()
        self._cards.clear()
        self._last_readings = None
        self._last_readings_revision = None

        self.service.reload_settings()

        count = len(keys)
        self._log_queue.put(
            i18n.t("log.declared", count=count) if count else i18n.t("log.automatic")
        )
        return True

    def toggle_pause(self):
        self.service.set_paused(not self.service.is_paused())
        self._refresh_pause_button()

    def _refresh_pause_button(self):
        """Napis i kolor przycisku pauzy zgodnie z biezacym stanem serwisu."""
        paused = self.service.is_paused()
        self.pause_button.configure(
            text=i18n.t("button.resume" if paused else "button.pause"),
            fg_color=ACCENT if paused else "#34343A",
            hover_color=ACCENT_HOVER if paused else "#41414A",
        )

    def toggle_autostart(self):
        """Dodaje albo usuwa wpis autostartu, pilnujac zgodnosci z checkboxem.

        Gdy operacja na rejestrze sie nie uda, checkbox wraca do stanu
        faktycznego. Zaznaczenie, ktore niczego nie zmienilo, byloby gorsze niz
        komunikat o bledzie - uzytkownik bylby przekonany, ze program wystartuje
        sam, a on by nie wystartowal.
        """
        wanted = bool(self.autostart_var.get())

        if autostart.set_enabled(wanted):
            self._log_queue.put(i18n.t("log.autostart_on" if wanted else "log.autostart_off"))
        else:
            self.autostart_var.set(autostart.is_enabled())
            self._log_queue.put(i18n.t("log.autostart_failed"))

    # -- jezyk --------------------------------------------------------------

    def _on_language_selected(self, name):
        """Reakcja na przelacznik PL/EN. Dostaje podpis, nie kod jezyka."""
        for code in self._language_codes:
            if i18n.LANGUAGE_NAMES[code] == name:
                self.set_language(code)
                return

    def set_language(self, code):
        """Zmienia jezyk interfejsu, zapisuje wybor i przerysowuje napisy."""
        if code == i18n.language():
            return

        # Zapisujemy konkretny kod, nie "auto": skoro uzytkownik wybral jezyk
        # recznie, program ma go trzymac takze wtedy, gdy Windows zmieni swoj.
        if not self.settings.set_language(code):
            self.language_switch.set(i18n.LANGUAGE_NAMES[i18n.language()])
            self._log_queue.put(i18n.t("log.settings_failed"))
            if not self._log_visible:
                self.toggle_log()
            return
        i18n.set_language(code)

        self.retranslate()
        self._log_queue.put(i18n.t("log.language"))

    def retranslate(self):
        """Wpisuje napisy od nowa we wszystkie widgety, bez restartu programu."""
        for widget, key in self._translatable:
            widget.configure(text=i18n.t(key))

        self._refresh_pause_button()

        # Przelacznik zwykle sam wie, co zostalo wybrane, ale jezyk da sie zmienic
        # takze programowo - wtedy zaznaczenie musi za tym nadazyc.
        self.language_switch.set(i18n.LANGUAGE_NAMES[i18n.language()])

        # Stopka i wskaznik stanu odswiezaja sie same przy najblizszym takcie,
        # ale karty nie: _update_devices porzuca liste, ktora juz widzialo.
        # Bez tego napisy na kartach zostalyby w poprzednim jezyku do nastepnego
        # odczytu baterii, czyli nawet minute.
        self._last_readings = None
        self._last_readings_revision = None
        self._last_tooltip = None

        # Klatka na klawiaturze powstaje w watku serwisu i trwa do konca cyklu
        # wyswietlania. Bez przerwania tej pauzy ekran - i podglad w oknie -
        # zostalby w poprzednim jezyku jeszcze przez kilka sekund.
        self.service.refresh_now()

        try:
            self._tray.update_menu()
        except Exception:
            # Menu zasobnika i tak sklada napisy przy otwarciu - to tylko
            # odswiezenie na wypadek, gdyby platforma trzymala je w pamieci.
            pass

    def toggle_log(self):
        self._log_visible = not self._log_visible
        if self._log_visible:
            self.log_box.grid(row=6, column=0, sticky="ew", padx=16, pady=(0, 12))
        else:
            self.log_box.grid_remove()
        self._autosize()

    def _autosize(self):
        """Dopasowuje wysokosc okna do zawartosci.

        Liczba urzadzen zmienia sie w trakcie pracy - mysz zasypia, sluchawki
        sa wlaczane. Bez dopasowania okno albo zostawialoby duza pusta przestrzen
        przy jednym urzadzeniu, albo obcinalo trzecie.

        Mierzymy dolna krawedz ostatniego widgetu, a nie winfo_reqheight okna:
        widgety CustomTkinter maja wlasny domyslny rozmiar 200x200, wiec
        zadana wysokosc okna jest suma tych domyslnych, a nie wysokoscia tresci.
        """
        scaling = ctk.ScalingTracker.get_window_scaling(self)
        widget_scaling = ctk.ScalingTracker.get_widget_scaling(self)
        left, top, right, bottom = _monitor_work_area(self)
        # Zostawiamy miejsce na ramke i belke okna, poza obszarem klienta Tk.
        decoration = round(48 * scaling)
        maximum = max(1, bottom - top - decoration)
        # PhotoImage i zwykla etykieta Tk maja rozmiar fizyczny. Skalowanie
        # samej obudowy CTk nie moze dodawac pustego miejsca pod podgladem.
        self._preview_bezel.configure(
            width=(oled_render.WIDTH * PREVIEW_ZOOM + 20) / widget_scaling,
            height=(oled_render.HEIGHT * PREVIEW_ZOOM + 20) / widget_scaling,
        )
        if self._log_visible:
            self.log_box.configure(height=150)
        self._devices_container.configure(height=1)
        self.geometry(f"{WINDOW_WIDTH}x{AUTOSIZE_PROBE_HEIGHT}")
        self.update_idletasks()

        last = self.log_box if self._log_visible else self.footer_status
        fixed = (
            last.winfo_y() + last.winfo_height() + AUTOSIZE_BOTTOM_PAD
            - self._devices_container.winfo_height()
        )
        if self._log_visible and fixed >= maximum:
            height = max(1, self.log_box.winfo_height() - (fixed - maximum + 1))
            self.log_box.configure(height=max(1, int(height / widget_scaling)))
            self.update_idletasks()
            fixed = (
                last.winfo_y() + last.winfo_height() + AUTOSIZE_BOTTOM_PAD
                - self._devices_container.winfo_height()
            )
        content = self.devices.winfo_reqheight()
        viewport = max(1, min(content, maximum - fixed))
        self._devices_container.configure(height=max(1, int(viewport / widget_scaling)))
        self.update_idletasks()
        needed = min(maximum, last.winfo_y() + last.winfo_height() + AUTOSIZE_BOTTOM_PAD)

        # winfo_* podaje piksele fizyczne, a CTk.geometry sam mnozy przez
        # skalowanie okna - bez podzielenia wynik bylby przeskalowany dwukrotnie.
        height = max(1, int(needed / scaling))
        x = max(left, min(self.winfo_x(), right - round(WINDOW_WIDTH * scaling)))
        y = max(top, min(self.winfo_y(), bottom - round(height * scaling) - decoration))
        self.geometry(f"{WINDOW_WIDTH}x{height}+{x}+{y}")
        self._last_layout_key = ((left, top, right, bottom), scaling, widget_scaling)
        self._last_layout_check = time.monotonic()

    def _check_display_layout(self, force=False):
        now = time.monotonic()
        if not force and now - self._last_layout_check < 1.0:
            return
        self._last_layout_check = now
        key = (
            _monitor_work_area(self), ctk.ScalingTracker.get_window_scaling(self),
            ctk.ScalingTracker.get_widget_scaling(self),
        )
        if key != self._last_layout_key:
            self._autosize()

    def quit_app(self):
        if self._quitting:
            return
        self._quitting = True
        # Zaplanowane odswiezenie trzeba odwolac przed zniszczeniem okna,
        # inaczej Tcl probuje wywolac je na juz nieistniejacych widgetach.
        if self._tick_id is not None:
            try:
                self.after_cancel(self._tick_id)
            except tk.TclError:
                pass
            self._tick_id = None
        self._finish_shutdown()

    def _finish_shutdown(self):
        self._shutdown_id = None
        # Timeout nie oznacza zakonczenia pracy. Tk i mutex pozostaja zywe,
        # dopoki worker nie zapisze historii i nie odda urzadzen.
        if self.service.stop(timeout=0) is False:
            self._shutdown_id = self.after(TICK_MS, self._finish_shutdown)
            return False
        self._finalize_shutdown()
        return True

    def _finalize_shutdown(self):
        if self._shutdown_complete:
            return
        if self._shutdown_id is not None:
            try:
                self.after_cancel(self._shutdown_id)
            except tk.TclError:
                pass
            self._shutdown_id = None
        try:
            if self._tray is not None:
                self._tray.stop()
        except Exception:
            pass
        self._shutdown_complete = True
        _cancel_pending_callbacks(self, all_callbacks=True)
        try:
            self.destroy()
        except tk.TclError:
            pass

    def wait_for_shutdown(self):
        """Domyka worker takze po wyjatku mainloop, gdy after juz nie dziala."""
        if self._shutdown_complete:
            return True
        if self.service.stop(timeout=None) is False:
            return False
        self._finalize_shutdown()
        return True

    # -- odswiezanie --------------------------------------------------------

    def _tick(self):
        self._tick_id = None
        if self._quitting:
            return

        self._drain_ui_actions()
        if self._quitting:
            return
        self._drain_log()
        snapshot = self.service.get_snapshot()
        hidden = self.state() == "withdrawn"
        if hidden:
            self._update_tray_tooltip(snapshot["readings"])
        else:
            self._check_display_layout()
            self._update_status(snapshot)
            self._update_devices(snapshot)
            self._update_preview(snapshot)

        self._tick_id = self.after(TRAY_TICK_MS if hidden else TICK_MS, self._tick)

    def _drain_ui_actions(self):
        for _ in range(100):
            try:
                action = self._ui_queue.get_nowait()
            except queue.Empty:
                return
            action()
            if self._quitting:
                return

    def _drain_log(self):
        lines = []
        for _ in range(200):
            try:
                lines.append(self._log_queue.get_nowait())
            except queue.Empty:
                break
        if not lines:
            return

        self.log_box.configure(state="normal")
        self.log_box.insert("end", "\n".join(lines) + "\n")
        # Bez limitu panel rosnie bez konca przy pracy liczonej w dniach.
        line_count = int(self.log_box.index("end-1c").split(".")[0])
        if line_count > 400:
            self.log_box.delete("1.0", f"{line_count - 400 + 1}.0")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _update_status(self, snapshot):
        if snapshot["connected"]:
            key, color = "status.connected", COLOR_OK
        elif snapshot["running"]:
            # Bateria jest odczytywana niezaleznie od GameSense, wiec to nie jest
            # blad krytyczny - stad kolor ostrzezenia, a nie bledu.
            key, color = "status.no_screen", COLOR_WARN
        else:
            key, color = "status.stopped", COLOR_MUTED

        if snapshot["paused"]:
            key, color = "status.paused", COLOR_WARN

        # Kropka jest w kodzie, nie w tlumaczeniu: to element graficzny wskaznika,
        # taki sam w kazdym jezyku.
        status_text = f"● {i18n.t(key)}"

        if snapshot["connected"]:
            last_poll = snapshot["last_poll"]
            when = time.strftime("%H:%M:%S", time.localtime(last_poll)) if last_poll else "-"
            detail = i18n.t("footer.last_poll", time=when)
        else:
            # Serwis oddaje klucz i parametry, wiec zdanie o stanie polaczenia
            # powstaje tutaj i jest zawsze w aktualnym jezyku.
            detail = i18n.t(snapshot["status_key"], **snapshot["status_params"])

        status = (status_text, color, detail)
        if status != self._last_status:
            self.status_label.configure(text=status_text, text_color=color)
            self.footer_status.configure(text=detail)
            self._last_status = status

    def _update_devices(self, snapshot):
        readings = snapshot["readings"]
        # Migawki izolują kolekcje, wiec ich tozsamosc nie znaczy nowego odczytu.
        revision = snapshot.get("readings_revision")
        if revision is not None:
            if revision == self._last_readings_revision:
                return
        elif readings == self._last_readings:
            return
        self._last_readings = readings
        self._last_readings_revision = revision

        # Karty kluczujemy modelem, a nie etykieta: etykieta jest kategoria
        # ("MYSZ"), wiec dwie zadeklarowane myszki dzielilyby jedna karte
        # i druga nadpisywalaby pierwsza.
        for index, reading in enumerate(readings):
            card = self._cards.get(reading["model"])
            if card is None:
                card = DeviceCard(self.devices, self.fonts)
                self._cards[reading["model"]] = card
            card.grid(row=index, column=0, sticky="ew", pady=(0, 8))
            card.update_reading(reading)

        active = {reading["model"] for reading in readings}
        for model, card in list(self._cards.items()):
            if model not in active:
                card.destroy()
                del self._cards[model]

        if readings:
            self.empty_label.grid_remove()
        else:
            self.empty_label.grid(row=0, column=0, pady=24)

        if len(readings) != self._card_count:
            self._card_count = len(readings)
            self._autosize()

        self._update_tray_tooltip(readings)

    def _update_tray_tooltip(self, readings):
        # W podpowiedzi zasobnika mieszcza sie tylko urzadzenia, ktore cos
        # raportuja - wpis "wyłączone" zajmowalby miejsce, nie wnoszac nic.
        online = [reading for reading in readings if reading.get("online", True)]
        if online:
            parts = [
                f"{i18n.window_label(r['label'])} {r['percent']}%"
                + (f" {i18n.t('tray.charging_short')}" if r["charging"] else "")
                for r in online
            ]
            title = f"{APP_TITLE} - " + "  ".join(parts)
        else:
            title = f"{APP_TITLE} - {i18n.t('tray.no_devices')}"
        # Windows ogranicza podpowiedz Shell_NotifyIcon do 127 znakow.
        title = title[:127]
        if title == self._last_tooltip:
            return
        try:
            self._tray.title = title
            self._last_tooltip = title
        except Exception:
            pass

    def _update_preview(self, snapshot):
        frame = snapshot["frame"]
        if frame is None or frame is self._last_frame or frame == self._last_frame:
            return
        self._last_frame = frame

        # Referencje do PhotoImage trzeba trzymac samemu - inaczej odsmiecanie
        # usuwa obrazek i podglad robi sie pusty.
        self._preview_image = ImageTk.PhotoImage(frame_to_preview(frame))
        self.preview.configure(image=self._preview_image)


def parse_arguments(argv=None):
    # Pomoc wiersza polecen zostaje po angielsku niezaleznie od jezyka interfejsu:
    # widzi ja tylko ktos, kto uruchamia program z konsoli, a jezyk nie jest
    # jeszcze wczytany w chwili parsowania argumentow.
    parser = argparse.ArgumentParser(
        prog="BatteryMonitor",
        description="SteelSeries battery monitor on the keyboard OLED screen.",
    )
    parser.add_argument(
        autostart.TRAY_ARGUMENT,
        dest="tray",
        action="store_true",
        help="start hidden, straight to the system tray (used by autostart)",
    )
    return parser.parse_args(argv)


def main(argv=None):
    options = parse_arguments(argv)

    if not single_instance.acquire():
        # Druga kopia programu walczylaby z pierwsza o ekran klawiatury i o plik
        # historii. Przy starcie z autostartu milczymy, bo nikt na to nie patrzy.
        if not options.tray:
            single_instance.warn_already_running(APP_TITLE)
        return 0

    app = None
    try:
        ctk.set_appearance_mode("dark")
        app = App(start_hidden=options.tray)
        app.mainloop()
    finally:
        can_release = app is None
        try:
            if app is not None:
                app.quit_app()
                can_release = app.wait_for_shutdown() is not False
        finally:
            if can_release:
                single_instance.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
