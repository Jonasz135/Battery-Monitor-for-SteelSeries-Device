"""Odczyt poziomu baterii urzadzen SteelSeries podlaczonych przez Bluetooth.

W trybie Bluetooth nie ma odbiornika USB, wiec protokoly HID z battery_hid nie
maja gdzie zadac pytania. Windows sam jednak zna poziom baterii urzadzen BT,
ktore go raportuja (przez HID over GATT albo profil Hands-Free), i udostepnia
go jako wlasciwosc urzadzenia PnP:

    DEVPKEY_Bluetooth_Battery = {104EA319-6EE2-4701-BD47-8DDBF425BBE5} 2

Czytamy ja przez PowerShell bez dodatkowej biblioteki i bez administratora.
PnP moze zachowac poziom wylaczonego, sparowanego urzadzenia, dlatego stan
polaczenia sprawdzamy niezaleznie przez System.Devices.Aep.IsConnected (WinRT).
To enumeracja aktualnego stanu Windows, bez laczenia lub parowania urzadzen.
"""

import json
import re
import subprocess
import tempfile
import time

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
$devices = @(Get-PnpDevice -Class Bluetooth -PresentOnly |
    Where-Object {{ $_.FriendlyName -match $pattern }})
if (-not $devices.Count) {{ '[]'; exit 0 }}

# IsConnected jest wlasnoscia AssociationEndpoint, nie kluczem PnP.
# Selektory z API Microsoft obejmuja sparowane urzadzenia Classic oraz LE.
$connectionByAddress = @{{}}
try {{
    Add-Type -AssemblyName System.Runtime.WindowsRuntime -ErrorAction Stop
    $deviceInformation = [Windows.Devices.Enumeration.DeviceInformation, Windows.Devices.Enumeration, ContentType=WindowsRuntime]
    $collectionType = [Windows.Devices.Enumeration.DeviceInformationCollection, Windows.Devices.Enumeration, ContentType=WindowsRuntime]
    $kind = [Windows.Devices.Enumeration.DeviceInformationKind, Windows.Devices.Enumeration, ContentType=WindowsRuntime]::AssociationEndpoint
    $bluetooth = [Windows.Devices.Bluetooth.BluetoothDevice, Windows.Devices.Bluetooth, ContentType=WindowsRuntime]
    $bluetoothLE = [Windows.Devices.Bluetooth.BluetoothLEDevice, Windows.Devices.Bluetooth, ContentType=WindowsRuntime]
    $selectors = @($bluetooth::GetDeviceSelectorFromPairingState($true),
                   $bluetoothLE::GetDeviceSelectorFromPairingState($true))
    $properties = [string[]]@('System.Devices.Aep.IsConnected', 'System.Devices.Aep.DeviceAddress')
    $asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() |
        Where-Object {{ $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and
            $_.GetGenericArguments().Count -eq 1 -and $_.GetParameters().Count -eq 1 -and
            $_.GetParameters()[0].ParameterType.Name -like 'IAsyncOperation*' }} |
        Select-Object -First 1
    $awaitCollection = $asTask.MakeGenericMethod($collectionType)
    foreach ($selector in $selectors) {{
        $operation = $deviceInformation::FindAllAsync($selector, $properties, $kind)
        $task = $awaitCollection.Invoke($null, @($operation))
        if (-not $task.Wait(3000)) {{
            $operation.Cancel()
            throw 'Bluetooth enumeration timeout'
        }}
        foreach ($endpoint in $task.Result) {{
            $address = ("$($endpoint.Properties['System.Devices.Aep.DeviceAddress'])" -replace '[^0-9a-f]', '').ToUpperInvariant()
            if ($address.Length -eq 12) {{
                $connectionByAddress[$address] = $connectionByAddress[$address] -or
                    ($endpoint.Properties['System.Devices.Aep.IsConnected'] -eq $true)
            }}
        }}
    }}
}} catch {{
    # Nie traktujemy zapamietanej baterii jako aktualnej przy braku stanu lacza.
    '[]'; exit 0
}}

@(foreach ($device in $devices) {{
    $addressMatch = [regex]::Match($device.InstanceId, '(?i)(?:^|\\\\)DEV_([0-9a-f]{{12}})(?:\\\\|_|$)')
    if ($addressMatch.Success -and $connectionByAddress[$addressMatch.Groups[1].Value.ToUpperInvariant()]) {{
        $p = Get-PnpDeviceProperty -InstanceId $device.InstanceId -KeyName $key
        if ($p -and "$($p.Data)" -ne '') {{
            [pscustomobject]@{{ name = $device.FriendlyName; battery = [int]$p.Data; connected = $true }}
        }}
    }}
}}) | ConvertTo-Json -Compress
"""


def _label_for(name):
    for pattern, label in _LABELS:
        if re.search(pattern, name, re.IGNORECASE):
            return label
    return name.upper()[:10]


def _run_cancellable(command, creationflags, cancel_event):
    """Uruchamia helper z limitem czasu i natychmiast sprzata po anulowaniu."""
    if cancel_event.is_set():
        return None
    # communicate(PIPE) tworzy watki czytajace. Gdy potomny proces odziedziczy
    # pipe, nawet close() moze na nich czekac po zabiciu samego helpera.
    # Pliki tymczasowe nie potrzebuja watkow ani EOF od obcych potomkow.
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        if cancel_event.is_set():
            return None
        process = subprocess.Popen(
            command, stdout=stdout_file, stderr=stderr_file, creationflags=creationflags
        )
        deadline = time.monotonic() + _POWERSHELL_TIMEOUT_SECONDS
        try:
            while not cancel_event.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                try:
                    process.wait(timeout=min(0.1, remaining))
                except subprocess.TimeoutExpired:
                    continue
                if cancel_event.is_set():
                    return None
                stdout_file.seek(0)
                stderr_file.seek(0)
                return subprocess.CompletedProcess(
                    command, process.returncode, stdout_file.read(), stderr_file.read()
                )
            return None
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=1.0)


def _run_powershell(cancel_event=None):
    """Zwraca surowa liste slownikow z PowerShella, albo pusta liste."""
    try:
        command = [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            _SCRIPT,
        ]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if cancel_event is None:
            completed = subprocess.run(
                command, capture_output=True, timeout=_POWERSHELL_TIMEOUT_SECONDS,
                creationflags=creationflags,
            )
        else:
            completed = _run_cancellable(command, creationflags, cancel_event)
    except (OSError, subprocess.SubprocessError):
        return []

    if completed is None or completed.returncode:
        return []

    output = completed.stdout.decode("utf-8-sig", errors="replace").strip()
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


def read_all(cancel_event=None):
    """Zwraca liste urzadzen Bluetooth z odczytanym poziomem baterii.

    Format wpisu jest taki sam jak w battery_hid, zeby obie listy dalo sie
    scalic. Windows nie udostepnia informacji o ladowaniu, wiec "charging"
    jest zawsze False.
    """
    devices = []
    seen = set()

    entries = _run_powershell() if cancel_event is None else _run_powershell(cancel_event=cancel_event)
    for entry in entries:
        if cancel_event is not None and cancel_event.is_set():
            return []
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        percent = entry.get("battery")
        if not isinstance(name, str) or type(percent) is not int:
            continue
        name = name.strip()
        if not name or ("connected" in entry and entry["connected"] is not True):
            continue
        if not 0 <= percent <= 100:
            continue
        # Po Bluetooth nie ma PID-u, wiec model rozpoznajemy po nazwie. Gdy jest
        # w katalogu, dostajemy z niego etykiete i katalogowy czas pracy dla tego
        # lacza; gdy nie, zostaje sama nazwa z Windows i etykieta rodziny.
        known = device_catalog.find_by_bt_name(name)
        identity = known.key if known else name.casefold()
        if identity in seen:
            continue
        seen.add(identity)

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
