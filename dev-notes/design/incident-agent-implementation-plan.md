# Incident Agent 总实现计划

状态：Stage 1–6 **实现完成，待统一验证**；Stage 7 **验证中**。见[统一验证记录](../architecture/incident-agent-validation.md)。

2026-09-28 统一改造不增加或重编号 Stage。现行状态归属、持续执行与只读历史边界
以[持续调查 v2](../architecture/incident-agent-v2.md)为准；原计划的任务预算、角色限制和旧案迁移建议已替代。

基线：2026-09-26，提交 `c66fb87`，Python 3.12+、`tau-ai 0.4.5`。架构依据为[目标设计](incident-agent.md)；Tau 的分层与前端接入顺序遵循[路线图 #1](https://github.com/huggingface/tau/issues/1)。本系列 Stage 是 Ariadne 的实施顺序，不重编号 Tau 原有 Phase。

## 1. 背景与目标

现有 Tau 提供模型调用、工具循环、编码会话和多种前端。Ariadne 在其上增加长期故障调查能力：围绕持久 Case 获取证据、比较假设、审查和修正判断，并在等待、并行执行、进程中断后继续调查。

完整交付包括案件与证据、请求级上下文、规划和执行、有界并行、预算与恢复、审查和局部修复、历史记忆、版本化报告、隔离 Python 分析、真实与离线遥测、执行 tracing，以及 CLI/TUI/RPC 和常驻运行入口。产品保持只读调查与处置建议的边界。

各 Stage 在同一条流程上接通能力：

> 人工描述或告警 → Case → 规划 → TaskAttempt → 上下文与工具 → 证据与 Finding → 审查及提交 → 继续、等待或修复 → 报告与历史记忆。

Stage 是开发顺序。全部阶段完成统一验证后交付完整版本；不以中间版本或实验结果决定是否实现已确定能力。

## 2. 当前代码与架构边界

| 边界 | 当前代码 | 实施方式 |
| --- | --- | --- |
| 可复用执行核心 | [harness.py](../../src/tau_agent/harness.py)、[loop.py](../../src/tau_agent/loop.py)、[events.py](../../src/tau_agent/events.py) | 增加可选的请求上下文接口和严格协议检查；复用事件、订阅、取消。核心不依赖 incident、存储路径、UI 或 OpenTelemetry SDK |
| 模型与工具协议 | [provider.py](../../src/tau_agent/provider.py)、[tools.py](../../src/tau_agent/tools.py) | 使用注入的 ModelProvider 和 AgentTool；`tau_ai` 继续负责供应商适配。工具循环目前顺序执行，外层调度承担任务并行 |
| 调查领域 | `tau_incident` 已完成 Stage 1–5 接线，待统一验证 | 审查、局部修复、版本化报告、记忆、live/fixture、隔离分析已沿用案件与证据协议；外部导出留在应用层 |
| 应用组装 | [provider_runtime.py](../../src/tau_coding/provider_runtime.py)、[paths.py](../../src/tau_coding/paths.py)、[context_window.py](../../src/tau_coding/context_window.py) | 在 `tau_coding/incident/` 组装配置、provider、路径、token 估算器、生命周期及外部适配，注入领域层；避免反向依赖 |
| 会话与前端 | [session.py](../../src/tau_coding/session.py)、[session_preparation.py](../../src/tau_coding/session_preparation.py)、[cli.py](../../src/tau_coding/cli.py)、[rpc.py](../../src/tau_coding/rpc.py)、[TUI](../../src/tau_coding/tui/) | 保留 coding 模式；Session 只保存案件绑定和交互。CLI 先接通，Stage 6 TUI/RPC 已消费统一 Host 应用接口，待统一验证 |
| 打包与检查 | [pyproject.toml](../../pyproject.toml)、[CI](../../.github/workflows/ci.yml) | 注册新增包与类型检查范围；依赖和锁文件随实际使用更新。统一验证覆盖受影响的原有功能 |

需要在实施中保持的三项事实：

- `CodingSession` 的持久化、自动压缩和扩展加载不会自动适用于独立调查 Harness；worker 必须显式配置其记录、工具和上下文。
- 当前 `_provider_context()` 会修复中断历史；新增上下文选择后必须严格检查，不能再用修复掩盖裁剪错误。
- `AgentTool.execution_mode`、`AgentToolResult.terminate` 不能当作当前 loop 已实现的并行或退出机制。结构化任务结果在应用执行边界校验，不依赖这些字段结束调查。

具体领域规则引用目标设计，不在阶段文档重复展开。下文出现的新接口名称是实施约定，尚非现有 API。

## 3. Stage 索引与依赖

按表中顺序实施。进入下一阶段要求前置阶段达到“实现完成，待统一验证”；这只表示实现和静态检查完成。

| Stage | 目标 | 前置依赖 | 主要交付物 | 计划 | 状态 |
| --- | --- | --- | --- | --- | --- |
| 1 | 接通人工开案、证据登记和进展查看 | 目标设计 | 领域模型、SQLite 提交协议、artifact 与执行记录、案件 CLI 入口 | [案件与持久化](incident-agent-stage-1.md) | 实现完成，待统一验证 |
| 2 | 接通规划、工具调查、Finding 与进展报告 | Stage 1 | 通用上下文 hook、Executor、Planner、FixtureProvider、请求快照与调用记录 | [自主调查闭环](incident-agent-stage-2.md) | 实现完成，待统一验证 |
| 3 | 支持长期执行和并行结果管理 | Stage 2 | 调度与所有权、预算预留、等待、取消、暂停和恢复 | [长期运行控制](incident-agent-stage-3.md) | 实现完成，待统一验证 |
| 4 | 完成诊断质量控制与跨案件记忆 | Stage 3 | Reviewer、局部修复、完整报告、记忆检索及失效更新 | [审查、报告与记忆](incident-agent-stage-4.md) | 实现完成，待统一验证 |
| 5 | 将同一调查流程接到真实微服务与分析环境 | Stage 4 | Demo 配置、遥测适配、数据导出与回放、Python 容器执行、OTel 导出 | [真实环境与分析](incident-agent-stage-5.md) | 实现完成，待统一验证 |
| 6 | 完成用户入口、后台运行和告警接入 | Stage 5 | 案件 TUI、RPC 扩展、常驻服务、Webhook、时间线与交接操作 | [交互与外部接入](incident-agent-stage-6.md) | 实现完成，待统一验证 |
| 7 | 集中编写、执行测试并完成修复复测 | Stage 1–6 实现完成 | 正确性测试、完整流程验收、Tau 回归、缺陷记录与验证结论 | [统一验证与修复](incident-agent-stage-7.md) | 验证中 |

tracing 从 Stage 1 的执行身份和记录开始，随每一条新执行路径接入；Stage 5 增加外部导出，Stage 6 增加完整交互展示。

## 4. 实施与验证约定

### Stage 1–6 的检查边界

按本次用户指令，前六个 Stage 不编写或执行单元、回归、集成或端到端测试，也不以手工演示替代测试提前验收。只做必要的语法、纯模块导入和改动范围内的静态检查；其中发现的语法、导入与类型问题当期修正。

- Python 命令统一通过 `uv`；可用 `uv run python -m compileall -q <变更文件>` 做语法检查。
- 对变更文件运行 `uv run ruff check <文件>`、`uv run ruff format --check <文件>`，按改动范围运行 `uv run mypy <模块或文件>`；仅在类型依赖要求时扩展检查范围。
- 导入检查不得启动调查、请求模型/遥测、启动后台服务或容器。配置可做 schema 校验及 `docker compose config` 等静态展开。
- FixtureProvider、回放格式和演示数据属于产品能力，可在实现阶段编写；FakeProvider 测试脚本、断言、故障测试及测试数据由 Stage 7 集中编写。
- 实际部署、模型调用、数据导出回放演练、容器分析和界面行为验收统一放到 Stage 7。前序阶段完成的是相应代码与配置接线，不宣称行为已经验证。

此安排取代目标设计中原有“各阶段随实现完成测试”的时间安排，保留全部正确性要求；不修改仓库通用 `AGENTS.md` 或 CI 规则。

### 状态与记录

Stage 1–6 使用“待实现 → 实现中 → 实现完成，待统一验证 → 验证通过”。最后一项只能在 Stage 7 对相应覆盖项验证通过后更新。Stage 7 使用“待执行 → 验证中 → 验证通过”；仍有影响交付的缺陷时保持验证中。

阶段完成时，在该阶段文档末尾记录实现提交、实际模块、静态检查命令与结果、留给统一验证的关注项，并同步本索引。Stage 7 保存测试结果、修复提交、复测范围和验证环境，再同步各阶段验证状态。静态检查通过不计为测试通过。

每阶段更新必要的开发说明；用户功能实现后同步 `website/content/` 的指南和参考，标明当前验证状态。本文和阶段计划只描述实施安排，不提前发布尚未实现的使用说明。

## 5. 环境与交付配置

- 环境按目标设计推荐的 Windows/WSL2、OpenTelemetry Demo + Docker Compose 规划，锁定 Demo 提交、镜像、配置和场景。宿主内存、Docker 安装方式、模型/API 和预算在部署前配置，不改变领域接口。
- 人工开案和告警导入为确定入口。外部上游仍是设计中的选型建议，Stage 6 按 Alertmanager 给出默认适配与演示配置；保留可替换的规范化入口，不将建议追记为已经部署或已确认的企业平台。
- Stage 1 已接通人工入口、持久化与记录查询，详见[实现记录](../architecture/incident-agent-stage-1.md)。本期仅通过静态检查，未执行行为验证；最终交付必须覆盖 Stage 7 的验证矩阵，不安排大规模 benchmark、多模型比较或消融实验。
- Stage 2 已接通已有 Case 的规划、只读 Fixture 工具、Finding 提交、更新上下文与进展输出，详见[实现记录](../architecture/incident-agent-stage-2.md)。Ruff、mypy（29 个源文件）、语法和纯导入检查通过；未编写或执行测试，未运行模型或 Fixture 调查，重要结论保持待复核。
- Stage 3 已接通租约所有权、有界派发、读取依据复验、预算预留与结算、等待、暂停/取消和恢复，详见[实现记录](../architecture/incident-agent-stage-3.md)。Ruff、mypy（24 个源文件）、语法和纯导入检查通过；未编写或执行测试及并发、故障注入、恢复演练。
- Stage 4 已接通 Reviewer、类型依赖、局部修复周期、完整/部分报告、历史卡片检索与失效重建及 CLI 入口，详见[实现记录](../architecture/incident-agent-stage-4.md)。Ruff、mypy（28 个源文件）、语法和纯导入检查通过；未编写或执行测试和审查、修复、记忆演练。模型判断保持未验证。
- Stage 5 已接通固定版本 Demo 配置、Prometheus/Jaeger/OpenSearch、版本化拓扑与真实变更、查询数据集导出回放、隔离 Python 分析和 OTLP/HTTP 导出，详见[实现记录](../architecture/incident-agent-stage-5.md)。Ruff、mypy（36 源文件）、语法、纯导入、配置 schema 和 Compose 静态展开通过。未构建/启动容器、请求模型或遥测、采集、演示或编写运行测试；部署参数与镜像 digest 留待部署前落实。Stage 6 前端与外部服务入口接线见下条记录。

- Stage 6 已接通统一 Host、持久运行队列与常驻服务、重连、CLI/RPC/TUI、Session 绑定、告警 inbox/规范化/去重/关联/自动启动、执行时间线与交接，详见[实现记录](../architecture/incident-agent-stage-6.md)。Ruff、mypy（30 源文件）、语法、17 纯模块导入、配置 schema 及 26 服务 Compose 静态展开通过。未编写或执行测试，未启动服务或容器，未发送告警或演练 UI/RPC；V4/V8/V9/V10/V11 留待 Stage 7。
  完成标准复核后，最新检查扩展到 33 个源文件和 31 个纯模块导入，全部通过；另补齐能力发现一致性、多环境告警分页、启动前关闭与 TUI 状态展示，配置静态展开仍为 26 服务。状态保持“实现完成，待统一验证”。

