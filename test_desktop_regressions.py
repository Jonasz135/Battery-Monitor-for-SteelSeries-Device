"""Regressions for desktop lifecycle and persistence, without UI or real data.

Run with ``python -m unittest test_desktop_regressions``.
Windows registry, tray, mutex, widgets and hardware are mocked.
"""

import ctypes
import json
import os
from pathlib import Path
import queue
import stat
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

import paths

# Import-time migration and default paths stay inside an isolated directory.
_IMPORT_DATA = tempfile.TemporaryDirectory(prefix="desktop_import_")
with mock.patch.object(paths, "_data_dir", _IMPORT_DATA.name):
    import app
    import autostart
    import build
    import i18n
    import settings
    import single_instance


class SettingsRegressions(unittest.TestCase):
    def test_separate_instances_preserve_each_others_fields(self):
        devices = settings.Settings(str(self.path))
        language = settings.Settings(str(self.path))
        devices.set_declared(["mouse"])
        language.set_language("en")
        current = settings.Settings(str(self.path))
        self.assertEqual(current.declared, ("mouse",))
        self.assertEqual(current.language, "en")

    def test_parallel_instances_merge_updates(self):
        devices = settings.Settings(str(self.path))
        language = settings.Settings(str(self.path))
        start = threading.Barrier(3)
        errors = []

        def save(configuration, operation, value):
            try:
                start.wait(1)
                self.assertTrue(getattr(configuration, operation)(value))
            except Exception as error:
                errors.append(error)

        threads = [
            threading.Thread(target=save, args=(devices, "set_declared", ["mouse"])),
            threading.Thread(target=save, args=(language, "set_language", "en")),
        ]
        for thread in threads:
            thread.start()
        start.wait(1)
        for thread in threads:
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        current = settings.Settings(str(self.path))
        self.assertEqual((current.declared, current.language), (("mouse",), "en"))

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="desktop_settings_")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "settings.json"

    def test_unhashable_language_in_json_falls_back(self):
        for value in ([], {}, ["pl"]):
            with self.subTest(value=value):
                self.path.write_text(json.dumps({"language": value}), encoding="utf-8")
                self.assertEqual(settings.Settings(str(self.path)).language, i18n.AUTO)

    def test_invalid_utf8_falls_back(self):
        self.path.write_bytes(b"\xff\xfe\x80")
        self.assertTrue(settings.Settings(str(self.path)).automatic)

    def test_loaded_devices_are_deduplicated(self):
        self.path.write_text(json.dumps({"declared": ["a", "a", "b", 7]}), encoding="utf-8")
        self.assertEqual(settings.Settings(str(self.path)).declared, ("a", "b"))

    def test_pathlike_and_missing_parent_can_be_saved(self):
        destination = self.path.parent / "nested" / "settings.json"
        config = settings.Settings(destination)
        self.assertTrue(config.set_language("en"))
        self.assertEqual(settings.Settings(destination).language, "en")

    def test_failed_replace_keeps_memory_and_disk_consistent(self):
        config = settings.Settings(str(self.path))
        config.set_declared(["old"])
        previous = self.path.read_bytes()
        with mock.patch.object(settings.os, "replace", side_effect=PermissionError("locked")):
            self.assertFalse(config.set_declared(["new"]))
        self.assertEqual(config.declared, ("old",))
        self.assertEqual(self.path.read_bytes(), previous)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_setter_tolerates_invalid_language_type(self):
        config = settings.Settings(str(self.path))
        self.assertTrue(config.set_language([]))
        self.assertEqual(config.language, i18n.AUTO)


class PathRegressions(unittest.TestCase):
    def test_frozen_migration_uses_legacy_file_beside_executable(self):
        with tempfile.TemporaryDirectory(prefix="desktop_frozen_") as directory:
            root = Path(directory)
            executable = root / "old-install"
            extraction = root / "_MEI123"
            destination = root / "data"
            for folder in (executable, extraction, destination):
                folder.mkdir()
            legacy = executable / "settings.json"
            legacy.write_text('{"language":"en"}', encoding="utf-8")
            with mock.patch.object(paths, "_SOURCE_DIR", str(extraction)), mock.patch.object(paths.sys, "frozen", True, create=True), mock.patch.object(paths.sys, "executable", str(executable / "BatteryMonitor.exe")), mock.patch.object(paths, "_data_dir", str(destination)):
                target = paths.data_file("settings.json")
            self.assertEqual(Path(target).read_bytes(), legacy.read_bytes())

    def test_migrated_readonly_source_does_not_make_settings_readonly(self):
        with tempfile.TemporaryDirectory(prefix="desktop_readonly_source_") as directory:
            root = Path(directory)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            destination.mkdir()
            legacy = source / "settings.json"
            target = destination / "settings.json"
            legacy.write_text('{"language":"pl"}', encoding="utf-8")
            legacy.chmod(stat.S_IREAD)
            try:
                with mock.patch.object(paths, "_SOURCE_DIR", str(source)), mock.patch.object(paths.sys, "frozen", False, create=True):
                    paths._adopt_from_source("settings.json", str(target))
                self.assertTrue(settings.Settings(target).set_language("en"))
            finally:
                for file in (legacy, target):
                    if file.exists():
                        file.chmod(stat.S_IWRITE | stat.S_IREAD)

    def test_migration_cannot_overwrite_file_created_by_another_instance(self):
        with tempfile.TemporaryDirectory(prefix="desktop_migration_race_") as directory:
            root = Path(directory)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            destination.mkdir()
            (source / "settings.json").write_text('{"language":"pl"}', encoding="utf-8")
            target = destination / "settings.json"
            real_copy = paths.shutil.copyfile

            def copy_then_new_settings(original, staging):
                result = real_copy(original, staging)
                target.write_text('{"language":"en"}', encoding="utf-8")
                return result

            with mock.patch.object(paths, "_SOURCE_DIR", str(source)), mock.patch.object(paths.sys, "frozen", False, create=True), mock.patch.object(paths.shutil, "copyfile", side_effect=copy_then_new_settings):
                paths._adopt_from_source("settings.json", str(target))
            self.assertEqual(settings.Settings(target).language, "en")

    def test_existing_readonly_directory_uses_writable_fallback(self):
        with tempfile.TemporaryDirectory(prefix="desktop_readonly_") as directory:
            roaming = Path(directory) / "roaming"
            local = Path(directory) / "local"
            (roaming / paths.APP_DIR_NAME).mkdir(parents=True)
            original_temporary_file = tempfile.NamedTemporaryFile

            def probe(*args, **kwargs):
                if kwargs.get("dir") == str(roaming / paths.APP_DIR_NAME):
                    raise PermissionError("existing roaming directory is read-only")
                return original_temporary_file(*args, **kwargs)

            with mock.patch.object(paths, "_data_dir", None), mock.patch.dict(os.environ, {"APPDATA": str(roaming), "LOCALAPPDATA": str(local)}), mock.patch.object(paths.tempfile, "NamedTemporaryFile", side_effect=probe):
                self.assertEqual(paths.data_dir(), str(local / paths.APP_DIR_NAME))
            self.assertEqual(list((local / paths.APP_DIR_NAME).iterdir()), [])

    def test_fallback_directory_survives_frozen_extraction_cleanup(self):
        with tempfile.TemporaryDirectory(prefix="desktop_paths_") as directory:
            roaming = os.path.join(directory, "denied")
            local = os.path.join(directory, "local")
            extraction = os.path.join(directory, "_MEI123")
            real_makedirs = os.makedirs

            def create(path, **kwargs):
                if path.startswith(roaming):
                    raise PermissionError("roaming denied")
                return real_makedirs(path, **kwargs)

            with mock.patch.object(paths, "_data_dir", None), mock.patch.object(paths, "_SOURCE_DIR", extraction), mock.patch.dict(os.environ, {"APPDATA": roaming, "LOCALAPPDATA": local}), mock.patch.object(paths.os, "makedirs", side_effect=create):
                self.assertEqual(paths.data_dir(), os.path.join(local, paths.APP_DIR_NAME))

    def test_failed_migration_leaves_no_partial_target(self):
        with tempfile.TemporaryDirectory(prefix="desktop_migrate_") as directory:
            source = os.path.join(directory, "source")
            destination = os.path.join(directory, "destination")
            os.makedirs(source)
            os.makedirs(destination)
            Path(source, "settings.json").write_text('{"language":"en"}', encoding="utf-8")
            target = os.path.join(destination, "settings.json")

            def broken_copy(source_path, target_path):
                Path(target_path).write_text('{"language":', encoding="utf-8")
                raise OSError("interrupted migration")

            with mock.patch.object(paths, "_SOURCE_DIR", source), mock.patch.object(paths.shutil, "copyfile", side_effect=broken_copy):
                paths._adopt_from_source("settings.json", target)
            self.assertFalse(os.path.exists(target))
            self.assertEqual(os.listdir(destination), [])


class NativeFunction:
    """Model ctypes' default c_int truncation on 64-bit Windows."""
    def __init__(self, value=None):
        self.value = value
        self.restype = ctypes.c_int
        self.argtypes = None
        self.calls = []

    def __call__(self, *arguments):
        self.calls.append(arguments)
        if self.restype is ctypes.c_int and isinstance(self.value, int):
            return ctypes.c_int(self.value).value
        return self.value


class SingleInstanceRegressions(unittest.TestCase):
    def test_mutex_preserves_full_64_bit_handle(self):
        handle = 0x123456789AB
        kernel = SimpleNamespace(CreateMutexW=NativeFunction(handle), CloseHandle=NativeFunction(True))
        with mock.patch.object(single_instance, "_handle", None), mock.patch.object(single_instance.ctypes, "WinDLL", return_value=kernel), mock.patch.object(single_instance.ctypes, "get_last_error", return_value=0):
            self.assertTrue(single_instance.acquire())
            self.assertEqual(single_instance._handle, handle)

    def test_duplicate_closes_its_handle_without_replacing_owner(self):
        handle = 0x123456789AB
        kernel = SimpleNamespace(CreateMutexW=NativeFunction(handle), CloseHandle=NativeFunction(True))
        with mock.patch.object(single_instance, "_handle", 42), mock.patch.object(single_instance.ctypes, "WinDLL", return_value=kernel), mock.patch.object(single_instance.ctypes, "get_last_error", return_value=183):
            self.assertFalse(single_instance.acquire())
            self.assertEqual(single_instance._handle, 42)
            self.assertEqual(kernel.CloseHandle.calls, [(handle,)])


class DesktopRegressions(unittest.TestCase):
    def test_returning_tray_backend_restores_access_to_app(self):
        shell = SimpleNamespace(_quitting=False, _tray_ready=threading.Event(), _ui_queue=queue.Queue(), _tray=mock.Mock(), _handle_tray_failure=mock.Mock())
        shell._tray_ready.set()
        app.App._run_tray(shell)
        self.assertFalse(shell._tray_ready.is_set())
        self.assertEqual(shell._ui_queue.qsize(), 1)
        shell._ui_queue.get_nowait()()
        shell._handle_tray_failure.assert_called_once()

    def test_waiting_for_shutdown_cancels_pending_retry_before_destroy(self):
        shell = SimpleNamespace(_shutdown_complete=False, _shutdown_id="retry", _tray=mock.Mock(), service=mock.Mock(), destroy=mock.Mock(), after_cancel=mock.Mock())
        shell.service.stop.return_value = True
        shell._finalize_shutdown = lambda: app.App._finalize_shutdown(shell)
        self.assertTrue(app.App.wait_for_shutdown(shell))
        shell.service.stop.assert_called_once_with(timeout=None)
        shell.after_cancel.assert_called_once_with("retry")
        shell.destroy.assert_called_once()
        self.assertTrue(shell._shutdown_complete)

    def test_worker_timeout_does_not_destroy_gui_or_stop_tray(self):
        shell = SimpleNamespace(
            _quitting=False, _tick_id=None, _shutdown_id=None,
            _shutdown_complete=False, _tray=mock.Mock(), service=mock.Mock(),
            destroy=mock.Mock(), after=mock.Mock(return_value="retry"),
        )
        shell.service.stop.return_value = False
        shell._finish_shutdown = lambda: app.App._finish_shutdown(shell)
        app.App.quit_app(shell)
        shell.destroy.assert_not_called()
        shell._tray.stop.assert_not_called()
        self.assertTrue(shell.after.called)

    def test_setup_after_quit_does_not_make_tray_visible_again(self):
        shell = SimpleNamespace(_quitting=True, _tray_ready=threading.Event(), _ui_queue=queue.Queue())
        icon = SimpleNamespace(visible=False)
        app.App._setup_tray(shell, icon)
        self.assertFalse(icon.visible)
        self.assertFalse(shell._tray_ready.is_set())

    def test_closing_window_during_tray_setup_is_honored_after_setup(self):
        shell = SimpleNamespace(_quitting=False, _hide_requested=False, _tray_ready=threading.Event(), _ui_queue=queue.Queue(), withdraw=mock.Mock(), _tray=mock.Mock())
        shell.hide_to_tray = lambda: app.App.hide_to_tray(shell)
        app.App.hide_to_tray(shell)
        shell.withdraw.assert_not_called()
        app.App._setup_tray(shell, SimpleNamespace(visible=False))
        while not shell._ui_queue.empty():
            shell._ui_queue.get_nowait()()
        shell.withdraw.assert_called_once()

    def test_picker_respects_current_monitor_work_area_and_dpi(self):
        picker = SimpleNamespace(
            WIDTH=470, HEIGHT=640, update_idletasks=mock.Mock(), geometry=mock.Mock(),
            winfo_screenwidth=lambda: 1920, winfo_screenheight=lambda: 1080,
        )
        master = SimpleNamespace(winfo_x=lambda: -1500, winfo_y=lambda: 10, winfo_width=lambda: 700, winfo_height=lambda: 500)
        with mock.patch.object(app, "_monitor_work_area", return_value=(-1600, 0, 0, 800)), mock.patch.object(app.ctk.ScalingTracker, "get_window_scaling", return_value=1.5):
            app.DevicePicker._place_over(picker, master)
        geometry = picker.geometry.call_args.args[0]
        width, height, x, y = app.ctk.CTk._parse_geometry_string(geometry)
        self.assertLess(x, 0)
        self.assertGreaterEqual(x, -1600)
        self.assertLessEqual(height * 1.5 + 48 * 1.5, 800)
        self.assertGreaterEqual(y, 0)
        self.assertLessEqual(y + height * 1.5 + 48 * 1.5, 800)

    def test_picker_targets_parent_dpi_before_its_own_window_moves_monitors(self):
        picker = SimpleNamespace(WIDTH=470, HEIGHT=640, update_idletasks=mock.Mock(), geometry=mock.Mock(), winfo_screenwidth=lambda: 1920, winfo_screenheight=lambda: 1080)
        master = SimpleNamespace(winfo_x=lambda: -1500, winfo_y=lambda: 10, winfo_width=lambda: 700, winfo_height=lambda: 500)
        with mock.patch.object(app, "_monitor_work_area", return_value=(-1600, 0, 0, 800)), mock.patch.object(app.ctk.ScalingTracker, "get_window_scaling", side_effect=lambda window: 1.5 if window is master else 1.0):
            app.DevicePicker._place_over(picker, master)
        width, height, x, y = app.ctk.CTk._parse_geometry_string(picker.geometry.call_args.args[0])
        self.assertLessEqual(height * 1.5 + 48 * 1.5, 800)

    def test_window_moving_to_another_monitor_recalculates_layout_without_new_readings(self):
        shell = self.shell()
        shell._last_layout_check = -1e10
        shell._last_layout_key = ((0, 0, 1920, 1080), 1.0, 1.0)
        shell._tick_id = None
        shell.state = mock.Mock(return_value="normal")
        shell._drain_ui_actions = mock.Mock()
        shell._drain_log = mock.Mock()
        shell._update_status = mock.Mock()
        shell._update_devices = mock.Mock()
        shell._update_preview = mock.Mock()
        shell._tick = mock.Mock()
        shell.after = mock.Mock()
        shell._check_display_layout = lambda: app.App._check_display_layout(shell)
        with mock.patch.object(app, "_monitor_work_area", return_value=(-1600, 0, 0, 800)), mock.patch.object(app.ctk.ScalingTracker, "get_window_scaling", return_value=1.5), mock.patch.object(app.ctk.ScalingTracker, "get_widget_scaling", return_value=1.5):
            app.App._tick(shell)
        shell._autosize.assert_called_once()

    def shell(self):
        shell = SimpleNamespace(
            _quitting=False,
            _ui_queue=queue.Queue(),
            _log_queue=queue.Queue(),
            _cards={},
            _last_readings=None,
            _last_readings_revision=None,
            _card_count=0,
            _log_visible=False,
            toggle_log=mock.Mock(),
            devices=object(),
            fonts={},
            settings=mock.Mock(),
            service=mock.Mock(),
            empty_label=mock.Mock(),
            _autosize=mock.Mock(),
            _update_tray_tooltip=mock.Mock(),
            after=mock.Mock(side_effect=AssertionError("Tk called from tray thread")),
            show_window=mock.Mock(),
            refresh_now=mock.Mock(),
            quit_app=mock.Mock(),
        )
        shell.settings.set_declared.return_value = True
        return shell

    def test_tray_callbacks_enqueue_without_touching_tk(self):
        shell = self.shell()
        errors = []

        def tray_actions():
            try:
                app.App._tray_show(shell)
                app.App._tray_refresh(shell)
                app.App._tray_quit(shell)
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=tray_actions)
        thread.start()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(shell._ui_queue.qsize(), 3)
        for _ in range(3):
            shell._ui_queue.get_nowait()()
        shell.show_window.assert_called_once()
        shell.refresh_now.assert_called_once()
        shell.quit_app.assert_called_once()

    def test_changed_declaration_destroys_old_cards(self):
        shell = self.shell()
        card = mock.Mock()
        shell._cards = {"old": card}
        self.assertTrue(app.App._apply_declaration(shell, ["new"]))
        card.destroy.assert_called_once()
        self.assertEqual(shell._cards, {})
        shell.service.reload_settings.assert_called_once()

    def test_failed_declaration_does_not_discard_cards_or_reload(self):
        shell = self.shell()
        card = mock.Mock()
        shell._cards = {"old": card}
        shell.settings.set_declared.return_value = False
        self.assertFalse(app.App._apply_declaration(shell, ["new"]))
        self.assertEqual(shell._cards, {"old": card})
        card.destroy.assert_not_called()
        shell.service.reload_settings.assert_not_called()

    def test_picker_stays_open_when_saving_failed(self):
        picker = SimpleNamespace(_on_save=mock.Mock(return_value=False), _selected=lambda: ["new"], destroy=mock.Mock(), _warning=mock.Mock())
        app.DevicePicker._save(picker)
        picker.destroy.assert_not_called()
        picker._warning.configure.assert_called_once_with(text=i18n.t("log.settings_failed"))

    def test_disappeared_cards_are_destroyed(self):
        shell = self.shell()
        card = mock.Mock()
        shell._cards = {"gone": card}
        app.App._update_devices(shell, {"readings": [], "readings_revision": 1})
        self.assertEqual(shell._cards, {})
        card.destroy.assert_called_once()

    def test_unchanged_revision_does_not_redraw_snapshot_copies(self):
        shell = self.shell()
        reading = {"model": "mouse", "label": "MYSZ", "percent": 60, "charging": False}
        card = mock.Mock()
        with mock.patch.object(app, "DeviceCard", return_value=card):
            app.App._update_devices(shell, {"readings": [dict(reading)], "readings_revision": 1})
            app.App._update_devices(shell, {"readings": [dict(reading)], "readings_revision": 1})
        card.update_reading.assert_called_once()

    def test_equivalent_frames_do_not_allocate_another_photo(self):
        frame = tuple([0] * 640)
        shell = SimpleNamespace(_last_frame=frame, preview=mock.Mock(), _preview_image=object())
        with mock.patch.object(app.ImageTk, "PhotoImage") as photo:
            app.App._update_preview(shell, {"frame": tuple(list(frame))})
        photo.assert_not_called()

    def test_quit_is_idempotent(self):
        shell = SimpleNamespace(_quitting=False, _tick_id=None, _shutdown_id=None, _shutdown_complete=False, _tray=mock.Mock(), service=mock.Mock(), destroy=mock.Mock())
        shell._finish_shutdown = lambda: app.App._finish_shutdown(shell)
        shell._finalize_shutdown = lambda: app.App._finalize_shutdown(shell)
        app.App.quit_app(shell)
        app.App.quit_app(shell)
        shell.service.stop.assert_called_once()
        shell.destroy.assert_called_once()

    def test_hidden_window_skips_rendering_and_uses_slower_tick(self):
        shell = self.shell()
        shell._tick_id = "old"
        shell._drain_ui_actions = mock.Mock()
        shell._drain_log = mock.Mock()
        shell.state = mock.Mock(return_value="withdrawn")
        shell.service.get_snapshot.return_value = {"readings": []}
        shell._update_status = mock.Mock()
        shell._update_devices = mock.Mock()
        shell._update_preview = mock.Mock()
        shell.after = mock.Mock(return_value="next")
        shell._tick = mock.Mock()
        app.App._tick(shell)
        shell._update_preview.assert_not_called()
        shell._update_status.assert_not_called()
        shell._update_devices.assert_not_called()
        shell.after.assert_called_once_with(app.TRAY_TICK_MS, shell._tick)

    def test_quit_action_stops_tick_before_accessing_destroyed_widgets(self):
        shell = self.shell()
        shell._tick_id = "old"
        shell._drain_log = mock.Mock()

        def quit_now():
            shell._quitting = True

        shell._ui_queue.put(quit_now)
        shell._drain_ui_actions = lambda: app.App._drain_ui_actions(shell)
        app.App._tick(shell)
        shell.service.get_snapshot.assert_not_called()
        shell._drain_log.assert_not_called()
        shell.after.assert_not_called()

    def test_failed_language_save_preserves_current_language(self):
        shell = self.shell()
        original = i18n.language()
        replacement = "en" if original == "pl" else "pl"
        shell.settings.set_language.return_value = False
        shell.language_switch = mock.Mock()
        shell.retranslate = mock.Mock()
        app.App.set_language(shell, replacement)
        self.assertEqual(i18n.language(), original)
        shell.retranslate.assert_not_called()
        shell.language_switch.set.assert_called_once_with(i18n.LANGUAGE_NAMES[original])

    def test_tray_failure_restores_access_to_hidden_application(self):
        shell = self.shell()
        shell._tray_ready = threading.Event()
        shell._tray_ready.set()
        shell.protocol = mock.Mock()
        app.App._handle_tray_failure(shell, "backend failed")
        self.assertFalse(shell._tray_ready.is_set())
        shell.show_window.assert_called_once()
        shell.protocol.assert_called_once_with("WM_DELETE_WINDOW", shell.quit_app)

    def test_mainloop_error_releases_mutex_and_stops_application(self):
        instance = mock.Mock()
        instance.mainloop.side_effect = RuntimeError("event loop failed")
        with mock.patch.object(app.single_instance, "acquire", return_value=True), mock.patch.object(app.single_instance, "release") as release, mock.patch.object(app.ctk, "set_appearance_mode"), mock.patch.object(app, "App", return_value=instance):
            with self.assertRaisesRegex(RuntimeError, "event loop failed"):
                app.main(["--tray"])
        instance.quit_app.assert_called_once()
        release.assert_called_once()

    def test_mainloop_error_waits_for_worker_before_releasing_mutex(self):
        implementation = app.App
        events = []
        instance = SimpleNamespace(
            _quitting=False, _shutdown_complete=False, _shutdown_id=None, _tick_id=None,
            _tray=mock.Mock(), service=mock.Mock(), after=mock.Mock(return_value="retry"),
            after_cancel=mock.Mock(), destroy=lambda: events.append("destroy"),
            mainloop=mock.Mock(side_effect=RuntimeError("loop failed")),
        )
        instance.service.stop.side_effect = lambda timeout: (events.append(("stop", timeout)), timeout is None)[1]
        instance.quit_app = lambda: implementation.quit_app(instance)
        instance._finish_shutdown = lambda: implementation._finish_shutdown(instance)
        instance._finalize_shutdown = lambda: implementation._finalize_shutdown(instance)
        instance.wait_for_shutdown = lambda: implementation.wait_for_shutdown(instance)
        with mock.patch.object(app.single_instance, "acquire", return_value=True), mock.patch.object(app.single_instance, "release", side_effect=lambda: events.append("release")), mock.patch.object(app.ctk, "set_appearance_mode"), mock.patch.object(app, "App", return_value=instance):
            with self.assertRaisesRegex(RuntimeError, "loop failed"):
                app.main(["--tray"])
        self.assertEqual(events, [("stop", 0), ("stop", None), "destroy", "release"])
        instance.after_cancel.assert_called_once_with("retry")


@unittest.skipUnless(os.environ.get("BATTERY_MONITOR_GUI_SMOKE") == "1", "Set BATTERY_MONITOR_GUI_SMOKE=1 for a hidden real-Tk smoke test")
class RealGuiSmoke(unittest.TestCase):
    def test_synchronous_tray_failure_keeps_gui_available(self):
        snapshot = {"connected": False, "running": True, "paused": False, "status_key": "service.not_started", "status_params": {}, "readings": [], "readings_revision": 0, "frame": None, "last_poll": None}
        service = mock.Mock()
        service.get_snapshot.return_value = snapshot
        service.is_paused.return_value = False
        with tempfile.TemporaryDirectory(prefix="desktop_tray_init_") as directory:
            configuration = settings.Settings(Path(directory) / "settings.json")
            with mock.patch.object(app.app_settings, "Settings", return_value=configuration), mock.patch.object(app.monitor_service, "MonitorService", return_value=service), mock.patch.object(app.App, "_build_tray", side_effect=RuntimeError("tray unavailable")), mock.patch.object(app.autostart, "is_enabled", return_value=False), mock.patch.object(app.autostart, "repair_if_stale", return_value=False), mock.patch.object(app.App, "_apply_icon"), mock.patch.object(app.App, "show_window") as show:
                instance = app.App(start_hidden=True)
                try:
                    self.assertIsNone(instance._tray)
                    self.assertIsNone(instance._tray_thread)
                    show.assert_called_once()
                    service.start.assert_called_once()
                finally:
                    instance.quit_app()
                service.stop.assert_called_once_with(timeout=0)
                self.assertFalse(instance.tk.call("after", "info"))

    def test_hidden_gui_picker_language_refresh_and_shutdown(self):
        original_language = i18n.language()
        self.addCleanup(i18n.set_language, original_language)
        snapshot = {
            "connected": False, "running": True, "paused": False,
            "status_key": "service.not_started", "status_params": {},
            "readings": [], "readings_revision": 0, "frame": None, "last_poll": None,
        }
        service = mock.Mock()
        service.get_snapshot.side_effect = lambda: dict(snapshot)
        service.is_paused.return_value = False
        tray = mock.Mock()
        tray_stopped = threading.Event()
        tray.run.side_effect = lambda setup: (setup(tray), tray_stopped.wait())
        tray.stop.side_effect = tray_stopped.set
        with tempfile.TemporaryDirectory(prefix="desktop_real_gui_") as directory:
            configuration = settings.Settings(Path(directory) / "settings.json")
            with mock.patch.object(app.app_settings, "Settings", return_value=configuration), mock.patch.object(app.monitor_service, "MonitorService", return_value=service), mock.patch.object(app.App, "_build_tray", return_value=tray), mock.patch.object(app.autostart, "is_enabled", return_value=False), mock.patch.object(app.autostart, "repair_if_stale", return_value=False), mock.patch.object(app.App, "_apply_icon"):
                instance = app.App(start_hidden=True)
                try:
                    instance.update_idletasks()
                    self.assertEqual(instance.state(), "withdrawn")
                    instance.refresh_now()
                    instance.set_language("en" if i18n.language() == "pl" else "pl")
                    instance.open_picker()
                    picker = instance._picker
                    application_tick = instance._tick_id
                    picker.withdraw()
                    picker._clear()
                    picker._save()
                    self.assertFalse(picker.winfo_exists())
                    self.assertIn(application_tick, instance.tk.call("after", "info"))
                    instance.after_cancel(instance._tick_id)
                    instance._tick()
                    service.refresh_now.assert_called()
                    service.reload_settings.assert_called_once()

                    # A long declaration must leave controls accessible on the
                    # current monitor, including when display scaling is used.
                    readings = [
                        {"model": f"device_{index}", "label": "MYSZ", "name": f"Mouse {index}",
                         "percent": 60, "charging": False, "online": True}
                        for index in range(12)
                    ]
                    snapshot.update(readings=readings, readings_revision=1)
                    with mock.patch.object(app, "_monitor_work_area", return_value=(0, 0, 1600, 800)):
                        instance._update_devices(snapshot)
                        instance.update_idletasks()
                        self.assertLessEqual(instance.winfo_height(), 800)
                        self.assertLess(instance.footer_status.winfo_y(), 800)
                        self.assertIsInstance(instance.devices, app.ctk.CTkScrollableFrame)
                    widget_scale = app.ctk.ScalingTracker.widget_scaling
                    window_scale = app.ctk.ScalingTracker.window_scaling
                    try:
                        # A second monitor may use negative desktop coordinates.
                        for factor in (1.25, 1.5, 2.0):
                            with self.subTest(scaling=factor):
                                app.ctk.set_widget_scaling(factor)
                                app.ctk.set_window_scaling(factor)
                                with mock.patch.object(app, "_monitor_work_area", return_value=(-1600, -100, 0, 700)):
                                    instance._autosize()
                                    instance.update_idletasks()
                                    scale = app.ctk.ScalingTracker.get_window_scaling(instance)
                                    self.assertLessEqual(instance._current_height * scale, 800)
                                    self.assertLess(instance.footer_status.winfo_y() + instance.footer_status.winfo_height(), 800)
                                    self.assertGreater(instance.devices.winfo_reqheight(), instance._devices_container.winfo_height())
                                    self.assertEqual(len(instance._cards), 12)
                                    instance.toggle_log()
                                    instance.update_idletasks()
                                    self.assertLess(instance.footer_status.winfo_y() + instance.footer_status.winfo_height(), 800)
                                    self.assertLessEqual(
                                        instance.log_box.winfo_y() + instance.log_box.winfo_height(), 800,
                                        [(type(widget).__name__, widget.winfo_y(), widget.winfo_height()) for widget in instance.grid_slaves()],
                                    )
                                    instance.toggle_log()
                    finally:
                        app.ctk.set_widget_scaling(widget_scale)
                        app.ctk.set_window_scaling(window_scale)
                finally:
                    instance.quit_app()
                    instance._tray_thread.join(1)
                service.stop.assert_called_once()
                tray.stop.assert_called_once()


class AutostartRegressions(unittest.TestCase):
    def test_empty_entry_is_not_enabled(self):
        with mock.patch.object(autostart, "current_entry", return_value="   "):
            self.assertFalse(autostart.is_enabled())

    def test_repair_adds_tray_flag_without_repointing_existing_copy(self):
        existing = '"C:\\old\\BatteryMonitor.exe"'
        with mock.patch.object(autostart, "current_entry", return_value=existing), mock.patch.object(autostart, "command", return_value='"C:\\new\\BatteryMonitor.exe" --tray'), mock.patch.object(autostart.os.path, "isfile", return_value=True), mock.patch.object(autostart, "enable", return_value=True) as enable:
            self.assertTrue(autostart.repair_if_stale())
            enable.assert_called_once_with(existing + " --tray")

    def test_malformed_arguments_are_repaired(self):
        with mock.patch.object(autostart, "current_entry", return_value='"C:\\old\\BatteryMonitor.exe" "unterminated'), mock.patch.object(autostart, "command", return_value='"C:\\new\\BatteryMonitor.exe" --tray'), mock.patch.object(autostart.os.path, "isfile", return_value=True), mock.patch.object(autostart, "enable", return_value=True) as enable:
            self.assertTrue(autostart.repair_if_stale())
            enable.assert_called_once()

    def test_stale_python_script_is_repaired_even_when_interpreter_exists(self):
        with mock.patch.object(autostart, "current_entry", return_value='"C:\\Python\\pythonw.exe" "C:\\missing\\app.py" --tray'), mock.patch.object(autostart, "command", return_value='"C:\\new\\BatteryMonitor.exe" --tray'), mock.patch.object(autostart.os.path, "isfile", side_effect=lambda name: name == "C:\\Python\\pythonw.exe"), mock.patch.object(autostart, "enable", return_value=True) as enable:
            self.assertTrue(autostart.repair_if_stale())
            enable.assert_called_once()

    def test_valid_python_installation_is_not_repointed(self):
        with mock.patch.object(autostart, "current_entry", return_value='"C:\\Python\\pythonw.exe" "C:\\old\\app.py" --tray'), mock.patch.object(autostart, "command", return_value='"C:\\new\\BatteryMonitor.exe" --tray'), mock.patch.object(autostart.os.path, "isfile", return_value=True), mock.patch.object(autostart, "enable") as enable:
            self.assertFalse(autostart.repair_if_stale())
            enable.assert_not_called()


class BuildRegressions(unittest.TestCase):
    def test_distpath_option_builds_separately_from_running_executable(self):
        with mock.patch.object(build.sys, "argv", ["build.py", "--distpath", "dist/updated", "--clean"]), mock.patch.object(build, "check_dependencies", return_value=True), mock.patch.object(build, "check_icon", return_value=True), mock.patch.object(build.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run, mock.patch.object(build.os.path, "isfile", return_value=True), mock.patch.object(build.os.path, "getsize", return_value=1):
            self.assertEqual(build.main(), 0)
        command = run.call_args.args[0]
        self.assertIn("--distpath", command)
        output = command[command.index("--distpath") + 1]
        self.assertEqual(output, str(Path(build.SPEC).parent / "dist" / "updated"))

    def test_spec_does_not_resolve_assets_from_callers_cwd(self):
        source = Path(build.SPEC).read_text(encoding="utf-8")
        hooks = SimpleNamespace(collect_all=lambda name: ([], [], []))
        analysis = mock.Mock(return_value=SimpleNamespace(pure=[], scripts=[], binaries=[], datas=[]))
        executable = mock.Mock()
        namespace = {"SPECPATH": str(Path(build.SPEC).parent), "Analysis": analysis, "PYZ": mock.Mock(), "EXE": executable}
        with mock.patch.dict("sys.modules", {"PyInstaller.utils.hooks": hooks}):
            exec(compile(source, build.SPEC, "exec"), namespace)
        self.assertEqual(analysis.call_args.args[0], [str(Path(build.SPEC).parent / "app.py")])
        self.assertTrue(Path(executable.call_args.kwargs["icon"]).is_absolute())


if __name__ == "__main__":
    unittest.main()
