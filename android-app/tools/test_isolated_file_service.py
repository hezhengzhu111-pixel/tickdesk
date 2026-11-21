"""Round-trip a generated PNG through isolated relay and local-service processes."""
import argparse
import base64
import contextlib
import hashlib
import os
from pathlib import Path
import secrets
import select
import socket
import socketserver
import subprocess
import tempfile
import threading
import time
import uuid


APP = Path(__file__).resolve().parents[1]
EMULATOR = "emulator-5566"
LOOPBACK = ("127.0.0.2", 8443)


class Proxy(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            with socket.create_connection(LOOPBACK, timeout=1) as upstream:
                upstream.settimeout(None)
                peers = {self.request: upstream, upstream: self.request}
                while True:
                    reads, _, _ = select.select(peers, (), (), 30)
                    if not reads:
                        break
                    for source in reads:
                        chunk = source.recv(65_536)
                        if not chunk:
                            return
                        interrupt = getattr(self.server, "interrupt_before_delivery", None)
                        if source is upstream and interrupt is not None and interrupt():
                            return
                        peers[source].sendall(chunk)
        except OSError:
            return


class TestProxy(socketserver.ThreadingTCPServer):
    daemon_threads = True


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_listener(address, process):
    for _ in range(50):
        if process.poll() is not None:
            raise RuntimeError(f"Isolated {Path(process.args[0]).name} stopped during startup")
        try:
            with socket.create_connection(address, timeout=0.2):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"Isolated service did not listen at {address}")


def process(stack, binary, folder, config):
    log = stack.enter_context((folder / (binary.stem + ".log")).open("wb"))
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    proc = subprocess.Popen([str(binary), "--config", config], cwd=folder, stdout=log,
                            stderr=subprocess.STDOUT, creationflags=flags)
    stack.callback(stop, proc)
    return proc


def stop(proc):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def create_config(folder, listen_port):
    run = dict(cwd=folder, stderr=subprocess.DEVNULL)
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-keyout", "key.pem", "-out", "cert.pem", "-subj", "/CN=TickDesk Test",
                    "-addext", "subjectAltName=IP:127.0.0.1,IP:127.0.0.2",
                    "-addext", "basicConstraints=critical,CA:TRUE"],
                   check=True, stdout=subprocess.DEVNULL, **run)
    pem = subprocess.check_output(["openssl", "x509", "-in", "cert.pem", "-pubkey", "-noout"], **run)
    public_key = subprocess.check_output(["openssl", "pkey", "-pubin", "-outform", "DER"], input=pem, **run)
    cert = subprocess.check_output(["openssl", "x509", "-in", "cert.pem", "-outform", "DER"], **run)
    pin = "sha256/" + base64.b64encode(hashlib.sha256(public_key).digest()).decode("ascii")
    pair = "loopback_" + uuid.uuid4().hex
    app_bearer = secrets.token_urlsafe(32)
    local_bearer = secrets.token_urlsafe(32)

    def digest(token):
        return hashlib.sha256(token.encode("ascii")).hexdigest()

    (folder / "local.token").write_text(local_bearer, encoding="ascii")
    (folder / "roles.toml").write_text(
        f'[pairs.{pair}.app]\nsha256 = "{digest(app_bearer)}"\n'
        f'[pairs.{pair}.local]\nsha256 = "{digest(local_bearer)}"\n', encoding="ascii")
    (folder / "relay.toml").write_text(
        'listen = "127.0.0.2:8443"\nroles_file = "roles.toml"\n'
        '[tls]\ncert = "cert.pem"\nkey = "key.pem"\n', encoding="ascii")
    (folder / "local.toml").write_text(
        f'listen = "127.0.0.1:{listen_port}"\ndata_dir = "fixture_data"\n[relay]\n'
        'relay_url = "wss://127.0.0.2:8443/relay/v2/connect"\n'
        f'pair_id = "{pair}"\ntoken_file = "local.token"\npins = ["{pin}"]\n', encoding="ascii")
    return pair, app_bearer, base64.b64encode(cert).decode("ascii")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--adb", default=str(Path(os.environ.get("LOCALAPPDATA", ".")) /
                                             "Android/Sdk/platform-tools/adb.exe"))
    parser.add_argument("--serial", default=EMULATOR)
    args = parser.parse_args()
    if not args.serial.startswith("emulator-"):
        raise RuntimeError("This test requires an isolated emulator")
    device = [args.adb, "-s", args.serial]

    def adb(*arguments, timeout=20):
        try:
            result = subprocess.run(device + list(arguments), capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"ADB {arguments[0]} timed out") from None
        if result.returncode != 0:
            raise RuntimeError(f"ADB {arguments[0]} failed with status {result.returncode}")
        return result.stdout

    if adb("shell", "getprop", "ro.kernel.qemu").strip() != "1":
        raise RuntimeError("Use only an Android emulator")
    if "tcp:8443" in adb("reverse", "--list"):
        raise RuntimeError("Existing reverse mapping on emulator port 8443; leave it unchanged")
    with socket.socket() as bound:
        bound.bind(LOOPBACK)
    service_port = free_port()
    binaries = [APP.parent / name / "target" / "debug" / exe for name, exe in [
        ("relay", "tickdesk-relay.exe"), ("local-service", "tickdesk-local.exe")]]
    if not all(exe.is_file() for exe in binaries):
        raise RuntimeError("Build isolated relay and local-service binaries before running this check")
    temporary_root = (APP / ".gradle").resolve()
    if not temporary_root.is_relative_to(APP):
        raise RuntimeError("Temporary data must stay within the app workspace")
    temporary_root.mkdir(exist_ok=True)
    with contextlib.ExitStack() as stack:
        folder = Path(stack.enter_context(tempfile.TemporaryDirectory(dir=temporary_root, prefix="isolated-file-")))
        pair, bearer, cert = create_config(folder, service_port)
        relay = process(stack, binaries[0], folder, "relay.toml")
        wait_listener(LOOPBACK, relay)
        local = process(stack, binaries[1], folder, "local.toml")
        wait_listener(("127.0.0.1", service_port), local)
        server = stack.enter_context(TestProxy(("127.0.0.1", 0), Proxy))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        stack.callback(server.shutdown)
        adb("reverse", "tcp:8443", f"tcp:{server.server_address[1]}")
        stack.callback(adb, "reverse", "--remove", "tcp:8443")
        result = adb("shell", "am", "instrument", "-w", "-e", "class", "dev.tickdesk.app.IsolatedServiceFileTest",
                     "-e", "isolatedFileTest", "true", "-e", "pairId", pair, "-e", "appBearer", bearer, "-e", "caDer", cert,
                     "dev.tickdesk.app.test/androidx.test.runner.AndroidJUnitRunner", timeout=100)
        print(result.replace(bearer, "[redacted]"))
        if "OK (1 test)" not in result or "FAILURES" in result:
            raise SystemExit(1)


if __name__ == "__main__":
    main()

