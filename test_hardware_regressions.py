"""Hardware/renderer regressions, with simulated reports and no device writes.

Run: python -m unittest test_hardware_regressions -v
"""

import subprocess
import ctypes
from ctypes import wintypes
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import battery_bluetooth
import battery_hid
import battery_oled
import device_catalog
import oled_render


class QueryHandle:
    def __init__(self, reports):
        self.reports = iter(reports)
        self.written = False
        self.close = Mock()

    def set_nonblocking(self, value):
        pass

    def read(self, length):
        return next(self.reports, []) if self.written else []

    def write(self, payload):
        self.written = True
        return len(payload)


class TransactionHandle(QueryHandle):
    def __init__(self, transactions):
        super().__init__([])
        self.transactions = iter(transactions)

    def write(self, payload):
        self.reports = iter(next(self.transactions, []))
        return super().write(payload)


class HidRegressionTests(unittest.TestCase):
    def test_malformed_matching_echo_does_not_hide_following_valid_battery(self):
        handle = QueryHandle([[0xD2, 0], [0xD2, 11]])
        family = battery_hid.PROTOCOL_FAMILIES["aerox"]
        self.assertEqual(battery_hid._read_opened(handle, 0x1852, family, (0, 0xD2)),
                         (50, False))

    def test_pro_wireless_foreign_status_report_does_not_hide_online_status(self):
        handle = TransactionHandle([[[255, 0], [4, 0]], [[2]]])
        family = battery_hid.PROTOCOL_FAMILIES["arctis_pro_wireless"]
        self.assertEqual(battery_hid._read_opened(handle, 0x1290, family, (0x40, 0xAA)),
                         (50, False))

    def test_invalid_discrete_battery_steps_are_not_full_batteries(self):
        cases = (
            (battery_hid._parse_nova_7, 0x2202, [0xB0, 0, 255, 3]),
            (battery_hid._parse_nova_pro, 0x12E0, [0] * 6 + [255] + [0] * 8 + [8]),
            (battery_hid._parse_arctis_7_plus, 0x220E, [0xB0, 0, 255, 0]),
            (battery_hid._parse_arctis_pro_wireless, 0x1290, [255]),
        )
        for parser, pid, frame in cases:
            with self.subTest(parser=parser.__name__):
                self.assertIsNone(parser(pid, frame))

    def test_pro_wireless_status_query_is_retried_when_first_query_is_lost(self):
        handle = Mock()
        family = battery_hid.PROTOCOL_FAMILIES["arctis_pro_wireless"]
        with patch.object(battery_hid, "_query", side_effect=[None, [4, 0], [2]]) as query:
            self.assertEqual(battery_hid._read_opened(handle, 0x1290, family, (0x40, 0xAA)),
                             (50, False))
        self.assertEqual([call.args[1] for call in query.call_args_list],
                         [(0x41, 0xAA), (0x41, 0xAA), (0x40, 0xAA)])

    def test_pro_wireless_unknown_status_never_becomes_an_online_battery(self):
        handle = Mock()
        family = battery_hid.PROTOCOL_FAMILIES["arctis_pro_wireless"]
        with patch.object(battery_hid, "_query", side_effect=[[255, 0], [2]]) as query:
            self.assertIsNone(battery_hid._read_opened(handle, 0x1290, family, (0x40, 0xAA)))
        self.assertEqual(query.call_count, 1)

    def test_pro_wireless_status_only_requires_the_byte_the_protocol_uses(self):
        handle = TransactionHandle([[[4]], [[2]]])
        family = battery_hid.PROTOCOL_FAMILIES["arctis_pro_wireless"]
        with patch.object(battery_hid, "_QUERY_TIMEOUT_SECONDS", 0.01):
            self.assertEqual(battery_hid._read_opened(handle, 0x1290, family, (0x40, 0xAA)),
                             (50, False))

    def test_cancelled_query_never_touches_hardware(self):
        stop = threading.Event()
        stop.set()
        handle = Mock()
        self.assertIsNone(battery_hid._query(handle, (0, 0xD2), 64, True, cancel_event=stop))
        self.assertEqual(handle.mock_calls, [])

    def test_cancellation_after_drain_prevents_write(self):
        stop = threading.Event()
        handle = Mock()

        def finish_drain(length):
            stop.set()
            return []

        handle.read.side_effect = finish_drain
        self.assertIsNone(battery_hid._query(handle, (0, 0xD2), 64, True, cancel_event=stop))
        handle.write.assert_not_called()

    def test_cancelled_battery_read_does_not_even_enumerate(self):
        stop = threading.Event()
        stop.set()
        with patch.object(battery_hid.hid, "enumerate") as enumerate_devices:
            self.assertIsNone(battery_hid.read_battery(0x1852, cancel_event=stop))
        enumerate_devices.assert_not_called()

    def test_continuous_input_queue_has_bounded_drain(self):
        handle = Mock()
        calls = 0

        def endless_input(length):
            nonlocal calls
            calls += 1
            if calls > 256:
                self.fail("HID drain never terminates while the receiver sends reports")
            return [0x99, 1]

        handle.read.side_effect = endless_input
        self.assertIsNone(battery_hid._query(handle, (0, 0xD2), 64, True))
        handle.write.assert_not_called()

    def test_truncated_echo_does_not_hide_following_battery_report(self):
        handle = QueryHandle([[0xD2], [0xD2, 11]])
        family = battery_hid.PROTOCOL_FAMILIES["aerox"]
        result = battery_hid._read_opened(handle, 0x1852, family, (0, 0xD2))
        self.assertEqual(result, (50, False))

    def test_wrong_known_interface_is_not_a_fallback(self):
        entries = [{"path": b"mouse", "interface_number": 0, "usage_page": 1}]
        with patch.object(battery_hid.hid, "enumerate", return_value=entries):
            self.assertIsNone(battery_hid._find_path(0x1852, 3, 0xFFC0))

    def test_unknown_windows_interface_is_recovered_from_path(self):
        entries = [
            {"path": b"\\\\?\\hid#vid_1038&pid_1260&mi_00#1", "interface_number": -1, "usage_page": 1},
            {"path": b"\\\\?\\hid#vid_1038&pid_1260&mi_05#2", "interface_number": -1, "usage_page": 0xFF00},
        ]
        with patch.object(battery_hid.hid, "enumerate", return_value=entries):
            self.assertEqual(battery_hid._find_path(0x1260, 5, None), entries[1]["path"])

    def test_failed_open_still_closes_handle(self):
        handle = Mock()
        handle.open_path.side_effect = OSError("device unplugged")
        with patch.object(battery_hid, "_find_path", return_value=b"vendor"), patch.object(
            battery_hid.hid, "device", return_value=handle
        ):
            self.assertIsNone(battery_hid.read_battery(0x1852))
        handle.close.assert_called_once_with()

    def test_failed_close_does_not_discard_a_valid_reading(self):
        handle = Mock()
        handle.close.side_effect = OSError("device unplugged")
        with patch.object(battery_hid, "_find_path", return_value=b"vendor"), patch.object(
            battery_hid.hid, "device", return_value=handle
        ), patch.object(battery_hid, "_read_opened", return_value=(50, False)):
            self.assertEqual(battery_hid.read_battery(0x1852), (50, False))

    def test_enumeration_failure_is_an_unavailable_reading(self):
        with patch.object(battery_hid.hid, "enumerate", side_effect=OSError("USB unavailable")):
            self.assertIsNone(battery_hid.read_battery(0x1852))

    def test_short_hid_write_is_not_treated_as_a_query(self):
        handle = Mock()
        handle.write.return_value = 1
        with self.assertRaises(OSError):
            battery_hid._write(handle, (0, 0xD2), None)

    def test_windows_hidapi_padding_is_a_successful_write(self):
        handle = Mock()
        handle.write.return_value = 64
        battery_hid._write(handle, (0, 0xD2), None)
        handle.write.assert_called_once_with(bytes([0, 0xD2]))


class BluetoothRegressionTests(unittest.TestCase):
    def test_real_cancellable_helper_keeps_complete_stdout_and_stderr(self):
        completed = battery_bluetooth._run_cancellable(
            [sys.executable, "-c", "import sys; sys.stdout.write('complete'); sys.stderr.write('diagnostic')"],
            getattr(subprocess, "CREATE_NO_WINDOW", 0), threading.Event(),
        )
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, b"complete")
        self.assertTrue(completed.stderr.endswith(b"diagnostic"))

    def test_inherited_output_handles_do_not_delay_helper_cancellation(self):
        stop = threading.Event()
        with tempfile.TemporaryDirectory(prefix="helper_child_") as directory:
            marker = os.path.join(directory, "child.json")
            child_source = (
                "import subprocess,sys,time,json; "
                "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(3)'], "
                "stdout=sys.stdout, stderr=sys.stderr); "
                "open(sys.argv[1],'w').write(json.dumps({'pid':child.pid})); time.sleep(30)"
            )
            timer = threading.Timer(0.5, stop.set)
            timer.start()
            started = time.monotonic()
            try:
                completed = battery_bluetooth._run_cancellable(
                    [sys.executable, "-c", child_source, marker],
                    getattr(subprocess, "CREATE_NO_WINDOW", 0), stop,
                )
                elapsed = time.monotonic() - started
            finally:
                timer.cancel()
                timer.join()
                if sys.platform == "win32" and os.path.isfile(marker):
                    with open(marker, encoding="utf-8") as handle:
                        pid = json.load(handle)["pid"]
                    # This PID belongs only to the child created by our fixture.
                    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
                    kernel.OpenProcess.restype = wintypes.HANDLE
                    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
                    kernel.WaitForSingleObject.restype = wintypes.DWORD
                    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
                    kernel.CloseHandle.restype = wintypes.BOOL
                    child_handle = kernel.OpenProcess(0x00100000, False, pid)
                    if child_handle:
                        try:
                            self.assertEqual(kernel.WaitForSingleObject(child_handle, 5000), 0)
                        finally:
                            kernel.CloseHandle(child_handle)
        self.assertIsNone(completed)
        self.assertLess(elapsed, 2.0)

    def test_helper_timeout_reaps_process_and_closes_capture_files(self):
        stop = threading.Event()
        process = Mock()
        process.poll.return_value = None
        with patch.object(battery_bluetooth.subprocess, "Popen", return_value=process) as popen, patch.object(
            battery_bluetooth, "_POWERSHELL_TIMEOUT_SECONDS", 0
        ):
            self.assertEqual(battery_bluetooth._run_powershell(cancel_event=stop), [])
        process.kill.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=1.0)
        self.assertTrue(popen.call_args.kwargs["stdout"].closed)
        self.assertTrue(popen.call_args.kwargs["stderr"].closed)

    @unittest.skipUnless(shutil.which("powershell"), "PowerShell/WinRT is required for script integration")
    def test_powershell_filters_disconnected_fixture_and_keeps_zero_battery(self):
        fixtures = r"""
function Get-PnpDevice {
    param($Class, [switch]$PresentOnly)
    [pscustomobject]@{FriendlyName='Aerox 5 WL'; InstanceId='BTHENUM\DEV_AABBCCDDEEFF\synthetic'}
    [pscustomobject]@{FriendlyName='Arctis Nova 7'; InstanceId='BTHLE\DEV_112233445566\synthetic'}
}
function Get-PnpDeviceProperty {
    param($InstanceId, $KeyName)
    [pscustomobject]@{Data=0}
}
"""
        # Real WinRT type bindings execute, while PnP and the final connection
        # snapshot use synthetic fixtures, without reading a physical battery.
        script = fixtures + battery_bluetooth._SCRIPT.replace(
            "@(foreach ($device in $devices)",
            "$connectionByAddress = @{'AABBCCDDEEFF'=$true; '112233445566'=$false}\n"
            "@(foreach ($device in $devices)",
        )
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(json.loads(completed.stdout.decode("utf-8-sig")),
                         {"name": "Aerox 5 WL", "battery": 0, "connected": True})

    def test_real_waiting_helper_exits_promptly_on_cancellation(self):
        stop = threading.Event()
        processes = []
        capture_files = []
        real_popen = subprocess.Popen

        def capture_process(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            processes.append(process)
            capture_files.extend([kwargs["stdout"], kwargs["stderr"]])
            return process

        timer = threading.Timer(0.2, stop.set)
        timer.start()
        started = time.monotonic()
        try:
            with patch.object(battery_bluetooth.subprocess, "Popen", side_effect=capture_process):
                completed = battery_bluetooth._run_cancellable(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    getattr(subprocess, "CREATE_NO_WINDOW", 0), stop,
                )
        finally:
            timer.cancel()
            timer.join()
        self.assertIsNone(completed)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].poll())
        self.assertTrue(all(handle.closed for handle in capture_files))
        self.assertIsNone(processes[0].stdout)
        self.assertIsNone(processes[0].stderr)

    @unittest.skipUnless(shutil.which("powershell"), "PowerShell is required for script-boundary test")
    def test_generated_powershell_matches_a_synthetic_pnp_address(self):
        address_line = next(line for line in battery_bluetooth._SCRIPT.splitlines()
                            if "$addressMatch =" in line)
        script = ("$ErrorActionPreference = 'Stop'\n"
                  "$device = [pscustomobject]@{InstanceId='BTHENUM\\DEV_AABBCCDDEEFF\\synthetic'}\n"
                  + address_line + "\n$addressMatch.Groups[1].Value")
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", errors="replace"))
        self.assertEqual(completed.stdout.strip(), b"AABBCCDDEEFF")

    def test_cancelled_bluetooth_read_does_not_start_a_helper(self):
        stop = threading.Event()
        stop.set()
        with patch.object(battery_bluetooth.subprocess, "Popen") as popen:
            self.assertEqual(battery_bluetooth.read_all(cancel_event=stop), [])
        popen.assert_not_called()

    def test_cancelled_helper_is_killed_and_reaped(self):
        stop = threading.Event()
        process = Mock()
        process.poll.return_value = None

        calls = 0

        def wait(timeout=None):
            nonlocal calls
            calls += 1
            if calls == 1:
                stop.set()
                raise subprocess.TimeoutExpired("powershell", timeout)
            return 0

        process.wait.side_effect = wait
        with patch.object(battery_bluetooth.subprocess, "Popen", return_value=process) as popen:
            self.assertEqual(battery_bluetooth._run_powershell(cancel_event=stop), [])
        process.kill.assert_called_once_with()
        self.assertEqual(process.wait.call_count, 2)
        self.assertTrue(popen.call_args.kwargs["stdout"].closed)
        self.assertTrue(popen.call_args.kwargs["stderr"].closed)

    def test_malformed_entries_and_boolean_battery_are_ignored(self):
        entries = [None, 42, "junk", {"name": 5, "battery": 30},
                   {"name": "Aerox 5 WL", "battery": True},
                   {"name": "Aerox 5 WL", "battery": 0}]
        with patch.object(battery_bluetooth, "_run_powershell", return_value=entries):
            devices = battery_bluetooth.read_all()
        self.assertEqual([item["percent"] for item in devices], [0])

    def test_duplicate_bluetooth_pnp_records_produce_one_card(self):
        entries = [{"name": "Aerox 5 WL", "battery": 50},
                   {"name": " aerox 5 wl ", "battery": 50}]
        with patch.object(battery_bluetooth, "_run_powershell", return_value=entries):
            devices = battery_bluetooth.read_all()
        self.assertEqual(len(devices), 1)

    def test_explicitly_disconnected_devices_are_not_online(self):
        entries = [{"name": "Aerox 5 WL", "battery": 50, "connected": False}]
        with patch.object(battery_bluetooth, "_run_powershell", return_value=entries):
            self.assertEqual(battery_bluetooth.read_all(), [])

    def test_malformed_connection_flags_do_not_become_online_devices(self):
        entries = [{"name": "Aerox 5 WL", "battery": 50, "connected": flag}
                   for flag in (0, "false", "true", None)]
        with patch.object(battery_bluetooth, "_run_powershell", return_value=entries):
            self.assertEqual(battery_bluetooth.read_all(), [])

    @unittest.skipUnless(shutil.which("powershell"), "PowerShell is required for script-boundary test")
    def test_generated_powershell_matches_classic_and_ble_top_level_ids(self):
        address_line = next(line for line in battery_bluetooth._SCRIPT.splitlines()
                            if "$addressMatch =" in line)
        script = ("$ErrorActionPreference = 'Stop'\n"
                  "foreach ($identifier in @('BTHENUM\\DEV_AABBCCDDEEFF\\synthetic',"
                  " 'BTHLE\\DEV_aabbccddeeff\\synthetic',"
                  " 'BTHLE\\DEV_AABBCCDDEEFF_01234567\\synthetic')) {\n"
                  "$device = [pscustomobject]@{InstanceId=$identifier}\n"
                  + address_line + "\n$addressMatch.Groups[1].Value.ToUpperInvariant()\n}")
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout.decode("utf-8-sig").split(), ["AABBCCDDEEFF"] * 3)

    def test_nonzero_powershell_exit_does_not_use_partial_output(self):
        completed = subprocess.CompletedProcess([], 1, b'{"name":"Aerox 5 WL","battery":50}')
        with patch.object(battery_bluetooth.subprocess, "run", return_value=completed):
            self.assertEqual(battery_bluetooth._run_powershell(), [])

    def test_utf8_bom_does_not_drop_valid_powershell_json(self):
        completed = subprocess.CompletedProcess([], 0, b'\xef\xbb\xbf{"name":"Aerox 5 WL","battery":50}')
        with patch.object(battery_bluetooth.subprocess, "run", return_value=completed):
            self.assertEqual(battery_bluetooth._run_powershell(), [{"name": "Aerox 5 WL", "battery": 50}])


class OledRegressionTests(unittest.TestCase):
    def setUp(self):
        cached = getattr(oled_render, "_render_frame_cached", None)
        if cached is not None:
            cached.cache_clear()

    def test_external_text_is_bounded_before_font_measurement_and_caching(self):
        lengths = []
        original = oled_render._text_box

        def measure(draw, text, font):
            lengths.append(len(text))
            return original(draw, text, font)

        with patch.object(oled_render, "_text_box", side_effect=measure):
            frame = oled_render.render_frame("\u017b" * 10000, 50, "h" * 10000, used_text="9" * 10000)
        self.assertEqual(len(frame), 640)
        self.assertLessEqual(max(lengths), 256)

    def test_unicode_message_and_repeating_animation_keep_a_bounded_cache(self):
        message = "\u5f69\u8272 \u0141ADOWANIE \U0001f50b " * 10000
        first = oled_render.render_frame(message, None)
        self.assertEqual(len(first), 640)
        for step in range(200):
            self.assertEqual(len(oled_render.render_frame("MYSZ", 75, charging=True, step=step)), 640)
        self.assertEqual(oled_render._render_frame_cached.cache_info().currsize, 11)

    def test_long_text_stays_inside_its_screen_band(self):
        drawn = []
        original = oled_render._draw_centered

        def record(draw, text, font, x, area_top, area_height):
            width, height, _, _ = oled_render._text_box(draw, text, font)
            drawn.append((width, height, x, area_height))
            return original(draw, text, font, x, area_top, area_height)

        with patch.object(oled_render, "_draw_centered", side_effect=record):
            data = oled_render.render_frame("LONG DEVICE " * 30, 100,
                                            "~999999999999h", True, 2, "-9999999999%")
        self.assertEqual(len(data), oled_render.FRAME_BYTES)
        for width, height, x, area_height in drawn:
            self.assertGreaterEqual(x, 0)
            self.assertLessEqual(x + width, oled_render.WIDTH)
            self.assertLessEqual(height, area_height)

    def test_static_frames_are_cached_but_result_lists_are_independent(self):
        with patch.object(oled_render.Image, "new", wraps=oled_render.Image.new) as make:
            first = oled_render.render_frame("MYSZ", 75, "~60h", False, 0, "-25%")
            second = oled_render.render_frame("MYSZ", 75, "~60h", False, 999, "-25%")
        self.assertEqual(make.call_count, 1)
        first[0] ^= 255
        self.assertNotEqual(first, second)

    def test_oversized_frame_is_not_silently_truncated(self):
        with self.assertRaises(ValueError):
            oled_render.frame_to_image([0] * (oled_render.FRAME_BYTES + 1))


class CatalogRegressionTests(unittest.TestCase):
    def test_arctis_pro_runtime_is_for_one_installed_battery(self):
        self.assertEqual(device_catalog.by_key("arctis_pro_wireless").rated_for(device_catalog.LINK_24), 10.0)

    def test_wired_arctis_pro_and_gamedac_do_not_get_battery_queries(self):
        self.assertIsNone(battery_hid._plan_for(0x1252))
        self.assertIsNone(battery_hid._plan_for(0x1280))

    def test_exact_catalog_model_name_is_recognized_over_bluetooth(self):
        self.assertEqual(device_catalog.find_by_bt_name("Arctis Nova 7").key, "arctis_nova_7")
        self.assertEqual(device_catalog.find_by_bt_name("Aerox 5 Wireless").key, "aerox_5_wireless")
        self.assertIsNone(device_catalog.find_by_bt_name("Arctis Nova 7P unknown suffix"))


class ConsoleRegressionTests(unittest.TestCase):
    def test_start_failure_still_stops_created_service(self):
        service = Mock()
        service.start.side_effect = RuntimeError("thread startup failed")
        with patch.object(battery_oled.app_settings, "Settings"), patch.object(
            battery_oled.single_instance, "acquire", return_value=True
        ), patch.object(battery_oled.monitor_service, "MonitorService", return_value=service), patch(
            "builtins.print"
        ):
            with self.assertRaises(RuntimeError):
                battery_oled.main()
        service.stop.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
