"""Odczyt poziomu baterii urzadzen SteelSeries podlaczonych przez Bluetooth.

W trybie Bluetooth nie ma odbiornika USB, wiec protokoly HID z battery_hid nie
maja gdzie zadac pytania. Windows sam jednak zna poziom baterii urzadzen BT,
ktore go raportuja (przez HID over GATT albo profil Hands-Free), i udostepnia
go jako wlasciwosc urzadzenia PnP:

    DEVPKEY_Bluetooth_Battery = {104EA319-6EE2-4701-BD47-8DDBF425BBE5} 2

Czytamy ja przez PowerShell, bo nie wymaga to zadnej dodatkowej biblioteki ani
uprawnien administratora. Zapytanie zawezamy do klasy Bluetooth - przemiatanie
wszystkich urzadzen PnP zajmuje kilka sekund, a sama klasa Bluetooth ponizej
jednej sekundy.
"""

import json
import re
import subprocess

import device_catalog

# Nazwa wlasciwosci PnP z poziomem baterii urzadzenia Bluetooth.
_BATTERY_PROPERTY_KEY = "{104EA319-6EE2-4701-BD47-8DDBF425BBE5} 2"

# Rodziny urzadzen SteelSeries, ktorych szukamy w nazwach urzadzen Bluetooth.
_DEVICE_FAMILIES = ("Aerox", "Arctis", "Rival", "Sensei", "Prime", "Apex")

# Zapasowe etykiety na OLED, uzywane dla urzadzen, ktorych nie ma w katalogu.
# Model z katalogu ma pierwszenstwo - te wzorce lapia tylko rodzine, zeby nieznany
# sprzet SteelSeries mial sensowna etykiete, a nie obcieta nazwe z Windows.
_LABELS = (
    ("Aerox", "MYSZ"),
    ("Rival", "MYSZ"),
    ("Sensei", "MYSZ"),
    ("Prime", "MYSZ"),
    ("Arctis", "SLUCHAWKI"),
)

_POWERSHELL_TIMEOUT_SECONDS = 15

_SCRIPT = f"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = 'SilentlyContinue'
$key = '{_BATTERY_PROPERTY_KEY}'
$pattern = '{"|".join(_DEVICE_FAMILIES)}'
@(Get-PnpDevice -Class Bluetooth -PresentOnly |
    Where-Object {{ $_.FriendlyName -match $pattern }} |
    ForEach-Object {{
        $p = Get-PnpDeviceProperty -InstanceId $_.InstanceId -KeyName $key
        if ($p -and "$($p.Data)" -ne '') {{
            [pscustomobject]@{{ name = $_.FriendlyName; battery = [int]$p.Data }}
        }}
    }}) | ConvertTo-Json -Compress
"""


def _label_for(name):
    for pattern, label in _LABELS:
        if re.search(pattern, name, re.IGNORECASE):
            return label
    return name.upper()[:10]


def _run_powershell():
    """Zwraca surowa liste slownikow z PowerShella, albo pusta liste."""
    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                _SCRIPT,
            ],
            capture_output=True,
            timeout=_POWERSHELL_TIMEOUT_SECONDS,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return []

    output = completed.stdout.decode("utf-8", errors="replace").strip()
    if not output:
        return []

    try:
        parsed = json.loads(output)
    except json.JSONDecodeError:
        return []

    # ConvertTo-Json zwraca obiekt dla jednego wyniku, a liste dla wielu.
    if isinstance(parsed, dict):
        return [parsed]
    if isinstance(parsed, list):
        return parsed
    return []


def read_all():
    """Zwraca liste urzadzen Bluetooth z odczytanym poziomem baterii.

    Format wpisu jest taki sam jak w battery_hid, zeby obie listy dalo sie
    scalic. Windows nie udostepnia informacji o ladowaniu, wiec "charging"
    jest zawsze False.
    """
    devices = []

    for entry in _run_powershell():
        name = entry.get("name")
        percent = entry.get("battery")
        if not name or not isinstance(percent, int):
            continue
        if not 0 <= percent <= 100:
            continue
        # Po Bluetooth nie ma PID-u, wiec model rozpoznajemy po nazwie. Gdy jest
        # w katalogu, dostajemy z niego etykiete i katalogowy czas pracy dla tego
        # lacza; gdy nie, zostaje sama nazwa z Windows i etykieta rodziny.
        known = device_catalog.find_by_bt_name(name)

        devices.append(
            {
                "name": f"{name} (Bluetooth)",
                # Bluetooth dostaje wlasna tozsamosc, mimo ze fizycznie to moze byc
                # to samo urzadzenie co na dongle'u. Na tym laczu bateria schodzi
                # zupelnie innym tempem (producent podaje 180 h wobec 80 h na
                # 2.4 GHz), wiec mieszanie cykli z obu lacz psuloby przewidywania.
                "model": f"{name} (Bluetooth)",
                "key": known.key if known else "",
                "link": device_catalog.LINK_BT,
                "label": known.label if known else _label_for(name),
                "rated_hours": known.rated_for(device_catalog.LINK_BT) if known else None,
                "percent": percent,
                "charging": False,
            }
        )

    return devices


if __name__ == "__main__":
    # Szybka diagnostyka: python battery_bluetooth.py
    found = read_all()
    if not found:
        print("Nie znaleziono urzadzen SteelSeries po Bluetooth z odczytem baterii.")
    for device in found:
        print(f"{device['name']:<34} {device['label']:<6} {device['percent']}%")
