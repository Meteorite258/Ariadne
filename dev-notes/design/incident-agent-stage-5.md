# Stage 5：真实遥测、可复现环境与隔离分析

状态：**实现完成，待统一验证**。前置依赖：[Stage 4](incident-agent-stage-4.md)实现完成。

[总计划](incident-agent-implementation-plan.md) · 下一阶段：[Stage 6](incident-agent-stage-6.md)

## 目标与贯通路径

将 Stage 1–4 的调查流程接到微服务演示环境，增加跨数据源调查与 Python 分析，并让同一环境的数据可导出后离线查询。业务调用链和 Agent 执行链使用清晰分开的入口。

## 模块与关键接口

| 模块 | 实施约定 |
| --- | --- |
| `tau_incident/telemetry/` | Prometheus、Jaeger、OpenSearch 适配器实现已有 Provider 协议；部署/配置与 ServiceCatalog 使用版本化快照 |
| `telemetry/` 的导出与 FixtureProvider | `DatasetExporter.export(query_scope)` 产出可查询数据与覆盖清单；FixtureProvider 使用同一数据语义 |
| 新增 `tau_incident/analysis/` | `AnalysisExecutor.run(script_ref, evidence_ids, limits)`；返回执行记录和可登记 artifact |
| `tau_coding/incident/` 与配置 | 组装数据源、Linux 容器执行驱动、OpenTelemetry exporter；注入领域接口 |
| 新增 `examples/incident-demo/` | Demo 版本清单、Compose 配置、数据导出/重置入口、场景定义、分析镜像和部署说明 |

## 实现步骤

1. **准备可复现环境。** 锁定 OpenTelemetry Demo 的提交/版本、镜像和配置；准备 Windows/WSL2 的 Linux 容器部署说明、服务地址和资源参数。当前内存和 Docker 安装方式未确认，写成部署配置，不能据此删除服务或调查能力。本阶段编写环境文件；实际构建、启动和场景演练在 Stage 7。
2. **实现真实查询。** 对接 Prometheus 指标、Jaeger 调用链、OpenSearch 日志，统一查询范围、分页、采样、截断、错误、单位和实际覆盖。将原始结果落为 artifact，经现有证据工具返回；能力发现必须反映数据源实际支持范围，缺少能力时明确表示。
3. **补齐拓扑和变更。** 从部署定义与观测调用分别生成声明拓扑和实际拓扑，保留版本和时间。环境管理适配保存真实部署/配置变更。`paymentUnreachable` 属于错误地址故障开关；涉及 deployment diff 的场景必须另有真实变更记录，不能用开关状态冒充部署历史。
4. **实现导出与回放接线。** 导出查询范围内的遥测、拓扑和变更记录，保存来源、覆盖、hash、事件时间与可用时间。FixtureProvider 能按新查询访问这些数据并模拟迟到数据，不能只回放固定调用顺序。场景控制器和标准答案与 Agent 可访问数据隔离；实际采集数据集与回放比对放在 Stage 7。
5. **实现隔离 Python 分析。** 通过受控 Evidence ID 将选定文件只读挂载，提供独立输出目录、固定依赖镜像，默认无网络，并限制 CPU、内存、进程数、时限和输出。模型无宿主目录、凭证或 Docker socket 访问；容器驱动只在应用组装层控制固定策略。保存脚本、镜像版本、输入输出、退出码和失败原因，派生结果经 EvidenceRecorder 登记。处理取消与容器清理，不开放通用宿主 shell。
6. **接通 OTel 导出。** 本地 ExecutionRecord 映射为 span 和 links，队列容量、重试次数及退出刷新有上限；导出失败可见且不改变 Case。Agent 使用独立服务/环境标识；导出只包含允许的元数据，全文留在受控 artifact。业务查询始终限定被调查服务，不能混入自身执行 trace。
7. **接回完整流程。** 应用配置选择 live 或 fixture 数据源，Executor 使用同一工具目录和证据协议；Python 分析同样受任务权限、预算/时限和 tracing 管理。更新数据源、分析工具和环境部署文档。

## 交付物与实现完成标准

- [x] 环境、镜像、数据源、场景与导出格式有固定配置及版本记录。
- [x] live 和 fixture 都接入现有 Planner/Executor，无第二套案件状态或独立演示流程。
- [x] Python 分析与 OTel 导出包含取消、失败及缺口记录；业务和 Agent trace 范围明确。
- [x] Python 与配置静态检查完成；实际环境、回放、隔离及导出验证交给 Stage 7 的 V6、V7、V8、V10。

## 阶段检查与记录

按[总计划](incident-agent-implementation-plan.md)仅做语法、导入、类型及配置静态检查；不构建/启动容器，不调用模型或遥测，不运行演示和测试。

2026-09-27：完成 `telemetry/live.py`、`catalog.py`、`dataset.py`、`analysis/`、应用服务配置、
Docker 驱动、OTLP/HTTP 导出及 CLI 接线，加入 `examples/incident-demo/`。Demo 2.0.2
固定提交 `63649d6d6a59de88fb421b88c3c3a6185b6d21ad`；版本、镜像引用和配置 hash 见
`examples/incident-demo/versions.json`。沿用基线 `c66fb87` 的工作区，尚未新建实现提交。

Ruff、格式、mypy（36 源文件）、语法、纯导入、JSON schema 及 Compose 静态展开通过；
Collector 只做 YAML 语法解析，运行时组件/OTTL 检查留给 Stage 7。没有编写或运行测试。
部署前还需落实 WSL2/Docker 方式、资源、端口/凭证、平台镜像 digest、分析镜像构建及
digest、实际后端索引/字段、模型与预算。完整实现说明、检查命令及 V6/V7/V8/V10
交接见[Stage 5 实现记录](../architecture/incident-agent-stage-5.md)。
