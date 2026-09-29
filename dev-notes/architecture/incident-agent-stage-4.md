# Amadeus Stage 4：审查、局部修复、版本化报告与历史记忆

状态：**实现完成，待统一验证**。日期：2026-09-27。

本阶段接续工作区内 Stage 1–3 的实现，未创建 Git 提交。没有编写或执行测试，
没有运行模型、Fixture 调查、审查、修复、记忆、迁移或报告演练。
以下描述实现契约，不是行为已经验证的结论。

## 1. 同一调查流程中的角色

`investigation.py` 在普通派发前调度质量任务。`review.py` 的 `Reviewer.review`
使用原有 `RoleRunner`、独立 Harness、受限工具、请求快照和预算；`repair` 继续使用
`InvestigatorExecutor`。`diagnose` 任务通过同一个执行器生成诊断判断，再交给 Reviewer。
这些任务一起占用案件/全局执行额度和本次 `max_tasks` 上限。

新判断默认待审查。证据冲突和重要排除沿用这一入口，不能自行晋升为当前结论。
审查输出包括具体缺口、影响、修正要求、依据、采纳状态和可选的新检查；可以接受、
要求修复、保留未决，也可以建议撤回目标判断。接受仅表示模型审查意见被接纳，
`ClaimRevision.verification` 和报告仍为 `unverified`。

`RecordReview` 与 Finding 使用同一个 `IncidentRuntime.execute → CaseStore.commit`
协议。事务内再次检查所有权代次、执行令牌、任务契约、issue 版本、预算预留、请求结算、
相关已读版本、当前证据和执行约束。模型和工具调用在事务外。提交回执、任务结束、
判断修订和预算释放在同一权威事务内完成。

## 2. 依赖、局部修复与停止

`JudgmentDependency` 区分 `supports`、`opposes`、`derived_from`，分别约束到证据或判断。
旧的 support/opposition/premises 字段继续保留，模型校验确保类型依赖与它们一致。
`InvestigationTask.rationale` 单独记录决策或问题为何触发任务；它不参与判断失效传播。

`ReviseEvidence` 追加适用性修订，不覆盖证据原文。`RepairPlanner.affected_claims`
沿显式判断依赖传播待复核状态；范围候选与仍可用的独立支持交给 Reviewer 检查，
范围重叠本身不证明解释无效。修复 Finding 可通过 `revises` 修订指向的原判断，
保留历史事件；组合原因可以引用多个前提，报告列出相应传播边。

新观测与已有诊断范围相交时会建立复核候选，保留判断本身；源配置版本明确改变则沿
已有依据传播复核。诊断审查必须回应范围内的观测与缺口。普通任务的部分结果也建立
任务问题，接受任务审查时需逐项声明原完成条件；修复任务不递归创建新的修复问题。

问题 ID 来自目标版本、问题类型、范围和依据版本。问题保存 cycle、repair_rounds、
max_repairs、修复任务 ID、审查 attempt、实际检查依据和停止原因。
默认两轮，`--max-repair-rounds` 可配置。修复产生的新判断版本继承同周期轮数，
不会借换版本清零。没有新证据或判断、执行失败、次数上限及预算不足保留未决。
新增相关证据或 `recheck --new-check` 才能开启新周期；普通 retry/revise-task 不能绕过质量周期。

## 3. 报告与记忆

`ReportBuilder.build/commit` 在事务外组装结构化报告，再统一提交。完整诊断要求：

- 至少有一个已经审查的 `diagnosis` 判断；
- 判断均为 current 或 withdrawn，问题已处理；
- 没有相关在途、就绪、等待或待复核任务，也没有 pending wait；
- 输入判断、问题、证据、人工输入、任务状态和证据适用性仍与构建时一致。

报告保存解释、组合前提和传播边、支持与反证、替代解释、缺口、建议、依据截止、
业务影响状态、验证来源、输入版本及 supersedes。报告内容来自模型判断和结构化 Case；
组装部分报告不需要额外模型调用。预算停止或清理结束后仍可保存部分报告。
`SetImpact` 单独提交业务恢复；recovered 需要仍适用、结果 complete 的观测引用，
报告完成不改变业务状态，也不证明观测的因果解释。

`MemoryStore` 从当前报告生成 diagnosis 或 incomplete 卡片，记录来源报告版本、
项目、环境、症状、有效/失败调查路径和未决项。“有效路径”表示完成的调查任务，
不表示生产处置效果已被证实。检索先限制项目和环境，再按实体、症状和时间背景排序；
显式跨环境查询附带差异。历史内容不能作为当前 Evidence ID 提交。

判断进入复核、报告修订、案件重开及相关输入变化在同一领域事件中标记报告/卡片 stale。
`rebuild_pending` 在源事务之后重建卡片；没有当前报告时保持待更新。
检索排除失效/撤回来源，并在请求快照落盘时再次核对源版本。
快照分别保存 `memory_retrieval`（返回版本）和 `memory_selection`（实际进入请求的版本），
历史材料的正文进入请求 artifact；预算紧张时先省略可选历史。

报告与卡片的内容版本不覆盖，历史版本和状态变化可通过领域事件回溯。
withdraw-report 撤回报告及来源卡片，reopen 与撤回会重新打开诊断判断的复核。
生成请求、审查、修复、提交回执、报告和记忆通过 attempt、版本引用与执行 links 关联。

## 4. 接入、持久化与边界

- `models.py`、`events.py`：领域模型、命令和可重放事件。
- `quality.py`：短事务校验、纯 Reducer 的质量更新和派生失效；不执行模型或工具。
- `review.py`、`executor.py`、`planner.py`、`investigation.py`：审查、修复和诊断角色接线。
- `reporting/builder.py`、`memory/`、`context.py`：报告、卡片和请求检索。
- `store/`、`execution/`、`coordinator.py`：统一提交、版本、记录和快照。
- `tau_coding/incident/cli.py`：报告版本、历史查询、重建、复查、撤回、适用性、恢复和契约修订入口。

SQLite schema 升至 4；通过已有事件重建投影并登记版本。旧命令的新默认字段从
幂等 hash 中排除，派生依赖使用旧字段作为 canonical 内容。迁移和旧回执兼容性尚待验证。
没有为领域层引入 Textual、Rich 或应用路径；继续遵循 Pi 的 Harness/环境/前端分离。
没有实现 Stage 5 的真实数据源、容器分析或 OTel 导出，也没有提前实现 Stage 6 前端服务。

## 5. 静态检查

PATH 中没有 `uv`，使用现有 `C:/Users/Meteorite/.local/bin/uv.exe`；默认缓存不可用，
仅将 `UV_CACHE_DIR` 指向 `%TEMP%/amadeus-uv-cache`，使用项目现有环境和 `--no-sync`。

```text
uv run --no-sync ruff check src/tau_incident src/tau_coding/incident
uv run --no-sync ruff format --check src/tau_incident src/tau_coding/incident
uv run --no-sync mypy -p tau_incident -p tau_coding.incident
uv run --no-sync python -m compileall -q src/tau_incident src/tau_coding/incident
uv run --no-sync python -c "import tau_incident.models; import tau_incident.events; import tau_incident.quality; import tau_incident.review; import tau_incident.memory; import tau_incident.reporting; import tau_incident.coordinator; import tau_incident.investigation; import tau_incident.execution.provider; import tau_coding.incident.cli"
```

结果：语法、纯模块导入、Ruff lint/格式与 mypy 检查通过，mypy 覆盖 28 个源文件。
检查发现并修正了类型推断、字段名、导入排序和格式问题。静态检查不计为测试通过。

## 6. Stage 7 待验证关注项

- V3/V5：审查和修复共享容量、预算；并发依据变化和无关提交；旧 attempt 不能接管新 issue。
- V8：显式依赖传播、独立观测保留、组合原因、缩小/撤回判断，以及修复版本继承轮数。
- V8：两轮停止、无进展、预算停止、新证据/新检查重新开周期；取消与任务修订不能绕过限制。
- V10：最终诊断必须经过审查；报告生成与提交间修订；部分报告、业务恢复与诊断完成分离。
- V10：误导记忆、跨项目/环境隔离、历史不能冒充证据、检索后源修订、预算省略材料的追踪。
- V3/V5/V10：报告/卡片原子失效、异步重建、撤回、重开、幂等及未知提交回执；schema 1–3 迁移。
- 原有 Stage 1–3 接口回归、CLI 参数与 JSON/Markdown 展示；实际模型质量和自然语言判断不由静态检查证明。
