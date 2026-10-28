# 03 · 评测报告

> 结论先行：`python scripts/generate_examples.py` 在离线 Mock 下生成 **14 个用例、106 项字段检查，全部 PASS**；6 个必须验收点（正常排期、信息缺失、规则风险、时段冲突、重复提交、保存失败）都有真实输出与 Trace 可核对。所有用例的 `published` 均为 `false`。**真实模型通道未在线运行**，因此本报告不包含真实 LLM 的决策质量证据；Mock 只证明流程与安全边界。测试结果一节由标准库 unittest 提供（`python -m unittest discover -s tests -v`）。

## 1. 评测口径

- 后端：`offline-policy-mock`（离线确定性 Mock）；**不是**真实大模型。
- 数据：`data/assets.json`、`data/rules.json`、`data/schedule.json`、`data/faults.json`。
- 隔离：每个用例使用独立状态目录 `examples/.run/<case_id>/`；阶段化用例（06）把 `data/` 复制到该目录的 `data-copy/` 并追加一条 19:00 占用，生成结束后清理 `data-copy`，不改仓库里的 `data/`。
- 判定：`scripts/generate_examples.py` 把实际输出与用例 `expect` 的字段逐项比较，写入 `examples/outputs/<case_id>.json` 的 `checks`；任一检查失败时脚本退出码非 0。
- 复现：仓库根目录执行 `python scripts/generate_examples.py`。

## 2. 用例总览（结果取自 `examples/outputs/*.json`）

| # | case_id | 覆盖验收点 | 检查 |
| --- | --- | --- | --- |
| 1 | `01-normal-slot-free` | 正常排期、草稿保存（非发布） | PASS 8/8 |
| 2 | `02-missing-info` | 素材信息缺失 | PASS 7/7 |
| 3 | `03-rule-risk-blocked` | 规则风险 | PASS 7/7 |
| 4 | `04-slot-conflict-confirm` | 时段冲突、确认边界、不得称为已发布（参考实例） | PASS 9/9 |
| 5 | `05-duplicate-submission` | 重复提交 | PASS 7/7 |
| 6 | `06-replan-after-slot-change` | 时段冲突、重复提交、动态重规划 | PASS 9/9 |
| 7 | `07-save-failure` | 保存失败 | PASS 7/7 |
| 8 | `08-hard-requirement-conflict` | 时段冲突、硬性要求不得擅改 | PASS 8/8 |
| 9 | `09-no-available-slot` | 时段冲突（当天已排满） | PASS 8/8 |
| 10 | `10-tool-failure` | 工具失败 | PASS 8/8 |
| 11 | `11-transient-save-retry` | 保存失败（可重试）、动态决策、确认边界 | PASS 9/9 |
| 12 | `12-open-time-auto-pick` | 正常排期（open 要求自选时段） | PASS 9/9 |
| 13 | `13-step-limit-incomplete` | 执行上限、未完成状态 | PASS 7/7 |
| 14 | `14-batch-pending` | 待处理素材发现、批量排期 | PASS 3/3 |

合计：**14 个用例、106 项检查全部通过**；每个用例的输入、输出、Trace 索引见 `examples/README.md`。

## 3. 六个必须验收点：预期 vs 实际

| 必须验收 | 用例（输出 / Trace） | 预期 | 实际结论 |
| --- | --- | --- | --- |
| 正常排期 | `examples/outputs/01-normal-slot-free.json`、`examples/traces/01-normal-slot-free.md` | M04 → 2026-11-03 10:00；`approved` / `saved` / `completed` / `published=false`；active 草稿 1 | 全部符合；草稿 `draft-59adea9c0a` |
| 素材信息缺失 | `examples/outputs/02-missing-info.json`、`examples/traces/02-missing-info.md` | M02 → `needs_input`；不尝试保存；待确认项含"请补充素材信息" | 符合；缺失字段为 `owner, duration_sec, publish_window.date` |
| 规则风险 | `examples/outputs/03-rule-risk-blocked.json`、`examples/traces/03-rule-risk-blocked.md` | M03 → `blocked`；不保存草稿；待确认项含 `R-COPY-01` | 符合；同时命中 `R-SENS-01`（warn）证据 |
| 时段冲突 | `examples/outputs/04-slot-conflict-confirm.json`（M01）、`08-hard-requirement-conflict.json`（M06）、`09-no-available-slot.json`（M09）、`06-replan-after-slot-change.json` | M01：18:00 被占 → 19:00 + `completed_pending_confirmation`，待确认含"是否同意改为 19:00"，不得称已发布；M06：fixed 冲突 → `blocked`、`suggested_time=null`；M09：满档 → `blocked` | 全部符合；M01 `published=false`、草稿 `draft-0d4c9bf5e0`；M06/M09 无草稿 |
| 重复提交 | `examples/outputs/05-duplicate-submission.json`（Traces `05-duplicate-submission--r1.md`、`--r2.md`）、`06-replan-after-slot-change.json` | 两次相同请求：第二次 `duplicate_existing`，同一 `draft_id`，active 草稿 1；时段变化重规划：旧草稿 superseded、active 1 | 符合；05 的检查含 `drafts_active=1`；06 的检查含 `superseded_count=1`、`drafts_active=1` |
| 保存失败 | `examples/outputs/07-save-failure.json`（M05）、`10-tool-failure.json`（M10）、`11-transient-save-retry.json`（M07） | M05：`draft_status=failed`、`task_status=failed`、无草稿、错误码 `DRAFT_STORE_UNAVAILABLE`；M10：时段服务失败 → `failed`；M07：可重试失败重试一次后成功，仍待确认 | 符合；M05 的待确认项为"草稿未保存（DRAFT_STORE_UNAVAILABLE）…请确认是否重试" |

补充：`13-step-limit-incomplete` 用 `max_steps=2` 产生 run/task 双 `incomplete`（"达到步数上限 2，任务未完成"）；`14-batch-pending` 先做一次发现运行（`discovered_assets` = M01–M10），再逐素材排期。

## 4. 实际观察到的数字（均可从 outputs 复核）

- 14 个用例、106 项检查、0 项失败；所有用例 `summary.published=false`。
- 批量用例 `14-batch-pending`：11 次运行（1 次发现 + 10 次逐素材）、10 个素材结果；任务状态分布 `completed_pending_confirmation=2`、`completed=2`、`needs_input=1`、`blocked=3`、`failed=2`；草稿保存 4 份；待确认项 8 个；`published=false`。
- 重复提交 `05`：2 次运行、2 条素材结果，但状态目录 active 草稿 1 份；第 2 次 `draft_status=duplicate_existing`。
- 重规划 `06`：第一阶段 19:00 草稿；19:00 被新增占用后第二阶段改为 20:00，旧草稿 superseded，active=1。
- 保存失败 `07`：`draft_status=failed`、`drafts_active=0`；待确认项明确"草稿未保存"。
- 执行上限 `13`：run status `incomplete`，task status `incomplete`。
- 故障注入：M05 永久失败（`DRAFT_STORE_UNAVAILABLE`）、M07 首次失败后重试成功（save 工具调用 2 次）、M10 时段查询永久失败（`SLOT_SERVICE_ERROR`）。

## 5. 测试结果

测试位于 `tests/`（标准库 unittest，零第三方依赖，用工作区内隔离目录作为状态目录）：

```bash
python -m unittest discover -s tests -v
```

**79 个 unittest 用例全部通过（0 fail / 0 error / 0 skip，约 7.5s），本机在 Python 3.13.0 与 3.9.13 上均全量通过；覆盖工具契约与护栏、M01–M10 全场景、批量 pending、幂等/supersede、预算中断、真实模型适配器协议（本地脚本化 OpenAI 兼容服务）。**

其中 2 个新增回归覆盖 `CHANNEL_NOT_ALLOWED` 渠道护栏（工具层拒绝 + `agent.build_outcome` 映射为 `blocked`）。命令与结果：

- `py -3.13 -m unittest discover -s tests -v` → `Ran 79 tests ... OK`（7.5s）
- `python -m unittest discover -s tests -v`（本机 Python 3.9.13）→ `Ran 79 tests ... OK`（7.6s）

## 6. Mock 边界与真实模型未验证

- 所有 examples 输出的 `model.backend` 为 `offline-policy-mock`，`model.notice` 会提示"Mock 仅验证流程与安全边界，不代表真实模型的决策效果"。
- **真实模型通道本次未在线运行**（环境无 `OPENAI_API_KEY` / `DEEPSEEK_API_KEY`）。`src/asset_scheduler/llm.py` 的 OpenAI 兼容适配器只经过本地协议级测试（脚本化 HTTP 服务返回 tool_calls），验证了请求头、messages/tools 格式、`tool_calls` 解析与 finish 解析。
- 因此本报告**不能**用于声称真实 LLM 也具备同样的冲突处理与确认边界行为；那需要真实调用与多轮评测。

## 7. 失败与限制

| 限制 | 影响 | 说明 |
| --- | --- | --- |
| 真实模型未在线运行 | 无真实模型决策/稳定性数据 | 仅协议级测试；Mock PASS 不等于模型 PASS |
| 单进程 JSON 存储、无并发控制 | 并发写同一状态目录会互相覆盖 | 原子写只能防半写文件，不能防丢更新 |
| 无 UI / 确认回写 | `pending_confirmation` 只能在系统外确认 | 系统只负责标记与给问题 |
| 规则固定为 5 条、逻辑在代码中 | 规则变更需改代码 | 不是可配置规则引擎 |
| 只在发布窗口当天、指定渠道内选整点时段 | 无跨日/跨渠道寻优 | 排期范围收窄，行为更可预测 |
| Python 版本已覆盖 3.9 与 3.13（本机），3.11 仅由 CI 覆盖 | 3.11 未在本机跑 | `pyproject.toml` 声明 `>=3.9`，CI 配置 3.9/3.11/3.13 矩阵 |

## 8. 评审复现建议

```bash
# 参考实例：M01 18:00 被占 → 19:00 待确认（关键：不得称已发布）
python run.py run --case examples/cases/04-slot-conflict-confirm.json --state-dir .runtime-review --trace-dir .runtime-review/traces

# 重复提交：同一 state-dir 连跑两次，只应有一份草稿
python run.py run --case examples/cases/05-duplicate-submission.json --state-dir .runtime-review-dup

# 保存失败：M05 必须明确失败
python run.py run --case examples/cases/07-save-failure.json --state-dir .runtime-review-fail

# 预算耗尽：max_steps=2 应返回 incomplete
python run.py run --case examples/cases/13-step-limit-incomplete.json --state-dir .runtime-review-budget

# 全量示例与测试
python scripts/generate_examples.py
python -m unittest discover -s tests -v
```

观察要点：终端汇总里的 `草稿`、`建议时段`、`待确认项`、`任务状态`、`已发布：否`；`examples/outputs/*.json` 的 `checks`；`examples/traces/*.md` 的"决策与工具调用"表与"最终结果（由工具观测计算）"表。更换占用时段（如给 06 再追加 20:00 占用）后重跑，应继续重规划并 supersede，而不是产生多份 active 草稿。

## 9. 结论

- 6 个必须验收点均以真实输出 + Trace + 自动检查三重证据通过；冲突调整、确认边界、防重复、失败如实报告四项高分依据均有对应用例。
- 系统的关键行为（不发布、fixed/日期/渠道不可改、保存失败如实报告、重复提交只留一份草稿）由工具层与结果推导层强制，不依赖模型自述；不可重试的存储失败落 `failed`，硬性要求/渠道违规落 `blocked`。
- 唯一的关键未验证项是真实大模型的决策质量；这一点已在 README、docs/04、docs/05 与本报告中一致标注，未包装成已完成。
