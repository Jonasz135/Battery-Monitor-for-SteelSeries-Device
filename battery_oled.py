"""Monitor baterii urzadzen SteelSeries na ekranie OLED - tryb konsolowy.

Cala logika siedzi w monitor_service; ten plik to tylko uruchomienie jej
z wypisywaniem komunikatow na konsole. Wersja okienkowa to app.py.

Poziom baterii odczytywany jest z dwoch zrodel, zaleznie od tego, jak urzadzenie
jest podlaczone:
  - 2.4 GHz przez odbiornik USB  -> battery_hid (zapytania HID),
  - Bluetooth                    -> battery_bluetooth (wlasciwosc PnP Windows).

Jezyk komunikatow bierze sie z ustawien programu, tych samych, ktore zmienia
przelacznik w wersji okienkowej. Tryb konsolowy nie ma czego przelaczac, wiec
tylko czyta zapisany wybor.

Uruchomienie:  python battery_oled.py
Zatrzymanie:   Ctrl+C
"""

import sys
import time

import i18n
import monitor_service
import settings as app_settings
import single_instance


def main():
    i18n.set_language(app_settings.Settings().resolved_language())

    # Ta sama blokada co w wersji okienkowej. Dwa monitory naraz wysylalyby do
    # GameSense konkurencyjne klatki i nadpisywaly sobie plik historii, a przy
    # uruchomionej wersji okienkowej lekko o to wpasc.
    if not single_instance.acquire():
        print(i18n.t("console.already_running"))
        return 1

    print(i18n.t("console.header"))

    service = monitor_service.MonitorService(on_log=print)

    try:
        service.start()
        # Praca dzieje sie w watku serwisu; tutaj tylko czekamy na Ctrl+C.
        while service.is_running():
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\n" + i18n.t("console.stopping"))
    finally:
        service.stop()

    return 0


if __name__ == "__main__":
    sys.exit(main())
