"""Per-failed-turn repair orchestration.

The loop is deliberately boring: read the ledger, take one step, write the ledger.
Every decision emits an event so the console can explain what happened without
re-reading code or logs.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request

from . import store

OBSERVABILITY_URL = os.environ.get("DEMO_OBSERVABILITY_URL", "http://127.0.0.1:9101")
REPLAY_URL = os.environ.get("DEMO_REPLAY_URL", "http://127.0.0.1:9102")
OPTIMIZER_URL = os.environ.get("DEMO_OPTIMIZER_URL", "http://127.0.0.1:9103")

STEP_DELAY = float(os.environ.get("DEMO_STEP_DELAY", "0.25"))
POLL_DELAY = float(os.environ.get("DEMO_POLL_DELAY", "0.12"))

UNVERIFIABLE = ("skill_template", "tool_description", "parameter_description")
CHANGE_KEYS = ("main_template", "skill_template", "tool_description", "parameter_description")

_STOP: set[str] = set()
_RUNNING: set[str] = set()


def request_stop(batch_id: str) -> None:
    _STOP.add(batch_id)


def is_running(batch_id: str) -> bool:
    return batch_id in _RUNNING


def _sleep(seconds: float | None = None) -> None:
    time.sleep(STEP_DELAY if seconds is None else seconds)


def _http(method: str, url: str, payload: dict | None = None, timeout: float = 8.0) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {url} -> {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"{method} {url} unreachable: {exc.reason}") from exc


def fetch_sessions() -> dict:
    return _http("GET", f"{OBSERVABILITY_URL}/api/sessions")


def _replay(request_id: str, prompt: str, trial: int) -> dict:
    return _http(
        "POST",
        f"{REPLAY_URL}/replay",
        {"request_id": request_id, "prompt": prompt, "trial": trial},
    )


def _history(turns: list[dict], index: int) -> list[dict]:
    messages = []
    for turn in turns[:index]:
        messages.append({"role": "user", "content": turn["user"]})
        messages.append({"role": "assistant", "content": turn["assistant"]})
    return messages


def _submit_and_wait(
    batch_id: str,
    item: dict,
    failed_turn: dict,
    current_prompt: str,
    attempt: int,
    max_polls: int = 8,
) -> dict:
    request_body = {
        "failed_turn_id": failed_turn["id"],
        "attempt": attempt,
        "current_prompt": current_prompt,
        "history": _history(failed_turn["case_turns"], failed_turn["turn_index"]),
        "error_history": item["error_history"],
        "constraints": {"allowed_changes": ["main_template"]},
    }
    accepted = _http("POST", f"{OPTIMIZER_URL}/prompt-optimization/tasks", request_body)
    task_id = accepted["task_id"]
    store.add_task(
        task_id,
        batch_id,
        failed_turn["id"],
        attempt,
        request_body,
        accepted,
        accepted.get("status", "queued"),
        0,
    )
    for poll in range(1, max_polls + 1):
        time.sleep(POLL_DELAY)
        result = _http("GET", f"{OPTIMIZER_URL}/prompt-optimization/tasks/{task_id}")
        store.add_task(
            task_id,
            batch_id,
            failed_turn["id"],
            attempt,
            request_body,
            result,
            result.get("status", "unknown"),
            poll,
        )
        status = result.get("status")
        if status == "succeeded":
            return result["result"]["candidate"]
        if status == "failed":
            raise RuntimeError(f"optimizer task {task_id} failed")
    raise RuntimeError(f"optimizer task {task_id} did not reach a terminal state")


def _reload(item: dict) -> dict:
    for candidate in store.batch_items(item["batch_id"]):
        if candidate["id"] == item["id"]:
            return candidate
    return item


def _emit(batch_id: str, kind: str, message: str, item: dict | None = None, **data) -> None:
    store.add_event(
        batch_id,
        kind,
        message,
        failed_turn_id=item["failed_turn_id"] if item else None,
        data=data,
    )


def start(batch_id: str) -> bool:
    if batch_id in _RUNNING:
        return False
    thread = threading.Thread(target=run, args=(batch_id,), daemon=True)
    thread.start()
    return True


def run(batch_id: str) -> None:
    _RUNNING.add(batch_id)
    _STOP.discard(batch_id)
    try:
        _run(batch_id)
    except Exception as exc:  # noqa: BLE001 - surfaced as a terminal state, not a crash
        store.add_event(
            batch_id,
            "batch.error",
            f"技术异常：{type(exc).__name__}: {exc}",
        )
        store.update_batch(
            batch_id,
            status="error",
            stop_reason=f"{type(exc).__name__}: {exc}",
            ended_at=store.now(),
        )
        store.refresh_counts(batch_id)
    finally:
        _RUNNING.discard(batch_id)
        _STOP.discard(batch_id)


def _run(batch_id: str) -> None:
    batch = store.get_batch(batch_id)
    if not batch or batch["status"] != "frozen":
        return
    store.update_batch(batch_id, status="running", started_at=store.now())
    _emit(batch_id, "batch.started", "批次开始执行（单进程线程语义，重启会中断）")

    asset = batch["asset"]
    items = store.batch_items(batch_id)
    max_attempts = batch["max_attempts"]
    verify_trials = batch["verify_trials"]

    for item in items:
        if batch_id in _STOP:
            break
        item = _reload(item)
        if item["state"] != "queued":
            continue
        failed_turn = store.get_failed_turn(item["failed_turn_id"])
        if not failed_turn:
            continue

        while True:
            if batch_id in _STOP:
                break
            attempt = item["attempts"] + 1
            _emit(
                batch_id,
                "item.proposal_submitted",
                f"第 {attempt} 次提案提交（资产 seq {asset['seq']}）",
                item,
                attempt=attempt,
                asset_seq=asset["seq"],
            )
            candidate = _submit_and_wait(
                batch_id, item, failed_turn, asset["main_template"], attempt
            )
            changes = [key for key in CHANGE_KEYS if key in candidate]
            _emit(
                batch_id,
                "item.candidate",
                f"候选返回：{', '.join(changes)}",
                item,
                attempt=attempt,
                changes=changes,
            )

            violations = [key for key in UNVERIFIABLE if key in candidate]
            if violations:
                error_history = item["error_history"] + [
                    f"试次 {attempt}：候选包含 {', '.join(violations)}，回放无法注入该字段，整份拒收。"
                ]
                store.update_item(
                    item["id"], attempts=attempt, error_history=error_history
                )
                _emit(
                    batch_id,
                    "item.candidate_rejected",
                    f"整份拒收：{', '.join(violations)} 无法通过回放验证（未消耗回放）",
                    item,
                    attempt=attempt,
                    violations=violations,
                    replays=0,
                )
                item = _reload(item)
                if attempt >= max_attempts:
                    store.update_item(item["id"], state="budget_exhausted")
                    _emit(
                        batch_id,
                        "item.budget_exhausted",
                        f"预算用尽（{attempt}/{max_attempts}），该条无结论",
                        item,
                        attempts=attempt,
                    )
                    break
                continue

            trials = []
            for trial in range(1, verify_trials + 1):
                if batch_id in _STOP:
                    break
                result = _replay(failed_turn["replay_key"], candidate["main_template"], trial)
                passed = bool(result.get("passed"))
                trials.append(
                    {
                        "trial": trial,
                        "passed": passed,
                        "rationale": result.get("rationale", ""),
                        "answer": result.get("answer", ""),
                        "request_id": failed_turn["replay_key"],
                    }
                )
                _emit(
                    batch_id,
                    "item.replay_trial",
                    f"回放 {trial}/{verify_trials}：{'通过' if passed else '失败'}"
                    f"——{result.get('rationale', '')}",
                    item,
                    attempt=attempt,
                    trial=trial,
                    passed=passed,
                )
                _sleep(0.15)

            if batch_id in _STOP:
                break

            store.update_item(item["id"], verification_json=trials)
            passed_all = len(trials) == verify_trials and all(t["passed"] for t in trials)

            if passed_all:
                previous_seq = asset["seq"]
                new_asset = {
                    "main_template": candidate["main_template"],
                    "seq": previous_seq + 1,
                    "hash": store.content_hash(candidate["main_template"]),
                }
                store.add_asset(batch_id, new_asset["seq"], candidate["main_template"], "accepted_candidate")
                store.update_batch(batch_id, asset_json=new_asset)
                asset = new_asset
                store.update_item(
                    item["id"],
                    state="passed",
                    attempts=attempt,
                    changes_json=changes,
                )
                _emit(
                    batch_id,
                    "item.accepted",
                    f"{verify_trials}/{verify_trials} 通过，接受候选；资产 seq {previous_seq} → {new_asset['seq']}",
                    item,
                    attempt=attempt,
                    asset_seq=new_asset["seq"],
                    asset_hash=new_asset["hash"],
                )

                for other in store.batch_items(batch_id):
                    if other["id"] == item["id"] or other["state"] != "queued":
                        continue
                    if not other["replayable"]:
                        continue
                    if batch_id in _STOP:
                        break
                    retest = _replay(other["replay_key"], asset["main_template"], 1)
                    _emit(
                        batch_id,
                        "item.retested",
                        f"用资产 seq {asset['seq']} 重测剩余轮次："
                        f"{'通过，顺手修好' if retest.get('passed') else '仍未通过'}",
                        other,
                        passed=bool(retest.get("passed")),
                    )
                    if retest.get("passed"):
                        store.update_item(other["id"], state="passed_incidentally")
                        _emit(
                            batch_id,
                            "item.passed_incidentally",
                            "重测通过：由其他条目的资产修复，不是本条提案的成果",
                            other,
                        )
                    _sleep(0.12)
                break

            failed_trials = [t["trial"] for t in trials if not t["passed"]]
            passed_count = len(trials) - len(failed_trials)
            error_history = item["error_history"] + [
                f"试次 {attempt}：回放 {passed_count}/{len(trials)} 通过"
                f"（失败 trial {failed_trials}），未接受。"
            ]
            store.update_item(item["id"], attempts=attempt, error_history=error_history)
            _emit(
                batch_id,
                "item.rejected",
                f"回放未全过（{passed_count}/{len(trials)}），拒绝该候选",
                item,
                attempt=attempt,
                passed=passed_count,
                total=len(trials),
            )
            item = _reload(item)
            if attempt >= max_attempts:
                store.update_item(item["id"], state="budget_exhausted")
                _emit(
                    batch_id,
                    "item.budget_exhausted",
                    f"预算用尽（{attempt}/{max_attempts}），该条无结论",
                    item,
                    attempts=attempt,
                )
                break

        store.refresh_counts(batch_id)

    items = store.batch_items(batch_id)
    replayable = [item for item in items if item["replayable"]]

    if batch_id in _STOP:
        _emit(batch_id, "batch.stopped", "人工停止：当前资产保留，批次不算通过")
        _finish(batch_id, "stopped", "人工停止", items, [], [], partial=True)
        return

    batch = store.get_batch(batch_id)
    regression_targets = store.all_replayable_turns()
    _emit(
        batch_id,
        "batch.regression",
        f"整集回归：{len(regression_targets)} 个可回放轮次（回归本身也有抖动，只跑 1 次）",
        total=len(regression_targets),
    )
    checks = []
    for target in regression_targets:
        if batch_id in _STOP:
            break
        result = _replay(target["request_id"], batch["asset"]["main_template"], 1)
        checks.append(
            {
                "session_id": target["session_id"],
                "case_title": target["case_title"],
                "turn_index": target["turn_index"],
                "request_id": target["request_id"],
                "passed": bool(result.get("passed")),
                "rationale": result.get("rationale", ""),
            }
        )
        _sleep(0.1)
    _emit(
        batch_id,
        "batch.regression_done",
        f"回归完成：{sum(1 for c in checks if c['passed'])}/{len(checks)} 通过",
        passed=sum(1 for c in checks if c["passed"]),
        total=len(checks),
    )

    excluded = [
        {
            "session_id": item["session_id"],
            "case_title": item["case_title"],
            "turn_index": item["turn_index"],
            "reason": "无回放主键，永久不可自动验证",
        }
        for item in items
        if not item["replayable"]
    ]

    if not replayable:
        _finish(batch_id, "stalled", "没有可回放的失败轮次", items, checks, excluded)
    elif any(item["state"] == "budget_exhausted" for item in items):
        _finish(batch_id, "stalled", "预算耗尽，仍有条目未通过", items, checks, excluded)
    elif checks and all(check["passed"] for check in checks):
        _finish(batch_id, "converged", "所有可回放轮次通过回归", items, checks, excluded)
    else:
        _finish(batch_id, "failed", "回归存在明确失败", items, checks, excluded)


def _finish(
    batch_id: str,
    status: str,
    reason: str,
    items: list[dict],
    checks: list[dict],
    excluded: list[dict],
    partial: bool = False,
) -> None:
    store.refresh_counts(batch_id)
    batch = store.get_batch(batch_id)
    asset_rows = store.assets(batch_id)
    start_asset = asset_rows[0] if asset_rows else {"seq": 1, "content_hash": "", "content": ""}
    final_asset = batch["asset"]
    report = {
        "batch_id": batch_id,
        "cycle": batch["cycle"],
        "status": status,
        "stop_reason": reason,
        "started_at": batch.get("started_at"),
        "ended_at": store.now(),
        "budget": {
            "max_attempts": batch["max_attempts"],
            "verify_trials": batch["verify_trials"],
        },
        "asset": {
            "start_seq": start_asset["seq"],
            "start_hash": start_asset.get("content_hash"),
            "final_seq": final_asset["seq"],
            "final_hash": final_asset["hash"],
            "final_prompt": final_asset["main_template"],
        },
        "counts": batch["counts"],
        "items": [
            {
                "failed_turn_id": item["failed_turn_id"],
                "case_title": item["case_title"],
                "session_id": item["session_id"],
                "turn_index": item["turn_index"],
                "state": item["state"],
                "replayable": item["replayable"],
                "reason": item["reason"],
                "attempts": item["attempts"],
                "changes": item["changes"],
                "verification": item["verification"],
                "error_history": item["error_history"],
            }
            for item in items
        ],
        "regression": {
            "checks": checks,
            "excluded": excluded,
            "partial": partial,
        },
        "limitations": [
            "候选只验证了主提示词；技能、工具描述、参数描述改动被整份拒收，未验证。",
            "回归每条只跑 1 次，不能代表统计稳定性。",
            "不可回放轮次被排除在回归之外，不计入通过率。",
            "结果来自脚本化替身，确定性输出不代表真实模型分布。",
            "单进程线程执行；重启会中断批次，需要人工继续。",
        ],
    }
    store.save_report(batch_id, report)
    store.update_batch(batch_id, status=status, stop_reason=reason, ended_at=report["ended_at"])
    store.refresh_counts(batch_id)
    _emit(
        batch_id,
        "batch.finished",
        f"批次结束：{status}｜{reason}",
        status=status,
        stop_reason=reason,
    )
