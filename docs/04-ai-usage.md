# 04 · AI 使用说明

> 结论先行：本交付（代码、测试、示例、文档）由 AI 编程助手在开发会话中生成，人类负责选题、设定硬约束、验收与复核，并对最终产物负责。下面列明 AI 辅助范围、人的决策、验证方式、AI 产出被检查后修正的真实例子，以及**无法验证的部分**。所有"已核对"的说法都对应仓库内可复跑的命令或文件。

## 1. AI 辅助范围

| 范围 | 说明 | 产物位置 |
| --- | --- | --- |
| 架构草案 | ReAct 循环 + 工具层 + 结果推导分层、模型双通道、幂等/supersede 方案 | `src/asset_scheduler/{agent,tools,store,llm,policy}.py`、`docs/02-solution.md` |
| 代码初稿 | 运行时、6 个工具、数据仓库、CLI、配置、Trace、示例生成脚本 | `src/`、`run.py`、`scripts/generate_examples.py` |
| 测试初稿 | 标准库 unittest 验收测试（工具契约、场景、幂等、预算、适配器协议） | `tests/` |
| 示例初稿 | 14 个用例、输出、Trace 与索引 | `examples/`（由 `scripts/generate_examples.py` 生成） |
| 文档初稿 | README 与 docs/01–05 | `README.md`、`docs/` |

AI 生成的内容没有直接当作结论使用：示例输出由脚本真实运行产生，测试结果由 unittest 真实执行产生，文档里引用的路径与数字都回到仓库核对过。

## 2. 人工决策与贡献

以下决策由人（出题/需求方与项目负责人）提出并作为验收条件，不是 AI 自行发挥：

1. **选择第 1 题**「素材审核与发布排期 Agent」，并锚定 6 个必须验收点。
2. **硬约束：只保存草稿、绝不发布**——不接受任何"发布"能力，产物只能是草稿与待确认项。
3. **硬约束：硬性发布要求不可擅改**——`fixed` 冲突时必须停下请求用户决定，不能自动换时段。
4. **双通道要求**：真实 LLM（OpenAI 兼容 tool calling）+ 离线确定性 Mock，且无凭据时必须自动回退并明示。
5. **可复现要求**：示例必须能一键重建（`python scripts/generate_examples.py`），测试必须零第三方依赖、用工作区内隔离目录、不污染仓库。
6. **验收用例与安全边界确认**：M01 参考实例（18:00→19:00 待确认）、重复提交只留一份草稿、保存失败必须明确失败、越权发布/虚报保存成功视为关键缺陷。
7. **诚实边界要求**：真实模型未在线运行时必须如实标注，不得把 Mock 的通过说成模型能力。

## 3. 验证方式（可复跑）

| 验证 | 命令 / 证据 |
| --- | --- |
| 逐用例检查产物 | `python scripts/generate_examples.py` 输出 14 个用例的 `checks`；每个 `examples/outputs/<case_id>.json` 内含 `checks` 与 `trace_files` |
| 人工复核关键用例 | `python run.py run --case examples/cases/04-slot-conflict-confirm.json`（M01→19:00 待确认、`published=false`） |
| 幂等复核 | 同一 `--state-dir` 连续跑两次 M04，或跑 `examples/cases/05-duplicate-submission.json`，状态文件中 active 草稿为 1 |
| 储存失败复核 | `examples/cases/07-save-failure.json`（M05）输出 `draft_status=failed` |
| 单元/场景测试 | `python -m unittest discover -s tests -v`（79 个测试，0 failure / 0 skip，约 7.5s；本机 Python 3.9.13 与 3.13.0） |
| 安全边界代码核对 | `src/asset_scheduler/tools.py` 的工具清单无发布工具；`src/asset_scheduler/store.py` 的 `save_draft` 拒绝 fixed 改期与 blocking 风险；`domain.py` 序列化 `published` 恒为 `false` |
| 环境自检 | `python run.py doctor` |

## 4. AI 产出被发现并修正的真实例子

以下三项都可在当前代码/示例中核对，是"AI 初版有问题、经检查后修正"的记录：

**① 重复提交把上一份草稿当成时段占用**

- 现象：同一素材第二次保存时，上一份草稿被计入占用，策略被迫重规划到另一个时段，并产生第二份草稿；重复提交用例一度不满足"只产生一份草稿"。
- 修正：查询与保存校验时排除该素材自己的草稿（`src/asset_scheduler/store.py` 的 `_draft_bookings` 与 `save_draft`）；用确定性请求签名做幂等键（同文件的 `_request_signature` / `_draft_key`）；新草稿保存时 supersede 旧草稿（同文件的 `save_draft`）。
- 核对：`examples/outputs/05-duplicate-submission.json`（第 2 次 `duplicate_existing`、active=1）、`examples/outputs/06-replan-after-slot-change.json`（时段被占后重规划、旧草稿 superseded、active=1）。复盘见 `docs/05-retrospective.md` 第 1 节。

**② 保存草稿时待确认问题重复**

- 现象：模型在 `open_questions` 里传了与工具自动生成的同类问题，草稿的待确认项出现重复文案。
- 修正：`save_draft` 先接收模型传入的问题，再仅在不存在时插入工具生成的默认问题（"原要求 X 已被占用，是否同意改为 Y？"）和 warn 风险问题（`src/asset_scheduler/store.py` 的 `save_draft`）。
- 核对：`examples/traces/04-slot-conflict-confirm.md` 的草稿 `open_questions` 只有一条"原要求 18:00 已被占用，是否同意改为 19:00？"。

**③ 保存后核验草稿会把幂等标记弄丢**

- 现象：首次保存返回 `deduplicated=true` 后，再调用 `get_schedule_draft` 核验，读到的持久记录没有该标记，最终结果可能把"命中去重"误报成"新保存"。
- 修正：`deduplicated` 是保存返回的瞬时标记，不持久化；运行时在读取草稿时若之前观测到该标记则合并回去（`src/asset_scheduler/agent.py` 的 `ReActAgent._apply_result`）。
- 核对：`examples/outputs/05-duplicate-submission.json` 的第二次运行仍为 `draft_status=duplicate_existing`。

## 5. 未使用内容

- 未使用真实模型对话记录：本次真实模型通道未在线运行（无凭据），没有可引用的真实调用日志；只通过本地脚本化 HTTP 服务验证协议。
- 未提交任何私人聊天记录、账号、密钥或客户数据；`data/` 中素材、规则、排期均为合成数据，素材 ID 为 M01–M10（另有 M77/M81–M99 等仅为排期占用占位）。
- 未使用第三方 Agent 框架或脚手架（LangGraph/ADK 等均未引入），无运行时第三方依赖；`requirements.txt` 为空依赖说明。
- 文档中的数字（14 个用例、106 项检查、批量 10 个素材的状态分布等）均来自 `examples/outputs/*.json` 与脚本运行输出，未手工编造。

## 6. 无法验证 / 如实声明

- **真实模型的决策质量与稳定性**：未在线运行，无法给出真实模型的工具调用序列、越权行为或延迟数据；Mock 的 PASS 只证明流程与安全边界。
- **多进程并发**：草稿为单 JSON 文件、无锁，未做并发验证（明确非目标）。
- **Python 3.11**：本机已验证 Python 3.9.13 与 3.13.0（79 个测试均通过）；3.11 仅由 CI 矩阵覆盖，未在本机执行。
- **人工确认闭环**：系统只写 `pending_confirmation` 与问题清单，没有确认/退回的 UI 或接口，无法验证确认后流程。
- 上述限制在 README「已知限制」、`docs/03-evaluation.md` 与 `docs/05-retrospective.md` 中口径一致，均未包装成"已完成"。
