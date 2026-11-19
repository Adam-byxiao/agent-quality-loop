# 修复闭环：逐失败轮次推进的状态机

## 为什么不是"整批一次性修"

更早的做法是把整批失败轮次打包给优化器，等它交一份提示词，再整体验证。问题是：

- 一份候选可能只修好其中两条，整批判定只能给"失败"，前一条的进展被抹掉；
- 某一条的失败原因会污染其他条的提案上下文；
- 预算、重试、错误历史只能按批记，无法回答"第 3 条为什么停在第三次"。

所以主流程改为**逐失败轮次串行**：每条独立提案、独立验证、独立记账；接受的资产立即成为下一条的起点。整批一次性修只是它的一种退化情况。

## 主流程

```mermaid
sequenceDiagram
    participant OP as 执行器
    participant DB as 批次账本
    participant OPT as 优化器
    participant RP as 回放服务

    Note over OP,DB: 冻结
    OP->>DB: 读取已固化的会话与评分（不再访问上游）
    OP->>DB: 无 replay_key → skipped（终态）
    OP->>DB: 其余 → queued，写入起始资产

    loop 每个 queued 条目
        OP->>DB: 读取当前资产 + 历史 + error_history
        OP->>OPT: 提交异步任务（第 n 次尝试）
        OPT-->>OP: task_id（原样存档请求体）
        OP->>OPT: 轮询终态
        OPT-->>OP: 候选
        alt 候选含不可验证字段
            OP->>DB: 整份拒收，记录原因（不消耗回放）
        else 候选在允许范围内
            loop 3 次独立回放
                OP->>RP: 冻结主键 + 新主提示词
                RP-->>OP: 逐次通过/失败
            end
            alt 3/3 通过
                OP->>DB: 接受，资产 seq+1
                OP->>RP: 用新资产重测剩余 queued 条目（各 1 次）
                OP->>DB: 命中者 → passed_incidentally
            else 有失败
                OP->>DB: 追加错误历史，attempts+1，重试或耗尽预算
            end
        end
    end

    Note over OP,DB: 收尾
    OP->>RP: 整集回归（所有可回放轮次）
    OP->>DB: 计算终态与报告
```

## 冻结阶段

1. 把选中的失败轮次**全部**收进批次（包括不可回放的）；
2. 逐条检查 `replay_key`：
   - 缺失 → `skipped`，原因写进事件与报告，**不进入队列**；
   - 存在 → `queued`；
3. 冻结起始资产、历史消息、`max_attempts`、验证次数；
4. 写 `batch.frozen` 事件。

关键：跳过发生在**冻结**，不是导入。导入时保留"不可回放"的失败轮次仍然有价值——它会被统计、被报告点名，提醒团队这类缺陷永远无法自动验证。

## 单条目的尝试循环

每次尝试的输入固定为四样：

```json
{
  "failed_turn_id": "ft_...",
  "attempt": 2,
  "current_prompt": "……当前已接受的提示词……",
  "history": ["……该轮次之前的历史消息……"],
  "error_history": ["试次 1：候选包含 tool_description，平台无法通过回放验证"],
  "constraints": { "allowed_changes": ["main_template"] }
}
```

第 n 次尝试的判定顺序：

1. **候选校验**（先于回放，零成本）：只接受 `main_template`；出现 `skill_template`、`tool_description`、`parameter_description` 中的任何一个 → 整份拒收，追加错误历史，`attempts + 1`。理由见 [ADR-0004](adr/0004-refuse-unverifiable-changes.md)。
2. **三次独立回放**：同一个冻结主键、同一份候选、三次独立调用。任一次失败即拒绝，记录每次的结果（不是只记"失败"）。
3. **接受**：更新批次资产，写 `asset` 新行，条目标 `passed`。
4. **重测剩余**：用刚接受的资产，对仍为 `queued` 的条目各回放一次。通过者标 `passed_incidentally`——它没有被单独提案，但当前的资产已经能修好它。这是真实收益，必须记录来源，不能算作"该条提案成功"。
5. **失败**：把失败理由写进 `error_history`（下一次提案能看到），`attempts + 1`；达到 `max_attempts` → `budget_exhausted`。

## 终态语义

| 终态 | 含义 | 算通过吗 |
| --- | --- | --- |
| `converged` | 所有可回放条目通过，回归全过 | **是** |
| `failed` | 回归存在明确失败 | 否 |
| `stalled` | 预算耗尽，仍有条目未通过 | 否 |
| `stopped` | 人工停止 | 否 |
| `error` | 技术异常，无结论 | 否 |

**只有 `converged` 算通过。** `stalled` 不是"接近成功"，`stopped` 不是"差不多完成"。报告必须原样写终态与停止原因。

## 回归

批次结束时，用最终资产跑一遍**所有可回放轮次**（不只是失败过的）：

- 修复可能引入新问题，只看失败轮次会漏掉；
- 每条回放一次（可配置多次），记录逐条结果；
- 不可回放的轮次列在"排除"清单里，写明原因，不混入通过率。

## 幂等与重试

- 同一个 `task_id` + 原样请求体重试是安全的；换了请求体就必须换 `task_id`；
- 回放是**只读重发**，可以重试，但每次 trial 独立计数；
- 任务终态有 TTL（演示中不触发）。超过窗口未落库只能得到 `expired`，不能假装成功。

## 在 demo 里看什么

| 文档主张 | demo 观察点 |
| --- | --- |
| 不可回放的在冻结时跳过 | 第 3 条进入批次但状态 `skipped` |
| 不可验证改动整份拒收 | 第 1 次提案，事件 `candidate_rejected`，回放次数为 0 |
| 3/3 才接受 | 第 2 次提案显示 `1/3 ✓ 2/3 ✓ 3/3 ✗` → 拒绝 |
| 接受后重测剩余 | 第 2 条变为 `passed_incidentally`，来源写明由第 1 条资产修复 |
| 只有 converged 算通过 | 报告顶部状态与停止原因 |
| 回归排除不可回放 | 报告"回归"一节列出 8 通过 / 1 排除 |

下一篇：[诚实口径](04-truthful-reporting.md)
