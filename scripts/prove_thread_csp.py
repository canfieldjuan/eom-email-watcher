"""Exercise the Linux host's production assets in a new, isolated profile.

Build the frontend/sidecar, then build the host with `--features tauri/custom-protocol`.
Run with --binary <host> --driver <WebKitWebDriver> --output <new directory outside Git>.
Requires Xvfb, D-Bus, xdotool, the matching WebKit driver and the project's Python environment.
No install, live profile, provider or Windows qualification is part of this proof.
"""

import argparse
import hashlib
import http.server
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from contextlib import suppress
from pathlib import Path

from coi_evidence import create_evidence_directory
from packaged_proof_environment import with_empty_user_manager

ROOT = Path(__file__).resolve().parents[1]


def request(base, path, data=None, method=None):
    payload = None if data is None else json.dumps(data).encode()
    req = urllib.request.Request(
        base + path, data=payload, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError(error.read().decode()) from error
    value = result.get("value")
    if isinstance(value, dict) and value.get("error"):
        raise RuntimeError(result)
    return value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--driver", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not __debug__:
        parser.error("Run without Python optimization so every proof assertion executes")
    binary, driver = args.binary.resolve(strict=True), args.driver.resolve(strict=True)
    if sys.platform != "linux":
        parser.error("This WebKit proof qualifies Linux only")
    if args.output.exists():
        parser.error("Use a new evidence directory; never reuse a profile")
    output = create_evidence_directory(args.output)
    profile = output / "profile"
    profile.mkdir(mode=0o700)
    env = {name: os.environ[name] for name in ("PATH", "LANG", "LC_ALL") if name in os.environ}
    env.update(
        HOME=str(profile),
        TAURI_WEBVIEW_AUTOMATION="true",
        GDK_BACKEND="x11",
        GIO_USE_VFS="local",
        GTK_USE_PORTAL="0",
        LIBGL_ALWAYS_SOFTWARE="1",
        NO_AT_BRIDGE="1",
        EOM_EMAIL_WATCHER_CONFIG=str(profile / "config.toml"),
    )
    for key, name in [
        ("XDG_CACHE_HOME", "cache"),
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_DATA_HOME", "data"),
        ("XDG_RUNTIME_DIR", "runtime"),
        ("XDG_STATE_HOME", "state"),
    ]:
        folder = profile / name
        folder.mkdir(mode=0o700)
        env[key] = str(folder)
    (profile / "config.toml").write_text(
        'timezone = "UTC"\nmodel_name = "synthetic-csp-proof"\n'
        'model_base_url = "http://127.0.0.1:9/v1"\nmodel_require_auth = false\n'
        "notifications_enabled = false\npoll_interval_minutes = 1440\n"
    )
    (profile / "config.toml").chmod(0o600)
    env = with_empty_user_manager(profile, env)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    attempts = []

    class Sink(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            attempts.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"window.__remoteCspExecuted = true;")

        do_POST = do_GET

        def log_message(self, *_):
            pass

    sink = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Sink)
    sink_thread = threading.Thread(target=sink.serve_forever, daemon=True)
    sink_thread.start()
    session = None
    with (output / "host.log").open("w") as log:
        process = subprocess.Popen(
            [
                "/usr/bin/xvfb-run",
                "-a",
                "/usr/bin/dbus-run-session",
                "--",
                sys.executable,
                "-c",
                "import json,os,sys; from pathlib import Path; "
                "Path(sys.argv[1]).write_text(json.dumps({k: os.environ[k] "
                "for k in ('DISPLAY', 'XAUTHORITY')})); "
                "os.execv(sys.argv[2], sys.argv[2:])",
                str(output / "display.json"),
                str(driver),
                "--host=127.0.0.1",
                f"--port={port}",
            ],
            env=env,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 20
            while True:
                try:
                    request(base, "/status")
                    break
                except (OSError, urllib.error.URLError) as error:
                    if process.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError("Private WebKit driver did not start") from error
                    time.sleep(0.1)
            created = request(
                base,
                "/session",
                {
                    "capabilities": {
                        "alwaysMatch": {
                            "browserName": "wry",
                            "webkitgtk:browserOptions": {"binary": str(binary)},
                        }
                    }
                },
            )
            session = created["sessionId"]

            def script(code, *arguments, asynchronous=False):
                return request(
                    base,
                    f"/session/{session}/execute/{'async' if asynchronous else 'sync'}",
                    {"script": code, "args": list(arguments)},
                )

            deadline = time.monotonic() + 40
            while not script(
                'const tab = document.querySelector("#vendors-tab"); return !!tab && !tab.disabled;'
            ):
                if time.monotonic() > deadline:
                    raise RuntimeError("Production UI did not reach admitted navigation")
                time.sleep(0.2)
            startup = script(
                """const done = arguments[0];
                window.__TAURI_INTERNALS__.invoke('config_admission_status').then(value => done({
                    admission: value.state, url: location.href,
                    styled: getComputedStyle(document.body).fontFamily,
                    stylesheets: [...document.styleSheets].map(s => s.href)
                })).catch(e => done({error: String(e)}));""",
                asynchronous=True,
            )
            assert startup["admission"] == "admitted", startup
            assert startup["url"].startswith("tauri://localhost"), startup
            assert startup["stylesheets"] and all(
                "/assets/" in item for item in startup["stylesheets"]
            ), startup
            for tab in ("vendors", "watchlist", "inbox", "settings"):
                deadline = time.monotonic() + 20
                while script(
                    'return document.getElementById(arguments[0] + "-tab").disabled;', tab
                ):
                    if time.monotonic() > deadline:
                        raise RuntimeError(f"{tab} navigation remained disabled")
                    time.sleep(0.1)
                assert script(
                    """
                    document.getElementById(arguments[0] + "-tab").click();
                    return !document.getElementById(arguments[0] + "-view").hidden;
                """,
                    tab,
                ), tab
            script("""window.__dialogResult = null;
                window.__TAURI_INTERNALS__.invoke('plugin:dialog|message', {
                    title: 'M3 CSP proof', message: 'Synthetic local dialog', kind: 'info'
                }).then(value => window.__dialogResult = value)
                  .catch(error => window.__dialogResult = {error: String(error)});
            """)
            display_env = dict(env, **json.loads((output / "display.json").read_text()))
            dialog_id = subprocess.check_output(
                ["xdotool", "search", "--sync", "--name", "^M3 CSP proof$"],
                env=display_env,
                text=True,
                timeout=15,
            ).splitlines()[-1]
            subprocess.run(
                ["xdotool", "windowfocus", "--sync", dialog_id, "key", "Return"],
                env=display_env,
                check=True,
                timeout=10,
            )
            deadline = time.monotonic() + 10
            while script("return window.__dialogResult;") is None:
                if time.monotonic() > deadline:
                    raise RuntimeError("Native dialog did not return through IPC")
                time.sleep(0.1)
            assert script("return window.__dialogResult;") == "Ok"
            script(
                """window.__cspEvents = [];
                window.__inlineCspExecuted = false;
                window.__handlerCspExecuted = false;
                window.__remoteCspExecuted = false;
                document.addEventListener('securitypolicyviolation', e => window.__cspEvents.push({
                  directive: e.effectiveDirective, blocked: e.blockedURI, policy: e.originalPolicy,
                  disposition: e.disposition
                }));
                const remote = arguments[0];
                const s = document.createElement('script');
                s.textContent = 'window.__inlineCspExecuted = true';
                document.body.append(s);
                const b = document.createElement('button');
                b.setAttribute('onclick', 'window.__handlerCspExecuted = true');
                document.body.append(b);
                b.click();
                const external = document.createElement('script');
                external.src = remote + '/script';
                document.body.append(external);
                const image = document.createElement('img');
                image.src = remote + '/image';
                document.body.append(image);
                fetch(remote + '/fetch').catch(() => {});
                navigator.sendBeacon(remote + '/beacon', 'synthetic');
                const frame = document.createElement('iframe');
                frame.name = 'csp-target';
                frame.src = remote + '/frame';
                document.body.append(frame);
                const form = document.createElement('form');
                form.action = remote + '/form';
                form.target = 'csp-target';
                document.body.append(form);
                form.submit();
                const literal = document.createElement('pre');
                literal.textContent = '<img src="' + remote + '/literal" '
                    + 'onerror="window.__inlineCspExecuted=true">';
                document.body.append(literal);
                window.__cspLiteral = literal;
            """,
                f"http://127.0.0.1:{sink.server_port}",
            )
            deadline = time.monotonic() + 10
            events = []
            required = {
                "script-src-elem",
                "script-src-attr",
                "img-src",
                "connect-src",
                "frame-src",
                "form-action",
            }
            while time.monotonic() < deadline:
                events = script("return window.__cspEvents;")
                if required <= {item["directive"] for item in events}:
                    break
                time.sleep(0.1)
            assert required <= {item["directive"] for item in events}, events
            assert all(e["disposition"] == "enforce" for e in events), events
            policy = events[0]["policy"]
            assert all(e["policy"] == policy for e in events), events
            assert (
                "'unsafe-inline'" not in policy
                and "'unsafe-eval'" not in policy
                and "localhost:1420" not in policy
            ), policy
            assert "nonce-" in policy or "sha256-" in policy, policy
            assert script("""return !window.__inlineCspExecuted && !window.__handlerCspExecuted
                && !window.__remoteCspExecuted && window.__cspLiteral.children.length === 0;""")
            assert not attempts, attempts
            result = {
                "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                "head": subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
                ).strip(),
                "config_sha256": hashlib.sha256(
                    (ROOT / "desktop/src-tauri/tauri.conf.json").read_bytes()
                ).hexdigest(),
                "probe_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "source_diff": subprocess.check_output(
                    ["git", "diff", "--stat"], cwd=ROOT, text=True
                ),
                "startup": startup,
                "native_dialog": "Ok",
                "effective_policy": policy,
                "violations": events,
                "remote_requests": attempts,
                "result": "PASS",
            }
            (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
            print(json.dumps(result, indent=2))
        finally:
            if session is not None:
                with suppress(Exception):
                    request(base, f"/session/{session}", method="DELETE")
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
            sink.shutdown()
            sink.server_close()


if __name__ == "__main__":
    main()
