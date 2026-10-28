# 05 · 复盘

> 结论先行：这个题目真正的难点不在"让模型调用几个工具"，而在三个边界——**重复提交时不能把自己的旧草稿当成占用**、**确认边界必须是系统强制而不是模型自觉**、**模型的自然语言不能覆盖工具观测**。后两者一旦松口，就会在演示里变成"虚报已发布/已保存"的关键缺陷。下面按问题、原因、解法、证据记录，最后是学习收获与下一步。

## 1. 最难的问题一：幂等 vs 自身占用 vs 时段变化

**现象（初版真实 bug）**：用同一个 `state_dir` 连续两次提交"M04 排 2026-11-03 10:00"时，第一次保存的草稿在第二次查询可用时段时被当成"已占用"；策略于是重新规划到 11:00 并又保存了一份草稿。结果：重复提交产生了两份草稿，第二次的时段还和用户请求不一致。

**为什么难**：三个目标互相拉扯——

1. 要防重复：同一请求必须只有一份草稿；
2. 要能重排：同一素材换了时段后必须允许重新保存，不能被自己上一份草稿挡住；
3. 要防覆盖：保存时仍要检查别人是否占了这个时段。

**解法（最终实现）**

- **确定性幂等键**：`request_signature = sha1(asset_id|date|channel|requested_time|flexibility)`，`draft_id = "draft-" + sha1(signature|time)[:10]`。相同请求 + 相同选择时段 → 命中同一 `draft_id`，保存工具直接返回已有记录并标记 `deduplicated=true`，不新增记录、不重复写盘（`src/asset_scheduler/store.py` 的 `_request_signature` / `_draft_key` / `save_draft`）。
- **查询与保存时排除自身草稿**：`list_slots(..., exclude_asset_id=asset_id)` 在统计占用时跳过该素材自己的 active 草稿（`src/asset_scheduler/store.py` 的 `_draft_bookings` 与 `save_draft` 占用校验），所以"重排自己"不会被自己挡住；如果换成别人占用了新时段，仍然会得到 `SLOT_CONFLICT`。
- **supersede 取代旧草稿**：同一素材保存新草稿时，旧的 active 草稿标记 `status=superseded`、`superseded_by=新 ID`，新记录写 `supersedes=[旧 ID]`（`src/asset_scheduler/store.py` 的 `save_draft`）。同一素材任何时候只有 1 份 active 草稿。
- **幂等标记跨"核验"保留**：`deduplicated` 是保存工具返回的瞬时标记，不写进持久记录；保存后再调 `get_schedule_draft` 核验时，运行时会把它合并回去（`src/asset_scheduler/agent.py` 的 `ReActAgent._apply_result`），否则"核验一次就把幂等结论弄丢了"。

**证据**

- `examples/outputs/05-duplicate-submission.json`：2 次运行、第 2 次 `draft_status=duplicate_existing`，状态目录 active 草稿数 1（`examples/traces/05-duplicate-submission--r1.md`、`--r2.md`）。
- `examples/outputs/06-replan-after-slot-change.json`：第一次得到 19:00 草稿；把 19:00 追加为他人占用后重跑，重规划到 20:00，旧 19:00 草稿 `superseded`，active 仍为 1（`examples/traces/06-replan-after-slot-change--phase1.md`、`--phase2.md`）。
- 复现命令：`python scripts/generate_examples.py`（检查项含 `drafts_active=1`、`superseded_count=1`）。

这个 bug 也解释了为什么"把查询与保存分成两个工具"是必要的：如果只在保存里做幂等，模型仍可能基于错误观测做出"换时段"的决策，产物就会既有重复草稿、又偏离用户要求。

## 2. 最难的问题二：确认边界

**现象**：最初的实现倾向"模型说需要确认就待确认"，但模型可以忘记标记、也可以被用户话术带偏（例如"M01 必须今天 18:00 发"）。同时，`fixed`（M06 品牌硬性 18:00）与 `preferred`（M01 希望 18:00）必须区别对待：前者绝不能自动换时段，后者可以建议替代时段但必须待确认。

**为什么难**：确认是**业务责任边界**，不是模型能力边界。只靠 system prompt 里的"必须待确认"，在换模型、换温度、换输入时就可能失效；而如果完全不让模型换时段，M01 的"18:00 被占、19:00 可用"参考实例又无法完成。

**解法**

- `fixed`：保存工具直接拒绝任何时段变更（`HARD_REQUIREMENT_CHANGE_BLOCKED`，`src/asset_scheduler/store.py` 的 `save_draft`），运行时结果固定为 `blocked` + "未经用户同意不得改期"（`src/asset_scheduler/agent.py` 的 `build_outcome`）。模型没有绕过路径。发布窗口日期与渠道同样在工具层拒绝（`DATE_OUT_OF_WINDOW` / `CHANNEL_NOT_ALLOWED`）。
- `preferred`：允许保存替代时段，但**存储层**在发现 `time != requested_time` 时强制 `confirmation_status=pending_confirmation`，并自动补一条"原要求 X 已被占用，是否同意改为 Y？"（`src/asset_scheduler/store.py` 的 `save_draft`）。
- warn 风险（敏感词/画幅）：同样在存储层强制待确认，问题文本带规则 ID 与命中证据。
- 系统没有"确认发布"工具：确认发生在系统外，系统只负责标记；`published` 在序列化层恒为 `false`（`src/asset_scheduler/domain.py`）。
- 输出层用 `completed_pending_confirmation` 与 `pending_confirmations` 表达"草稿存了、但没被批准"。

**证据**

- `examples/outputs/04-slot-conflict-confirm.json`：M01 请求 18:00 被占 → 19:00、`completed_pending_confirmation`、待确认项含"是否同意改为 19:00"、`published=false`。
- `examples/outputs/08-hard-requirement-conflict.json`：M06 `fixed` 18:00 被占 → `blocked`、`suggested_time=null`、无草稿，待确认项要求用户决定。
- `examples/outputs/11-transient-save-retry.json`：M07 敏感词命中 → 草稿保存但强制待确认。

## 3. 最难的问题三：模型虚拟/虚报

**现象**：ReAct 循环里，模型最后一句自然语言很容易写成"已为您发布到抖音""草稿已保存"。如果运行时直接采用模型自述，保存失败（M05）或步数耗尽（用例 13）的场景就会被包装成成功，这是题面明确列为"关键缺陷"的行为。

**为什么难**：模型输出的文本和工具观测是两条信息流，前者自由、后者受约束；只要在任意一处把"模型说"当成"系统状态"，整个安全边界就破了。

**解法**

- **最终结果只由观测推导**：`build_outcome`（`src/asset_scheduler/agent.py`）只读 `AgentState` 中的工具结果；模型结束语只写入 `status_reason`/Trace，不参与 `draft_status`、`review_status`、`task_status`、`published` 的判定。
- **状态优先级写死**：运行 `incomplete`/`failed` 优先于一切；有草稿才是 `completed*`；保存失败是 `failed`；规则阻断是 `blocked`；缺信息是 `needs_input`。
- **序列化层兜底**：`published` 在 `RunResult.to_dict` 与 `AssetOutcome` 里恒为 `false`，CLI 每次结尾打印"已发布：否（系统无发布能力，所有结果均为草稿）"（`src/asset_scheduler/cli.py` 的 `_print_aggregate`、`src/asset_scheduler/domain.py`）。
- **失败即失败**：保存工具 `ok=false` → `draft_status=failed`、`task_status=failed`、`draft_error` 带错误码；预算耗尽 → `run_status=incomplete` + 明确原因。
- **Trace 不写隐藏思维链**：只记录可公开的 `reason`、工具参数与观测摘要，避免"用一段漂亮的推理掩盖事实"。

**证据**

- `examples/outputs/07-save-failure.json`：M05 `draft_status=failed`、`task_status=failed`、active 草稿 0（`examples/traces/07-save-failure.md`）。
- `examples/outputs/13-step-limit-incomplete.json`：`max_steps=2` → run/task 均 `incomplete`，原因"达到步数上限 2，任务未完成"。
- `examples/outputs/10-tool-failure.json`：时段服务失败 → `failed`，没有编造时段、没有保存。

## 4. 其他值得记录的工程问题

- **故障注入必须发生在状态变更之前**：`save_draft` 先做完整性/规则/fixed/占用校验，再调用 `_maybe_fault`，最后才 supersede 旧草稿并写入（`src/asset_scheduler/store.py`）。否则一次注入失败可能留下"旧草稿已被取代、新草稿没写成"的半写状态。
- **查询后到保存前的竞态**：`query_publish_slots` 与 `save_schedule_draft` 会各查一次占用；保存时的二次校验保证即使模型基于稍旧的观测提交，也不会覆盖别人（`src/asset_scheduler/store.py` 的 `save_draft`）。
- **Mock 的定位要说清楚**：Mock 是"确定性策略替身"，它能证明流程、预算、护栏、幂等、失败路径正确，但**不能**证明真实模型会这样决策。README 与 `docs/03-evaluation.md` 都明确写了这一点，避免把 Mock 的 PASS 当成模型能力的证据。

## 5. 学习收获

1. **Agent 的可靠性主要来自工具契约，而不是提示词**：把"能不能做"下沉到工具与存储层后，模型再自由也越不过 fixed、blocking、占用这些硬约束。
2. **结果推导要与决策分离**：模型负责"下一步"，运行时负责"最终状态"。这条分离让失败场景可解释，也让测试可以只断言产物而不用揣测模型文本。
3. **幂等不是加个去重判断，而是一组一致的设计**：确定性 key + 自身草稿排除 + supersede，三者缺一都会出现重复草稿或错误重规划。
4. **确定性 Mock 是很好的测试夹具**：无凭据环境下，它让 14 个用例、106 项检查可重复生成；同时必须诚实标注它的边界。
5. **可观测性要一开始就设计**：Trace 字段（决策理由/工具/参数/观测/下一步）在调试幂等 bug 时直接定位了"自己的草稿被当成占用"。

代码规模：`src/asset_scheduler/` 11 个文件、约 2300 行 Python，零第三方依赖。

## 6. 下一步

按性价比排序：

1. **真实模型在线评测**：配置凭据后，在同一批用例上跑真实模型，记录工具调用序列、是否越权改 fixed、是否虚报保存/发布、步数消耗与延迟；与 Mock 的期望结果做差分。当前协议适配器已经可用，缺的是真实调用数据。
2. **确认闭环**：补"人工确认/退回"接口与 UI，把 `pending_confirmation` 变成可回写状态（确认后仍只生成"可发布草稿"，不直接发布）。
3. **并发与存储**：草稿存储换 SQLite/数据库，加唯一约束（asset_id + active）、事务与乐观锁；补齐多进程/并发测试。
4. **规则引擎化**：把 `rules.json` 的检查逻辑从代码里抽成可配置规则（条件 + severity + 证据模板），支持运营改规则不改代码。
5. **更复杂的排期**：跨日期/跨渠道寻优、多素材同时竞争时段、按渠道优先级与历史数据推荐时段；对应增加"多素材冲突"用例。
6. **可观测性与回归**：把 14 个用例接入 CI（已有 `.github/workflows/ci.yml`），对真实模型跑多次评估稳定性；对工具契约做 schema 校验与版本化。

**未完成/未验证**：真实模型未在线运行（无凭据），因此以上第 1 项只有协议级测试，没有效果数据；并发、UI、数据库均未实现；确认动作目前只在系统外完成。这些限制在 README「已知限制」与 `docs/03-evaluation.md` 中有同样口径的说明。
