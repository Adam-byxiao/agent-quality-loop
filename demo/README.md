# demo

一个离线可跑的切片：图书馆预约助手的修复闭环。业务、数据、提示词都是虚构的。

```bash
pip install -r requirements.txt
python serve.py
# http://127.0.0.1:8788
```

想自动验证一次完整流程：

```bash
python scripts/smoke.py
```

smoke 会用自己的端口和临时数据库，断言 `converged`、资产 `seq 1→2`、回归 8/8、排除 1 条、以及"不可验证改动在回放前被整份拒收"。

## 结构

```
app/fixtures.py   虚构会话、评分、提示词版本与演示剧本
app/store.py      SQLite 账本：9 张表
app/runner.py     逐失败轮次的修复编排
app/mocks.py      观测 / 回放 / 优化器三个替身（独立端口，真实 HTTP）
app/main.py       平台 API + 静态页面
web/              四屏前端，无构建步骤
scripts/smoke.py  端到端断言
```

## 三个替身

| 服务 | 端口 | 行为 |
| --- | --- | --- |
| mock-observability | 9101 | 固定返回 5 个会话；`lib-103` 的失败轮没有回放主键 |
| mock-replay | 9102 | 按冻结主键 + 提示词内容判定；`req_lib_101_t1` 的第二次尝试会在 trial 2 失败 |
| mock-optimizer | 9103 | 异步任务：第一次轮询 running，第二次 succeeded；候选按 `attempt` 切换 |

它们故意是确定性的。演示需要的是"每次都一样"，不是"看起来像真的"。

## 数据落在哪

`demo/data/demo.sqlite3`。删掉它再启动，会重新从替身观测平台导入。

## 端口被占用时

```bash
DEMO_API_PORT=8789 DEMO_OBS_PORT=9111 DEMO_REPLAY_PORT=9112 DEMO_OPT_PORT=9113 python serve.py
```

runner 的目标地址会跟着端口走（`serve.py` 在导入应用前设置）。
