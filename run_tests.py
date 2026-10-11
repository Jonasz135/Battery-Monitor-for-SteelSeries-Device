"""Run the legacy checks and unittest regressions with isolated application data."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gui", action="store_true", help="also create a hidden Tk window")
    parser.add_argument("--http", action="store_true", help="also test a local GameSense HTTP server")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parent
    temporary_root = root / ".test-tmp"
    temporary_root.mkdir(exist_ok=True)
    suites = [
        ["test_devices.py"], ["test_battery_runtime.py"], ["test_i18n.py"],
        ["-m", "unittest", "discover", "-p", "test_*regressions.py", "-v"],
    ]
    failures = 0
    with tempfile.TemporaryDirectory(prefix="suite-", dir=temporary_root) as directory:
        environment = os.environ.copy()
        environment.update(TEMP=directory, TMP=directory,
                           APPDATA=os.path.join(directory, "appdata"),
                           LOCALAPPDATA=os.path.join(directory, "localappdata"),
                           PYTHONIOENCODING="utf-8",
                           BATTERY_MONITOR_GUI_SMOKE="1" if args.gui else "0",
                           BATTERY_MONITOR_HTTP_SMOKE="1" if args.http else "0")
        for suite in suites:
            result = subprocess.run([sys.executable, *suite], cwd=root, env=environment,
                                    capture_output=True, text=True, encoding="utf-8", errors="replace")
            label = suite[0] if suite[0] != "-m" else "unittest regressions"
            print(f"{'PASS' if result.returncode == 0 else 'FAIL'}: {label}", flush=True)
            if result.returncode:
                failures += 1
                print(result.stdout + result.stderr, flush=True)
            else:
                output = result.stdout if suite[0] != "-m" else result.stderr
                print("\n".join(output.strip().splitlines()[-4:]), flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
