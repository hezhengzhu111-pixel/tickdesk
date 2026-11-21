"""Run real Android -> TLS relay -> local service scenarios on the selected USB phone.

Uses temporary credentials, separate Room storage, and isolated backend data.
The existing phone pairing is never replaced. Its ADB reverse mapping is restored.
"""
import argparse
import contextlib
import json
import os
from pathlib import Path
import re
import socket
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "android-app/tools"))
from test_isolated_file_service import (LOOPBACK, Proxy, TestProxy, create_config,
                                        free_port, process, stop, wait_listener)


def seed(folder):
    fixture = json.loads((ROOT / "contracts/v2/fixtures.json").read_text(encoding="utf-8"))
    example = next(item["payload"]["data"] for item in fixture["cases"] if item["name"] == "task")
    now = int(time.time() * 1000)
    with contextlib.closing(sqlite3.connect(folder / "fixture_data/tickdesk.sqlite3")) as db, db:
        keys = ["integration_action", "integration_reminder"] + [f"integration_page_{i:02d}" for i in range(21)]
        for key in keys:
            due = now + 8_000 if key == "integration_reminder" else None
            task = dict(example, taskId=key, sourceEventKey=key, symbol="300001.SZ", name="isolated integration fixture",
                        direction="risk", riskEventKey=f"fixture_risk_{key}", eventRevision=1,
                        level="warn" if key in ("integration_reminder", "integration_action") else "info",
                        status="pending", version=1, actionVersion=1,
                        createdAt=now-300_000, dataAsOf=now-1000, validUntil=now+600_000,
                        remindedCount=1, lastRemindedAt=now-300_000, nextReminderAt=due,
                        currentReminder=None, snoozeUntil=None)
            db.execute("INSERT INTO objects VALUES ('tasks',?,?)", (key, json.dumps(task)))
        raw = db.execute("SELECT settings FROM domain_state").fetchone()[0]
        settings = dict(json.loads(raw), nightStart="00:00", nightEnd="23:59", criticalAtNight=False)
        db.execute("UPDATE domain_state SET settings=?", (json.dumps(settings),))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--allow-unpaired", action="store_true", help="Allow a fresh Android emulator with no saved pairing")
    parser.add_argument("--complete-functions", action="store_true", help="Use isolated test-only market/text/vision providers")
    parser.add_argument("--python-assistant", action="store_true",
                        help="Route complete-scenario chat and vision through the real Python API")
    parser.add_argument("--backend-python", type=Path, help="Backend interpreter (defaults to the backend .venv)")
    parser.add_argument("--local-binary", type=Path, help="Explicit integration-fixtures binary for full flow testing outside trading hours")
    parser.add_argument("--adb", default=str(Path(os.environ.get("LOCALAPPDATA", ".")) / "Android/Sdk/platform-tools/adb.exe"))
    parser.add_argument("--log", type=Path, default=ROOT / "data/app-integration/logs/three-tier-android.log")
    args = parser.parse_args()
    if args.allow_unpaired and not re.fullmatch(r"emulator-[0-9]+", args.serial):
        parser.error("--allow-unpaired requires an explicit emulator serial")
    if args.python_assistant and not args.complete_functions:
        parser.error("--python-assistant requires --complete-functions")
    if args.backend_python and not args.python_assistant:
        parser.error("--backend-python requires --python-assistant")
    device = [args.adb, "-s", args.serial]
    sensitive = []

    def safe(text):
        if isinstance(text, bytes):
            text = text.decode("utf-8", errors="replace")
        for value in sensitive:
            text = text.replace(value, "[redacted]")
        return text

    def adb(*items, timeout=20):
        try:
            result = subprocess.run(device + list(items), capture_output=True, text=True, encoding="utf-8",
                                    errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired as error:
            args.log.write_text(safe(error.stdout or "") + "\nADB command timed out\n", encoding="utf-8")
            raise RuntimeError(f"ADB {items[0]} timed out") from None
        if result.returncode:
            raise RuntimeError(f"ADB {items[0]} failed (exit {result.returncode})")
        return result.stdout

    def pairing_digest():
        # A missing file is a successful, explicit probe. run-as and hashing failures
        # still propagate through adb as failures.
        probe = adb("shell", "run-as dev.tickdesk.app sh -c '"
                    "if test -e no_backup/pairing-v2; then sha256sum no_backup/pairing-v2; "
                    "else echo absent; fi'").strip()
        if probe == "absent":
            return None
        match = re.fullmatch(r"([0-9a-f]{64})\s+no_backup/pairing-v2", probe)
        if not match:
            raise RuntimeError("Could not verify the installed pairing digest")
        return match[1]

    if adb("get-state").strip() != "device":
        raise RuntimeError("Selected device is unavailable")
    # Reading only a digest, never exporting a token or replacing the actual pairing.
    pairing_before = pairing_digest()
    if pairing_before is None and not args.allow_unpaired:
        raise RuntimeError("An existing pairing is required. Fresh emulators may use --allow-unpaired.")
    prior = None
    for line in adb("reverse", "--list").splitlines():
        fields = line.split()
        if len(fields) == 3 and fields[1] == "tcp:8443":
            prior = fields[2]
    with socket.socket() as probe:
        probe.bind(LOOPBACK)  # Do not stop any unrelated listener.
    binaries = [ROOT / name / "target/debug" / exe for name, exe in
                [("local-service/relay", "tickdesk-relay.exe"), ("local-service", "tickdesk-local.exe")]]
    if args.local_binary:
        binaries[1] = args.local_binary.resolve()
    if not all(path.is_file() for path in binaries):
        raise RuntimeError("Build both services before this test")
    temporary_root = ROOT / "data/app-integration"
    temporary_root.mkdir(parents=True, exist_ok=True)
    args.log.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.ExitStack() as stack:
        folder = Path(stack.enter_context(tempfile.TemporaryDirectory(dir=temporary_root, prefix="three-tier-")))
        # Freeze executable bytes for this run; parallel builds must not replace or lock them.
        (folder / "bin").mkdir()
        binaries = [Path(shutil.copy2(binary, folder / "bin" / binary.name)) for binary in binaries]
        port = free_port()
        pair, bearer, cert = create_config(folder, port)
        sensitive.append(bearer)
        if args.complete_functions:
            from testing_providers import BACKEND_PYTHON, assistant_fixture, fixture_providers
            provider_port = stack.enter_context(fixture_providers())
            with (folder / "local.toml").open("a", encoding="utf-8") as config:
                config.write(f'\n[kernel]\naddress="127.0.0.1:{provider_port}"\n')
                if not args.python_assistant:
                    for name in ("chat", "vision"):
                        config.write(f'\n[{name}]\nendpoint="http://127.0.0.1:{provider_port}/{name}"\nmodel="integration-fixture"\n')
            if args.python_assistant:
                stack.enter_context(assistant_fixture(folder, f"127.0.0.1:{port}", provider_port,
                                                      (args.backend_python or BACKEND_PYTHON).resolve()))
        relay = process(stack, binaries[0], folder, "relay.toml")
        wait_listener(LOOPBACK, relay)
        local = process(stack, binaries[1], folder, "local.toml")
        wait_listener(("127.0.0.1", port), local)
        stop(local)
        seed(folder)
        local = process(stack, binaries[1], folder, "local.toml")
        wait_listener(("127.0.0.1", port), local)
        server = stack.enter_context(TestProxy(("127.0.0.1", 0), Proxy))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        stack.callback(server.shutdown)
        # Instrumentation owns the app process during the isolated test. Restore before normal launch.
        adb("shell", "am", "force-stop", "dev.tickdesk.app")
        try:
            adb("reverse", "tcp:8443", f"tcp:{server.server_address[1]}")
            result = adb("shell", "am", "instrument", "-w", "-r", "-e", "class",
                         "dev.tickdesk.app.ThreeTierIntegrationTest", "-e", "threeTierTest", "true",
                         "-e", "pairId", pair, "-e", "appBearer", bearer, "-e", "caDer", cert,
                         "-e", "completeFeatures", str(args.complete_functions).lower(),
                         "-e", "pythonAssistant", str(args.python_assistant).lower(),
                         "dev.tickdesk.app.test/androidx.test.runner.AndroidJUnitRunner", timeout=160)
            safe_result = result.replace(bearer, "[redacted]")
            args.log.write_text(safe_result, encoding="utf-8")
            print(safe_result, flush=True)
            stages = adb("logcat", "-d", "-s", "TickDeskThreeTier:I", "*:S")
            args.log.with_suffix(".stages.log").write_text(stages, encoding="utf-8")
            if "OK (1 test)" not in result or "FAILURES" in result or "INSTRUMENTATION_STATUS_CODE: -" in result:
                raise RuntimeError("Android scenario failed or skipped; see sanitized test log")
            with contextlib.closing(sqlite3.connect(f"{(folder / 'fixture_data/tickdesk.sqlite3').as_uri()}?mode=ro", uri=True)) as db:
                counts = dict(db.execute("SELECT collection,count(*) FROM objects GROUP BY collection"))
                expected_executions = 2 if args.complete_functions else 1
                if counts.get("positions") != 1 or counts.get("executions") != expected_executions:
                    raise RuntimeError("Missing expected isolated business records")
                print(f"PASS: isolated local ledger has one position and {expected_executions} executions", flush=True)
                if args.python_assistant:
                    chats = db.execute("SELECT model, json_extract(reply, '$.data.status') FROM chat_jobs").fetchall()
                    imports = db.execute("SELECT state FROM vision_jobs WHERE import_id='complete_import'").fetchall()
                    if len(chats) != 4 or any(row != ("fixture-chat", "completed") for row in chats) or imports != [("finished",)]:
                        raise RuntimeError("Python assistant chat or OCR did not complete in the isolated ledger")
                    reports = db.execute("SELECT raw_output FROM chat_jobs WHERE json_extract(raw_output, '$.artifact.report') IS NOT NULL").fetchall()
                    if len(reports) != 1 or json.loads(reports[0][0])["artifact"]["report"]["body"]["stance"] != "insufficient":
                        raise RuntimeError("Android manual report was not durably committed")
                    print("PASS: Android chat, structured report, historical/current follow-up and OCR used the Python assistant", flush=True)
        finally:
            cleanup_errors = []
            # Preserve test-only diagnostics even when instrumentation times out.
            try:
                if args.python_assistant:
                    shutil.copyfile(folder / "assistant.log", args.log.with_suffix(".assistant.log"))
                stages = adb("logcat", "-d", "-s", "TickDeskThreeTier:I", "*:S")
                args.log.with_suffix(".stages.log").write_text(safe(stages), encoding="utf-8")
                with contextlib.closing(sqlite3.connect(folder / "fixture_data/tickdesk.sqlite3")) as source:
                    with contextlib.closing(sqlite3.connect(args.log.with_suffix(".fixture.sqlite3"))) as target:
                        source.backup(target)
            except Exception as error:
                print(f"Test diagnostic capture failed: {type(error).__name__}", flush=True)
            try:
                adb("shell", "am", "force-stop", "dev.tickdesk.app")
            except Exception as error:
                cleanup_errors.append(f"stop test: {type(error).__name__}")
            restored = False
            try:
                if prior is not None:
                    adb("reverse", "tcp:8443", prior)
                else:
                    adb("reverse", "--remove", "tcp:8443")
                restored = True
            except Exception as error:
                cleanup_errors.append(f"restore USB mapping: {type(error).__name__}")
            try:
                pairing_after = pairing_digest()
                if pairing_after != pairing_before:
                    cleanup_errors.append("installed pairing changed")
            except Exception as error:
                cleanup_errors.append(f"verify pairing: {type(error).__name__}")
            if restored:
                try:
                    adb("shell", "am", "start", "-W", "-n", "dev.tickdesk.app/.MainActivity")
                except Exception as error:
                    cleanup_errors.append(f"reopen App: {type(error).__name__}")
            if cleanup_errors:
                raise RuntimeError("Cleanup incomplete: " + "; ".join(cleanup_errors))
            print("PASS: original USB mapping restored; installed pairing unchanged", flush=True)


if __name__ == "__main__":
    main()
