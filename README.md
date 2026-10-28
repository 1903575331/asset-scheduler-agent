# 素材审核与发布排期 Agent

小影科技 2027 校招 AI Agent 笔试题 · 第 1 题。用一个受预算约束的 ReAct 循环，替内容团队完成「读素材 → 查审核规则 → 查档期占用 → 保存排期草稿」的排期准备工作。

**一句话定位：只保存草稿、绝不发布。** 系统没有任何发布工具，所有结果里的 `published` 恒为 `false`；遇到风险、冲突、信息缺失或保存失败时，返回明确状态与待确认项，并把最终结论固定为草稿。

- 真实、可复核的运行结果：14 个用例、106 项检查全部通过（`python scripts/generate_examples.py`，详见 `examples/README.md`、`docs/03-evaluation.md`）。
- 参考实例已覆盖：M01 请求 18:00 被占用 → 建议 19:00 并标记待确认、不称"已发布"（`examples/outputs/04-slot-conflict-confirm.json`）。
- 零运行时依赖：只用 Python 标准库（`pyproject.toml` 的 `dependencies = []`）。
- 测试：79 个标准库 unittest 全部通过（0 failure / 0 skip，约 7.5s；本机 Python 3.9.13 与 3.13.0 双版本实测）。
- **真实模型通道本次未在线运行**（环境无 `OPENAI_API_KEY`），只通过本地协议级测试验证了适配器与 ReAct 协议的对接；离线 Mock 只证明流程与安全边界，不代表真实模型的决策效果。

## 环境与安装

| 项目 | 要求 |
| --- | --- |
| Python | 3.9+（`pyproject.toml` 声明 `requires-python >= 3.9`）；本机在 3.9.13 与 3.13.0 上实测 |
| 第三方依赖 | 无。运行时、测试、示例生成均为标准库 |
| 安装 | 不需要。从仓库根目录直接 `python run.py ...` |
| 可选配置 | 复制 `.env.example` 为 `.env`（不要提交真实密钥） |

`requirements.txt` 为空依赖说明，`pip install -r requirements.txt` 是安全空操作。`pyproject.toml` 仅用于声明包元数据与 `asset-scheduler-agent` 入口（`src.asset_scheduler.cli:main`）；本仓库的受支持运行方式是 `python run.py`。

## 快速开始

以下命令均在仓库根目录（`asset-scheduler-agent/`）执行：

```bash
# 1) 环境自检：Python 版本、数据目录、模型后端、预算
python run.py doctor

# 2) 看看有哪些待审素材（M01–M10 全部待审）
python run.py list-pending

# 3) 跑一个用例（推荐先用参考实例：M01 的 18:00 冲突 → 19:00 待确认）
python run.py run --case examples/cases/04-slot-conflict-confirm.json

# 4) 只指定素材，走离线 Mock（结果写入 .runtime/drafts.json，并打印在终端）
python run.py run --asset M01 --model mock

# 5) 发现待处理素材并逐项排期（一次跑完 M01–M10）
python run.py run --pending --limit 10

# 6) 重新生成全部示例输出与 Trace（会重建 examples/outputs、examples/traces、examples/README.md）
python scripts/generate_examples.py

# 7) 测试（标准库 unittest）
python -m unittest discover -s tests -v
```

常用可选参数：

- `--model auto|llm|mock`：模型后端（默认 `LLM_PROVIDER`，未配置时为 `auto`）。
- `--state-dir <目录>`：草稿状态目录（默认 `.runtime`）。
- `--trace-dir <目录>`：把本次运行的 Trace 写到指定目录（Markdown + JSON）。
- `--json-out <路径>`：把聚合结果写成 JSON。
- `--max-steps` / `--max-tool-calls`：覆盖本次预算，用于演示 `incomplete`。
- `--fresh`：运行前清空草稿状态。**演示幂等时不要用**，否则会清掉已有草稿。

退出码：`0` 成功；`2` 配置/用例/模型调用错误；`3` 至少一次运行因预算耗尽而 `incomplete`。

## 项目结构

```text
asset-scheduler-agent/
├── README.md                 # 本文件（评审入口）
├── run.py                    # 启动器：python run.py <command>
├── pyproject.toml            # 元数据 + 零依赖声明 + CLI 入口
├── requirements.txt          # 空依赖说明
├── .env.example              # 模型通道与预算配置样例
├── .github/workflows/ci.yml  # CI：3.9/3.11/3.13 跑测试、doctor、示例生成、关键用例冒烟
├── src/asset_scheduler/
│   ├── cli.py                # 子命令：run / list-pending / doctor；退出码
│   ├── config.py             # env / .env 解析、预算与路径
│   ├── domain.py             # Request / Decision / AgentState / AssetOutcome / RunResult
│   ├── agent.py              # ReAct 循环、预算、观测回填、build_outcome（结果推导）
│   ├── tools.py              # 6 个工具的契约与分发
│   ├── store.py              # 素材/规则/档期/草稿；幂等、supersede、故障注入、原子写
│   ├── policy.py             # 离线确定性 Mock 决策器
│   ├── llm.py                # OpenAI 兼容 tool calling 适配器 + build_model
│   ├── runner.py             # 用例加载/执行/聚合/trace 写出
│   └── trace.py              # Trace 渲染（只记录可观测事实）
├── data/
│   ├── assets.json           # M01–M10 素材
│   ├── rules.json            # 5 条审核规则（blocking / warn）
│   ├── schedule.json         # 12 个时段 + 已有占用
│   └── faults.json           # 仅演示/测试的故障注入
├── examples/
│   ├── cases/                # 14 个输入用例
│   ├── outputs/              # 14 份真实输出（含 checks 与 trace_files 索引）
│   ├── traces/               # 每次运行的 Markdown + JSON Trace
│   └── README.md             # 自动生成的用例/检查/证据索引
├── scripts/generate_examples.py  # 一键重建 examples 并校验预期
├── tests/                    # 标准库 unittest 验收测试
└── docs/
    ├── 01-requirements.md    # 需求、验收点、验收映射
    ├── 02-solution.md        # 架构、工具契约、分支决策、取舍
    ├── 03-evaluation.md      # 评测：用例结果、测试结果、限制
    ├── 04-ai-usage.md        # AI 辅助范围与人工贡献/验证
    └── 05-retrospective.md   # 复盘：难点、学习、下一步
```

运行时产物（默认不提交）：`.runtime/drafts.json`；`--trace-dir`/`--json-out` 指定的文件；`examples/.run/`。

## 模型接入与边界（双通道）

`build_model`（`src/asset_scheduler/llm.py`）按配置选择后端，两条通道共用同一个 ReAct 循环、同一组工具、同一套安全护栏：

| 通道 | 触发方式 | 作用与边界 |
| --- | --- | --- |
| 真实 LLM | 配置 `OPENAI_API_KEY`（或 `DEEPSEEK_API_KEY`），`LLM_PROVIDER=auto`（默认）或 `llm` | 走 OpenAI 兼容 `POST {OPENAI_BASE_URL}/chat/completions`，使用 function tools；把模型返回的第一个 `tool_calls` 解析为动作，无 tool_calls 视为结束 |
| 离线 Mock | 无凭据的 `auto`，或 `--model mock` / `LLM_PROVIDER=mock` | `src/asset_scheduler/policy.py` 的确定性策略；只是"能严格遵守流程的替身"，用来验证运行时契约、预算、护栏与全部失败路径 |

配置项（`.env.example` 已给出样例）：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `LLM_PROVIDER` | `auto` | `auto` / `llm` / `mock`；`llm` 缺 key 会直接报配置错误，不静默回退 |
| `OPENAI_API_KEY` / `DEEPSEEK_API_KEY` | 空 | 真实模型凭据；**不要提交** |
| `OPENAI_BASE_URL` | `https://api.deepseek.com/v1` | OpenAI 兼容网关地址 |
| `OPENAI_MODEL` | `deepseek-chat` | 模型名 |
| `OPENAI_TEMPERATURE` | `0.0` | 生成温度 |
| `AGENT_MAX_STEPS` | `14` | ReAct 步数上限 |
| `AGENT_MAX_TOOL_CALLS` | `16` | 工具调用次数上限 |
| `AGENT_TIMEOUT_SEC` | `60` | 单次模型 HTTP 超时 |
| `AGENT_STATE_DIR` | `.runtime` | 草稿 JSON 状态目录 |

**必须如实说明的边界**：本次交付**没有在线运行真实模型**（环境无凭据），因此无法给出真实模型的决策质量证据；真实通道只经过本地 `ThreadingHTTPServer` 脚本化响应做协议级验证（请求头、消息格式、tool_calls 解析、finish 解析）。所有 `examples/` 输出均来自 Mock，`model.notice` 会明确提示这一点。

## Agent 决策循环与预算

```text
messages = [system 提示, 本次任务说明]
循环:
  若已用步数 >= max_steps        → status=incomplete，结束
  模型 decide(messages, tools, state) → Decision(action=tool|finish)
    finish → 记录结束原因，结束
    tool   → 执行工具 → 把 ToolResult 回填 state → 追加 assistant/tool 消息
             若工具调用已用 >= max_tool_calls → status=incomplete，结束
结束后: 从 state 计算每个素材的 AssetOutcome，写 Trace，返回 RunResult
```

- 模型每轮**只做一个动作**，不允许一次并发多个工具；工具观测以 JSON 文本回灌。
- 失败处理：`finish`/`tool` 之外的非法动作 → `failed`；模型 HTTP/格式错误 → `failed`；未预期异常 → `failed`（保留 Trace）；预算耗尽 → `incomplete`。这些状态都会进入产物，不会被"运行成功"掩盖。
- Mock 与真实模型看到的状态完全一致；更换后端不需要改运行时。

## 工具契约（6 个）

| 工具 | 作用 | 关键参数 | 安全约束 |
| --- | --- | --- | --- |
| `list_pending_assets` | 列出 `status=pending_review` 的素材 | `limit?` | 只读 |
| `get_asset` | 读素材元数据 + 缺失字段 | `asset_id` | 只读 |
| `check_review_rules` | 按 `rules.json` 检查并返回 findings/blocked/needs_confirmation | `asset_id` | 只读 |
| `query_publish_slots` | 查询某日某渠道占用与可用时段 | `asset_id`（必填，用于审计与故障注入）、`date`、`channel` | 只读；不得凭空猜时段 |
| `save_schedule_draft` | 保存排期**草稿** | `asset_id/date/time/channel/decision_note?/open_questions?` | 工具层校验：完整性、blocking 风险、fixed 不改、日期窗口与渠道不可改、时段空闲 |
| `get_schedule_draft` | 读取已保存草稿用于核验 | `draft_id` | 只读 |

统一返回 `{ok, data, error:{code,message,retryable,details}}`。常见错误码：`INCOMPLETE_ASSET`、`RULE_BLOCKING`、`HARD_REQUIREMENT_CHANGE_BLOCKED`、`SLOT_CONFLICT`、`DATE_OUT_OF_WINDOW`、`INVALID_ARGS`、`ASSET_NOT_FOUND`、`DRAFT_NOT_FOUND`，以及注入的 `DRAFT_STORE_UNAVAILABLE` / `DRAFT_STORE_TIMEOUT` / `SLOT_SERVICE_ERROR`。完整契约见 [docs/02-solution.md](docs/02-solution.md) 第 4 节。

## 安全护栏（写在工具与运行时里，不只写在 prompt 里）

1. **无发布能力**：工具清单不含 publish/confirm；序列化里 `published` 恒为 `false`。
2. **硬性要求不可擅改**：`flexibility=fixed` 时，保存工具拒绝任何时段变更（`HARD_REQUIREMENT_CHANGE_BLOCKED`）；发布窗口日期与渠道同样在工具层拒绝（`DATE_OUT_OF_WINDOW` / `CHANNEL_NOT_ALLOWED`）。
3. **阻断风险不可绕过**：保存时由存储层重新执行审核规则，blocking 直接拒绝，不采信模型"已处理"的自述。
4. **确认边界由存储层强制**：改动 preferred 时段或存在 warn 风险时，草稿强制 `confirmation_status=pending_confirmation` 并写入待确认问题。
5. **信息不全不保存**：字段缺失直接 `INCOMPLETE_ASSET`。
6. **最终结果由观测推导**：`build_outcome` 只读工具观测；模型结束语只进入 `status_reason`，不能把失败说成成功、把草稿说成发布。
7. **幂等防重复**：确定性 `draft key`，相同请求重复保存命中同一草稿（`duplicate_existing`）；时段变化则新草稿 supersede 旧草稿。
8. **可审计 Trace**：每步记录模型给出的简短理由、工具名、参数、观测摘要与结果；不记录内部思维链。

## 数据与场景

素材（`data/assets.json`）与用途：

| 素材 | 关键设定 | 覆盖场景 |
| --- | --- | --- |
| M01 | preferred 18:00；2026-11-02 抖音 18:00 已被 M99 占用，19:00 可用 | 参考实例：翻到 19:00 + 待确认，不发布 |
| M02 | 缺 owner、duration_sec、publish_window.date | 素材信息缺失 → `needs_input` |
| M03 | 版权未确认 + 敏感词"全网最便宜" | blocking 风险 → `blocked` |
| M04 | preferred 10:00，2026-11-03 空闲 | 正常排期 / 重复提交 |
| M05 | 保存故障注入（不可重试，永久失败） | 保存失败 → `failed` |
| M06 | fixed 18:00；2026-11-04 18:00 已被 M98 占用 | 硬性要求冲突 → `blocked`，不擅改 |
| M07 | 敏感词"限时秒杀"（warn）+ 保存首次失败可重试 | 动态重试一次成功，仍待确认 |
| M08 | open 要求，视频号 2026-11-05 | Agent 自选最早可用时段 10:00 |
| M09 | preferred 15:00；2026-11-06 全时段已占满 | 无可用时段 → `blocked` |
| M10 | 时段查询故障注入（永久失败） | 工具失败 → `failed`，不伪造时段 |

`data/schedule.json` 定义 12 个整点时段（10:00–21:00）与已有占用；`data/rules.json` 定义 5 条规则：完整性/版权/音乐授权为 blocking，促销敏感词/画幅为 warn。

`data/faults.json` 只做演示与测试的故障注入：按「素材 ID + 工具名」配置 `fail_times`、`retryable`、`code`、`message`。失败次数记在状态文件 `meta.fault_attempts`，因此"第一次失败、重试成功"跨进程可复现。当前注入：M05 的 save 永久失败、M07 的 save 失败 1 次可重试、M10 的 slots 永久失败。实测输出见 `examples/outputs/07-save-failure.json`、`11-transient-save-retry.json`、`10-tool-failure.json`。

## 验收点对照表

| 必须验收 | 实现位置 | 证据（真实输出 / Trace） |
| --- | --- | --- |
| 正常排期 | `store.save_draft`（`src/asset_scheduler/store.py`）、`policy._plan_slot`（`src/asset_scheduler/policy.py`） | `examples/outputs/01-normal-slot-free.json`、`12-open-time-auto-pick.json`；`examples/traces/01-normal-slot-free.md` |
| 素材信息缺失 | `store.missing_fields`、`agent.build_outcome` 与 `agent.build_pending_confirmations`（`src/asset_scheduler/agent.py`） | `examples/outputs/02-missing-info.json`；`examples/traces/02-missing-info.md` |
| 规则风险 | `store.check_rules`、`save_draft` 内的阻断校验与 warn 强制待确认（`src/asset_scheduler/store.py`） | `examples/outputs/03-rule-risk-blocked.json`、`11-transient-save-retry.json`；对应 traces |
| 时段冲突 | `store.list_slots` / `_draft_bookings` / `save_draft` 的占用复核与 fixed 校验、`agent.propose_slot`（`src/asset_scheduler/agent.py`） | `examples/outputs/04-slot-conflict-confirm.json`（M01→19:00 待确认）、`08-hard-requirement-conflict.json`（fixed 冲突 blocked）、`09-no-available-slot.json`、`06-replan-after-slot-change.json` |
| 重复提交 | `store._request_signature` / `_draft_key` / 去重返回 / supersede；`agent._apply_result` 保留 `deduplicated` 标记（`src/asset_scheduler/agent.py`） | `examples/outputs/05-duplicate-submission.json`（第 2 次 `duplicate_existing`，active=1）、`06-replan-after-slot-change.json`（19:00 被占后重规划 20:00，旧草稿 superseded，active=1） |
| 保存失败 | `store._maybe_fault`、`agent.build_outcome` 失败分支、`policy._handle_save_error`（`src/asset_scheduler/policy.py`） | `examples/outputs/07-save-failure.json`（M05 `failed`，active=0）、`11-transient-save-retry.json`（重试成功）、`10-tool-failure.json`（工具失败） |

补充：`13-step-limit-incomplete`（`max_steps=2` → run/task `incomplete`）、`14-batch-pending`（先 `list_pending_assets` 再逐项排期，10 个素材、4 份草稿、8 个待确认项）。完整 14 个用例与 106 项检查见 `examples/README.md`。

## 测试与示例生成

测试共 79 个用例，0 failure / 0 error / 0 skip，约 7.5s；本机 Python 3.9.13 与 3.13.0 上均全量通过（命令见下）。

```bash
# 全部测试（标准库 unittest，无第三方包；测试使用临时目录作为 state_dir，不污染仓库）
python -m unittest discover -s tests -v

# 重建 examples 并逐项校验预期（14 用例 / 106 项检查；任一检查失败时退出码非 0）
python scripts/generate_examples.py
```

`scripts/generate_examples.py` 会为每个用例使用干净的 `examples/.run/<case_id>/` 状态目录；阶段化用例（06）会把 `data/` 复制到该目录下的 `data-copy/` 再追加一条临时占用，生成结束时清理 `data-copy`，不改仓库里的 `data/`，因此示例可重复生成、互不污染。生成的 `examples/README.md` 给出用例、预期、实际、PASS/FAIL 与输出/Trace 链接。

## 已知限制、实际投入与未完成项

**已知限制**

- **真实模型未在线验证**：本次环境无模型凭据，无法证明真实 LLM 在同样任务上的决策质量与稳定性；Mock 的通过只说明流程与护栏正确。
- **单进程、无并发控制**：草稿存储在单个 JSON 文件（`.runtime/drafts.json`），用临时文件 + `os.replace` 原子写，但无文件锁；多进程同时写同一状态目录会互相覆盖。
- **确认是离线的**：系统只把问题写进草稿的 `open_questions` 并标记 `pending_confirmation`，没有"人工确认"工具或 UI 回写；确认动作发生在系统之外。
- **排期范围有限**：只在素材的发布窗口当天、指定渠道内选整点时段；没有跨日期/跨渠道寻优，也没有真正的日历服务。
- **规则是固定 5 条**：规则存在于 `data/rules.json`，但检查逻辑是代码内固定实现，不是可配置规则引擎。
- **Trace 不含隐藏思维链**：只记录模型给出的简短 `reason` 与工具事实，便于审计，但不能完整还原模型内部推理。
- **Python 版本**：本机在 3.9.13 与 3.13.0 上全量 unittest 均通过（79/79，约 7.5s）；`requires-python >=3.9`，CI 矩阵覆盖 3.9/3.11/3.13。

**实际投入**：开发由 AI 编程助手在会话中完成（需求拆解、架构、代码、测试、示例与文档），人类负责选题、约束（只存草稿/不发布、fixed 不可改、双通道）、验收与复核。代码规模约 2350 行 Python（`src/asset_scheduler/` 11 个文件，含 `__init__.py`），无第三方依赖；未单独统计人时。

**未完成项**：真实模型在线评测与对比；并发/数据库化；Web UI 与人工确认回写；跨日期/跨渠道寻优；可配置规则引擎；更多渠道素材类型（如直播切片、图文）。真实发布不在范围内，按题目要求不做。

## docs 索引

- [docs/01-requirements.md](docs/01-requirements.md)：用户与目标、输入材料、交付字段、动态决策、6 个必须验收、范围与非目标、可核对成功标准、验收映射。
- [docs/02-solution.md](docs/02-solution.md)：架构、ReAct 时序、工具契约、分支决策、确认边界、幂等与 supersede、持久化、框架取舍、真实模型 vs Mock、错误处理、安全护栏、结果推导。
- [docs/03-evaluation.md](docs/03-evaluation.md)：14 个用例的评测结果、测试结果、Mock 边界与已知限制、评审复现步骤。
- [docs/04-ai-usage.md](docs/04-ai-usage.md)：AI 辅助范围、人工决策与贡献、验证方式、AI 产出被修正的真实例子、未使用内容声明。
- [docs/05-retrospective.md](docs/05-retrospective.md)：最难的问题（幂等 vs 自身占用 vs 时段变化、确认边界、模型虚报）、解决方式、学习收获、下一步。
