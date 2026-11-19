"""Three deterministic stand-ins for the platform's upstream systems.

They speak real HTTP so the platform's transport layer is exercised exactly as it
would be against a live system. Nothing here is random: the same input always
produces the same verdict, which is what makes the demo script reproducible.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException

from .fixtures import (
    P_BASE,
    P_FINAL,
    P_RENEWAL,
    P_WHITE_V1,
    P_WHITE_V2,
    PROJECT,
    SCORE_NAME,
    SESSIONS,
)

observability_app = FastAPI(title="mock-observability")
replay_app = FastAPI(title="mock-replay")
optimizer_app = FastAPI(title="mock-optimizer")

REPLAY_KEYS = {
    turn["replay_key"]
    for session in SESSIONS
    for turn in session["turns"]
    if turn.get("replay_key")
}


@observability_app.get("/health")
def observability_health():
    return {"status": "ok", "service": "mock-observability"}


@observability_app.get("/api/sessions")
def observability_sessions():
    return {"project": PROJECT, "sessions": SESSIONS}


@replay_app.get("/health")
def replay_health():
    return {"status": "ok", "service": "mock-replay"}


def _verdict(request_id: str, prompt: str, trial: int) -> tuple[bool, str, str]:
    if request_id not in REPLAY_KEYS:
        raise HTTPException(status_code=404, detail="no frozen request for this key")

    if request_id == "req_lib_101_t1":
        full = "逐项核对容量、设备" in prompt and "不要假设" in prompt
        partial = "确认房间容量" in prompt
        if full:
            return (
                True,
                "已核对 5 号研讨间容量 6 人、配备白板，14:00–15:00 开放。",
                "答复逐项核对了容量、设备与开放时段。",
            )
        if partial:
            if trial != 2:
                return (
                    True,
                    "5 号研讨间可容纳 6 人。",
                    "本次答复只确认了容量，未提及白板与开放时段。",
                )
            return (
                False,
                "5 号研讨间可容纳 6 人。",
                "仍未核对白板与开放时段，直接承诺可改约。",
            )
        return (
            False,
            "已为您改到 5 号研讨间，时间不变。",
            "未核对容量、白板与开放时段。",
        )

    if request_id == "req_lib_102_t0":
        if "逐项回复" in prompt or "多事项请求" in prompt:
            return (
                True,
                "《分布式系统》已归还；《数据库原理》已续借 14 天，到期日 12-02。",
                "归还与续借均已逐项确认。",
            )
        return (
            False,
            "《分布式系统》已归还。",
            "只处理了归还，漏掉续借。",
        )

    return (True, "回放通过：答复满足该轮要求。", "既有行为未回归。")


@replay_app.post("/replay")
def replay(body: dict):
    request_id = str(body.get("request_id") or "")
    prompt = str(body.get("prompt") or P_BASE)
    trial = int(body.get("trial") or 1)
    passed, answer, rationale = _verdict(request_id, prompt, trial)
    return {
        "status": "succeeded",
        "request_id": request_id,
        "trial": trial,
        "passed": passed,
        "answer": answer,
        "rationale": rationale,
        "score_name": SCORE_NAME,
        "score_value": 1 if passed else 0,
    }


optimizer_app = FastAPI(title="mock-optimizer")
_POLLS: dict[str, int] = {}


@optimizer_app.get("/health")
def optimizer_health():
    return {"status": "ok", "service": "mock-optimizer"}


def _candidate(failed_turn_id: str, attempt: int) -> dict:
    if failed_turn_id == "ft_lib101_t1":
        if attempt == 1:
            return {
                "main_template": P_WHITE_V1,
                "tool_description": "查询房间详情（容量、设备、开放时段）",
                "target_id": "tool.room_detail",
                "rationale": "补充改约前的核对步骤，并扩展房间查询工具描述。",
            }
        if attempt == 2:
            return {
                "main_template": P_WHITE_V2,
                "target_id": "prompt.main",
                "rationale": "只补充容量确认，暂不处理设备与开放时段。",
            }
        return {
            "main_template": P_FINAL,
            "target_id": "prompt.main",
            "rationale": "逐项核对容量、设备与开放时段，并覆盖多事项与闭馆冲突。",
        }
    if failed_turn_id == "ft_lib102_t0":
        return {
            "main_template": P_RENEWAL,
            "target_id": "prompt.main",
            "rationale": "要求多事项逐项回复。",
        }
    return {
        "main_template": P_FINAL,
        "target_id": "prompt.main",
        "rationale": "通用修复。",
    }


@optimizer_app.post("/prompt-optimization/tasks", status_code=202)
def submit_task(body: dict):
    failed_turn_id = str(body.get("failed_turn_id") or "")
    attempt = int(body.get("attempt") or 1)
    task_id = f"task_{failed_turn_id}_a{attempt}"
    _POLLS[task_id] = 0
    return {"task_id": task_id, "status": "queued"}


@optimizer_app.get("/prompt-optimization/tasks/{task_id}")
def get_task(task_id: str):
    _POLLS[task_id] = _POLLS.get(task_id, 0) + 1
    if _POLLS[task_id] < 2:
        return {"task_id": task_id, "status": "running"}
    raw = task_id.removeprefix("task_")
    failed_turn_id, _, attempt_text = raw.rpartition("_a")
    attempt = int(attempt_text or 1)
    candidate = _candidate(failed_turn_id, attempt)
    return {
        "task_id": task_id,
        "status": "succeeded",
        "result": {"candidate": candidate},
    }
