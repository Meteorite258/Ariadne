# Ariadne Stage 2：自主调查闭环实现记录

状态：**实现完成，待统一验证**。

日期：2026-09-26。代码保留在当前工作区，未创建 Git 提交；保留 Stage 1
已有改动。依据为[阶段计划](../design/incident-agent-stage-2.md)和
[目标设计](../design/incident-agent.md)。没有编写或执行测试，没有运行模型、
Fixture 调查或手工调查演练。

## 已接通的路径

```text
tau incident run EXISTING_CASE --fixture replay.json
  → 应用解析 provider/model、限额、项目/环境与 replay 时钟
  → IncidentRuntime.run / run_investigation
  → ContextBuilder.build_decision → Planner / 独立 Harness
  → 校验 PlanOutput → RecordPlan Command → Decision + Task + Attempt
  → ContextBuilder.build_task → InvestigatorExecutor / 独立 Harness
  → 只读工具 → EvidenceRecorder → AddObservation Command
  → FindingOutput 校验与有限修正 → SubmissionService.submit
  → 事务内重查 → 事件 + Reducer + Case + attempt 结果 + 回执
  → 下一轮读取已提交 Case，或返回确定性进展和停止原因
```

这是一条已实现的调用链，不是端到端效果验证结果。

Pi/Tau 的分层保持不变：Harness 只增加通用 hook 与协议检查；领域层接收
provider、路径、时钟和 token 估算器；`tau_coding` 组装环境。worker 不创建
CodingSession，不继承 coding 工具、项目指令、扩展或聊天历史，也不叠加其
自动压缩。角色使用同一个 `RoleRunner` 结构化输出边界；后续审查角色可以复用，
本期没有实现 Reviewer。

## 模块与设计选择

| 模块 | 本期变化 |
| --- | --- |
| `tau_agent/request_context.py`、`harness.py`、`loop.py` | 可选异步 `RequestContextHook`，输入是修复后历史的深拷贝，仅输出 system/messages；工具与模型仍由配置决定 |
| `tau_agent/tool_history.py` | 检查调用 ID 唯一、相邻完整结果、工具名、错误状态；投影不得拆分多工具组、伪造或改写调用。无 hook 沿用原路径 |
| `tau_incident/context.py`、`reporting/` | CaseBrief、DecisionContext、TaskContext；范围筛选、预算信息、固定契约、反证/冲突/缺口保留、选择/省略清单、证据回取 |
| `execution/provider.py`、`budget.py` | RecordedProvider、实际请求快照、最终输入估算、输出预留、未知 usage、持久化账本、报告保留额度 |
| `telemetry/` | TelemetryProvider、ServiceCatalog、FixtureProvider，以及显式授权的四种只读工具 |
| `planner.py`、`executor.py` | 独立 Harness、事件保存、有限 turns/格式修正、PlanOutput/FindingOutput schema 校验和领域校验 |
| `submission.py`、`events.py`、`store/`、`models.py` | RecordPlan、RecordRead、SubmitFinding、FinishAttempt；原子提交任务、结果、候选 Claim 和待复核问题 |
| `investigation.py`、`coordinator.py` | 有界顺序循环、停止结果、失败/取消/未知执行状态、确定性进展 |
| `tau_coding/incident/investigation.py`、`cli.py` | provider 配置、限额、Fixture 时钟、生命周期、run/request/budget 用户入口 |
| `tau_coding/provider_runtime.py` | 新增可选 `max_output_tokens`，默认未指定时保留原有行为 |
| `examples/incident/` | 产品回放格式说明与可查询样例，不包含评分答案、测试脚本或断言 |

### 请求顺序与记录

既有历史清理/中断修复之后，hook 分配 request ID 并开始 context 操作。
投影保留完整工具组，必要时把长工具正文转换为有 Evidence/version/operation/
artifact 来源的确定性局部摘要，再省略完整旧交互。摘要无需模型，额外模型成本
为零，摘要文本自身计入下一请求输入估算。固定契约、反证、冲突及缺口不被裁掉；
仍无法装入时抛出 `ContextInsufficient`，运行返回部分进展。

严格协议检查通过后，RecordedProvider 对实际 system、messages、工具 schema
及路由信息估算输入并预留输出，先发布 artifact、保存 RequestSnapshot，再登记
model_request、调用底层 provider。构建或预留失败没有模型调用记录。快照记录
交给 ModelProvider 的中立输入，不声称记录了供应商最终 wire payload。

模型终止消息、usage 与执行状态使用同一 request ID；异常、取消、空结束流均有
明确处理。执行结束时保存 Harness 事件 artifact；provider 终止输出也有独立
artifact。工具操作先记录调用参数，再查询和登记证据。Evidence 的注册操作与
原始工具操作、attempt、trace 均可关联；提交结果以 Receipt 为准。

### 三种上下文

CaseBrief 用于人工进展；DecisionContext 包含已提交状态、已查询范围、任务、
判断、待复核事项、来源能力和预算；TaskContext 固定派发契约与依据。后续请求
只补入本 attempt 新取得或显式回取的证据，不用全案最新解释替换 worker 前提。

环境、实体与时间窗参与确定性选择；既有判断和 Finding 引用的关键反证优先保留。
省略内容保留 ID 和理由，`evidence_read` 通过 Case 内 ID 校验、hash 校验和分页
读取原文，同时提交 RecordRead。自然语言执行约束进入角色上下文；真正的执行
边界由任务 scope、工具白名单、schema 与请求额度检查承担。

### Fixture 与证据

Fixture 按查询条件筛选数据，不消费固定响应序列。每行区分 data_at 与
available_at；时钟可注入，CLI 可以固定 replay 的可用时间。查询支持分页、
单位、采样说明、真实覆盖或覆盖未知，返回 no_match/partial/complete/failed。
能力目录和服务目录可以查询；目录有版本和 scope。产品样例涵盖五种 signal，
只提供观察材料，不给调查者隐藏评分答案。

工具返回 runtime 分配的 Evidence ID。查询成功不等于覆盖完整，无匹配不等于
服务正常；确定性进展保留这些缺口。原始查询、dataset hash 与响应保存在证据
或工具 artifact。数据源/参数错误允许模型读取错误结果；本地存储/提交记录
故障会终止 worker，避免继续调查后误报成功。

### 提交与审查边界

模型只生成候选输出。Runtime 创建 Decision、Task、Attempt、Finding、Claim
及 ReviewIssue 的身份，模型不能直接覆盖 Case。SubmissionService 使用稳定
Finding Command ID 和确定性 ReviewIssue ID；重复相同内容返回原回执。

Store 在短事务内重查 attempt 执行身份、契约、引用版本和允许读取的材料、
scope、未结算请求及完成条件声明。最终 Finding 与 attempt 结果、候选 Claim、
ReviewIssue、领域事件、Case 投影和回执一起提交。一次自由文本结束不是提交。
任务失败仍保留此前独立登记的观测。

所有候选 Claim 均为 `needs_review` 并有开放 ReviewIssue，涵盖其解释及潜在
语义冲突。没有最终 diagnosis、重要排除结论批准或业务恢复判定。字符串完成
条件能校验声明是否覆盖契约、引用是否有效及观察是否失败，但其自然语言含义
和因果正确性仍需后续审查与 Stage 7 验证。

### 预算、存储和本期边界

SQLite schema 1 → 2 增加 requests 与 request_usage；Case 的新增字段有默认值。
Stage 1 人工 AddObservation 的内容 hash 保持原格式，避免旧回执因新增可选
attempt_id 字段失配。迁移与兼容只做了源码检查，尚未做数据库行为验证。

请求账本按 request ID 预留、结算去重。调用次数和 token envelope 累计跨 run；
Task 也有请求额度。未知 usage 保留预留并标 unknown，明确未调用的失败准备
可结算为零。查询区分 settled、reserved、unknown；原始 usage 含供应商返回
的成本字段。默认预留 1 次调用、1024 tokens 用于报告策略，当前进展生成不需模型。
预算模型含 deadline 检查；本期不接收 task 的 monetary limit，以免默默忽略。

应用禁用可配置的 provider 内部重试。支持的适配器接收显式输出 cap；Codex
适配器没有输出 cap 配置，快照标记 `output_limit_enforced=false`。字符估算和
输出预留都不是精确花费上限，实际 usage 可超出预留；已知成本不等于完整网络
重试计费。Stage 3 再把账本接入所有权、并行调度、恢复和额度变更事件。

Stage 2 只执行顺序任务。已有 running attempt 时停止，不接管旧执行；未确认
提交结果保留 unknown。完成的有界 run 可以再次进入规划，已有证据和消耗不清零。
没有实现后台、自动等待唤醒、恢复、局部修复、历史记忆、真实遥测、分析容器、
OTel 导出或 TUI/RPC。

## 静态检查记录

使用现有项目 `.venv` 与 Stage 1 准备的
`%TEMP%/amadeus-stage1-tools/uv.exe`。PATH 中没有 uv；默认缓存目录不可写，
因此显式使用 `UV_CACHE_DIR=%TEMP%/amadeus-stage1-tools/cache`。`--no-sync`
复用已有环境，不安装依赖、改变 PATH 或修改锁文件。

以下 `uv` 指上述绝对路径。检查集合 `CHANGED` 为：

```text
src/tau_incident src/tau_coding/incident
src/tau_coding/provider_runtime.py src/tau_coding/cli.py
src/tau_agent/__init__.py src/tau_agent/request_context.py
src/tau_agent/loop.py src/tau_agent/harness.py src/tau_agent/tool_history.py
```

实际检查命令：

```text
uv run --no-sync ruff check CHANGED
uv run --no-sync ruff format --check CHANGED
uv run --no-sync mypy -p tau_incident -p tau_coding.incident -m tau_coding.provider_runtime -m tau_coding.cli -m tau_agent.request_context -m tau_agent.loop -m tau_agent.harness -m tau_agent.tool_history -m tau_agent
uv run --no-sync python -m compileall -q CHANGED
uv run --no-sync python -c "import tau_agent; import tau_agent.request_context; import tau_agent.tool_history; import tau_incident.models; import tau_incident.events; import tau_incident.store; import tau_incident.evidence; import tau_incident.execution; import tau_incident.execution.provider; import tau_incident.context; import tau_incident.budget; import tau_incident.telemetry; import tau_incident.telemetry.tools; import tau_incident.planner; import tau_incident.executor; import tau_incident.submission; import tau_incident.coordinator; import tau_incident.investigation; import tau_incident.reporting; import tau_coding.incident.config; import tau_coding.incident.host; import tau_coding.incident.investigation; import tau_coding.incident.cli; import tau_coding.provider_runtime; import tau_coding.cli"
uv run --no-sync python -m json.tool examples/incident/replay.json
```

结果：lint、format、mypy（29 个源文件）、compileall、纯导入和样例 JSON
语法检查均通过。检查过程中修正了类型推断、未使用导入与格式问题。纯导入没有
实例化 Host/Store/Provider、创建 Case 数据库、解析 CLI、查询 Fixture 或启动调查。

**未编写或执行任何单元、回归、集成、端到端测试；未运行模型或 Fixture 调查。**
静态通过不证明调查效果、SQL 事务、恢复、CLI 解析或供应商行为。

## 留给 Stage 7 的验证关注项

- V2：已有 Case → 规划 → 工具证据 → Finding → 再规划；无任务、blocked、
  任务上限、无进展、自由文本/截断输出/格式修正上限；真实 CLI 参数和退出路径。
- V3：深拷贝与无 hook 原行为；多调用完整组、重复/孤立 ID、错误状态、防止投影
  伪造；摘要来源、证据回取、反证保留、固定契约和不足时不调用 provider。
- V6：精确的调用次数、请求与任务预留、usage 缺失、取消、重复结算、实际超额、
  报告保留额度、再次 run 的累计消耗及适配器输出限制差异。
- V8：请求/响应/工具/注册/提交的 trace 和 artifact 关联；失败前后本地写入、
  预留失败、COMMIT 未知、回执已存在但执行记录失败；快照不是 wire payload。
- V10：Fixture 时间/可用时间、范围、分页、目录版本、采样、空结果、失败、
  产品回放不依赖调用顺序；provider 异常、取消和不完整终止流。
- V1/V11 相关回归：schema 1 → 2、旧命令 hash、事件重放、最终提交幂等、
  同 attempt 双结果、版本/权限/范围拒绝、项目/环境隔离、wheel 和 Tau 原 CLI。
- Stage 4 接入后验证 pending Claim/ReviewIssue 不绕过审查；重要结论、冲突及
  完成条件的语义正确性不能由 schema/引用检查代替。

用户指南及 CLI/配置参考已同步到 `website/content/`，命令例子仅说明接口。
