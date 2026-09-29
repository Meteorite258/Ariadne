# Stage 6：案件交互、常驻运行与外部接入

状态：**实现完成，待统一验证**。前置依赖：[Stage 5](incident-agent-stage-5.md)实现完成。

[总计划](incident-agent-implementation-plan.md) · 下一阶段：[Stage 7](incident-agent-stage-7.md)

## 目标与贯通路径

让人工描述、导入告警与外部事件进入同一 Case 流程，完成案件工作区、聊天引导、CLI/RPC 操作、后台续查和执行时间线。复用前五阶段的领域与执行实现。

## 模块与关键接口

| 模块 | 实施约定 |
| --- | --- |
| `tau_coding/incident/host.py` | `dispatch(action)`、`query(case_id, view)`、`events(case_id, after_cursor)`，统一转发 Runtime Command 与查询 |
| 新增应用层 `incident/service.py`、`intake.py`、`rpc.py` | 常驻进程、受控 HTTP 入口、告警格式适配、`incident.*` RPC 分派；领域层只处理规范化输入 |
| `tau_incident/models.py`、`store/` 与协调者 | AlertEvent、inbox 接收记录、去重/关联策略和外部 incident 映射；持久输入进入统一提交 |
| `tau_coding/tui/`，新增案件视图与适配 | 案件状态、假设/证据、任务、报告与执行时间线；消费 host 事件 |
| `cli.py`、`commands.py`、`session.py`、`rpc.py` | 命令接入、会话绑定和异步应用动作分派；保留既有 coding 行为和 Pi RPC 表面 |

## 实现步骤

1. **完成统一 host API。** 所有前端调用同一动作模型，包含创建/绑定、运行/暂停/继续、补充观察/解释/约束、扩大预算、报告与交接导出。携带请求 ID 和版本，返回回执；长操作通过事件与查询展示。以持久游标恢复事件，慢客户端可重新查询快照，不能阻塞调查。
2. **接通常驻服务。** `tau incident serve` 持有 Runtime，`connect` 或配置连接已有服务；嵌入模式退出时暂停，后台模式断开客户端时继续。应用层使用小型 ASGI HTTP 适配器（Starlette/Uvicorn，依赖在本阶段加入）提供命令、查询和事件流，默认绑定回环地址并校验配置凭证；WSL/容器接入地址显式配置。已有协调者有效时，其他前端转发到它，避免重复启动。
3. **扩展 CLI 与 RPC。** CLI 延续前序入口；现有 RpcServer 注入可选 incident dispatcher，采用 `incident.*` 命令与事件命名空间，并提供能力发现。旧 prompt、abort、会话与 agent 事件语义保持独立；`agent_end`/`agent_settled` 不代表案件结束。JSONL 响应保持请求关联和写入互斥。
4. **接入现有 TUI。** 增加案件工作区和 `/incident` 命令。当前 CommandRegistry 是同步 handler，新增动作请求由 CommandResult 交给应用异步执行，不在 handler 阻塞 Runtime。界面展示 CaseBrief、候选与组合原因、证据、任务、等待、预算和报告；聊天输入在案件模式转为调查请求或明确类型的人工补充，coding 模式保留原路径。
5. **处理 Session 与 Case 绑定。** 复用 `CodingSession.append_custom_entry` 保存命名空间内的 active_case_id 与必要交互元数据。新建、切换、fork、清空和压缩聊天不回滚 Case；恢复绑定时读取当前案件状态。继续使用既有 session preparation 和 provider 资源关闭边界，worker 不继承 coding 环境。
6. **实现告警入口。** 人工导入与 HTTP webhook 共用规范化逻辑。按目标设计的建议提供 Alertmanager adapter 和演示配置；上游选型保持可替换。来源/格式校验后先写 inbox 再确认接收，后台处理：投递 ID 或规范化载荷 hash 去重；fingerprint + 发生时间区分同一告警的更新与复发；外部 incident ID 优先关联案件，否则按环境、实体、时间及配置规则匹配，歧义保留供人工处理。分组 ID 不直接作为 Case ID，乱序冲突保留，resolved 仅更新相应告警。
7. **完成自动启动与展示。** 配置可自动调查的环境、服务及级别，受全局并发、队列和预算限制。接收、关联和派发都有执行引用。案件时间线支持从报告判断跳转到提交、请求上下文和证据，展示重试、等待与观测缺口；结构化导出与外部 Jaeger 链接遵循同一访问范围。补充 `website/content/` 的 incident 指南及 CLI、RPC、配置和工具参考。

## 交付物与实现完成标准

- [x] 人工、导入和 webhook 的代码路径均到达同一 Command/Runtime，重复输入不会设计成重复创建任务。
- [x] CLI、TUI、RPC 和常驻服务共享操作模型；后台断连与 Runtime 退出分别处理。
- [x] 案件视图、证据回取、时间线、报告和交接入口全部接通，旧 coding 与 Pi RPC 接口有明确兼容边界。
- [x] 静态检查及配置校验完成；实际告警、断连重连和界面行为统一交给 Stage 7 的 V4、V8、V9、V10、V11。

## 阶段检查与记录

按[总计划](incident-agent-implementation-plan.md)仅做必要静态检查，不启动服务、发送告警、演练 UI/RPC 或执行测试。本阶段已标记“实现完成，待统一验证”。接口、模块、静态检查结果与 V4/V8/V9/V10/V11 关注项见[Stage 6 实现记录](../architecture/incident-agent-stage-6.md)。实现保留在当前工作区，未创建提交；未编写或执行测试、启动服务、发送告警或演练 UI/RPC。


2026-09-27：统一 Host、持久运行队列、常驻服务与重连、incident.* RPC、TUI 工作区、Session 绑定、告警 inbox/规范化/去重/关联/自动启动、时间线与交接已接通。新增 ASGI 依赖与 Alertmanager 演示配置；Ruff、mypy（30 源文件）、语法、17 纯模块导入、配置 schema 与 26 服务 Compose 静态展开通过。行为验收留在 Stage 7。

同日完成标准复核：统一能力发现，补齐跨环境告警分页、启动前关闭窗口、TUI 导出缺口与切换状态，明确模型请求必须绑定 Case。最新 Ruff、mypy（33 源文件）、语法、31 纯模块导入、配置及 26 服务 Compose 静态展开通过；详见实现记录的复核补充。未进行行为验证。

