# 01 · 需求说明（素材审核与发布排期 Agent）

> 结论先行：本项目的交付物是**排期草稿 + 审核结论 + 待确认项**，系统没有任何发布能力，`published` 恒为 `false`。需求验收不追求"模型看起来聪明"，而追求四件事：事实来自工具、硬性要求不被擅改、冲突/风险/缺失时能停下来请求确认、重复与失败时结果可核对。6 个必须验收点全部有真实运行输出与 Trace（映射见第 8 节）。

## 1. 背景与用户

- 出题方：小影科技 2027 校招 AI Agent 笔试题，第 1 题「素材审核与发布排期 Agent」。
- 直接用户：内容/运营团队（素材负责人、渠道运营）。
- 用户目标：把一批待审素材（M01–M10）过一遍审核规则与档期占用，得到**无冲突、可确认**的发布排期草稿，并清楚知道哪些素材还缺信息、哪些有风险、哪些需要人拍板。
- 系统目标：用"模型决策 + 工具执行 + 观测反馈"的 ReAct 循环完成上述工作，并在模型不可靠时用**工具层与运行时的硬约束**兜底。

## 2. 输入与材料

| 输入 | 位置 | 说明 |
| --- | --- | --- |
| 素材元数据 | `data/assets.json` | 10 个素材 M01–M10，含标题、类型、时长、画幅、负责人、审核字段、发布窗口与发布要求 |
| 审核规则 | `data/rules.json` | 5 条规则：完整性、版权、音乐授权为 blocking；促销/敏感词、画幅为 warn |
| 已有排期与可发布时段 | `data/schedule.json` | 12 个整点时段；`bookings` 为已发布/已排期占用 |
| 故障注入 | `data/faults.json` | 仅演示/测试用：按素材 ID + 工具名配置失败次数与错误码 |
| 请求 | `examples/cases/*.json` 或 CLI 参数 | 素材 ID、用户消息、`mode=asset|pending`、上限；一次请求可含多个素材 |
| 运行时配置 | 环境变量 / `.env` | 模型后端、密钥、base_url、模型名、步数与工具调用预算（见 README） |

素材字段的"完整性"口径由代码定义（`src/asset_scheduler/store.py` 的 `missing_fields`）：`title`、`owner`、`asset_type`、`aspect_ratio`、视频的 `duration_sec`、`publish_window.date`、`publish_requirement.channel`、`flexibility` 合法值，以及 fixed/preferred 时的 `requested_time`。M02 即缺少 `owner`、`duration_sec`、`publish_window.date`。

## 3. 交付结果字段

每次运行输出一份 `RunResult`，逐素材给出 `AssetOutcome`（均在 `src/asset_scheduler/domain.py`）：

| 字段 | 含义 | 取值来源 |
| --- | --- | --- |
| `review_status` | 审核状态 | 工具观测推导：`approved` / `needs_confirmation` / `blocked` / `needs_input` / `unverified` |
| `risk_reasons` | 风险原因 | `check_review_rules` 返回的 `findings`（rule_id、severity、message、evidence） |
| `suggested_slot` | 建议时段 | 已保存草稿的 date/time/channel，或按可用时段推导的候选 |
| `pending_confirmations` | 待确认项 | 草稿的 `open_questions` 或由风险/冲突/缺失推导 |
| `draft_status` | 草稿保存状态 | `saved` / `duplicate_existing` / `failed` / `not_attempted` |
| `draft_id` / `draft_error` | 草稿标识 / 失败原因 | 保存工具返回的观测 |
| `task_status` | 任务状态 | `completed` / `completed_pending_confirmation` / `needs_input` / `blocked` / `failed` / `incomplete` |
| `published` | 是否发布 | 恒为 `false`（无发布工具，`RunResult.to_dict` 固定输出 `false`，见 `src/asset_scheduler/domain.py`） |
| `steps_used` / `tool_calls` | 预算消耗 | 运行时计数 |

最终结果**由工具观测计算**（`src/asset_scheduler/agent.py` 的 `build_outcome`），不采用模型自述；模型结束时说的话只写入 Trace 的 `status_reason`，不能改变 `draft_status`、`published` 或 `task_status`。

## 4. 动态决策要求

模型（或离线策略）每一步只能做一件事：调用一个工具，或结束。必须根据观测调整，而不是按固定脚本走：

1. **先补事实**：未读素材先 `get_asset`；未做审核先 `check_review_rules`；未查占用先 `query_publish_slots`。
2. **缺失即停**：M02 缺 owner/时长/窗口日期 → 不再查询时段，直接请求补充信息。
3. **风险分级**：M03 版权未确认（blocking）→ 阻断、不保存草稿；M07 敏感词（warn）→ 可保存但必须待确认。
4. **冲突分流**：`fixed` 被占（M06）→ 不擅自换时段，停下请求用户决定；`preferred` 被占（M01）→ 选最近可用时段并标记待确认；当天无可用时段（M09）→ 未完成并请求确认。硬性要求不限于时段：发布窗口日期与渠道也被工具层限制为不可改（`DATE_OUT_OF_WINDOW` / `CHANNEL_NOT_ALLOWED`）。
5. **失败如实**：保存失败（M05）→ 明确失败；可重试故障（M07 首次失败）→ 允许重试一次；时段服务失败（M10）→ 不猜时段、不保存。
6. **预算约束**：步数/工具调用达到上限 → `incomplete` 并说明原因，不伪造完成（`ReActAgent.run` 的预算检查，`src/asset_scheduler/agent.py`）。

## 5. 必须验收（题面 6 项）

| # | 必须验收 | 验收口径 |
| --- | --- | --- |
| 1 | 正常排期 | 请求时段空闲 → 保存 1 份草稿，`task_status=completed`，`published=false` |
| 2 | 素材信息缺失 | `needs_input`，不保存草稿，输出缺失字段并要求补充 |
| 3 | 规则风险 | blocking 风险（版权/音乐/完整性）阻断、不保存；warn 风险进入待确认 |
| 4 | 时段冲突 | preferred 冲突 → 建议最近可用时段 + `pending_confirmation`；fixed 冲突 → 阻断；满档 → 阻断并请求确认 |
| 5 | 重复提交 | 相同请求重复保存只产生 1 份草稿，第二次 `draft_status=duplicate_existing`；时段变化后重规划替换旧草稿（旧草稿 `superseded`） |
| 6 | 保存失败 | `draft_status=failed`、`task_status=failed`，错误码与原因明确，不出现"已保存/已发布"措辞 |

参考实例（M01）的落地：18:00 被占用、19:00 可用 → 建议 19:00、`completed_pending_confirmation`、`pending_confirmations` 含"是否同意改为 19:00"、`published=false`（`examples/outputs/04-slot-conflict-confirm.json`）。

## 6. 范围与非目标

**范围内**

- 10 个合成素材的审核 + 单日排期草稿；`asset` 与 `pending` 两种请求模式。
- 6 个工具、ReAct 循环、预算中断、Trace 记录、CLI 与示例生成脚本。
- 真实 LLM 适配器（OpenAI 兼容 tool calling）与离线确定性 Mock 双通道。
- 单元/场景测试（`tests/`）覆盖 6 个必须验收与安全护栏。

**非目标（明确不做）**

- 不做真实发布，不接各平台 OpenAPI；无"发布"工具（`src/asset_scheduler/tools.py` 的 `TOOL_SPECS`）。
- 不做 Web UI、鉴权、多租户、消息队列、并发控制与数据库；状态为 JSON 文件（`.runtime/drafts.json`）。
- 不做跨日期/跨渠道的智能改期寻优，只在同一发布窗口内选择可用时段。
- 不用 LangGraph/ADK 等 Agent 框架（取舍见 `docs/02-solution.md`）。
- 本次交付**未在线运行真实大模型**（环境无凭据），Mock 结果不代表真实模型决策效果。

## 7. 可核对的成功标准

以下每条都能用仓库内命令与文件直接核对：

1. `python -m unittest discover -s tests -v` 全部通过：79 个测试、0 failure / 0 error / 0 skip（约 7.5s；本机 Python 3.9.13 与 3.13.0 双版本，结果细节见 `docs/03-evaluation.md`）。
2. `python scripts/generate_examples.py` 退出码 0，14 个用例的 `checks` 全部 `ok=true`（检查明细在各 `examples/outputs/*.json` 的 `checks` 字段与 `examples/README.md` 汇总表）。
3. 所有输出中 `published` 为 `false`，且工具清单（`tools.py` 的 `TOOL_SPECS`）只有 6 个读取/保存草稿工具，没有任何发布类工具。
4. M01 的输出为 19:00 + 待确认（`examples/outputs/04-slot-conflict-confirm.json`），且 Trace 显示先查询时段再保存（`examples/traces/04-slot-conflict-confirm.md`）。
5. 重复提交用例输出 2 次运行的素材结果，但状态目录中 active 草稿数为 1（`examples/outputs/05-duplicate-submission.json` 的 `checks` 含 `drafts_active=1`）。
6. 保存失败用例 `draft_status=failed` 且 `drafts_active=0`（`examples/outputs/07-save-failure.json`）。
7. M06 fixed 冲突时 `suggested_time=null`、`task_status=blocked`（`examples/outputs/08-hard-requirement-conflict.json`）。

## 8. 验收点 → 实现位置 → 证据

| 必须验收 | 实现位置（函数 / 文件） | 真实运行证据 |
| --- | --- | --- |
| 正常排期 | `store.list_slots`、`store.save_draft`（`src/asset_scheduler/store.py`）；`policy._plan_slot`（`src/asset_scheduler/policy.py`） | `examples/outputs/01-normal-slot-free.json`、`examples/outputs/12-open-time-auto-pick.json`；`examples/traces/01-normal-slot-free.md` |
| 素材信息缺失 | `store.missing_fields`、`store.save_draft` 首段校验；`agent.build_outcome`（`src/asset_scheduler/agent.py`） | `examples/outputs/02-missing-info.json`；`examples/traces/02-missing-info.md` |
| 规则风险 | `store.check_rules`、`store.save_draft` 阻断校验；确认标记逻辑（`src/asset_scheduler/store.py`） | `examples/outputs/03-rule-risk-blocked.json`、`examples/outputs/11-transient-save-retry.json`；对应 traces |
| 时段冲突 | `store._draft_bookings`（自身草稿排除）、`store.save_draft` 占用与 fixed 校验；`agent.propose_slot` | `examples/outputs/04-slot-conflict-confirm.json`、`08-hard-requirement-conflict.json`、`09-no-available-slot.json`、`06-replan-after-slot-change.json` |
| 重复提交 | `store._request_signature` / `_draft_key` / 去重；`agent._apply_result` 保留 `deduplicated` 标记；`agent.build_outcome` | `examples/outputs/05-duplicate-submission.json`、`examples/outputs/06-replan-after-slot-change.json` |
| 保存失败 | `store._maybe_fault`；`agent.build_outcome` 失败分支；`policy._handle_save_error`（`src/asset_scheduler/policy.py`） | `examples/outputs/07-save-failure.json`、`examples/outputs/10-tool-failure.json`、`examples/outputs/11-transient-save-retry.json` |

补充验收（题面审核要点延伸）：`13-step-limit-incomplete`（预算耗尽 → `incomplete`）、`14-batch-pending`（`--pending` 发现 10 个待审素材并逐项排期）、`tests/` 中的真实模型适配器协议测试（本地 HTTP 服务模拟 OpenAI 兼容接口）。

## 9. 关键术语

- **草稿（draft）**：写入 `.runtime/drafts.json` 的排期记录，`status=draft`、`published=false`；唯一可写产物。
- **待确认（pending_confirmation）**：草稿已保存但需要人确认（改了 preferred 时段或存在 warn 风险）；确认前不得视为可发布。
- **supersede**：同一素材的新草稿保存后，旧的 active 草稿标记为 `superseded`，保证同一素材只有一份有效草稿。
- **幂等键（draft key）**：`sha1(asset_id|date|channel|requested_time|flexibility | time)` 的前 10 位作为 `draft_id`，相同请求重复保存命中同一记录。
