# Stage 1：案件、证据与持久化入口

状态：**实现完成，待统一验证**。前置依赖：[目标设计](incident-agent.md)已确定。

[总计划](incident-agent-implementation-plan.md) · 下一阶段：[Stage 2](incident-agent-stage-2.md)

## 目标与贯通路径

接通“人工描述 → 创建 Case → 补充观察 → 统一提交 → 查看进展与记录”。本阶段建立后续调查共同使用的存储和应用入口，不调用模型。

## 模块与关键接口

| 模块 | 实施约定 |
| --- | --- |
| 新增 `tau_incident/models.py`、`events.py`、`store/` | 领域类型、Command、Receipt、DomainEvent、纯函数 Reducer；`CaseStore.commit(command, expected_versions)` 原子返回回执 |
| 新增 `evidence/`、`execution/` | `ArtifactStore.put/read`、`EvidenceRecorder.record`、`ExecutionRecorder.start/finish/link`；以已有 ID 引用内容 |
| 新增 `coordinator.py`、`reporting/` | `IncidentRuntime.execute(command)`、`get_case(case_id)`、`events(case_id, after_cursor)`；确定性的 CaseBrief 与进展报告 |
| 新增 `tau_coding/incident/{config,host,cli}.py` | `IncidentHost` 组装配置与 Runtime；提供人工入口和生命周期 |
| 修改 `tau_coding/cli.py`、`paths.py`、`pyproject.toml` | 接入案件命令、应用数据路径、新包打包及类型检查范围 |

## 实现步骤

1. **确定模型和标识。** 按设计第 3 节定义 Case、Observation、ClaimRevision、Hypothesis、Task/Attempt、Finding、ReviewIssue、WaitCondition、RequestSnapshot、Report/MemoryCard 与 ExecutionRecord。环境、实体、时间窗、版本、来源为显式字段；观察、候选解释和执行约束分别建模。后续命令随使用阶段补齐，不预埋可绕过提交协议的直接写入口。
2. **实现存储与提交。** 使用 SQLite、显式 schema 版本与迁移；区分领域事件、状态投影、命令回执和执行记录。命令 ID + 内容 hash 实现幂等；事件、Reducer、版本及回执在同一短事务写入。建立任务、预算、报告等对象的版本引用规则，网络和模型工作全部留在事务外。
3. **接通 artifact 与证据。** 文件先完整写入并计算 hash，再登记持久引用；人工观察保存来源和采集范围。定义完整、部分、无匹配、失败及未知覆盖的结果类型。实现受控 ID 回取，失败路径不能留下已提交的悬空引用。
4. **接通执行记录。** 建立操作、trace/span、父子/links 和领域引用；记录人工命令、证据登记及提交结果。提交是否成功以回执为准；本地关键记录失败显式返回。记录查询支持 Case、Attempt、操作类型和游标，完整内容仍保存在原快照或 artifact。
5. **接通应用入口。** `IncidentHost` 显式接收项目、环境和路径，数据默认落在应用生成的 `TAU_HOME/incidents/<project_key>/` 下，领域层只接收路径。沿用 CLI 当前的参数分派方式加入 `tau incident new/show/observe/report`，不将新的子命令体系强加给现有 positional prompt 解析。所有写操作经 Runtime Command。
6. **形成可查看产物。** CaseBrief、确定性进展报告及结构化记录导出使用已提交状态，保留版本、来源和未决项；未调查的案件不产生根因判断。补充对应 CLI 与数据存放说明。

## 交付物与实现完成标准

- [x] 新包已纳入 wheel 和 mypy 范围；领域模块不导入 `tau_coding`、前端或供应商实现。
- [x] CLI 到 Command、事务、证据登记和进展读取的代码路径全部接通，无占位成功返回。
- [x] 幂等、版本、artifact 引用和执行关联具有明确的数据约束，后续阶段复用同一入口。
- [x] 静态检查完成，接口与用户文档已同步；行为覆盖交给 [Stage 7](incident-agent-stage-7.md) 的 V1、V8、V11。

## 阶段检查与记录

2026-09-26：实现保留于当前工作区，未创建 Git 提交。实际模块、提交协议、
命令和失败语义见[Stage 1 实现记录](../architecture/incident-agent-stage-1.md)。

通过 `uv` 完成变更范围的 Ruff lint/format、mypy（14 个源文件）、compileall
与纯模块导入检查，结果通过。未编写或执行测试，未运行人工流程演练。
用户指南和 CLI/配置参考已同步，并注明待统一验证。

Stage 7 重点验证 V1 的事务/幂等/事件重放/artifact 引用，V8 的关联/游标/
未知与本地记录失败，V11 的 CLI 参数分派、项目路径与打包兼容；具体关注项
及检查命令保存在上述实现记录中。本状态不表示行为已验证。
