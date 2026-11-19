"""Start the demo: three stand-in services + the platform API/UI."""

from __future__ import annotations

import os
import threading
import time
import urllib.request

API_PORT = int(os.environ.get("DEMO_API_PORT", "8788"))
OBS_PORT = int(os.environ.get("DEMO_OBS_PORT", "9101"))
REPLAY_PORT = int(os.environ.get("DEMO_REPLAY_PORT", "9102"))
OPTIMIZER_PORT = int(os.environ.get("DEMO_OPT_PORT", "9103"))

# Set before importing the app: the runner reads these at import time.
os.environ.setdefault("DEMO_OBSERVABILITY_URL", f"http://127.0.0.1:{OBS_PORT}")
os.environ.setdefault("DEMO_REPLAY_URL", f"http://127.0.0.1:{REPLAY_PORT}")
os.environ.setdefault("DEMO_OPTIMIZER_URL", f"http://127.0.0.1:{OPTIMIZER_PORT}")

import uvicorn  # noqa: E402

from app.main import app as platform_app  # noqa: E402
from app.mocks import observability_app, optimizer_app, replay_app  # noqa: E402

SERVICES = [
    ("mock-observability", observability_app, OBS_PORT, "/api/sessions"),
    ("mock-replay", replay_app, REPLAY_PORT, "/health"),
    ("mock-optimizer", optimizer_app, OPTIMIZER_PORT, "/health"),
]


def _start(app, port: int) -> uvicorn.Server:
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    threading.Thread(target=server.run, daemon=True).start()
    return server


def _wait(url: str, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as response:
                if response.status == 200:
                    return True
        except Exception:  # noqa: BLE001 - retry until the deadline
            time.sleep(0.2)
    return False


def main() -> None:
    servers = [_start(app, port) for _, app, port, _ in SERVICES]
    for name, _, port, path in SERVICES:
        if not _wait(f"http://127.0.0.1:{port}{path}"):
            raise SystemExit(f"{name} did not become ready on port {port}")

    servers.append(_start(platform_app, API_PORT))
    if not _wait(f"http://127.0.0.1:{API_PORT}/api/health"):
        raise SystemExit(f"platform API did not become ready on port {API_PORT}")

    print("")
    print("  Agent Quality Loop - demo")
    print(f"  UI / API              http://127.0.0.1:{API_PORT}")
    print(f"  mock-observability    http://127.0.0.1:{OBS_PORT}/api/sessions")
    print(f"  mock-replay           http://127.0.0.1:{REPLAY_PORT}/health")
    print(f"  mock-optimizer        http://127.0.0.1:{OPTIMIZER_PORT}/health")
    print("")
    print("  Ctrl+C to stop (state lives in demo/data/demo.sqlite3)")
    print("")

    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        for server in servers:
            server.should_exit = True
        time.sleep(0.5)
        print("stopped")


if __name__ == "__main__":
    main()
