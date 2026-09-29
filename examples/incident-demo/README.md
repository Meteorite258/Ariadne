# Ariadne Stage 5 环境

状态：**Stage 7 验证中**。WSL2 中已启动全部 25 个服务，固定镜像 digest，
完成真实采集导出回放、容器隔离与派生证据链、Collector/Jaeger 检查。
固定模型完整调查和其余产品验收尚未通过，详见
[统一验证记录](../../dev-notes/architecture/incident-agent-validation.md)。

## 版本与文件

- OpenTelemetry Demo **2.0.2**，提交 `63649d6d6a59de88fb421b88c3c3a6185b6d21ad`。
- Collector 0.120.0、Jaeger 1.66.0、Prometheus 3.2.0、OpenSearch 2.19.0、
  Grafana 11.5.2、OpenSearch Grafana 插件 2.34.4、flagd 0.12.1、Valkey 8.1-alpine。
- 分析镜像源为 Python 3.12.10 slim-bookworm，只有标准库，运行必须配置镜像 digest。
- `versions.json` 记录发行版本、镜像引用和配置 hash。发行 tag 不等于不可变 digest；
  Stage 7 拉取后由 `lock-images` 保存本机平台的 RepoDigests，后续控制命令自动使用该锁。
- `upstream/` 保留上游 Compose 及实际挂载的配置、产品资料和 Grafana 面板，许可证见
  `upstream/LICENSE`。`compose.yaml` 覆盖构建入口、监听地址和 Collector 配置。
  全部 25 个 Demo 服务保留，内存限额沿用固定版本。
- `public/environment.json` 初始为空。真实部署后才追加声明拓扑和部署记录；
  查询到的业务 span 形成独立的 observed 拓扑版本。空历史不声称没有发生变更。
- `control/` 属于操作员，包含场景、注入记录和执行回执；不进入导出数据集或分析挂载。
  Investigator 不提供文件系统、场景开关或部署工具。

## Windows / WSL2 部署前参数

先落实 Docker Desktop WSL2 backend 或 WSL2 内 Docker Engine，使用 Linux containers。
建议在同一 WSL2 发行版中运行 Tau 与 Docker CLI；Windows 原生运行需确认 Docker Desktop
能读取 Tau 的暂存目录。使用本地 Docker context，禁止把暂存路径交给远端 daemon。
确认宿主可分配内存、磁盘与端口；上游业务环境约需 6 GB 内存，另为宿主、Tau、
并行分析和观测留余量。这里未根据未知机器规格裁剪任何服务。

需要 Compose **2.24.4+**（`!override` / `!reset`）。为 OpenSearch 配置 WSL2 的
`vm.max_map_count`，并确认 memlock 权限；具体配额与安装方式由部署时确定。
所有公开端口绑定 localhost：8080（业务/Grafana）、9090、16686、9200、4317、4318。
本地 Demo 的 OpenSearch 无鉴权；远程部署时使用受控地址、TLS 和只读凭证。

`live.json` 中应核实：

1. 环境与服务 allowlist、端点地址、保留窗口、日志 keyword 映射、指标名称与单位。
2. 需要凭证时设置每个 endpoint 的 `token_env`；凭证通过环境提供，不放入 JSON。
3. 模型 provider/model、输出上限、可选 Case token/deadline、运行检查点和并发。
4. 分析镜像构建后填入 `analysis_image: repository@sha256:...`。默认 `null` 明确禁用，
   不用占位 digest 冒充可用镜像。按宿主容量选择 `analysis_limits`。
5. Agent 导出服务 `amadeus-agent` / 环境 `amadeus-control` 与业务 `amadeus-demo` 分开。

## 静态展开（本阶段已执行）

```sh
docker compose --env-file examples/incident-demo/demo.env \
  -f examples/incident-demo/upstream/docker-compose.yml \
  -f examples/incident-demo/compose.yaml config --format json
```

这是配置展开，不拉取镜像、不联系观测后端、不启动容器。Collector YAML 另做语法解析；
Collector 自身的 OTTL/组件校验留给 Stage 7，未伪称容器配置已经运行验证。

## Stage 7 操作入口（本阶段未执行）

```sh
# 从项目根目录执行。先拉取发行镜像，再记录平台 digest。
docker compose --env-file examples/incident-demo/demo.env \
  -f examples/incident-demo/upstream/docker-compose.yml \
  -f examples/incident-demo/compose.yaml pull
uv run python examples/incident-demo/manage.py lock-images
uv run python examples/incident-demo/manage.py start

# 固定依赖分析镜像：构建/发布后取得 digest，写入 live.local.json。
docker build -t amadeus-analysis:stage5 examples/incident-demo/analysis

# 两种独立场景。
uv run python examples/incident-demo/manage.py flag-on
uv run python examples/incident-demo/manage.py flag-off
uv run python examples/incident-demo/manage.py deploy-regression
uv run python examples/incident-demo/manage.py reset
```

`paymentUnreachable` 只改 flagd 文件，私有记录说明尚未确认 flagd 何时观察到更新。
`deploy-regression` 真正重建 checkout，把 `PAYMENT_ADDR` 改为 `payment-old:50051`；
前后容器 ID、镜像 ID、实际环境变量和运行状态经 inspect 后形成公开部署记录。
失败或中断保留私有 unknown 记录，不能自动宣称应用成功。`reset` 关闭开关并恢复
真实 checkout 配置，也记录变更；它不删除遥测、Case 或数据集。

只允许一个操作员控制器；异常退出留下锁时，应先核对私有回执和实际环境再由操作员
处理锁文件。公开历史也采用短时排他锁与原子文件替换；冲突产生可见缺口。

## 同一调查入口

```sh
tau incident run CASE_ID --environment amadeus-demo \
  --services-config examples/incident-demo/live.local.json
```

Planner、Executor、Reviewer、EvidenceRecorder 和 Case Store 沿用 Stage 1–4。
`telemetry_capabilities` 说明当前配置的信号、服务、指标和查询限制。`contains` 是规范化
数据行的字面子串，不是 PromQL/OpenSearch DSL。指标是固定名称的 range query，支持按
实体、时间和子串筛选；累计值不是速率，后续统计必须说明窗口、重置处理和单位。
Jaeger 使用 1.66 查询 API 和 `/jaeger/ui` 基路径。OpenSearch SSO 字段使用
`resource.service.name`、`resource.deployment.environment.name`、`@timestamp`，精确过滤
默认追加 `.keyword`；`observedTimestamp` 保留在原始记录，不与事件时间混淆。

适配器不把成功 HTTP 响应当完整覆盖。查询结果记录后端参数、来源、采样、上限、
下一 offset、事件时间与首次取得时间。Jaeger 无稳定游标，OpenSearch 当前采用有界
窗口抓取后本地分页；达到上限必须缩小时间窗。跨页 live 数据不是冻结快照，导出清单
保留每页采集时刻及缺口。追求一致切片时，在 Stage 7 停止场景变更并选定固定窗口。

## 导出与回放

先由操作员准备 Scope JSON，包含 `environment`、`entities` 和带时区的
`time_window.start/end`。然后：

```sh
uv run python -m tau_coding.incident.data_cli \
  --services-config examples/incident-demo/live.local.json \
  --scope scope.json --output examples/incident-demo/datasets/capture-01
```

输出 `manifest.json`（schema 1）、`data.json`（ReplayData schema 1 的可选扩展字段）、
按 SHA-256 命名的原始查询响应。manifest 最后写入；未完成导出不可作为完整数据集。
导出保存覆盖未知、失败查询、信号、采样、原始响应 hash、拓扑版本及真实变更，
不导出 `control/`。原始 Jaeger 响应仅保留授权业务 span，范围外 span 明确不进入证据。

回放配置使用 `mode: fixture`、`fixture: datasets/capture-01`；路径相对配置文件。
通过 `--services-config replay.local.json --fixture-now TIME` 进入同一调查入口。
也支持旧的 `--fixture examples/incident/replay.json`。查询由实体、时间、子串和分页
决定；`available_at` 与注入时钟控制晚到数据，不依赖调用顺序。回放不会重新执行模型
历史或使外部环境回滚。在线初次可见时间不是精确的后端入库时间。

## 分析与 tracing

`python_analysis` 只在配置固定 digest 后提供，并要求任务允许该工具。
模型提交 Python 源码和 Evidence ID；运行时验证案件/范围，记录读取依据，将脚本和
选定证据暂存后只读挂载 `/inputs`。`/inputs/manifest.json` 映射 Evidence ID 到文件。
脚本在 `/outputs` 写文件，默认 1 CPU / 256 MiB / 32 pids / 30 秒 / 1 MiB 输出，
最多 16 文件。任务 deadline 与 role timeout 继续生效。

容器无网络、无额外 capability、非 root、只读根目录、no-new-privileges，
保留 Docker 默认 seccomp；不挂载宿主目录树、凭证或 socket。输出和 `/tmp` 使用有界
tmpfs。记录脚本、digest、输入、退出码、OOM/超时/取消、输出 artifact、耗时和清理状态。
每个输出经同一 EvidenceRecorder 提交成派生证据；二进制以 base64 表达。
清理未确认标为 unknown，可从记录中的容器名称由操作员核对；不会当成功统计。

OTLP/HTTP JSON 将完成的本地 ExecutionRecord 映射为 span 和 links。队列 256、重试 2、
退出刷新 5 秒；仅导出 ID、状态与引用，不导出请求、响应、查询、错误正文或 usage。
失败保存在 Tau 数据目录 `otel-gaps.jsonl`；导出失败不重跑已提交调查或重复结算预算。
本地记录仍是权威来源。Collector 分开业务/Agent trace pipeline；业务查询再按环境和
服务限制，Agent span 不成为业务证据。

## 统一验证关注项

Stage 7 V6/V7/V8/V10：API 字段和迟到时间、缺页/错误/无匹配、live→export→fixture、
真实部署与故障开关隔离、标准答案过滤、容器网络/权限/资源与取消清理、导出后端故障、
队列溢出和退出刷新，以及完整 Planner→证据→分析→Finding→审查→报告。

上游依据：[Demo 2.0.2](https://github.com/open-telemetry/opentelemetry-demo/releases/tag/2.0.2)、
[Collector SSO 编码](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/v0.120.0/exporter/opensearchexporter/encoder.go)、
[Prometheus API](https://prometheus.io/docs/prometheus/latest/querying/api/)、
[OTLP](https://opentelemetry.io/docs/specs/otlp/)。

## Stage 6 告警接入配置

状态：**实现完成，待统一验证**。新增 `host.json`、`alerts.compose.yaml` 和 `alerts/`。
当前仅校验配置并静态展开 26 个服务；没有启动 Alertmanager、发送通知或验证 PromQL。

运行时需要设置用户操作与 webhook 两个不同用途的凭证变量，均至少 24 字符。
`AMADEUS_ALERT_TOKEN_FILE` 指向部署时自行准备的 secret 文件，其内容应与接收服务的
`AMADEUS_ALERT_TOKEN` 相同；仓库不附带 token。Alertmanager 配置使用 credentials_file。

在 Stage 5 的两个 Compose 文件之后追加 `-f examples/incident-demo/alerts.compose.yaml`。
相对挂载路径沿用第一份 upstream Compose 的目录；Prometheus 保留 OTLP 接收配置，
增加告警规则和 Alertmanager 目标。Alertmanager 固定发行 tag `v0.28.1`，部署时补充实际 digest。
不要直接用默认 loopback Host 配置接收容器回调：在部署时显式选择可达监听地址、
设置 allow_remote，并核对 host.docker.internal / WSL2 网络与 endpoint。凭证保持必填。

演示规则使用 checkout span-metrics 错误计数和示例阈值。真实指标名称、标签、采样覆盖与
阈值在 Stage 7 对选定环境核对；规则不是根因答案。auto_start 三个 allowlist 默认全部为空，
因此接收与关联不会自动调用模型，需部署者明确配置环境、服务与严重级别。

服务/CLI/TUI/RPC 的使用见 `website/content/guides/incidents.md`；本阶段实现及验证关注项见
`dev-notes/architecture/incident-agent-stage-6.md`。
