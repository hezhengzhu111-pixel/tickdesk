"""Open a real scheduled report notification from an isolated Android PIN lock screen."""

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import runpy
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

from test_isolated_file_service import LOOPBACK, Proxy, TestProxy, create_config, free_port, process, wait_listener


ROOT = Path(__file__).resolve().parents[2]
DEMO = "dev.tickdesk.app.demo"
MARKERS = "files/lockscreen-report"
PIN = "8246"  # Temporary credential exclusively for an emulator that initially has no lock.
PERMISSION = "android.permission.POST_NOTIFICATIONS"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--local", type=Path, required=True)
    parser.add_argument("--relay", type=Path, required=True)
    args = parser.parse_args()
    if not args.serial.startswith("emulator-"):
        parser.error("Only an isolated emulator is supported")
    sys.path.insert(0, str(ROOT / "scripts"))
    helpers = runpy.run_path(str(ROOT / "scripts/verify-local-ai.py"))
    evidence = args.evidence.resolve()
    evidence.mkdir(exist_ok=True, parents=True)
    device = [str(Path(os.environ["LOCALAPPDATA"]) / "Android/Sdk/platform-tools/adb.exe"), "-s", args.serial]

    def adb(*arguments):
        return subprocess.run(device + list(arguments), capture_output=True, timeout=15, check=True).stdout

    def shell(*arguments):
        return adb("shell", *arguments).decode("utf-8", errors="replace").strip()

    if shell("getprop", "ro.kernel.qemu") != "1" or shell("cmd", "lock_settings", "get-disabled") != "true":
        raise RuntimeError("Use an emulator with no prior configured screen lock")
    demo_pairing = ("run-as", DEMO, "test", "!", "-e", "no_backup/pairing-v2")
    shell(*demo_pairing)
    settings = {"lock_screen_show_notifications": "1", "lock_screen_allow_private_notifications": "0",
                "lock_screen_show_silent_notifications": "1", "lock_screen_notification_minimalism": "0",
                "lock_screen_lock_after_timeout": "0"}
    prior_settings = {key: shell("settings", "get", "secure", key) for key in settings}
    normal = "dev.tickdesk.app"

    def normal_preferences():
        result = {}
        for file in ("no_backup/pairing-v2", "shared_prefs/background-reminders.xml"):
            check = subprocess.run(device + ["shell", "run-as", normal, "test", "-f", file], capture_output=True, timeout=15)
            result[file] = hashlib.sha256(adb("exec-out", "run-as", normal, "cat", file)).hexdigest() if check.returncode == 0 else None
        return result

    prior_normal = normal_preferences()
    prior_mapping = next((line.split()[2] for line in adb("reverse", "--list").decode().splitlines()
                          if len(line.split()) == 3 and line.split()[1] == "tcp:8443"), None)
    before = shell("dumpsys", "package", DEMO)
    had_permission = f"{PERMISSION}: granted=true" in before
    with socket.socket() as probe:
        probe.bind(LOOPBACK)

    temp = ROOT / "data/app-integration"
    temp.mkdir(parents=True, exist_ok=True)
    with contextlib.ExitStack() as stack:
        folder = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="report-lockscreen-", dir=temp)))
        service_port = free_port()
        pair, bearer, cert = create_config(folder, service_port)
        gate = folder / "start-fixture-scheduler"
        model = stack.enter_context(helpers["models"](risk_case=True))
        stack.enter_context(helpers["assistant_fixture"](folder, f"127.0.0.1:{service_port}", model.server_port, schedule_gate=gate))
        for path, name, address in ((args.relay, "relay", LOOPBACK), (args.local, "local", ("127.0.0.1", service_port))):
            binary = Path(shutil.copy2(path.resolve(), folder / path.name))
            wait_listener(address, process(stack, binary, folder, name + ".toml"))
        database = folder / "fixture_data/tickdesk.sqlite3"
        seed_position(database)
        proxy = stack.enter_context(TestProxy(("127.0.0.1", 0), Proxy))
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        stack.callback(proxy.shutdown)

        shell("am", "force-stop", DEMO)
        shell("run-as", DEMO, "rm", "-f", *[f"{MARKERS}/{name}" for name in
              ("ready", "keyguard.png", "expanded.png", "redacted.png", "authentication.png", "destination.png")])
        try:
            if "Pin set" not in shell("cmd", "lock_settings", "set-pin", PIN):
                raise RuntimeError("Could not setup the temporary emulator PIN")
            for key, value in settings.items():
                shell("settings", "put", "secure", key, value)
            if not had_permission:
                shell("pm", "grant", DEMO, PERMISSION)
            adb("reverse", "tcp:8443", f"tcp:{proxy.server_address[1]}")
            shell("input", "keyevent", "KEYCODE_WAKEUP")
            with (folder / "instrumentation.raw.log").open("wb") as log:
                tester = subprocess.Popen(device + ["shell", "am", "instrument", "-w", "-r", "-e", "class",
                    "dev.tickdesk.app.SettingsIntegrationTest#openRedactedRiskReportFromSecureLockscreen",
                    "-e", "lockscreenReportIntegration", "true", "-e", "pairId", pair, "-e", "appBearer", bearer,
                    "-e", "caDer", cert, "-e", "lockscreenPin", PIN, f"{DEMO}.test/androidx.test.runner.AndroidJUnitRunner"],
                    stdout=log, stderr=subprocess.STDOUT)
                for _ in range(125):
                    marker = subprocess.run(device + ["shell", "run-as", DEMO, "test", "-f", f"{MARKERS}/ready"],
                                            capture_output=True, timeout=10)
                    if marker.returncode == 0:
                        gate.touch()
                        break
                    if tester.poll() is not None:
                        break
                    time.sleep(0.2)
                try:
                    tester.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    tester.kill()
                    tester.wait(timeout=5)
        finally:
            for name in ("keyguard", "expanded", "redacted", "authentication", "destination"):
                result = subprocess.run(device + ["exec-out", "run-as", DEMO, "cat", f"{MARKERS}/{name}.png"],
                                        capture_output=True, timeout=15)
                if result.returncode == 0 and result.stdout.startswith(b"\x89PNG"):
                    (evidence / f"{name}.png").write_bytes(result.stdout)
            shell("am", "force-stop", DEMO)
            shell("cmd", "lock_settings", "clear", "--old", PIN)
            shell("cmd", "lock_settings", "set-disabled", "true")
            for key, value in prior_settings.items():
                if value == "null":
                    shell("settings", "delete", "secure", key)
                else:
                    shell("settings", "put", "secure", key, value)
            shell("wm", "dismiss-keyguard")
            if not had_permission:
                shell("pm", "revoke", DEMO, PERMISSION)
            if prior_mapping is None:
                adb("reverse", "--remove", "tcp:8443")
            else:
                adb("reverse", "tcp:8443", prior_mapping)
        assert shell("cmd", "lock_settings", "get-disabled") == "true"
        shell(*demo_pairing)
        assert normal_preferences() == prior_normal
        assert {key: shell("settings", "get", "secure", key) for key in settings} == prior_settings
        print("PASS: normal demo-neighbor preferences, prior emulator keyguard settings and USB mapping preserved", flush=True)
        result = (folder / "instrumentation.raw.log").read_text(encoding="utf-8", errors="replace")
        (evidence / "instrumentation.log").write_text(result.replace(bearer, "[redacted]"), encoding="utf-8")
        checked = sqlite_evidence(database)
        (evidence / "sqlite.json").write_text(json.dumps(checked, indent=2, ensure_ascii=False), encoding="utf-8")
        if "OK (1 test)" not in result or "FAILURES" in result:
            raise RuntimeError("Android lockscreen test failed; see instrumentation.log")
        assert len(model.chat_prompts) == 1 and model.chat_prompts[0]["context"]["purpose"] == "automatic"
        assert checked["matchingReads"] >= 2 and checked["riskPublication"]["notifyUntil"] > 0
        print("PASS: one sourced automatic risk publication and multiple genuine same-report Android read receipts", flush=True)


def seed_position(database):
    fixtures = json.loads((ROOT / "contracts/v2/fixtures.json").read_text(encoding="utf-8"))
    page = next(case["payload"]["data"] for case in fixtures["cases"] if case["name"] == "snapshot_page_1")
    position = page["positions"][0] | {"name": "隔离联调证券", "asOf": int(time.time() * 1000), "lastPrice": None,
                                     "priceAsOf": None, "priceStatus": "missing", "marketValue": None,
                                     "unrealizedPnl": None, "unrealizedPnlRatio": None}
    with contextlib.closing(sqlite3.connect(database)) as db, db:
        db.execute("INSERT INTO objects VALUES('positions',?,?)", (position["symbol"], json.dumps(position, ensure_ascii=False)))
        db.execute("UPDATE domain_state SET position_revision=1")
        db.execute("UPDATE metadata SET value=1 WHERE key='snapshot_seq'")


def sqlite_evidence(database):
    with contextlib.closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
        events = db.execute("SELECT frame FROM transport_outbox WHERE json_extract(frame,'$.payload.kind')='analysis_report'").fetchall()
        published = [json.loads(frame)["payload"]["data"] for (frame,) in events]
        risk = next((item for item in published if item["header"]["stance"] == "restricted"), None)
        report = risk["header"]["reportId"] if risk else None
        checked = db.execute("SELECT count(*) FROM command_results WHERE json_extract(body,'$.data.cmd')='get_report'"
                             " AND json_extract(body,'$.data.result.header.reportId')=?", (report,)).fetchone()[0]
        return {"riskPublication": risk, "matchingReads": checked}


if __name__ == "__main__":
    main()
