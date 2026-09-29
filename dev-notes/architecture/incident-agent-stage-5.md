# Ariadne Stage 5 实现记录

状态：**实现完成，待统一验证**。日期：2026-09-27。

源码基线 `c66fb87` / `tau-ai 0.4.5` / Python 3.12+。Stage 1–4 已有未提交实现，
本次保留并在同一工作区继续；Stage 5 尚未创建独立提交，不虚构提交号。
对应[阶段计划](../design/incident-agent-stage-5.md)与[总索引](../design/incident-agent-implementation-plan.md)。

## 1. 接通的流程

`--fixture` 或 `--services-config` → 应用组装 → 原 Planner / Executor / Reviewer →
统一工具目录 → 证据登记、读取依据与 artifact → 原 Finding 提交、报告和恢复。
没有第二套 Case 状态、独立演示 Agent 或新模型协议。

- `telemetry/live.py`：Prometheus range query、Jaeger 1.66 查询 API、OpenSearch SSO
  查询。HTTP 客户端限制时限/响应大小，禁用自动重定向与隐式代理凭证。
  环境和服务 allowlist 由配置固定；业务 API 不接受模型任意表达式或端点。
- `telemetry/catalog.py`：公开环境历史、真实部署/配置记录、声明与观测拓扑版本。
  原子替换文件及排他锁避免覆盖写；查询时区分事件时间、记录可用时间和来源。
  observed 拓扑只表达取得的业务 span 调用边，不能证明因果或完整拓扑。
- `telemetry/dataset.py`：按 Scope 导出，保存响应 hash、每页查询/来源/时间、数据、
  覆盖、采样、失败查询和缺口。manifest 最后发布；回放加载时核对 hash。
  Fixture 按新条件查询并按 `available_at` 开放数据。旧 schema 1 fixture 仍可读取，
  新字段为可选扩展；导出 manifest 与服务配置均为 schema 1。
- `analysis/`：领域协议、固定限制、脚本/输入/结果 schema。
  `tau_coding/incident/analysis.py` 提供 Linux Docker 驱动；核心 harness 不依赖 Docker。
- `tau_coding/incident/otel.py`：完成记录到 OTLP/HTTP JSON span/links 的映射，
  有界后台队列、重试与退出刷新；仅允许的元数据进入 span，全文保留在本地 artifact。
- `tau_coding/incident/services.py`：live/fixture、认证环境变量、分析与 OTel 生命周期。
  `data_cli.py` 是显式采集入口，不创建模型或案件；`cli.py` 保留既有命令。
- `examples/incident-demo/`：固定上游版本、全部服务及必要配置、覆盖 Compose、
  操作员控制入口、分析镜像、版本/hash 清单和 Windows/WSL2 部署说明。

这保持 Pi 的 Harness / 应用环境 / 前端边界；通用 `tau_agent` 无本阶段新增依赖。
HTTP 复用已有 httpx，OTLP JSON 不要求给核心引入 OTel SDK，依赖锁文件无需更新。

## 2. 证据语义与隔离

live 和 fixture 共用 TelemetryQuery、QueryResult、EvidenceRecorder、AddObservation。
实际后端参数写入证据；原始响应保存在 artifact。返回模型的正文有大小上限，可通过
Evidence ID 回取。失败、部分、无匹配、采样和未知覆盖分别保留；导出失败不会在
回放中变成正常空查询。当前 live 分页在有界抓取结果上进行，不承诺跨请求快照一致。
Prometheus 返回 range-query 步长上的值，不能把它说成完整原始采样或直接当作速率。

Jaeger 查询按服务和环境过滤，返回的完整 trace 再投影到授权业务 span；范围外 span
不进入 artifact。OpenSearch 使用固定版本实际 SSO 字段及 keyword 过滤，Collector
过滤控制端日志及显式开关信息。标准答案、flag 文件和控制日志只在操作员目录存在。
Stage 7 仍需检查业务日志中间接泄露答案的情况，不能以静态规则声称已完成泄露验证。

`paymentUnreachable` 仅记录到控制日志。部署场景真实重建 checkout 并检查前后容器
配置，公开记录保留镜像/配置版本和执行回执 hash。操作失败记录 unknown，不将
故障开关冒充 deployment diff。reset 恢复配置并追加记录，不删除历史。

Python 工具受既有任务权限、Case/任务 deadline 和 role timeout 管理；每次固定
CPU、内存、进程、时限、输入/输出与文件数上限。模型不能修改这些策略。
脚本与指定 Evidence ID 暂存后只读挂载，输出为容器内有界 tmpfs。非 root、无网络、
只读根目录、cap-drop ALL、no-new-privileges、默认 seccomp；无宿主目录树、凭证或 socket。
每个输出登记为派生证据，保留脚本、镜像 digest、输入引用、退出码、耗时、OOM/
超时/取消和清理状态。取消仍完成有界清理；清理未确认标为 unknown 并保存容器名称。

Agent 采用独立 `amadeus-agent` / `amadeus-control` 标识和 Collector pipeline。
导出容量 256、最多重试 2、默认退出刷新 5 秒，失败写入 `otel-gaps.jsonl`。
本地 execution、Case 及预算不依赖外部后端，不由导出重复结算。

## 3. 固定版本与部署前参数

Demo 2.0.2，提交 `63649d6d6a59de88fb421b88c3c3a6185b6d21ad`；Collector 0.120.0、
Prometheus 3.2.0、Jaeger 1.66.0、OpenSearch 2.19.0、Grafana 11.5.2、
Grafana OpenSearch plugin 2.34.4、flagd 0.12.1、Valkey 8.1-alpine。
分析基础镜像 Python 3.12.10 slim-bookworm，仅标准库。
完整镜像引用及配置 hash 在 `examples/incident-demo/versions.json`。

部署前仍需落实：Docker/WSL2 安装方式、宿主内存/磁盘、OpenSearch sysctl 和权限、
本地 Docker context 与暂存路径映射、平台镜像 digest 锁、分析镜像构建/发布 digest、
实际服务地址与鉴权、日志索引/keyword 映射和保留期、模型/API 与预算及运行时钟。
`analysis_image` 默认 null，填入真实 digest 后才提供分析工具；没有伪造本期未构建的镜像。
控制器 start 要求先有 images.lock.json，防止将可变 tag 当作已锁定平台镜像。
全部服务保留；没有按未知内存规格减少范围。

## 4. 静态检查记录

沿用前阶段现有 `C:/Users/Meteorite/.local/bin/uv.exe`，设置
`UV_CACHE_DIR=%TEMP%/amadeus-uv-cache`，使用 `uv run --no-sync`，未安装工具或依赖。

```text
uv run --no-sync ruff check src/tau_incident src/tau_coding/incident examples/incident-demo/manage.py examples/incident-demo/analysis
uv run --no-sync ruff format --check src/tau_incident src/tau_coding/incident examples/incident-demo/manage.py examples/incident-demo/analysis
uv run --no-sync mypy -p tau_incident -p tau_coding.incident
uv run --no-sync mypy examples/incident-demo/manage.py examples/incident-demo/analysis/runner.py
uv run --no-sync python -m compileall -q src/tau_incident src/tau_coding/incident examples/incident-demo/manage.py examples/incident-demo/analysis
```

结果：Ruff、格式、mypy（36 个源文件及另查 2 个操作脚本）、语法和新增模块纯导入通过。
ServiceSettings、EnvironmentHistory、原有 ReplayData 配置 schema 及 JSON 文件解析通过。
Compose 基础与覆盖配置静态展开通过；全部 bind 文件存在，覆盖后无 build 入口。
Collector YAML 通过 Compose extension 包装进行语法解析；没有启动 Collector 验证 OTTL。
本地 Docker CLI 提示用户 Docker config 无读取权限，但配置展开退出码为 0；未请求
额外权限或访问 daemon。初次 PATH 找不到 uv、尝试导入可选 YAML 库不可用，随后使用
既有 uv 和 Compose YAML 解析完成相应检查。静态检查发现的类型/格式/字段映射和
Compose 覆盖问题已修正。

没有编写或运行测试，没有构建/启动容器，没有请求模型/遥测，没有采集或演示。

## 5. Stage 7 验证交接

- V6/V7：后端真实字段、采样/retention/分页、查询失败与空结果、晚到数据、hash 完整性，
  live 导出后新查询回放，声明/observed 拓扑和真实变更记录。
- V7/V8：分析权限、证据范围、只读挂载、网络与资源边界、超时/OOM/重复取消、
  输出限额、清理失败与派生证据回取；任务/Case 停止及预算边界保持既有语义。
- V6/V10：OTLP 协议与字段、业务/Agent 隔离、links、队列溢出、重试和退出刷新，
  后端不可用不改变已接受 Case，不复制敏感正文，不重复记账。
- V8/V10：同一调查流程的 live/fixture→分析→Finding→审查→报告，控制端答案隔离，
  以及 Stage 1–4 与普通 coding 行为回归。不能把静态通过记为这些行为验证通过。
