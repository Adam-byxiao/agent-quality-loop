"""Fictional library-booking data and prompt texts.

Everything here is synthetic. Prompt versions encode the demo script:
attempt 1 carries an unverifiable tool change, attempt 2 is incomplete,
attempt 3 actually fixes the failed turn and also fixes the renewal turn.
"""

PROJECT = {
    "id": "lib-demo",
    "name": "星桥图书馆 · 预约助手质量演练",
    "domain": "library-booking",
}

SCORE_NAME = "task_completion"

P_BASE = """你是「星桥图书馆」的预约助手。可以查询馆藏、办理归还与续借、预约研讨间。

回答要求：
- 只根据查询结果回答，不确定时明确说明。
- 办理成功后简洁说明结果。
"""

P_WHITE_V1 = P_BASE + """
改约研讨间时，逐项核对容量、设备（白板/投影）与开放时段；无法核实时明确告知，并给出替代方案，不要假设可用。
"""

P_WHITE_V2 = P_BASE + """
改约研讨间前，先确认房间容量是否满足人数。
"""

P_FINAL = P_BASE + """
改约研讨间时，逐项核对容量、设备（白板/投影）与开放时段；任一条件无法确认就明确说明，不要假设可用。

多事项请求要逐项回复已完成与未完成的部分，不要只处理第一项。

预约时段必须落在开放时段内；跨越闭馆时间时要提示冲突并给出可行时段。
"""

P_RENEWAL = P_BASE + """
多事项请求要逐项回复已完成与未完成的部分，不要只处理第一项。办理归还、续借后逐项确认结果。
"""


def _turn(index, user, assistant, tools, value, reason, replay_key=None):
    return {
        "index": index,
        "user": user,
        "assistant": assistant,
        "tools": tools,
        "score": {"name": SCORE_NAME, "value": value, "reason": reason},
        "replay_key": replay_key,
    }


SESSIONS = [
    {
        "session_id": "lib-101",
        "title": "研讨间改约（容量与设备）",
        "source": "persona",
        "turns": [
            _turn(
                0,
                "帮我订明天 14:00 的 3 号研讨间。",
                "已为您预订 3 号研讨间，明天 14:00–15:00。",
                [
                    {"name": "room.search", "args": {"date": "tomorrow", "slot": "14:00"}, "ok": True},
                    {"name": "room.book", "args": {"room": "3", "slot": "14:00-15:00"}, "ok": True},
                ],
                1,
                "预约信息完整，时间与房间一致。",
                "req_lib_101_t0",
            ),
            _turn(
                1,
                "我们有四个人，3 号会不会太小？能换一间带白板的吗？",
                "已为您改到 5 号研讨间，时间不变。",
                [
                    {"name": "room.book", "args": {"room": "5", "slot": "14:00-15:00"}, "ok": True},
                ],
                0,
                "未核实 5 号容量与白板条件，也未说明无法确认，直接承诺可改约。",
                "req_lib_101_t1",
            ),
            _turn(
                2,
                "好的，谢谢。",
                "不客气，如需调整随时告诉我。",
                [],
                1,
                "礼貌结束，无业务风险。",
                "req_lib_101_t2",
            ),
        ],
    },
    {
        "session_id": "lib-102",
        "title": "归还与续借（多事项）",
        "source": "persona",
        "turns": [
            _turn(
                0,
                "帮我把《分布式系统》还了，再续借《数据库原理》两周。",
                "《分布式系统》已归还。",
                [
                    {"name": "loan.return", "args": {"title": "分布式系统"}, "ok": True},
                ],
                0,
                "只执行了归还，漏掉续借；没有逐项回复完成情况。",
                "req_lib_102_t0",
            ),
            _turn(
                1,
                "续借呢？",
                "抱歉漏了，已为您续借《数据库原理》14 天。",
                [
                    {"name": "loan.renew", "args": {"title": "数据库原理", "days": 14}, "ok": True},
                ],
                1,
                "补充完成了续借。",
                "req_lib_102_t1",
            ),
        ],
    },
    {
        "session_id": "lib-103",
        "title": "闭馆时间与预约冲突（不可回放）",
        "source": "external_batch",
        "turns": [
            _turn(
                0,
                "今晚几点闭馆？",
                "今晚 21:00 闭馆。",
                [],
                1,
                "信息正确。",
                "req_lib_103_t0",
            ),
            _turn(
                1,
                "那帮我约 20:30 到 21:30 的研讨间。",
                "已预约 2 号研讨间，20:30–21:30。",
                [
                    {"name": "room.book", "args": {"room": "2", "slot": "20:30-21:30"}, "ok": True},
                ],
                0,
                "预约时段跨越闭馆时间，未提示冲突。",
                None,
            ),
        ],
    },
    {
        "session_id": "lib-104",
        "title": "借阅到期提醒",
        "source": "external_batch",
        "turns": [
            _turn(
                0,
                "我的借阅里有哪些快到期？",
                "《算法导论》将在 3 天后到期，其余还有 6 天以上。",
                [{"name": "loan.list", "args": {}, "ok": True}],
                1,
                "信息完整。",
                "req_lib_104_t0",
            )
        ],
    },
    {
        "session_id": "lib-105",
        "title": "自习区咨询",
        "source": "persona",
        "turns": [
            _turn(
                0,
                "附近有安静的自习区吗？",
                "三层东侧自习区，工作日 8:00–22:00，期末周延长至 23:00。",
                [{"name": "space.search", "args": {"quiet": True}, "ok": True}],
                1,
                "信息完整，包含时段差异。",
                "req_lib_105_t0",
            )
        ],
    },
]


def failed_turn_id(session_id: str, index: int) -> str:
    return f"ft_{session_id.replace('-', '')}_t{index}"
