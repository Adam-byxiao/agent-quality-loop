"""End-to-end smoke test: start the demo, run one batch, check the report.

    python demo/scripts/smoke.py

Uses its own database and ports so it never touches a demo you are using.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

DEMO = Path(__file__).resolve().parents[1]
API = "http://127.0.0.1:8899"


def _request(path: str, method: str = "GET", payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        API + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8") or "{}")


def _wait_health() -> None:
    for _ in range(150):
        try:
            if _request("/api/health").get("status") == "ok":
                return
        except Exception:  # noqa: BLE001 - still starting
            pass
        time.sleep(0.2)
    raise SystemExit("demo API did not become ready on 8899")


def _check(condition: bool, label: str, detail=None) -> None:
    if condition:
        print(f"PASS  {label}")
        return
    print(f"FAIL  {label}  {detail!r}")
    raise SystemExit(1)


def main() -> None:
    workdir = tempfile.mkdtemp(prefix="aql-smoke-")
    env = os.environ.copy()
    env.update(
        {
            "DEMO_DB": str(Path(workdir) / "smoke.sqlite3"),
            "DEMO_API_PORT": "8899",
            "DEMO_OBS_PORT": "9191",
            "DEMO_REPLAY_PORT": "9192",
            "DEMO_OPT_PORT": "9193",
            "DEMO_STEP_DELAY": "0.01",
            "DEMO_POLL_DELAY": "0.01",
        }
    )
    process = subprocess.Popen([sys.executable, "serve.py"], cwd=DEMO, env=env)
    try:
        _wait_health()

        turns = _request("/api/failed-turns")
        _check(len(turns) == 3, "three failed turns imported", len(turns))
        _check(sum(1 for turn in turns if turn["replayable"]) == 2, "two replayable turns")

        created = _request(
            "/api/batches",
            "POST",
            {"cycle": "smoke", "failed_turn_ids": [turn["id"] for turn in turns]},
        )
        batch_id = created["batch_id"]
        _request(f"/api/batches/{batch_id}/start", "POST")

        status = None
        for _ in range(300):
            detail = _request(f"/api/batches/{batch_id}")
            status = detail["batch"]["status"]
            if status not in ("frozen", "running"):
                break
            time.sleep(0.1)
        _check(status == "converged", "batch converged", status)

        report = _request(f"/api/batches/{batch_id}/report")
        _check(report["asset"]["final_seq"] == 2, "asset advanced 1 -> 2", report["asset"])
        states = {item["session_id"]: item["state"] for item in report["items"]}
        _check(states.get("lib-101") == "passed", "lib-101 passed by its own proposal", states)
        _check(
            states.get("lib-102") == "passed_incidentally",
            "lib-102 passed incidentally",
            states,
        )
        _check(states.get("lib-103") == "skipped", "lib-103 skipped at freeze", states)

        checks = report["regression"]["checks"]
        _check(len(checks) == 8, "regression covers 8 replayable turns", len(checks))
        _check(all(check["passed"] for check in checks), "regression 8/8 passed")
        _check(
            len(report["regression"]["excluded"]) == 1,
            "one turn excluded from regression",
            report["regression"]["excluded"],
        )

        kinds = [event["kind"] for event in _request(f"/api/batches/{batch_id}/events")]
        _check("item.candidate_rejected" in kinds, "unverifiable change rejected before replay")
        _check("item.rejected" in kinds, "2/3 candidate rejected")
        _check("item.accepted" in kinds, "3/3 candidate accepted")
        _check("item.passed_incidentally" in kinds, "incidental fix recorded")
        _check("batch.finished" in kinds, "batch reached a terminal state")

        print("\nsummary")
        print(f"  batch    {batch_id}  {report['status']}  ({report['stop_reason']})")
        print(
            f"  asset    seq {report['asset']['start_seq']} -> {report['asset']['final_seq']}"
            f"  {report['asset']['final_hash']}"
        )
        print(
            f"  items    passed={report['counts'].get('passed', 0)}"
            f"  incidental={report['counts'].get('passed_incidentally', 0)}"
            f"  skipped={report['counts'].get('skipped', 0)}"
        )
        print(f"  regression  {sum(1 for c in checks if c['passed'])}/{len(checks)} + 1 excluded")
        print("  smoke passed")
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


if __name__ == "__main__":
    main()
