# Ariadne Stage 6 实现记录

状态：**实现完成，待统一验证**。日期：2026-09-27。
实现位于当前工作区，未创建提交；保留已有 Stage 1–5 的未提交改动。
本阶段未编写或执行测试，未启动服务、容器或调查，未发送告警，未演练 UI/RPC。

## 实现与边界

- `tau_coding/incident/actions.py` 定义 `IncidentAction`、`IncidentQuery`；
  `host.py` / `api.py` 提供 `dispatch`、`query`、持久游标事件和后台调度。
  人工输入仍调用同一 `Command`、`IncidentRuntime.execute` 与 Case Store。
  前端不能提交 worker 的 Finding、计划或内部恢复命令。
- `incident/service.py` / `client.py`：Starlette/Uvicorn ASGI、Bearer 凭证、请求体限额、
  HTTP 查询、POST SSE 事件/时间线、操作系统进程锁及重连客户端。
  默认回环地址；远程监听必须显式设置 `allow_remote`。接收和操作使用不同凭证环境变量。
- `incident_actions` 保存稳定请求 ID、完整动作内容、队列状态、结果、错误和调度租约。
  同一 Case 只有一个 queued/running 请求；相同 ID 不同内容拒绝。
  有效协调者仍受 Stage 3 所有权保护。旧进程的运行结果只能更新自身持有的队列记录。
  队列重启恢复先检查调度租约与 Runtime 租约；重连不会接管有效执行。
- 服务拥有按案件隔离的 Host/Runtime，沿用 Stage 2–5 的 provider、预算、工具与清理。
  全局案件额度在 SQLite 短事务内检查，worker 并发仍使用 Stage 3 全局额度。
  队列容量和自动启动 allowlist 独立配置。后台客户端关闭只关闭 HTTP transport；
  嵌入 Host 关闭会停止调度、请求暂停并有界收尾；等待条件留在 Case 中。
- `incident/frontend_cli.py` 增加 serve/connect/dispatch/query/import-alert/inbox/associate/
  timeline/handoff。既有人工、控制和质量操作继续使用原 Command；有效服务所有者存在时，
  旧 CLI 控制及 run/resume 可转发。服务描述文件只保存配置和凭证变量名。
- `incident/rpc.py` 接入现有 `RpcServer` 的可选 dispatcher；采用 `incident.*` 命名空间。
  JSONL 继续沿用请求 ID 和互斥写入，管道写入在线程中完成，避免慢客户端阻塞嵌入调查；事件分页携带 `incident.events` / `incident.timeline`。
  原 prompt/abort/session/agent 事件路径保留。`agent_end` 和 `agent_settled` 不表示案件结束。
- `incident/session.py` 使用 `amadeus.incident` custom entry 保存 active_case_id。
  恢复从当前 Session 分支读取绑定，再查询当前 Case；Session 新建、切换、fork、压缩和清空
  不提交任何 Case 回滚。Incident cleanup 加入既有 CodingSession 资源关闭流程。
- `commands.py` 的同步 handler 只返回 `CommandResult.incident_action`，TUI 异步执行。
  `tui/incident.py` 展示范围、状态、候选/组合解释、判断、证据、任务、预算、等待和报告。
  时间线可展开、过滤并查看请求和提交；References 入口追溯报告和判断。
  详情完整显示在滚动页。案件模式普通聊天记录为观察；解释和约束使用显式动作。
  `/incident coding` 解除绑定，恢复编码输入路径。
- `tau_incident/alerts.py` 与 `incident/intake.py`：Alertmanager v4 和规范化输入，
  原始消息先写 inbox，再确认接收。按投递 ID/hash 去重，按来源/环境/fingerprint/发生时间
  区分 occurrence；外部 incident ID 优先，其次已有 occurrence，再按环境、实体、时间关联。
  分组键不成为案件 ID。歧义和未匹配 resolved 留待人工关联；复发不沿用旧 occurrence。
  乱序 firing/resolved 冲突保留，resolved 不设置 Case impact 或 investigation 状态。
  关联与调度分阶段持久化，自动启动受环境/服务/级别 allowlist、队列和预算约束。
  接收、关联和派发均有 ExecutionRecord；原始 inbox 和错误可通过受控入口回取。
- `incident/views.py`：结构化交接包括 Case、事件、回执、请求快照引用、执行记录、预算和
  导出缺口；不复制所有原始 artifact。报告/判断可回查提交命令、生成 attempt、请求和证据。
  外部 Jaeger URL 从经过范围检查的执行 trace_id 构造，不读取任意外部链接。

## 存储与访问

Case Store schema 从 4 升至 5，增加 incident_actions、alert_inbox、alert_updates 和
external_incidents；保留原 Case/事件/预算/请求与执行表。
未关联的接收操作允许 `ExecutionRecord.case_id=None`；旧 executions 索引列以空字符串
表示该无案件记录，JSON 中仍为 null，不构造虚假 Case。接收记录通过 inbox 权限边界读取。
所有案件查询核对项目与环境；inbox 列表按环境过滤。凭证不写入配置、Case 或执行正文。

运行状态可用 query 的 `action` view + request_id 查询；事件 cursor 和执行 cursor 独立。
执行记录发生结束/link 更新时继续沿用 Stage 1 的变化游标。客户端按 operation_id 更新
已有记录；过滤页可为空，但仍推进扫描游标。重新连接可从游标继续，或先读 Case 快照。

## 检查结果

全部 Python 命令通过 `C:/Users/Meteorite/.local/bin/uv.exe`（下文简称 uv）执行。

- `uv run ruff check`：改动范围通过；`uv run ruff format --check`：通过。
- `uv run mypy`：30 个源文件通过，包含应用接入、TUI/Session/RPC 和相关领域/存储文件。
- `uv run python -m compileall -q ...`：通过。
- `uv run python -c ...`：17 个纯模块导入通过；未实例化 Host、CaseStore 或 ASGI app。
- `uv run --with pyyaml python -c ...`：HostSettings 与 ServiceSettings schema、2 个告警 JSON
  和 2 个 YAML 解析通过；通过 subprocess 执行 `docker compose ... config --format json`，
  静态展开 26 个服务。凭证文件路径仅用于插值，未创建凭证或启动 Docker daemon/容器。
- pyproject 与 uv.lock 同步加入 Starlette/Uvicorn。PyYAML 仅用于临时静态配置检查。

检查命令范围：`src/tau_coding/incident`、`src/tau_coding/tui/{incident,app}.py`、
`src/tau_coding/{session,rpc,commands}.py`、`src/tau_incident/{alerts,models}.py`、
`src/tau_incident/store/{__init__,intake}.py`、`src/tau_incident/execution/__init__.py`。
这些结果不是行为验证通过。

## Stage 7 统一验证关注项

- V4：嵌入关闭、有界清理、进程异常退出、租约到期/未到期、队列恢复、有效所有者转发、
  后台客户端断开和持久游标重连。特别覆盖派发入队后、Runtime 建立前的中断窗口。
- V8：无案件接收 span、关联/派发引用、变更游标、过滤空页、提交/请求/证据追溯、
  exporter gap、本地关键记录写失败和 receipt 核对，跨环境查询隔离。
- V9：重复投递、同 ID 不同内容、规范化重复、firing/resolved 乱序、恢复后复发、外部 ID、
  歧义人工关联、先入 inbox 后中断、关联后派发前中断、风暴限额与队列背压解除。
- V10：人工和告警进入同一调查链路，暂停/继续与扩大预算、版本化报告、结构化交接。
  报告中的推理依赖与执行父子关系保持不同含义。
- V11：原 coding CLI/RPC、Session 树与压缩、扩展关闭、Textual 生命周期与输入路由。
  RPC JSONL 写入互斥、请求关联、错误路径与已有 Pi 消费端兼容。
- 部署前落实两个凭证、服务/模型配置、Windows/WSL2 可达地址、容器 secret、Alertmanager
  镜像 digest。PromQL 指标名/标签及阈值必须用 Stage 7 的真实遥测确认；当前仅为演示配置。
 默认自动启动 allowlist 为空；配置完成和服务常驻是无人值守调查的前提。

## 完成标准复核补充（2026-09-27）

保留工作区已有 Stage 1–6 实现，按本阶段完成标准再次核对入口与边界，补齐：

- `actions.capabilities()` 统一嵌入和 HTTP/RPC 的能力发现字段，包含 inbox 与环境/项目身份。
- 告警待关联和待派发查询先按环境过滤再分页，避免其他环境占满每批 32 条而阻塞本环境。
- 关闭期间尚未取得 Runtime 所有权的已领取任务不再启动调查；常驻模式重新排队，嵌入模式
  标记暂停。客户端断开仍只关闭 transport。
- TUI Overview 展示 exporter gaps；刷新保留已展开的执行，工作区切换案件重置游标和记录。
- `RecordedProvider` 明确拒绝没有 Case 的模型请求。告警接收允许无 Case 的 ExecutionRecord，
  但该宽化不延伸到请求快照和预算账本。
- 用户指南和配置参考统一到 schema 5 与 Stage 6 实现状态，清除旧的待实现描述。

本次检查使用 `uv run --no-sync`，缓存设置到已忽略的 `.venv/uv-cache`，不更改项目依赖。
范围为 `src/tau_coding/incident`、TUI app/incident、CLI/Session/RPC/commands、
`tau_incident` alerts/models/store/execution，共 33 个源文件。
Ruff check、format check、mypy、compileall 均通过；31 个纯模块导入通过，未实例化 Host、
CaseStore、ASGI app 或 provider。HostSettings、ServiceSettings、两个告警 JSON、
pyproject/uv.lock TOML 校验通过；经 uv Python subprocess 执行 Compose config 静态展开
26 个服务。没有编写/执行测试、启动服务/容器、发送告警或演练 UI/RPC。

Stage 7 另需覆盖：多环境 inbox 分页公平性；任务领取后、首次运行前关闭；两种连接模式的
能力发现一致性；无 Case 模型请求拒绝；TUI 跨案件切换和时间线刷新状态。阶段状态仍为
**实现完成，待统一验证**，这些静态检查不构成行为验收。

