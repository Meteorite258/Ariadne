# Stage 2：接通自主调查闭环

状态：**实现完成，待统一验证**。前置依赖：[Stage 1](incident-agent-stage-1.md)实现完成。

[总计划](incident-agent-implementation-plan.md) · 下一阶段：[Stage 3](incident-agent-stage-3.md)

## 目标与贯通路径

将同一个 Case 接入“规划 → 单任务执行 → 查询证据 → Finding 校验提交 → 更新上下文 → 继续或输出进展”。此阶段使用可查询的 FixtureProvider；真实遥测在 Stage 5 接入同一协议，最终诊断的审查门槛在 Stage 4 接通。

## 模块与关键接口

| 模块 | 实施约定 |
| --- | --- |
| `tau_agent/harness.py`、`loop.py`，新增通用上下文类型 | `AgentHarnessConfig.request_context_hook`：接收独立的模型请求上下文副本，返回本轮 system/messages；模型路由和工具授权由配置固定 |
| `tau_agent/tool_history.py` | 新增严格消息协议校验；保留现有中断修复路径及默认无 hook 行为 |
| 新增 `tau_incident/context.py`、`executor.py`、`submission.py`、`budget.py` | `ContextBuilder.build_decision/build_task/project_request`、`InvestigatorExecutor.run(attempt)`、`SubmissionService.submit(finding)`；请求额度预留与结算 |
| 新增 `telemetry/` | `TelemetryProvider.capabilities/query`、`ServiceCatalog.snapshot`、FixtureProvider；查询结果携带实际范围、覆盖和 artifact 来源 |
| 扩展 `coordinator.py`、`execution/` 与应用 host | `Planner.plan(context)`、`IncidentRuntime.run(case_id)`；`RecordedProvider.stream_response` 包装注入的 provider，记录请求、结束、异常和 usage |

## 实现步骤

1. **接入通用请求 hook。** 调整 Harness 到 loop 的参数传递，先执行既有历史清理/中断修复，再对副本投影，随后严格校验消息协议。完整多工具调用及结果按单元处理，保留错误与调用 ID；投影失败不会触发自动补造结果。无 hook 时维持现有路径。
2. **实现三种上下文。** 从已提交 Case 构造 CaseBrief、DecisionContext、TaskContext。固定任务契约，按环境、实体、时间和相关性选择材料，保留反证、冲突及缺口，建立省略清单和证据回取入口。必要时生成带来源和版本的局部任务摘要，生成成本计入预算；不叠加 CodingSession 的自动压缩。应用层注入 token 估算器和模型限额，领域层不导入 `tau_coding/context_window.py`。
3. **接通请求边界。** hook 为本轮建立 request_id 与选择清单；严格协议校验通过后，RecordedProvider 对实际接收到的 system、工具定义、消息检查预算并预留输出，持久化 RequestSnapshot 后才调用底层 provider。通过本次执行上下文关联清单，不向 ModelProvider 协议加入 incident 字段。记录输入输出引用、配置、耗时和 usage；失败或取消也保留状态。先实现单执行下的预算预留、未知 usage 和报告保留额度，Stage 3 将同一账本接入并行调度。
4. **接通只读工具。** FixtureProvider 按查询条件读取产品回放数据，支持模拟时钟、数据可用时间、分页与覆盖说明；能力目录和 ServiceCatalog 也可查询。遥测工具通过 EvidenceRecorder 返回 Evidence ID。创建供产品使用的回放格式与样例数据，评分答案和测试断言留到 Stage 7，不能用固定工具响应序列代替查询。
5. **实现 Planner 与 Executor。** 用独立 Harness 执行，显式注入任务提示词、允许工具、取消与记录器。从最终 assistant 输出解析并校验结构化 Decision/Finding，错误时在预算内进行有限次格式修正。自由文本结束不等于有效完成；不依赖 loop 尚未消费的 `AgentToolResult.terminate`。所有角色后续共用此执行和解析边界。
6. **接通提交与下一轮。** Finding 校验引用、来源、范围和任务完成条件，形成 Command，经 Stage 1 的短事务写入；模型不能直接更改 Case。Planner 读取更新后的状态继续调查。需要重要结论审查或存在语义冲突时保存待复核状态，Stage 4 接通其处理，不能提前标记最终诊断。
7. **扩展同一 CLI。** 加入 `tau incident run`，使用应用层 `create_model_provider` 及配置解析。输出进展、任务与证据引用；停止条件包括暂无可执行任务、执行限制及预算不足，保留可继续的 Case 与确定性部分报告。

## 交付物与实现完成标准

- [x] 从已有 Case 到规划、证据查询、Finding 和下一轮规划的调用链已实现。
- [x] 新 hook、严格协议检查、请求快照、预算和执行记录具有一致的调用顺序。
- [x] worker 不继承 coding 工具、项目指令或未知的 CodingSession 压缩策略。
- [x] 查询、输出校验、上下文不足和 provider 失败都有明确结果；静态检查完成，行为覆盖交给 Stage 7 的 V2、V3、V6、V8、V10。

## 阶段检查与记录

2026-09-26：实现保留在当前工作区，未创建 Git 提交。模块、接线、数据契约、
停止结果和检查命令见[Stage 2 实现记录](../architecture/incident-agent-stage-2.md)。

通过 `uv run --no-sync` 完成 Ruff lint/format、mypy（29 个源文件）、compileall、
纯模块导入及产品样例 JSON 语法检查。未编写或执行测试，未运行模型、Fixture
调查或手工演练。用户指南、CLI 和配置参考已同步。

重要解释全部保存为 `needs_review` Claim 和开放 ReviewIssue，未接通 Stage 4
审查，也不标记最终诊断。Stage 3 的所有权、并行、等待和恢复没有提前实现。
Stage 7 重点覆盖 V2/V3/V6/V8/V10，并补查 schema 迁移、旧回执兼容、事件重放
与 Tau 原 CLI；此状态只说明实现和静态检查完成，不说明调查效果已验证。
