"""Service regressions: no hardware, registry changes or user history writes."""
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch

import battery_timeline
import device_catalog as catalog
import monitor_service as monitor


def hid_device(key="aerox_5_wireless", link=catalog.LINK_24):
    device = catalog.by_key(key)
    return dict(pid=next(iter(device.pids[link])), key=key, link=link,
                name=device.display_name(link), model=device.name,
                label=device.label, rated_hours=device.rated_for(link))


def bt_device(key):
    device = catalog.by_key(key)
    return dict(key=key, link=catalog.LINK_BT, name=device.display_name(catalog.LINK_BT),
                model=device.name + " (Bluetooth)", label=device.label,
                rated_hours=device.rated_for(catalog.LINK_BT), percent=70, charging=False)


class MonitorRegressions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.timeline = battery_timeline.Timeline(
            path=os.path.join(self.tmp.name, "history.json"),
            legacy_path=os.path.join(self.tmp.name, "missing.json"))
        for target, name in ((monitor.battery_hid, "discover_devices"),
                             (monitor.battery_bluetooth, "read_all")):
            guard = patch.object(target, name, return_value=[])
            guard.start()
            self.addCleanup(guard.stop)

    def service(self):
        with patch.object(monitor.battery_timeline, "Timeline", return_value=self.timeline), \
             patch.object(monitor.app_settings, "Settings", return_value=Mock(declared=())):
            return monitor.MonitorService()

    def test_hid_preserves_transport(self):
        with patch.object(monitor.battery_hid, "discover_devices", return_value=[hid_device(link=catalog.LINK_WIRED)]), \
             patch.object(monitor.battery_hid, "read_battery", return_value=(90, True)), \
             patch.object(monitor.battery_bluetooth, "read_all", return_value=[]):
            readings = monitor.read_all_batteries(timeline=self.timeline)
        self.assertEqual(readings[0].get("link"), catalog.LINK_WIRED)

    def test_different_mice_are_not_duplicates(self):
        with patch.object(monitor.battery_hid, "discover_devices", return_value=[hid_device()]), \
             patch.object(monitor.battery_hid, "read_battery", return_value=(90, False)), \
             patch.object(monitor.battery_bluetooth, "read_all", return_value=[bt_device("aerox_3_wireless"), bt_device("aerox_5_wireless")]):
            readings = monitor.read_all_batteries(timeline=self.timeline)
        self.assertEqual({r["key"] for r in readings}, {"aerox_5_wireless", "aerox_3_wireless"})

    def test_rejected_glitch_is_not_displayed(self):
        self.timeline.record("aerox_5_wireless", 90, False)
        with patch.object(monitor.battery_hid, "discover_devices", return_value=[hid_device()]), \
             patch.object(monitor.battery_hid, "read_battery", return_value=(10, False)), \
             patch.object(monitor.battery_bluetooth, "read_all", return_value=[]):
            readings = monitor.read_all_batteries(timeline=self.timeline)
        self.assertEqual(readings[0]["percent"], 90)

    def test_broken_hid_does_not_hide_bluetooth(self):
        with patch.object(monitor.battery_hid, "discover_devices", side_effect=OSError("USB disconnected")), \
             patch.object(monitor.battery_bluetooth, "read_all", return_value=[bt_device("aerox_3_wireless")]):
            readings = monitor.read_all_batteries(timeline=self.timeline)
        self.assertEqual(readings[0]["key"], "aerox_3_wireless")

    def test_empty_poll_is_throttled(self):
        service = self.service()
        with patch.object(monitor, "read_all_batteries", return_value=[]) as read, \
             patch.object(monitor.time, "monotonic", return_value=100):
            service._poll_if_due()
            service._poll_if_due()
        self.assertEqual(read.call_count, 1)

    def test_unsupported_card_does_not_force_fast_polling(self):
        service = self.service()
        service._readings = [{"online": True, "supported": True},
                             {"online": False, "supported": False}]
        service._last_poll_monotonic = 100
        with patch.object(monitor, "read_all_batteries", return_value=[]) as read, \
             patch.object(monitor.time, "monotonic", return_value=120):
            self.assertFalse(service._poll_if_due())
        read.assert_not_called()

    def test_frequent_polls_do_not_starve_later_oled_devices(self):
        service = self.service()
        service._readings = [dict(online=True, charging=False, label="DEVICE%d" % index,
                                  percent=80, runtime="") for index in range(4)]
        shown = []
        def display(client, label, *args):
            shown.append(label)
            return len(shown) < 8
        service._show_static = display
        calls = [0]
        def polling():
            calls[0] += 1
            return calls[0] % 3 == 1
        service._poll_if_due = polling
        try:
            service._display_loop(Mock())
        finally:
            service._stop.set()
            if service._poll_thread is not None:
                service._poll_thread.join(1)
        self.assertEqual(set(shown), {"DEVICE0", "DEVICE1", "DEVICE2", "DEVICE3"})

    def test_stop_does_not_hold_control_lock_while_joining_callback(self):
        service = self.service()
        joining = threading.Event()
        completed = threading.Event()
        def run():
            if joining.wait(1):
                service.start()  # a callback may inspect/start the existing worker
                completed.set()
        service._run = run
        service.start()
        worker = service._thread
        original_join = worker.join
        def join(timeout=None):
            joining.set()
            return original_join(timeout)
        worker.join = join
        try:
            self.assertTrue(service.stop(timeout=0.3))
            self.assertTrue(completed.is_set())
        finally:
            joining.set()
            original_join(1)

    def test_failed_worker_start_does_not_claim_to_be_running(self):
        service = self.service()
        with patch.object(monitor.threading, "Thread") as thread:
            thread.return_value.start.side_effect = RuntimeError("cannot create thread")
            with self.assertRaises(RuntimeError):
                service.start()
        self.assertFalse(service.get_snapshot()["running"])
        self.assertIsNone(service._thread)

    def test_preview_rotates_and_animates_without_gamesense(self):
        with patch.object(monitor.time, "monotonic", return_value=0):
            service = self.service()
        service._readings = [dict(online=True, charging=True, label="ONE", percent=80),
                             dict(online=True, charging=False, label="TWO", percent=50)]
        with patch.object(monitor.time, "monotonic", return_value=0):
            service._publish_preview_only()
            first = service.get_snapshot()["frame"]
        with patch.object(monitor.time, "monotonic", return_value=0.3):
            service._publish_preview_only()
            animated = service.get_snapshot()["frame"]
        self.assertNotEqual(first, animated)
        with patch.object(monitor.time, "monotonic", return_value=4):
            service._publish_preview_only()
        self.assertEqual(service.get_snapshot()["frame_label"], "TWO")

    def test_slow_battery_poll_does_not_freeze_oled(self):
        service = self.service()
        service._readings = [dict(online=True, charging=True, label="MYSZ", percent=80)]
        reading_started = threading.Event()
        release_read = threading.Event()
        frame_sent = threading.Event()
        def slow_read(*args, **kwargs):
            reading_started.set()
            release_read.wait(2)
            return []
        client = Mock()
        client.send_frame.side_effect = lambda *args: frame_sent.set()
        with patch.object(monitor, "find_server_address", return_value="127.0.0.1:1234"), \
             patch.object(monitor, "GameSenseClient", return_value=client), \
             patch.object(monitor, "read_all_batteries", side_effect=slow_read):
            service.start()
            try:
                self.assertTrue(reading_started.wait(1))
                self.assertTrue(frame_sent.wait(0.2), "a battery read blocked OLED animation")
            finally:
                release_read.set()
                self.assertTrue(service.stop(timeout=2))

    def test_battery_poll_starts_while_gamesense_registration_waits(self):
        service = self.service()
        reading_started = threading.Event()
        release_registration = threading.Event()
        client = Mock()
        client.register_metadata.side_effect = lambda: release_registration.wait(2)
        def read(*args, **kwargs):
            reading_started.set()
            return []
        with patch.object(monitor, "find_server_address", return_value="127.0.0.1:1234"), \
             patch.object(monitor, "GameSenseClient", return_value=client), \
             patch.object(monitor, "read_all_batteries", side_effect=read):
            service.start()
            try:
                self.assertTrue(reading_started.wait(0.2))
            finally:
                release_registration.set()
                self.assertTrue(service.stop(timeout=2))

    def test_absent_automatic_device_breaks_short_online_gap(self):
        self.timeline.record("aerox_5_wireless", 90, False, now=0)
        with patch.object(monitor.battery_hid, "discover_devices", return_value=[]), \
             patch.object(monitor.battery_bluetooth, "read_all", return_value=[]):
            monitor.read_all_batteries(timeline=self.timeline)
        state = self.timeline.record("aerox_5_wireless", 80, False, now=120)
        self.assertEqual(state.active_seconds, 0)
        self.assertEqual(state.link_drop, 0)

    def test_shutdown_breaks_continuity_before_flushing(self):
        service = self.service()
        self.timeline.record("aerox_5_wireless", 90, False, now=0)
        service._run_loop = service._stop.set
        service._run()
        reopened = battery_timeline.Timeline(path=self.timeline._path, legacy_path="missing")
        state = reopened.record("aerox_5_wireless", 80, False, now=120)
        self.assertEqual(state.active_seconds, 0)
        self.assertEqual(state.link_drop, 0)

    def test_only_one_poll_runs_and_refresh_waits_for_it(self):
        service = self.service()
        reading_started = threading.Event()
        release_read = threading.Event()
        def read(*args, **kwargs):
            reading_started.set()
            release_read.wait(1)
            return []
        with patch.object(monitor, "read_all_batteries", side_effect=read) as read_mock:
            try:
                self.assertTrue(service._schedule_poll_if_due())
                self.assertTrue(reading_started.wait(1))
                service.refresh_now()
                self.assertFalse(service._schedule_poll_if_due())
                release_read.set()
                service._poll_thread.join(1)
                self.assertTrue(service._schedule_poll_if_due())
                service._poll_thread.join(1)
                self.assertEqual(read_mock.call_count, 2)
            finally:
                release_read.set()
                service._stop.set()
                if service._poll_thread is not None:
                    service._poll_thread.join(1)

    def test_pending_refresh_wakes_display_only_once(self):
        service = self.service()
        service.refresh_now()
        with patch.object(service._stop, "wait", return_value=False) as wait:
            with patch.object(monitor.time, "monotonic", return_value=0):
                self.assertTrue(service._sleep(1))
            wait.reset_mock()
            with patch.object(monitor.time, "monotonic", side_effect=[0, 0, .05, .1, .15, .2]):
                self.assertTrue(service._sleep(.2))
            self.assertEqual(wait.call_count, 4)

    def test_poll_callback_can_request_stop_without_joining_its_owner(self):
        service = self.service()
        service._thread = Mock()
        service._thread.is_alive.return_value = True
        service._poll_thread = threading.current_thread()
        self.assertFalse(service.stop(timeout=None))
        service._thread.join.assert_not_called()
        self.assertTrue(service._stop.is_set())

    def test_cancelled_start_does_not_register_gamesense(self):
        service = self.service()
        service._schedule_poll_if_due = service._stop.set
        with patch.object(monitor, "find_server_address") as address:
            service._run()
        address.assert_not_called()

    def test_stop_during_metadata_does_not_bind_another_handler(self):
        service = self.service()
        service._schedule_poll_if_due = lambda: None
        client = Mock()
        client.register_metadata.side_effect = service._stop.set
        with patch.object(monitor, "find_server_address", return_value="127.0.0.1:1234"), \
             patch.object(monitor, "GameSenseClient", return_value=client):
            service._run()
        client.bind_screen_handler.assert_not_called()
        client.close.assert_called_once()

    def test_failed_poll_breaks_history_continuity(self):
        service = self.service()
        self.timeline.record("aerox_5_wireless", 100, False, now=0)
        self.timeline.record("aerox_5_wireless", 100, False, now=60)
        with patch.object(monitor, "read_all_batteries", side_effect=OSError("read failed")):
            service._poll_if_due()
        state = self.timeline.record("aerox_5_wireless", 90, False, now=120)
        self.assertEqual(state.active_seconds, 60)
        self.assertEqual(state.link_drop, 0)

    def test_real_poll_callback_stops_both_workers_without_deadlock(self):
        service = self.service()
        callback_ran = threading.Event()
        def log(message):
            service.stop(timeout=None)
            callback_ran.set()
        service._on_log = log
        with patch.object(monitor, "find_server_address", side_effect=monitor.GameSenseUnavailable("service.gg_missing")), \
             patch.object(monitor, "read_all_batteries", return_value=[]):
            service.start()
            worker = service._thread
            try:
                self.assertTrue(callback_ran.wait(1))
                worker.join(1)
                self.assertFalse(worker.is_alive())
                if service._poll_thread is not None:
                    self.assertFalse(service._poll_thread.is_alive())
            finally:
                service.stop(timeout=1)

    def test_restart_after_shutdown_uses_one_new_poll_worker(self):
        service = self.service()
        with patch.object(monitor, "find_server_address", side_effect=monitor.GameSenseUnavailable("service.gg_missing")), \
             patch.object(monitor, "read_all_batteries", return_value=[]):
            for _ in range(3):
                service.start()
                service.refresh_now()
                with patch.object(monitor.app_settings, "Settings", return_value=Mock(declared=())):
                    service.reload_settings()
                service.set_paused(True)
                self.assertTrue(service.stop(timeout=1))
                self.assertFalse(service.get_snapshot()["running"])
                if service._poll_thread is not None:
                    self.assertFalse(service._poll_thread.is_alive())
                service.set_paused(False)

    def test_failed_poll_clears_online_snapshot_and_retries_soon(self):
        service = self.service()
        readings = [dict(percent=80, online=True, charging=False, supported=True)]
        service._readings = readings
        service._update(readings=readings, readings_revision=1)
        with patch.object(monitor, "read_all_batteries", side_effect=OSError("transport failed")), \
             patch.object(monitor.time, "monotonic", return_value=120):
            service._poll_if_due()
        snapshot = service.get_snapshot()
        self.assertFalse(snapshot["readings"][0]["online"])
        self.assertIsNone(snapshot["readings"][0]["percent"])
        self.assertEqual(snapshot["readings_revision"], 2)
        self.assertTrue(service._wake.is_set())
        with patch.object(monitor.time, "monotonic", return_value=130):
            self.assertTrue(service._poll_due())

    def test_failed_old_poll_does_not_replace_new_settings_snapshot(self):
        service = self.service()
        service._readings = [dict(percent=80, online=True, charging=False, supported=True)]
        def fail(*args, **kwargs):
            with patch.object(monitor.app_settings, "Settings", return_value=Mock(declared=("arctis_nova_7",))):
                service.reload_settings()
            raise OSError("old selection failed")
        with patch.object(monitor, "read_all_batteries", side_effect=fail):
            service._poll_if_due()
        snapshot = service.get_snapshot()
        self.assertEqual(snapshot["readings"], [])
        self.assertEqual(snapshot["readings_revision"], 1)
        self.assertTrue(service._refresh.is_set())

    def test_poll_failure_does_not_escape_idle_recovery(self):
        service = self.service()
        with patch.object(monitor, "read_all_batteries", side_effect=OSError("read failed")):
            service._poll_if_due()
        self.assertTrue(service.get_snapshot()["status_key"])

    def test_stop_timeout_keeps_live_worker(self):
        service = self.service()
        worker = Mock()
        worker.is_alive.return_value = True
        service._thread = worker
        service._update(running=True)
        stopped = service.stop(timeout=0)
        self.assertIs(service._thread, worker)
        self.assertFalse(stopped)
        self.assertTrue(service.get_snapshot()["running"])

    def test_snapshot_cannot_modify_internal_state(self):
        service = self.service()
        service._update(readings=[{"percent": 90}], status_params={"address": "localhost:1234"})
        snapshot = service.get_snapshot()
        snapshot["readings"][0]["percent"] = 1
        snapshot["status_params"]["address"] = "other"
        self.assertEqual(service.get_snapshot()["readings"][0]["percent"], 90)
        self.assertEqual(service.get_snapshot()["status_params"]["address"], "localhost:1234")

    def test_settings_reload_discards_inflight_result(self):
        service = self.service()
        def old_poll(*args, **kwargs):
            with patch.object(monitor.app_settings, "Settings", return_value=Mock(declared=("arctis_nova_7",))):
                service.reload_settings()
            return [{"key": "aerox_5_wireless", "online": True}]
        with patch.object(monitor, "read_all_batteries", side_effect=old_poll):
            service._poll_if_due()
        self.assertEqual(service.get_snapshot()["readings"], [])
        self.assertTrue(service._refresh.is_set())

    def test_invalid_coreprops_is_classified_as_unavailable(self):
        directory = os.path.join(self.tmp.name, "SteelSeries", "SteelSeries Engine 3")
        os.makedirs(directory)
        path = os.path.join(directory, "coreProps.json")
        for value in ([], {"address": []}, {"address": "localhost:noport"}):
            with self.subTest(value=value), open(path, "w", encoding="utf-8") as handle:
                json.dump(value, handle)
            with patch.dict(os.environ, PROGRAMDATA=self.tmp.name):
                with self.assertRaises(monitor.GameSenseUnavailable):
                    monitor.find_server_address()

    def test_gamesense_session_is_closed_after_failure(self):
        service = self.service()
        client = Mock()
        client.register_metadata.side_effect = monitor.requests.ConnectionError("GG stopped")
        service._idle_loop = lambda seconds: service._stop.set()
        with patch.object(monitor, "find_server_address", return_value="127.0.0.1:1234"), \
             patch.object(monitor, "GameSenseClient", return_value=client):
            service._run()
        client.close.assert_called_once()

    def test_slow_gamesense_does_not_send_animation_backlog(self):
        service = self.service()
        tick = [0.0]
        client = Mock()
        def slow_send(*args):
            tick[0] += 3.0
        client.send_frame.side_effect = slow_send
        with patch.object(monitor.time, "monotonic", side_effect=lambda: tick[0]):
            service._show_animated(client, "MYSZ", 90, "", 4, 0)
        self.assertLessEqual(client.send_frame.call_count, 2)

    def test_start_cannot_revive_worker_still_stopping(self):
        service = self.service()
        worker = Mock()
        worker.is_alive.return_value = True
        service._thread = worker
        service.stop(timeout=0)
        with patch.object(monitor.threading, "Thread") as factory:
            service.start()
        factory.assert_not_called()
        self.assertTrue(service._stop.is_set())

    def test_cancelled_poll_does_not_read_hardware(self):
        cancelled = threading.Event()
        cancelled.set()
        with patch.object(monitor.battery_hid, "discover_devices") as hid, \
             patch.object(monitor.battery_bluetooth, "read_all") as bt:
            self.assertEqual(monitor.read_all_batteries(timeline=self.timeline, cancel_event=cancelled), [])
        hid.assert_not_called()
        bt.assert_not_called()

    def test_offline_poll_budget(self):
        service = self.service()
        tick = [0.0]
        with patch.object(monitor, "read_all_batteries", return_value=[]) as read, \
             patch.object(monitor.time, "monotonic", side_effect=lambda: tick[0]):
            for tick[0] in range(61):
                service._poll_if_due()
        self.assertEqual(read.call_count, 7)

    @unittest.skipUnless(os.environ.get("BATTERY_MONITOR_HTTP_SMOKE") == "1",
                         "set BATTERY_MONITOR_HTTP_SMOKE=1 to test loopback HTTP")
    def test_live_gamesense_http_lifecycle(self):
        requests = []
        frame_received = threading.Event()
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append((self.path, data))
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()
                if self.path == "/game_event":
                    frame_received.set()
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        service = self.service()
        logs = []
        service._on_log = logs.append
        try:
            with patch.object(monitor, "find_server_address", return_value="127.0.0.1:%d" % server.server_port), \
                 patch.object(monitor, "read_all_batteries", return_value=[]):
                service.start()
                self.assertTrue(frame_received.wait(2), "registration and frame were not sent: " + repr(logs))
                self.assertTrue(service.stop(timeout=2))
            self.assertEqual([path for path, data in requests][:3],
                             ["/game_metadata", "/bind_game_event", "/game_event"])
            self.assertEqual(requests[0][1]["deinitialize_timer_length_ms"], 60000)
            event = requests[2][1]
            self.assertEqual(len(event["data"]["frame"]["image-data-128x40"]), 640)
            self.assertFalse(service.is_running())
            self.assertFalse(service.get_snapshot()["connected"])
        finally:
            service.stop(timeout=2)
            server.shutdown()
            server.server_close()
            worker.join(2)


if __name__ == "__main__":
    unittest.main()
