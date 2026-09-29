# Incident Agent Stage 7 验证记录

状态：**验证中，未通过交付验收**。初始验证日期：2026-09-27；2026-09-28 恢复实测。

本记录持续更新。部分确定性测试通过不代表 V1–V11 全部通过；必要实际环境项目
不得用 fake、静态检查或未执行的测试替代。Stage 1–6 保持“实现完成，待统一验证”。

## 2026-09-28：持续调查 v2 离线改造

统一改造的状态归属、六步交付、行为矩阵、命令及环境见
[持续调查 v2 实现与验证](incident-agent-v2.md)。新流程使用 `v2/` 存储，旧案件只读保留；
本轮没有运行任何真实模型，既有累计 **3,981,002 / 4,000,000 token** 不变。

已完成 incident 离线全组 **130 passed**，含持续进度、13 次连续请求/312 秒受控时钟、
并发检查点、恢复、请求级额度、批审和诊断依赖、上下文回取、历史只读及导出。
实际隔离容器 **8 项通过**，含五种数据格式；真实 daemon/远程 CLI/告警流程通过，模型请求 0。
最终 Python 全量 **2144 passed / 3 skipped**；3 项是已有平台条件，没有新增跳过。
补齐按任务跨 attempt 汇总用量后，预算/进度/调度范围另复测 **18 passed**。
mypy 181 文件、全仓库 Ruff、sdist/wheel 和包内容检查已通过。
本次可宣布“统一改造与离线验证完成”，不能宣布 Stage 7 完成。

补测发现前提审查更新版本后综合判断可能一直等待，已修正为先修订引用，再独立审查。
fake provider 用例覆盖批量前提审查、综合修复、诊断审查直至报告提交。
这些结果不替代固定模型完整诊断验收；Stage 7 仍未完成。原 Stage 历史不重编号。

## 冻结输入与环境

最新检查点：增加 SQLite 升级测试后的 WSL2 完整 Python **2103 passed / 3 skipped**；Windows 最近完成的
全套为 **2063 passed / 35 failed / 4 skipped**。另一次修正 PATH 后的 Windows 全套
在约 92% 的原 TUI 区段停滞，经 faulthandler 栈核对后结束，不能计为完成。
全仓 Ruff 通过，WSL2 `mypy src` **169 source files passed**；先前含脚本的类型检查
174 项和源码包/wheel 构建通过。完整固定模型场景仍未通过。

- 基线：`c66fb879c1058f7b3d8514fb7f92c919d3c3e3b3`，Tau 0.4.5。
- Stage 1–6 均在本次开始前已实现于未提交工作区；各阶段实现记录明确没有独立提交。
  incident 实现及修复仍未提交；独立 Tau 拖拽修复提交见后续记录，
  不将基线冒充实现或修复提交。
- 已阅读根 `AGENTS.md`、目标设计、总计划和 Stage 1–7 计划，复核
  [Tau 路线图 #1](https://github.com/huggingface/tau/issues/1) 的分层约束。
- Windows 11 build 26200，CPython 3.12.14，uv 0.12.19；pytest 9.1.0、
  Pydantic 2.13.4、Textual 8.2.8、HTTPX 0.28.1、Ruff 0.15.17、mypy 2.1.0。
- `uv.lock` SHA-256：`e7d5af5e90db24e03836a5921771a515132cbe5ae7f1cd38bac51c267f484bfc`。
- `uv` 已安装于 `C:/Users/Meteorite/.local/bin/uv.exe`，当前 shell PATH 未包含它。
  默认缓存初始化失败，验证命令统一设置 `UV_CACHE_DIR=$env:TEMP/amadeus-uv-cache`，
  使用现有 `.venv`，通过 `uv run --no-sync` 执行 Python、pytest 与检查。
- WSL2 Ubuntu 24.04.3，内核 `6.6.87.2-microsoft-standard-WSL2`；可见内存约 15 GiB，
  swap 4 GiB。已安装 Linux uv 0.12.19，使用 Python 3.12.3；通过 `uv sync --frozen`
  创建 `/var/tmp/amadeus-stage7-venv`，缓存 `/var/tmp/amadeus-stage7-cache`。WSL 需沙箱外执行。
- Docker Desktop 4.83.0、客户端/引擎 29.6.2；已按用户授权恢复 Ubuntu WSL 集成，
  25 个服务全部启动。设置和旧运行时 socket 均保留备份，恢复细节见下方检查点。
- Demo 2.0.2，上游提交 `63649d6d6a59de88fb421b88c3c3a6185b6d21ad`。
  镜像引用与配置 hash 在 `examples/incident-demo/versions.json`；平台 digest 在
  `images.lock.json`。分析镜像、运行和实际导出结果见下方。Collector 镜像 tag 为
  0.120.0，实际进程报告 0.120.1；保留 tag/digest 与运行观察两种记录。

## 实现依赖核对

| Stage | 依赖 | 已存在实现入口 | 行为状态 |
| --- | --- | --- | --- |
| S1 | 目标设计 | models/events/store/evidence/execution/coordinator，CLI/host | 部分验证 |
| S2 | S1 | request_context、context、executor、planner、RecordedProvider、fixture | 部分验证 |
| S3 | S2 | store/control、budget、recovery、investigation | 部分验证 |
| S4 | S3 | review、quality、reporting/builder、memory | 部分验证 |
| S5 | S4 | telemetry/live/catalog/dataset、analysis、应用 Docker/OTLP 驱动、Demo | 隔离/派生证据/Jaeger/实际导出通过；完整模型场景待验证 |
| S6 | S5 | host/actions/api/service/client/intake/rpc/chat、TUI 案件工作区、session 绑定 | 部分验证 |

核对发现代码入口已存在；仍需用行为测试证明完整交付，无条件宣称实现正确不成立。

## 固定模型与预算

用户指定 LongCat，并说明可用额度为 1000 万 token、要求节省使用。本次实际
模型验收总量先获批准提高为 **100 万 token**，随后提高为 **200 万**和
**300 万**及 **400 万 token**；
用户账户的其余额度不作为自动可用预算。模型固定
`LongCat-2.5-Preview`；不做模型扫描、benchmark 或消融。

`examples/incident/longcat/catalog.toml` 使用 Tau 原有 OpenAI-compatible adapter。
完整 endpoint 为 `https://api.longcat.chat/openai/v1/chat/completions`，输出字段为
`max_tokens`，不发送 `store` 或 `reasoning_effort`。
依据：[官方聊天接口](https://longcat.chat/platform/docs/zh/api/chat)。凭证仅用于进程环境，
不写入仓库、快照或本报告。

已执行一次真实工具调用探测：HTTP 200，模型 `LongCat-2.5-Preview`，返回 `probe` 调用，
usage 为输入 141、输出 14、合计 **155 token**，输出上限 128。
命令：`uv run --no-sync python scripts/validate_longcat.py`。
`uv run --no-sync python scripts/validate_longcat.py --stream` 也通过：实际 Tau adapter
返回 OK、stop，输入 17、输出 45（含 reasoning 41）、合计 62；探测总量 **217 token**。

通过独立临时 `TAU_HOME` 执行了真实 CLI `new`、`run`，固定样例 `examples/incident/replay.json`：
Case `4291460acffe5e139a2d670a0801de3e`，3 tasks、5 turns、1 format repair、15 calls、
100000 tokens、24576 context、2048 output、concurrency 2。结果为 **失败**：
`OutputInvalid: plan basis was not supplied in this decision context`；6 次请求，
计费/保守预留 26776 token，其中 1 次取消后 usage 未知。2 份工具观察已持久保存，
没有已提交 Finding，不能算完整调查通过。修改规划提示后正在同案 `resume` 复测。
此前累计预算记账 26993 token（含未知用量预留），不能把该数写成全部已知实际用量。

## V1–V11 矩阵

| ID | 已执行证据 | 未完成项目 / 结论 |
| --- | --- | --- |
| V1 | `test_store.py`、`test_store_concurrency.py`、`test_store_migrations.py`：幂等/冲突、事件重放、版本冲突、文件失败、SQL 回滚、提交后核对、事务外文件写入、hash/路径校验、双连接竞争、schema 1–4 升级与失败回滚 | 确定性覆盖通过；迁移使用重建的旧版 schema，真实历史数据库文件仍未单独验收 |
| V2 | `test_context_protocol.py`、`test_recorded_provider.py`、`test_context_retention.py`：副本隔离、多工具原子组、错误身份、先快照后调用、溢出不伪造调用、关键反证及省略追踪、派生版本投影、最终 JSON 编码后大小、摘要保留有界原文片段 | 确定性覆盖通过；真实请求与持久记录核对通过，完整诊断仍未通过 |
| V3 | `test_budget_recovery.py`、`test_dispatch_wait.py`、`test_investigation.py`、`test_read_versions.py`：屏障并行、依赖、角色共享额度、预算竞争、未知 usage、重复结算、格式修复记账、同源变化须复核/无关更新可合并 | 确定性覆盖通过；固定模型并行及中断场景待验 |
| V4 | 所有权接管、旧 attempt 拒绝、前序关联、等待不阻断、三种超时、未知结束核对；Windows/WSL 实际 daemon 断连、强制结束、重启、回执和游标恢复 | 自动化覆盖通过；完整模型场景中断恢复待验 |
| V5 | 诊断审查、无进展停止/新证据周期、晚到证据拒绝旧审查、原观察保留、卡片即时失效、检索过滤、局部修复依赖传播及独立判断保留 | 确定性覆盖通过；误导历史完整模型流程待验 |
| V6 | 晚到/分页/单位/覆盖、失败与空结果、hash 损坏；实际三信号导出及 482 次回放、来源/答案隔离 | 上述实测通过；固定模型对真实部署差异的完整调查待验 |
| V7 | fake 边界测试及真实 Docker 7 组：只读/非 root/网络/凭证/资源/越界/超时/取消/清理、多输出证据→Finding→provenance；工具 manifest 契约回归 | 容器边界 7/7 实测通过；真实模型脚本仍未完成逐行分析，见后文 |
| V8 | 本地关联、恢复 links、快照、白名单、队列溢出；实际 Collector→Jaeger、导出失败不改案件、业务隔离、flag 事件过滤；真实案件 TUI 显示 Jaeger 链接且路由 HTTP 200 | 导出与界面链接实测通过；完整诊断报告关联的 Jaeger 导航仍待固定模型场景验证 |
| V9 | 先持久化/去重/冲突/解除不结案/复发/乱序、Webhook 存储错误、自动派发运行/队列限额；RPC、Textual Pilot、绑定新会话/恢复、daemon SSE；WSL2 独立 daemon + 真实 Webhook/远程 CLI/交接/重启短流程；真实案件 TUI 只读验收 | 自动化与无模型实况短流程通过；固定模型告警启动闭环和完整产品操作仍待验 |
| V10 | `test_investigation.py`：fake Planner→工具→证据→Finding→独立审查→诊断报告→卡片；请求与工具/attempt 可追溯；另有双任务并行；固定模型实际保存指标/链路/部署观察 | 离线诊断链路通过；固定模型迄今无一个通过独立审查的完整诊断，告警/晚到/历史组合场景待验 |
| V11 | 受影响 Tau 回归；最近完成的 Windows 全套 2063 passed/35 failed/4 skipped；增加 SQLite 升级测试后 WSL2 全套 2103 passed/3 skipped；分支/压缩/重载不回滚案件 | Windows 拖拽已修复；新增修复定向通过，当前完整失败项除两项 PATH 环境问题外均在原始 HEAD 失败列表；不能宣称 Windows 全套通过 |

目标设计 §12 的场景全部仍在范围：旧 endpoint→V6/V10；中断重启→V1/V4/V8；
晚到/范围→V2/V3/V5/V6；覆盖失败→V6；并行迟到→V3/V4；等待预算→V3/V4；
证据回取→V2；误导历史→V5/V10；无进展审查→V5；模型/分析超时→V7/V8；
导出/本地失败→V1/V8；业务与 Agent trace 隔离→V6/V8。

## 缺陷与复测

1. **失败回放仍宣称完整覆盖。** FixtureProvider 在失败捕获匹配后只改变 result，
   未清除 complete。现清除完整覆盖；复现：
   `test_failed_capture_never_becomes_successful_empty_query`，修复后通过。
2. **告警关联被自己的恢复流程提前结束。** Host 临时取得所有权时恢复了当前正在执行的
   `alert_associate`，后续 finish 报 `operation already finished`，已登记观察对应的 inbox
   仍停留失败状态并占用容量。现显式传递活动 parent，并在这次恢复中保护该 parent；
   旧操作继续恢复。告警去重/解除/复发测试与旧代次恢复测试复测通过。
3. **旧 TUI Session 适配器没有持久 entries，绑定检查引发异常。** 案件绑定读取允许
   未提供 entries 的适配器保持 coding 模式；原有两个 terminal command 用例复测通过。
   此前全套停滞发生在等待活动 agent 启动的用例；修复后单独执行不再停滞。
4. **RoleRunner 可接收无 Case 的 parent。** mypy 发现 role 预留收到可空 case_id；
   补充显式前置校验，与 RecordedProvider 的既有约束保持一致。mypy 49 个源文件通过。
5. **原 commands 精确列表未包含 incident。** 增加新命令及转发行为测试，46 项通过。
6. **原 TUI 时间戳测试假定 UTC 日期。** 改为断言对应完整本地时间戳，覆盖本机 UTC+8，
   未修改界面行为；单项复测通过。
7. **规划提示没有解释引用白名单和支持的等待条件。** 真实 LongCat 生成可见但未授权的
   Case/Task/Attempt 引用，格式修复耗尽后停止。补充 schema 描述、当前时间和运行中
   任务由 Runtime 等待的说明；仍严格拒绝白名单外依据，新增回归证明拒绝行为未放宽。

上述修复均为当前工作区修改，尚无修复提交 hash。

## 命令与当前结果

PowerShell 前缀：

```powershell
$env:UV_CACHE_DIR = Join-Path $env:TEMP 'amadeus-uv-cache'
# 下文 uv 指 C:/Users/Meteorite/.local/bin/uv.exe
```

- `uv run --no-sync pytest tests/incident -q`：**50 passed**；随后新增 planner 合约测试单项通过，最终汇总待重跑。
- `uv run --no-sync pytest tests/test_agent_loop.py tests/test_agent_harness.py tests/test_tool_history.py -q`：**30 passed**。
- 两个原有 TUI terminal command 用例与 dispatch/wait 组曾联合复测；TUI 两项通过。
- `uv run --no-sync pytest -q`：已启动，82% 附近持续停滞后中断；不得视为完整运行通过。
- `uv run --no-sync pytest -vv --tb=short -o faulthandler_timeout=60`：逐用例日志定位停滞，
  修复绑定问题后的 Windows 全套：**49 failed, 2002 passed, 4 skipped in 159.95s**。
  日志：系统临时目录 `amadeus-full-retest.log`。失败包括 Windows 路径/权限/进程与 TUI；
  不因 WSL2 通过就忽略 Windows 结果。
- WSL2：`UV_PROJECT_ENVIRONMENT=/var/tmp/amadeus-stage7-venv UV_CACHE_DIR=/var/tmp/amadeus-stage7-cache uv run --no-sync pytest -q --tb=short -o faulthandler_timeout=60`：
  **1 failed, 2050 passed, 3 skipped in 132.50s**。失败为本地时区断言；既有跳过为
  macOS 文件系统用例和两项 Windows PowerShell updater 用例。pytest cache 写权限警告，
  下次将缓存定向 `/var/tmp`，不影响测试断言。
- `uv run --no-sync pytest --collect-only -q`：新增 conftest 初版与原测试导入冲突；
  将 `tests/incident` 设为包后，完整收集通过。未修改原测试导入或跳过测试。
- `uv run --no-sync ruff check tests/incident --fix`、`ruff format tests/incident`：通过。
- `uv run --no-sync mypy src/tau_incident src/tau_coding/incident`：**49 source files passed**。

## 2026-09-27 08:45 UTC 复测进展（替代上文对应的早期结果）

- Docker Desktop 4.83.0 / Docker 29.6.2 已恢复，按用户授权启用 Ubuntu 集成。
  设置备份在系统临时目录 `amadeus-docker-settings-before-wsl.json`；旧运行时 socket
  目录保留为 `run.stage7-stale-20260927`，没有删除持久 Docker 数据。
- WSL2 Demo 的 25 个服务已启动，`images.lock.json` 记录实际镜像 digest。
  分析镜像为 `amadeus-analysis@sha256:7a07c4a57b8ac115d727ca2f9b4f5eea30ecc620a3734a640246d5ff909a526b`。
- WSL2 完整 Python 复测：**2063 passed, 3 skipped in 143.52s**，日志
  `/var/tmp/amadeus-stage7-full-retest.log`。此后新增测试和修复仍需最终完整复测。
- Windows 原始 HEAD 独立检出运行：**45 failed, 1966 passed, 4 skipped in 169.85s**。
  日志为系统临时目录 `amadeus-stage7-baseline-tests.log`；不能把这些既有失败算作新增回归，
  当前工作区与基线的逐项差异仍待最终核对。
- Provider 与 incident 联合组：**250 passed**；随后 incident 单独 **56 passed**，
  再新增上下文保留测试单项通过。V3 读取来源版本变化/无关更新合并、V9 HTTP 鉴权、
  Webhook 持久化失败不确认、重复请求与重启后游标恢复已有确定性测试通过。
- `uv run --no-sync python scripts/validate_incident_containers.py --image <上述 digest>
  --output /var/tmp/amadeus-stage7-containers.json`：真实容器六组通过，覆盖只读输入与根目录、
  路径越界、非 root、无凭证/socket、网络、capability、CPU/内存/PID、符号链接输出、
  超时、退出码、OOM、运行中取消及清理。派生证据完整调查链仍待验证。
- `uv run --no-sync python scripts/validate_incident_tracing.py`：实际 Collector/Jaeger
  父子关系、links、脱敏、导出失败不改变记录、业务/Agent 隔离通过，结果
  `/var/tmp/amadeus-stage7-tracing.json`。
- 导出暴露故障开关的 span event 泄漏。Collector 原配置仅删除 span 属性，现增加
  feature flag/flagd 事件过滤；实际发送带开关事件及普通事件的业务 trace，确认仅前者
  被删除。参考固定版本 filterprocessor 配置，并经实际 Collector 启动与 Jaeger 查询验证。
- 同一固定窗口 checkout/payment/cart 导出中，cart traces 超过默认 8 MiB；保留失败结果，
  在本地配置中将 Jaeger 有界响应额度设为 16 MiB。重导出并执行
  `uv run --no-sync python scripts/validate_incident_dataset.py /var/tmp/amadeus-stage7-filter-fixed-16m`：
  **482 个查询通过，metrics 1200、traces 1413、logs 686、gaps 20**；核对来源、原始哈希、
  任意实体/分页、可用时间、答案隔离。数据哈希
  `7a50207c9d44d2f5245a587e2476a1aa26f5e6990e63d567f96c30d46e02417d`。
- LongCat 固定 `LongCat-2.5-Preview`、thinking off、重试 0。成功探测累计 410 token。
  Windows 回放案件累计记账 86319 token（含两次未知 usage 的保守预留）；另两次小探测
  超时没有服务端 usage，额外保守计 296 token，不记为免费。
- WSL2 真实 Demo 案件 `718144583c2857dd908b7b184644574e` 首次执行 8 次请求、70688 token，
  8 个观察，0 Finding：worker 用完五次响应仍在调用工具。规划器曾在 worker 运行时返回
  progress，worker 失败后没有重新规划。新增确定性复现，修复所有终止结果触发重新规划；
  回归 3 项通过。RoleRunner 同时明确响应额度及预留最后一次 JSON 响应要求。
- 修复后的真实恢复仍在执行；已发生一次服务端读取超时，未知 usage 保留计费预留。
  暂不能宣称完整调查通过。验证总 token 上限仍为 500000；每次继续前核对累计账本。
- 新增 `test_context_retention.py` 验证缩小任务范围仍保留关键依据、记录未选中证据及
  原子旧工具交互的省略、投影不修改完整历史，单项通过。

本节修复仍是工作区修改，尚无独立修复提交。V7/V8/V6 的上述实际结果不能替代
V10 的三个完整固定模型场景及其余产品操作验收。

## 09:12 UTC 追加复测

- Windows 完整复测日志：系统临时目录 `amadeus-stage7-current-windows-verbose.log`，
  **45 failed, 2027 passed, 4 skipped in 228.01s**。其中 44 个失败与未修改基线一致；
  原基线时间戳失败已修复；本次额外的 `test_clicking_loaded_model_uses_exact_model_id`
  在当前代码和基线单独复测均通过，保留为全套中的不稳定结果，不能宣称 Windows 全绿。
  沙箱内一次全套在 Textual 阶段停滞后中断，日志另存 `amadeus-stage7-current-windows.log`。
- WSL2 最新全套：`/var/tmp/amadeus-stage7-full-current.log`，
  **2071 passed, 3 skipped in 205.57s**；随后服务锁导入修复、TUI 和模型配置更新另行复测。
- `uv run --no-sync mypy --cache-dir /var/tmp/amadeus-stage7-mypy-cache`：
  **174 source files passed**。发现新增 service 的 msvcrt 静态导入无法在 Linux 类型检查，
  改为平台分支动态导入，与已有 fcntl 分支一致；真实 Windows/WSL2 锁与进程验收通过。
  Windows 全仓 mypy 仍有 14 个错误，集中在未修改的 `project_trust.py`、
  `tau_agent/session/storage.py` 的平台类型分支，不将 WSL2 结果冒充 Windows 类型检查通过。
- `uv run --no-sync ruff check src tests scripts/validate_*.py`（实际显式列出本次五个脚本）：通过。
  `uv build --no-sources --out-dir <temp>/amadeus-stage7-dist`：sdist 与 wheel 成功。
- `test_service_process.py` 在 Windows、WSL2 均实际启动 `uv run tau incident serve`：
  SSE 断连后服务保持运行，强制终止整个测试进程树并重启，同一命令返回原回执，
  游标只返回断连后新增事件。测试只终止自己启动的进程树，不影响 Demo。
- `test_workspace.py` 使用实际 Textual Pilot 和 IncidentHost：观察/解释/约束分别入账，
  pause 生效，未知证据显示错误，切案清空旧时间线，返回 coding 清除绑定且 Host 保持打开。
  此项自动 UI 测试不替代尚未完成的全部产品操作人工验收。
- V7 新缺陷：派生输出来源写成 `operation_id:filename`，存储要求真实工具操作 ID，导致
  登记失败。新增双输出复现测试后修复为真实 ID，并按 actual_query.output 区分文件；
  多文件引用不混淆。真实 Docker **7 组通过**，增加工具→派生证据→Finding 提交、
  脚本/输入/镜像/操作/trace 追溯；结果 `/var/tmp/amadeus-stage7-containers-retest.json`。
- V1 双 SQLite 连接并发提交同一命令：同一回执、只提交一个观察和领域事件，重放一致，
  `test_store_concurrency.py` 单项通过。
- 60 秒读取超时后的小额固定模型探测恢复成功，新增 22 token。运行配置现在通过
  `providers.json` 固定 timeout 180 秒、max_retries 0；曾误放进 catalog 的超时字段被
  本地预检拒绝，没有模型调用，已更正并补充配置加载断言。
- Demo 案件累计 **15 requests / 166339 tokens / 2 unknown usage**。最近三次实际调用成功，
  但原任务 100000 token 累计额度耗尽，尚无 Finding。尝试通过 revise-task 更改任务预算
  被现有不可变预算约束拒绝；该回执保留，不绕过检查或清零历史。已记录节约查询的约束，
  让 Planner 在案件剩余额度内选择后续工作。案件上限 250000，其他已用记账 86319，
  成功探测 432，加两次探测超时保守预留 296；总上限仍为 500000。

## 09:45 UTC 追加复测与外部调用暂停

- 用户已明确批准总上限从 500000 提高到 **1000000 token**，仍只运行少量固定模型场景。
  当前累计账本 **443586**：Demo 案件 356539、Windows 回放案件 86319、成功探测 432、
  探测未知用量预留 296。两个案件的未知 usage 已包含于各自记账值，不重复相加。
  Demo 案件当前 25 requests / 2 unknown usage，仍为 **0 Finding / 0 claims**。
  原任务累计调用额度耗尽，不清零、不修改已冻结任务预算。案件额度显式扩展为
  520000 token / 44 calls，deadline 10:00 UTC。
- 最近一次恢复在本地 ContextInsufficient 检查时终止，没有产生模型请求。定位到 Case
  历史报告和卡片约 42 KB 被全部重复放进每轮规划。新增 8 版报告/卡片测试先复现失败，
  然后修复 DecisionContext 按对象保留最新派生版本，逐条记录省略版本；完整事件、
  Case 历史、当前观察、判断、反证和缺口不改。上下文/报告/记忆相关 **10 项通过**。
- RoleRunner 的响应预算提示现在按本次格式修复响应数、案件剩余调用和任务累计剩余
  调用取最小值，并进入真实请求快照。两种边界的确定性测试通过；尚未完成真实模型复测。
- 审查任务原来只携带按范围筛选的证据，丢失原 issue 中的 claim premises。
  新增依赖修订传播测试复现后，修复为保留当前有效 issue basis 并合并范围证据。
  原始来源失效会传播至派生判断，独立判断和原始观察保留；审查/修复 4 项通过。
- V9 新增实际 Host 调度器的可控调查屏障测试：3 个案件、运行上限 1、队列上限 1；
  超额告警保留待派发状态，释放运行槽后逐个完成，不丢观察、不重复启动。
  `uv run --no-sync pytest tests/incident/test_intake_frontends.py -q`：5 passed。
- V1 双连接提交、V2 关键反证保留、V3 读取版本及无关更新、V4 实际 daemon 重启、
  V5 局部依赖传播、V6 482 次实际回放、V7 7 组真实隔离、V8 Jaeger、V9 乱序/绑定/UI
  已有上述记录；早期矩阵待办以这些后续实测记录为准。V10 三个完整场景仍未通过，
  V11 Windows 全套仍有失败，不更新 Stage 为验证通过。
- 恢复模型验收的启动被自动审批拒绝，理由为外部案件数据披露授权与密钥置于命令参数。
  已核对 live 配置只指向本机 OpenTelemetry Demo 后端；请求用户明确允许将合成验证
  遥测/上下文/分析结果发送至 LongCat 官方 API，并将密钥改为不经命令参数传递。
  审批拒绝没有执行进程、没有新增调用。确认前仅继续离线检查。

本节修复尚未形成独立提交；原有 Stage 1–6 工作区改动保留，未混入提交。

最终离线复测：

- WSL2：`uv run --no-sync pytest -q -o cache_dir=/var/tmp/amadeus-stage7-pytest-cache`，
  **2083 passed, 3 skipped in 176.28s**。
  日志 `/var/tmp/amadeus-stage7-full-retest.log`。跳过项是原有 macOS 大小写回归和
  两项 Windows/PowerShell updater 用例，未新增跳过条件。
- `uv run --no-sync ruff check src tests` 加本次五个验证脚本：通过。
- `uv run --no-sync mypy src`：169 source files passed。
- `uv build --no-sources --out-dir <temp>/amadeus-stage7-dist`：sdist/wheel 均成功。
  通过 `uv run --no-sync python` 检查 wheel 包含 incident、TUI 和 request_context 模块，
  不包含本地 Demo 配置及镜像锁文件。`git diff --check` 通过。

## Windows 拖拽修复与会话分支复测

- 复现原 coding TUI 在 Windows 的 8 项拖拽失败：POSIX shell 解析吞掉路径分隔符、
  带空格路径重复转义、file URI 不能解析。修复 Windows 分隔符保留和本地 drive URI
  转换；继续拒绝 remote authority。补充标准 URI、Unicode 和多文件引号断言，
  没有修改或删除原失败断言。
- 原子修复提交 **`858be3f27ecbfeb71c96c2dad9539b79d22aa5f2`**，仅包含
  `src/tau_coding/tui/file_drop.py`、`tests/test_tui_file_drop.py` 和 TUI 使用指南。
  既有 Stage 1–6 工作区实现没有被纳入此提交。
- Windows `uv run --no-sync pytest tests/test_tui_file_drop.py -q`：25 passed。
  WSL2 加 `tests/incident/test_session_binding.py`：27 passed。
- 新增真实 CodingSession/JsonlSessionStorage 的分支→压缩→关闭→重新加载测试，
  验证绑定随聊天分支恢复，提交后案件状态和完整事件重放不回滚；压缩使用 fake provider。
- 全仓 Ruff、配置范围 mypy（174 source files）和更新后的 sdist/wheel 构建通过。
- WSL2 完整复测：**2086 passed, 3 skipped in 187.96s**，
  `/var/tmp/amadeus-stage7-full-filedrop.log`，跳过仍是原有平台用例。
- Windows 完整复测：**37 failed, 2050 passed, 4 skipped in 208.26s**，
  系统临时目录 `amadeus-stage7-windows-retest.log`；使用独立 GUID basetemp。
  8 项拖拽失败已消除；先前 loaded model 点击用例本轮通过，但
  `test_closing_download_modal_detaches_and_reopen_offers_explicit_cancel` 本轮全套失败，
  单独复测通过（1 passed）。保留该不稳定结果，不将单项通过替代全套结果。
  其余 36 项失败仍与先前基线失败集合一致。
- 外部 LongCat 调用仍等待具体数据发送确认，记账没有增加。

## 下一步与通过门槛

### 后续离线缺陷定位与复测

- Windows shell 失败的实际输出是 `printf`/`shopt` 不被识别；当前 native Windows
  分支使用系统 shell，环境中的 `bash.exe` 是 WSL 启动器，并未配置原生 Bash。
  不将这些用例的 Linux 通过结果冒充 Windows 通过，也没有修改断言或新增跳过。
- 更新器同步失败测试在 Windows 上走了后台 handoff，绕过注入的 fake runner。
  固定该用例的同步平台，并加禁止启动真实后台更新器的断言；原结果与命令断言保留。
  提交 **`8ef3775`**，Windows 单项通过。只读检查没有发现测试留下的仍活跃更新器。
  真实 PowerShell handoff 的两项失败仍保留，没有据此宣布修复。
- provider 的立即错误/格式失败测试使用 10 ms 超时，与 Windows 调度竞争。
  为这两种非超时路径使用 1 秒期限，真正超时分支保持 10 ms，保留失败类型、原快照、
  单条诊断、敏感文本不泄漏全部断言。提交 **`a194b5c`**。
  Windows provider 全组 **70 passed**；WSL updater/provider **92 passed, 2 skipped**，
  两项跳过仍是需要真实 Windows PowerShell 的原用例。
- 新增 `test_historical_diagnosis_is_only_a_lead_and_rechecked_each_request`：旧案件经过
  Finding→审查→诊断报告→记忆，新案件有相反观察；请求将历史与当前证据分开记录，
  旧来源失效后即使复用已构造的视图也移除卡片，当前案件不被改写。记忆组 3 passed。
  此测试不替代尚未通过的完整模型历史修正场景。
- 该检查点尚未重新生成全套计数；后续 WSL2 2088 项结果见下一节。
  Windows 最近一次 37 项失败的完整结果保留其原执行范围。

### 完整离线诊断与真实 CLI 交接

- `test_investigation.py` 增加诊断场景，保留原有单任务/双任务测试：同一次
  `run_investigation` 自动规划 diagnose 任务、查询、提交 Finding、自动调度独立审查，
  审查前不生成报告，审查后 claim 更新为 v2/current，报告和记忆均为 diagnosis。
  请求快照、工具→证据注册、review 操作、报告 support 和卡片报告版本全部核对。
  4 项通过；这是 fake 确定性链路，不等于真实 LongCat 场景通过。
- 更新后 WSL2 完整 Python：**2088 passed, 3 skipped in 193.32s**，日志
  `/var/tmp/amadeus-stage7-full-diagnosis.log`。Ruff 通过；3 项仍是原有平台跳过。
- 实际 CLI：`uv run --no-sync tau incident handoff --config examples/incident-demo/host.json
  --case-id 718144583c2857dd908b7b184644574e --output /var/tmp/amadeus-stage7-handoff.json`；
  同配置执行 `timeline`，输出 `/var/tmp/amadeus-stage7-timeline.json`。
  通过 `uv run --no-sync python scripts/validate_incident_handoff.py <handoff> <timeline>`
  核对 **124 个事件、447 条执行、25 个请求、时间线首批 100 条**；完整事件重放
  与 v124 案件相同，证据来源操作、请求身份、游标、预算及 Jaeger 链接对应。
  该真实案件仍为 0 claims，不能将交接验证记为诊断成功。
- 只读请求实际 Jaeger trace 路由返回 HTTP 200；这只验证链接路由，不能证明每个
  早期未启用导出的本地 trace 都已存在于 Jaeger。

### 历史阻断与恢复清单（2026-09-28 用户确认前）

自动审批对恢复 LongCat 调用的同一拒绝已持续超过三个目标轮次；用户尚未回复此前
发出的具体数据发送确认。期间已继续完成独立离线修复、完整回归、诊断链路和交接
检查。当前没有正在运行的模型调用或测试作业。继续重复这些已通过检查不能替代
下一步所需的真实调查结果，因此目标记录为 blocked，交付仍未完成。

恢复所需确认是：允许将本机 `amadeus-demo` 的合成遥测、任务上下文和分析结果
发送至 LongCat 官方 API。**100 万 token 上限已经获批，无需再次批准预算**；
后续密钥不放入命令参数。累计记账保持 443586 token。

恢复后仍须完成：

1. 复核案件期限与剩余额度，实测最新上下文和响应预算提示修复；完成三个计划内
   固定模型场景，不以 fake 诊断或当前 0 claims 的交接结果代替。
2. 完成与实际调查关联的告警自动启动、并行中断恢复、误导历史修正及产品验收，
   核对报告来源，修复实际运行新暴露的缺陷。
3. 对后续变更复测；保留 Windows 全套已知失败及环境限制，审计其交付影响。
   完整受影响验证未通过前，不将任何 Stage 或总索引更新为“验证通过”。
4. 完成 Demo 故障恢复与最终交付记录；当前故障配置及案件证据保留以供续验。

继续补齐矩阵的未覆盖行为，处理完整 Tau 回归失败，执行 Ruff/mypy/打包与文档检查；
继续完整 Demo 场景与产品验收，计量 LongCat 总 usage。
真实调用工具探测与 fake 流程均不能替代完整真实场景。只有全部必要验证通过、
影响交付的缺陷解决并复测后，才更新各 Stage 和总索引为“验证通过”。

## 2026-09-28 恢复检查点

- 用户明确确认允许将 Demo 合成遥测、任务上下文和分析结果发送至 LongCat 官方 API。
  此前自动审批的数据披露阻断已解除；100 万 token 总上限沿用已批准额度，
  恢复前已记账 443586 token。凭证仅经进程标准输入设置为当前进程环境变量，
  不出现在命令参数、仓库文件或本记录中；本地仅验证了接收方式，没有新模型调用。
- 旧真实案件 `718144583c2857dd908b7b184644574e` 仍为 v124、25 requests、
  356539 token、10 条观察、0 Finding/0 claim，旧案件 deadline 已过，不冒充完成。
- Docker Desktop 当日重启后 25 个 Demo 容器均停止，Ubuntu 的 CLI 集成暂时不可用。
  错误报告精确指向运行目录中不可访问的 `dockerInference` socket。确认 Docker
  已停止且 `run` 内仅有三个零字节 socket reparse 条目后，将该目录保留为
  `%LOCALAPPDATA%/Docker/run.stage7-stale-20260928` 并重建空 `run`；镜像、卷、
  案件与数据集均未移动或删除。Desktop 启动及 Demo 恢复结果待后续检查。
- 再次启动暴露 `%LOCALAPPDATA%/docker-secrets-engine/engine.sock` 同类失效 socket。
  自动审批拒绝清理该 socket（返回 `blocked by policy`）；没有绕过该拒绝。
  按 [Docker Desktop CLI 文档](https://docs.docker.com/desktop/features/desktop-cli/) 尝试启动，
  关闭可选 `EnableDockerAI` 并备份设置后仍在重新生成的 `dockerInference` socket 处失败。
  Docker Desktop 的镜像和卷均未删除；其引擎仍不可用。
- 按 [Docker 官方 Ubuntu Engine 安装说明](https://docs.docker.com/engine/install/ubuntu/)
  曾在 WSL2 Ubuntu 安装 Docker CE/CLI **29.6.2**、containerd 和 Compose 插件。
  随后发现 Docker Desktop 已恢复；Windows 和 WSL2 `docker info` 均报告
  `docker-desktop`，WSL2 CLI 实际连接的是 Desktop 引擎，不能把它记成独立引擎验收。
  已停用 Ubuntu 额外的 `docker.service`、`docker.socket` 与 `containerd.service`，
  避免两个 daemon 并行；Desktop 引擎仍可用。固定 digest 镜像及旧卷均保留。
  WSL2 实例/引擎在 18:10 UTC 左右重启导致 25 个容器停止；重新执行
  `manage.py start` 后 25 个容器运行，Jaeger/前端均返回 HTTP 200。
- 从同一 Dockerfile 构建本地分析镜像
  `amadeus-analysis@sha256:389716cc9aef21bc6c6bf37ba68b8a8289ef66bf929e41dabb0f1e5731a2329c`，
  仅修改忽略的 `live.local.json`。真实隔离与派生证据脚本在当时连接的引擎上 **7/7 通过**，
  结果 `/var/tmp/amadeus-stage7-containers-native-20260928.json`。
- 已通过操作员入口启用 `paymentUnreachable` 场景；新完整模型调查及当前部署的
  数据导出尚待执行，旧案件 v124 结果不作为新部署的通过依据。

## 2026-09-28 当前模型与部署复测

- 新窗口首案 `f5869396f58e582b9c11326de0f6e0d3`：2 次 LongCat 请求，**6987 token**，
  0 任务/0 Finding。模型两次返回同一段括号层级错误的 JSON；请求快照证实第二次确实
  收到格式修复提示。补强重建 JSON 与任务范围提示，严格解析和范围校验保留。
- 双实体案件 `7135baafcb5459979d595bcf08243342`：18 次请求，**236002 token**
  （包含一次 180 秒读取超时后的未知 usage 保守预留），9 条观察，两个任务均
  `needs_review`，**0 Finding / 0 claim / 无诊断报告**。首轮模型把 `choice=progress`
  与非空 task 同时返回；补明确互斥条件，并增加拒绝此输出的复现测试。后来两次
  worker 最终响应在 2048 输出 token 处截断；按预算命令延长截止时间并增加
  80000 token / 8 calls，保留之前记账，使用 4096 输出重试。16K context 无法容纳
  完整请求，32K 后有效请求返回，但最后的 Claim 把 evidence 放入仅允许 claim 的
  `premises`。严格关系校验拒绝该结果；格式修复前案件 240000 token 上限耗尽。
  已补明确的字段说明和拒绝 evidence premise 的回归测试；尚未真实模型复测。
- 本轮模型总保守记账 **686575 / 1000000 token**：恢复前 443586，加上述 6987
  和 236002。未知 usage 计入，不把超时视为零费用。至此没有一个完整真实场景通过。
- Docker Desktop 后续恢复，但 WSL2/引擎约 18:10 UTC 重启，原容器停止；
  `docker info` 在 Windows 与 WSL2 均指向 `docker-desktop`。停用 Ubuntu 额外 daemon
  后重启 Demo，25 服务运行，Jaeger/前端均 HTTP 200。操作员 `reset` 已关闭早期开关
  并恢复 checkout 配置，准备在稳定窗口重新记录场景。Desktop 重启后的
  `validate_incident_tracing.py` 实际 Collector→Jaeger 六项检查通过，结果
  `/var/tmp/amadeus-stage7-tracing-desktop-restart-20260928.json`。
- 相关确定性复测：`uv run --no-sync pytest tests/incident/test_review_repair.py
  tests/incident/test_planner_contract.py tests/incident/test_recorded_provider.py -q`：
  **16 passed**；相关 Ruff 通过。新增代码尚待全套 Python 回归。
- 随后完整 WSL2 Python：`uv run --no-sync pytest -q --tb=short -o
  cache_dir=/var/tmp/amadeus-stage7-pytest-cache`，**2090 passed, 3 skipped in 146.48s**；
  日志 `/var/tmp/amadeus-stage7-full-20260928.log`。此后又修复了历史行来源，需最终全套复测。
- 新 Demo 窗口 18:28–18:31:30 UTC 依次执行操作员 `reset`、`flag-on`、
  `deploy-regression`；实际 checkout 容器 `PAYMENT_ADDR=payment-old:50051`。
  checkout/payment 两实体首次导出成功但无实际 logs 行；保留该数据集及校验失败，
  不虚构日志。将 cart 加入固定 18:29:30–18:31:00 窗口后，三种信号都有实际行，
  但公开 deployment 行缺失 `source`，`validate_incident_dataset.py` 正确拒绝。
- 修复 `VersionedCatalog.provider()`：将同一环境历史快照的稳定来源写入每个
  deployment/configuration 行，并保持查询结果来源一致。新增有真实变更的导出→回放
  复现测试，`test_telemetry_replay.py` **4 passed**。同一固定窗口重新导出，
  `validate_incident_dataset.py` **164 次回放查询通过**：metrics 252、traces 561、
  logs 266、deployments 1，保留 9 个覆盖缺口；数据 SHA-256
  `c23bf410a5980e8bcb6c4cd2acb2b68c6aca6ebe32176e7fdb2277229ca4b576`。
  结果 `/var/tmp/amadeus-stage7-live-dataset-three-signals-fixed-20260928`。
  故障开关属于操作员私有动作，不伪装成公开配置记录。

## 2026-09-28 后续完整调查与最终环境核对

- 真实新案件 `d7d1fc8ab4b35a2f8aede08359078a36` 使用 checkout/payment 的
  18:29:30–18:31:00 UTC 固定窗口、`LongCat-2.5-Preview`、4 responses/role、
  4096 output、28672 context。首轮生成 **1 Finding / 3 条 interpretation claim**，
  8 条工具观察；审查任务并行运行。案件额度 175000 token 耗尽时只有未审查结论，
  显式追加 100000 token / 8 calls 后恢复，仍在审查阶段耗尽。最终 **15 requests /
  276749 token / 2 unknown usage、9 observations、1 Finding、3 needs_review claims、
  0 diagnosis**；报告为 `progress/unverified`。不得记为完整场景通过。
- 从交接快照逐项核对：该 Finding 的证据包括 metrics 126 行、traces 15 行、
  checkout/payment logs 无匹配、公开部署记录 1 行。模型没有把无日志误写为无故障，
  但未核出受审查的因果诊断。两次 `python_analysis` 实际失败，因为引擎切换后
  `live.local.json` 指向仅存在于此前引擎的本地镜像；失败观察被保留，不能算分析成功。
- 重新核对 `docker info`：当前 WSL2 CLI 与 Windows 均连接 Desktop 引擎。
  先前 `amadeus-analysis@sha256:389716...` 镜像只存在于切换前引擎；因此该镜像的
  隔离 7/7 结果不能单独代表当前环境。使用空的临时 `DOCKER_CONFIG` 避免 WSL2
  缺失的 `docker-credential-desktop.exe`，在当前 Desktop 引擎从同一 Dockerfile
  重建并验证镜像
  `amadeus-analysis@sha256:39e89b6c4ac18e8c2434292fd1723f200b06603118e09d087252d05eccf60140`，
  更新仅本机忽略的配置，再次运行真实隔离与派生证据脚本：**7/7 通过**，结果
  `/var/tmp/amadeus-stage7-containers-desktop-final-20260928.json`。模型失败案件未重放
  以冒充修复后通过。
- 本轮最终全局模型保守记账 **963324 / 1000000 token**：恢复前 443586，首案
  6987，双实体旧窗口案 236002，新窗口案 276749。剩余额度 **36676 token**；
  停止新增模型调用。三组计划内完整模型场景及审查诊断门槛仍未通过。
- 用户已明确确认可将这次验证所需的合成 Demo 数据发送至 LongCat；凭证通过
  不回显的进程标准输入交给 WSL2 环境，仅驻留进程环境，不进入命令参数、仓库或日志。
  数据发送授权不扩大先前批准的 100 万 token 验证上限。
- 实际 CLI 对新案件导出 handoff/timeline，
  `validate_incident_handoff.py` 核对 **73 个事件、249 条执行、15 个请求、时间线
  100 条、完整事件重放**；报告入口返回 `progress/unverified`、0 diagnosis。
- 来源修复后的完整 WSL2 Python：**2091 passed, 3 skipped in 147.00s**，日志
  `/var/tmp/amadeus-stage7-full-finalcode-20260928.log`；全仓 Ruff 通过，
  `mypy src` 169 source files 通过，`uv build --no-sources` 成功。
  Stage 1–7 仍保留“验证中/待统一验证”。

## 2026-09-28 Windows 全量测试复核

- 对最终代码再次通过 `uv run --no-sync pytest -q` 执行 Windows 全套；运行到约
  92% 后长时间无进展，主动中断。日志 `%TEMP%/amadeus-stage7-windows-final-20260928.log`。
- 为定位停滞，使用独立 GUID `--basetemp` 再执行
  `uv run --no-sync pytest -vv --tb=short -o faulthandler_timeout=60`；运行到约 95%，
  `tests/test_tui_local_backends.py::test_closing_download_modal_detaches_and_reopen_offers_explicit_cancel`
  报失败后等待未结束的下载 worker；60 秒 faulthandler 栈显示 asyncio 测试仍在等待。
  主动中断，日志 `%TEMP%/amadeus-stage7-windows-final-verbose-20260928.log`。此运行
  没有完整汇总，不能把进度百分比换算成通过数。
- 单独执行 `test_backend_open_auto_refreshes_and_renders_clickable_models` 为 **1 passed**；
  单独执行整个 `test_tui_local_backends.py` 为 **8 passed / 3 failed**。三个失败分别是
  刷新完成前进度仍显示、点击模型后确认屏幕未出现、关闭并重开下载界面后进度为空。
  该文件在 Stage 7 中未改动，失败在本机 Windows/Textual 的异步界面时序中复现；
  不能据此证明新的 incident TUI 有回归，也不能声称 Windows 全套通过。
- Windows 最近一次**完成**的全套仍为前述 **2050 passed / 37 failed / 4 skipped**；
  其中与原 coding 拖拽、provider、updater 直接相关的已修复项目分别单独复测通过。
  最终代码完整 Python 的通过结果是 WSL2 的 2091 项。本机 Windows 完整运行仍是
  V11 的平台限制/待验证项，未删除、跳过或放宽这些用例。

## 2026-09-28 200 万额度内的固定模型续测与最终代码

- 用户明确将本次验证总上限提高为 **2000000 token**，模型仍固定为
  `LongCat-2.5-Preview`，只运行代表性完整调查；凭证仍通过不回显的进程标准输入交付。
- 旧实况案件 `d7d1fc8ab4b35a2f8aede08359078a36` 在当前 Desktop 分析镜像下
  显式扩充 125000 token / 8 calls 后恢复审查：新增 4 次请求、120066 token；
  累计 **19 requests / 396815 token**（含 2 次未知 usage 保守预留）。报告 v3 仍为
  `progress/unverified`，1 Finding / 3 未审查判断 / 0 diagnosis。未将重试记为通过。
- 新实况三实体案件 `443ee5158f20523bb4e8238bae34cc90`：checkout/payment/cart、
  固定 18:29:30–18:31:00 UTC，限量查询。首轮 6 次请求取得 catalog、metrics 30、
  logs 30、deployment 1 等 7 条观察，但 worker 用完 4 轮未返回完整结构化结果；
  将轮次调到 8 并重试后，原任务冻结的 8 次调用上限耗尽。累计 **12 requests /
  162587 token、0 Finding、0 diagnosis**。这暴露了任务预算不会因后来提高案件轮次而
  自动改变；任务耗尽被明确记录，旧观察保留。
- 新实况 8 轮案件 `0aaab6bb5ecb5d9295151b43017e3259`：初始任务获 16 次调用，
  **13 requests / 248952 token**（含 1 次未知 usage 预留）、7 条观察、1 Finding、
  3 条未审查判断、0 diagnosis。一次独立审查指出目录结论过强并要求读取原始记录；
  后续角色输入超出 28K/32K 上下文，48K 时案件剩余额度不足以预留请求。
  同一窗口较晚的实时查询出现 `no_match`，但此前导出存在三信号数据；这只能说明
  查询时点结果不同，不能单凭它确认数据保留策略或业务健康。
- 在 `/var/tmp/amadeus-stage7-replay-local.json` 选择已通过 164 次回放校验的固定导出
  `/var/tmp/amadeus-stage7-live-dataset-three-signals-fixed-20260928`，保留当前 Desktop
  分析镜像和 OTLP 配置；`fixture-now=2026-09-27T19:00:00Z`。新回放案件
  `5c7e3d5924745cf6aef871aca525e250` 首轮真实模型输出把任务完成条件改写，
  严格提交校验以 `unknown completion condition` 拒绝，只有 2 条 catalog 观察。
  修复 `FindingOutput.satisfied_conditions` schema 与 worker 指令，要求逐字复制任务契约，
  不放宽提交规则；`test_investigation.py` 4 项通过。重试后提交 1 Finding / 3 条
  interpretation 判断，实际目录中识别出 checkout 的 `PAYMENT_ADDR` 从
  `payment:50051` 变为 `payment-old:50051`；判断仍未审查，不能推出根因。
- 回放审查反复遇到最终 JSON 编码后的请求大于预估上下文。修复 `ContextBuilder`
  按最终 JSON 编码的 system/messages 大小选择与省略，而非按编码前文本估算；
  引号密集输入复现测试及相关上下文/请求 **15 passed**。再次真实复测得到第 10 条
  观察，但审查任务因案件预算耗尽未完成。此案件累计 **20 requests / 463573 token**
  （含 1 次未知 usage 预留）、10 observations、1 Finding、3 未审查 interpretation、
  **0 diagnosis**；报告 v5 `progress/unverified`。案件最后只剩约 5400 token，停止调用。
- 全局模型保守记账：此前 963324 + 旧案续测 120066 + 新实况 162587 + 8 轮实况
  248952 + 固定回放 463573 = **1958502 / 2000000 token**，余额 **41498 token**。
  不再发起模型请求；所有未知 usage 保留预留，不按零费用计算。人工、告警、晚到中断
  和相似历史的计划内完整模型场景仍未全部通过，V10 未达交付门槛。
- 最终代码 WSL2：`uv run --no-sync pytest -q --tb=short -o
  cache_dir=/var/tmp/amadeus-stage7-pytest-cache`，**2092 passed, 3 skipped in 151.95s**，
  日志 `/var/tmp/amadeus-stage7-full-after-context-fix-20260928.log`；
  `uv run --no-sync ruff check src tests` 及六个验证脚本通过，`uv run --no-sync mypy src`
  **169 source files passed**。3 项跳过仍为已有 macOS 大小写、两项 Windows PowerShell
  updater 用例；未新增跳过。Windows 全量未通过的情况见上节。

上述新 incident 代码和验证记录仍在原有未提交 Stage 1–6 工作区；不能写出不存在的
实现/修复提交。既有独立 Tau 修复提交仍按前文列出的实际 hash 记录。V1–V9 的
确定性及实况边界证据不抵消 V10/V11 的未完成项，Stage 1–7 和总索引保持验证中。

## 2026-09-28 300 万额度内的固定模型续测

- 用户将本次验证总上限提高到 **3000000 token**。继续使用固定
  `LongCat-2.5-Preview`、同一三信号导出和当前 Desktop 分析镜像；没有开展模型比较。
- 回放案件 `5c7e3d5924745cf6aef871aca525e250` 增加额度并继续审查：累计
  **28 requests / 805231 token**（含 1 次未知 usage 的 22908 token 保守预留）、
  10 observations、1 Finding、3 条仍未被接受的 interpretation 判断、0 diagnosis，
  报告 v7 `progress/unverified`。补入紧凑证据投影后，三项独立审查均实际返回，
  disposition 均为 `unresolved`；没有把完成审查误记为认可诊断。其中一次
  `python_analysis` 返回 `script_exit_nonzero`，失败观察保留。该案额度接近耗尽。
- 另一聚焦回放案 `0698e1d4dcf956c786b39f62732ba02d` 在人工 `constraint` 要求
  三信号查询后继续，但模型仍集中修复目录判断。累计 **16 requests / 422619 token**
  （含 1 次未知 usage 的 34451 token 保守预留），只有 2 条目录观察、
  1 Finding / 3 条未接受判断、0 diagnosis，报告 v2 `progress/unverified`。
- 新建 `79c18bc63cc35610a83b571f13b82ce7`，在模型开始前提交人工约束，要求
  指标、链路、日志、部署与隔离分析。**11 requests / 209322 token**，取得
  telemetry capabilities、service catalog、metrics 30、traces 30、logs 30、
  deployments 1、configuration `no_match`，并在当前 Desktop 镜像内完成两次
  `python_analysis`。两次容器均成功退出且完成清理，但脚本只打印所请求的 Evidence ID，
  **没有实际解析逐行数据**；不能将容器运行成功当作分析结论。已有 **1 Finding /
  2 条未接受的 interpretation 判断**；
  一项独立审查完成，另两项待处理，仍为 **0 diagnosis**、报告 v1
  `progress/unverified`。真实模型已使用三种信号和容器分析，但因果诊断尚未过审。
  CLI handoff/timeline 导出经 `validate_incident_handoff.py` 核对：**48 events、
  166 executions、11 requests、100 条时间线、完整事件重放**；输出在
  `/var/tmp/amadeus-stage7-signal-first-handoff.json` 与
  `/var/tmp/amadeus-stage7-signal-first-timeline.json`。
- 针对实测的上下文膨胀，在 `ContextBuilder.evidence_context()` 中投影供角色决策的
  证据摘要，保留引用、来源、时间、覆盖、单位、样本/截断、查询过滤、基线及派生
  证据关系；原始 Observation 与 artifact 保持完整，可通过 `evidence_read` 回取。
  回归测试确认大体积 backend 不进入简要决策上下文，查询过滤仍可见，原始存储未变。
  Planner 指令补充：目录只说明有哪些来源，不能单独证明故障原因；独立信号任务
  可在目录审查前继续。定向复测 13 项通过。对请求大小的严格限制和判断审查门槛
  均保留。
- 保守全局记账为此前 **1958502** + 回放案新增 **341658** + 聚焦案
  **422619** + 三信号优先案 **209322** = **2932101 / 3000000 token**，
  余额 **67899 token**。未知 usage 仍按预留计入。当前余额不足以合理安排
  完整审查及其后续场景，已停止新模型调用并请求用户决定是否提高上限。
  三组完整场景、告警入口的真实模型闭环、晚到恢复及误导历史流程仍未全部通过；
  V10 不通过，Stage 状态不更改。

## 2026-09-28 400 万额度内的审查与修复复测

- 用户进一步批准本次验证总上限 **4000000 token**。先给上述三信号案件增加
  300000 token / 12 calls；同一截止时间的预算命令被拒绝，因为延期必须晚于旧期限，
  改为 21:00 UTC 后接受，拒绝命令未改变案件或模型用量。
- 原有两项独立审查实际完成；新增 **7 requests / 238381 token**，案件累计
  **18 requests / 447703 token**，9 observations / 1 Finding / 2 未接受判断 / 0 diagnosis。
  审查正确指出“原始逐行 artifact 不可访问”的模型说法不成立：原始数据仍在 artifact，
  但先前模型只读取摘要，所写 Python 脚本只输出 ID。三个 review issue 仍为 repair，
  新修复任务已排队，不将审查完成算作诊断认可。保守全局记账为
  **3170482 / 4000000 token**。
- 修复真实模型暴露的工具契约缺口：`python_analysis` 的模型可见说明现在明确
  `/inputs/manifest.json` 如何把 Evidence ID 映射到只读输入文件，并说明 `/outputs`
  的派生输出；遥测查询说明按实体缩小范围、分页/过滤，以及部分结果不能视为全覆盖。
  `evidence_read` 说明有界切片与 `next_offset`。上下文必须摘要大工具结果时，
  对已读取的原文保留最多 1200 字符的直接片段与下一偏移，完整 artifact 与持久历史
  不变。新增/定向回归 **6 passed**，Ruff 通过。修复任务在新增 250000 token / 8 calls
  后使用固定模型续测；新脚本仍根据 Evidence ID 猜测文件路径，六项输入均报告
  `exists False`，没有解析 manifest，也没有输出逐行分析。案件累计 **26 requests /
  710152 token**（含一次未知 usage 的 27238 token 保守预留）、10 observations、
  1 Finding / 2 未认可判断 / 0 diagnosis，三个审查均 `unresolved`，报告仍为
  `progress/unverified`。据此把可直接复制的 manifest 读取示例同时写入
  `AnalysisRequest.script` schema 和工具说明；定向回归 **6 passed**、Ruff 通过。
- 全局保守记账至此 **3432931 / 4000000 token**，剩余 **567069 token**。
  新建 checkout/payment 聚焦案件 `54458e2dc6f7541eba06b577d892b6c2`，在调用前
  以人工约束要求分实体查询、检查部署前后字段和使用 manifest；同一固定模型以
  400000 token 案件上限运行。结果 **14 requests / 384919 token**、
  2 条目录观察、1 Finding / 3 待审查判断 / 0 diagnosis，报告
  `progress/unverified`。模型仍只规划了目录探查；目录判断生成的三项审查和修复
  占据后续任务预算，未查询指标、链路或部署行。此结果不能作为完整调查通过。
- 为避免目录探查阻断独立信号工作，Planner 指令改为：案件要求分析故障信号时，
  首个任务须把有界信号读取与必要的目录发现放在一起；worker 对纯目录任务只提交
  观察与缺口，不生成事故解释 claim。定向规划/调查 **9 passed**、Ruff 通过。
  全局记账现为 **3817850 / 4000000 token**，剩余 **182150 token**。
  新建最后一轮小额复测案件 `92159813b809535ba94650fe83f2f75e`，模型调用前
  通过人工约束写明首任务需读指标、链路和部署，案件本身限 150000 token / 8 calls；
  实际首任务确实包含了有界信号查询。分别查询 checkout/payment 指标（各 50 行）、
  链路（checkout 15 行、payment 无匹配）和部署（checkout 1 行、payment 无匹配），
  加目录共 **8 条观察**。这是规划修复的真实复测，但没有独立审查或诊断。
- 首轮 **7 requests / 117286 token** 后，worker 仍未提交 Finding，下一次请求因
  为案件调用预算保留报告额度而被拒；失败类别为 `context request_not_sent` 和
  `investigation no_valid_finding`，8 条观察未丢失。明确增加 50000 token / 2 calls
  并 `retry` 同一任务，模型再次用于读取而没有交付结构化 Finding。案件累计
  **9 requests / 163152 token**、8 observations、0 Finding / 0 claim / 0 diagnosis，
  任务为 `needs_review`、报告 `progress/unverified`。运行时严格拦截超预算调用，
  未伪造终结结果；最后两个模型响应均为批量 `evidence_read` 工具调用，没有
  返回 Finding JSON。模型未遵守最后一轮返回 JSON 的指令仍是未解决的完成性问题。
  CLI handoff/timeline 经 `validate_incident_handoff.py` 核对：**51 events、
  185 executions、9 requests、100 条时间线、完整事件重放、0 diagnosis**，文件为
  `/var/tmp/amadeus-stage7-first-signal-handoff-after-retry.json` 与
  `/var/tmp/amadeus-stage7-first-signal-timeline-final.json`。
- 全局保守记账 **3981002 / 4000000 token**，余额 **18998 token**。暂停后续模型调用；
  已请求用户决定是否将本次上限提高到 5000000 token，尚未收到批准。即使目前三种实际信号与真实隔离边界已实测，
  V10 的固定模型完整诊断、告警启动、晚到恢复、历史修正均未达验收门槛。
- 最后一次工具约束修改前的完整 WSL2 Python：`uv run --no-sync pytest -q --tb=short -o
  cache_dir=/var/tmp/amadeus-stage7-pytest-cache`，**2094 passed, 3 skipped in 202.38s**，
  日志 `/var/tmp/amadeus-stage7-full-after-tool-guidance-20260928.log`。跳过仍是既有
  macOS 大小写和两项 Windows PowerShell 用例；没有新增跳过。全仓 Ruff（含六个验证
  脚本）通过、`mypy src` **169 source files passed**、`uv build --no-sources`
  源码包和 wheel 构建成功、`git diff --check` 通过。Windows 全量限制仍按前节记录。
  Stage 1–7 与总索引保持“验证中/待统一验证”。
- 最后一次工具约束修改前的 Windows 定向回归：`uv run --no-sync pytest
  tests/incident/test_analysis_evidence.py tests/incident/test_context_retention.py
  tests/incident/test_planner_contract.py tests/incident/test_investigation.py
  tests/test_agent_loop.py tests/test_agent_harness.py tests/test_tool_history.py -q
  --tb=short --basetemp <系统临时目录内的新 GUID 路径>`，**45 passed in 5.64s**，
  日志 `%TEMP%/amadeus-stage7-windows-targeted-after-tool-guidance.log`。
  首次未设置独立 basetemp 时，旧 `%TEMP%/pytest-of-Meteorite` 拒绝访问，45 项都在
  `tmp_path` fixture setup 报错，未执行断言；换独立临时根后同一批测试通过。
  这不改变此前 Windows 全量未通过的记录。

## 最后可用调用的工具约束

- 真实案件 `92159813b809535ba94650fe83f2f75e` 的最后两次模型响应仍各发起了
  批量 `evidence_read`，虽然角色指令已要求返回 Finding JSON。对
  `RecordedProvider.project` 增加可用调用数检查：案件、任务或当前角色只剩一次
  调用时，
  实际发送给 provider 的工具列表为空，请求快照与上下文估算均使用相同有效列表。
  正常调用仍保留已授权工具；原始历史、证据、预算预留和 Finding 审核规则均未修改。
  最后一轮模型仍须自己给出有效 JSON，系统不代写结果。
- 复现测试分别断言案件最后调用、角色最后调用均无工具，正常轮次仍有工具，
  并核对 provider 实参与持久请求快照一致；`test_recorded_provider.py` 与
  `test_investigation.py` **13 passed**，Windows 同组 **13 passed**。
  全仓 Ruff、`mypy src` 169 项、
  `uv build --no-sources`、`git diff --check` 已通过。最终完整 WSL2 Python
  `uv run --no-sync pytest -q --tb=short -o cache_dir=/var/tmp/amadeus-stage7-pytest-cache`
  **2097 passed, 3 skipped in 222.25s**；日志
  `/var/tmp/amadeus-stage7-full-final-role-no-tools-20260928.log`。三项跳过仍为既有的
  macOS 大小写及两项 Windows PowerShell 用例，没有新增跳过。
  本改动尚未在真实模型下复测，不能计入 V10 通过。

## 当前代码的 Windows 全量复核

- 为避免旧 `%TEMP%/pytest-of-Meteorite` 的拒绝访问，用系统临时目录内新 GUID
  `--basetemp` 执行完整 `uv run --no-sync pytest -q --tb=short`，结果
  **2063 passed / 35 failed / 4 skipped in 202.57s**；日志
  `%TEMP%/amadeus-stage7-windows-full-independent-basetemp.log`。本次运行完整结束，
  不再用 92%/95% 的中断进度推算结果。
- 与原始 HEAD 的 **45 failed** 列表逐项比较，当前失败中有两项不在基线：
  `test_daemon_survives_sse_disconnect_and_process_restart` 和
  `test_wheel_includes_release_notes_package_data`。两项都需要可通过 PATH 找到 `uv`；
  最初仅使用绝对路径启动 `uv.exe`，该 shell 的 PATH 未包含其目录。将同一目录加入
  PATH 后，用独立 basetemp 单独复测两项 **2 passed**，说明这两项不是代码回归。
  其余 33 项均见于原始 HEAD 失败列表，主要涉及 Windows 路径大小写/斜杠、
  POSIX shell 命令、文件权限及原 TUI 布局。正确 PATH 的再次全套运行结果见下项；
  V11 Windows 全量仍未通过。
- 修正 PATH 后的再次完整 Windows 测试运行至约 92% 的原 TUI 区段停止前进；
  `faulthandler_timeout=60` 写出 asyncio 等待栈后，进程仍无进展。核对进程命令行
  只匹配该次独立 GUID basetemp 的 pytest/uv 进程后，结束该次测试；日志
  `%TEMP%/amadeus-stage7-windows-full-uvpath.log` 没有完整汇总，不能把百分比当作
  完整运行。上述两项 PATH 相关用例的 **2 passed** 是单独复测，不替代整套结果。

## 最后请求的工具调用执行检查

- 检查发现上一项修复只从 provider 请求中移除工具定义，而 Harness 仍保有该角色原先
  授权的工具对象。如果模型仍返回未经本次请求提供的工具调用，原 Harness 会执行它。
  现将本次请求实际提供的工具名记录在 `RecordedProvider`，并在 Harness 执行工具前
  拦截未提供的名称；阻断结果保留为工具错误，不执行工具副作用，也不伪造 Finding。
- fake provider 复现：最后一轮即使返回 `ToolCall`，工具执行计数仍为零；正常轮次
  的工具仍可调用，请求快照中的工具列表与 provider 实参一致。相关
  `test_role_response_budget.py`、`test_recorded_provider.py`、`test_investigation.py`
  **16 passed**，Windows 同组 **16 passed**；Ruff、`mypy src` 169 项、构建和
  `git diff --check` 通过。最终完整 WSL2 复测命令为
  `uv run --no-sync pytest -q --tb=short -o cache_dir=/var/tmp/amadeus-stage7-pytest-cache`，
  **2098 passed, 3 skipped in 206.05s**；日志
  `/var/tmp/amadeus-stage7-full-offered-tool-guard-20260928.log`。三项跳过仍是既有的
  macOS 大小写及两项 Windows PowerShell 用例。真实模型尚待额度批准后复测。

## 当前代码的入口定向验收

- Windows 以 `uv run --no-sync pytest tests/incident/test_intake_frontends.py
  tests/incident/test_workspace.py tests/incident/test_service.py
  tests/incident/test_session_binding.py tests/incident/test_service_process.py -q
  --tb=short --basetemp <系统临时目录内的新 GUID 路径>` 复测实际 HTTP/RPC、
  告警去重与限流、Textual Pilot 案件工作区、会话绑定、daemon 断线和进程重启，
  **11 passed in 11.43s**。将 `uv.exe` 目录加入子进程 PATH；pytest 的旧仓库缓存
  目录仍有权限警告，不影响这 11 项断言。此项是入口集成测试，尚不能代替固定模型
  的告警启动闭环和计划中的完整产品操作验收。
- WSL2 对当前真实案件 `92159813b809535ba94650fe83f2f75e` 分别执行三个只读 CLI 入口：
  `uv run --no-sync tau incident report --environment amadeus-demo <case_id> --format json`、
  `uv run --no-sync tau incident memory --environment amadeus-demo <case_id>` 和
  `uv run --no-sync tau incident show --environment amadeus-demo <case_id> --view budget`，
  均退出 0；
  输出分别存于 `/var/tmp/amadeus-stage7-current-report-cli.json`、
  `/var/tmp/amadeus-stage7-current-memory-cli.json`、
  `/var/tmp/amadeus-stage7-current-budget-cli.json`，均经 `uv run --no-sync python -m
  json.tool` 解析。报告仍明确为 `progress/unverified`，检索卡片亦为
  `unverified`，案件可用调用数为 0；没有把入口可用误记成调查完成。
- 同一真实案件用 `uv run --no-sync tau incident show --environment amadeus-demo
  <case_id> --view evidence --evidence-id 317da37c9f304d0da93f58dc82cd0583`
  以及 `--view request --request-id de0cc80a94cf4739864dc815f74eb610`
  回取实际指标观察和持久请求快照，均退出 0、通过 `uv run --no-sync python
  -m json.tool`，且返回对象的案件 ID 与所请求的 Evidence/Request ID 一致。
  输出 `/var/tmp/amadeus-stage7-evidence-cli.json`、
  `/var/tmp/amadeus-stage7-request-cli.json`；它们包含原始材料，不纳入仓库。

## SQLite 旧版升级补测

- 原指南写明 schema 1–4 升级至 5，但已有 Stage 7 incident 测试未覆盖这条升级路径。
  新增 `tests/incident/test_store_migrations.py`，从当前 schema 删除各版之后新增的表或列，
  构造包含已提交案件、事件和回执的旧版数据库；分别升级 1、2、3、4 至 5，
  检查案件、回执、事件及新表保留，重复打开不重复迁移。另注入中途表名冲突，
  断言失败升级回滚 schema 和版本号而保留旧数据。这里使用可控的重建旧版 schema，
  没有声称已测试真实历史数据库文件。
- WSL2 `uv run --no-sync pytest tests/incident/test_store_migrations.py -q --tb=short`
  **5 passed in 2.66s**；Windows 同组 **5 passed in 0.76s**。Ruff check 和 format
  通过。增加用例后的 WSL2 全量命令 `uv run --no-sync pytest -q --tb=short
  -o cache_dir=/var/tmp/amadeus-stage7-pytest-cache` 为 **2103 passed, 3 skipped in
  219.11s**；日志 `/var/tmp/amadeus-stage7-full-with-migration-20260928.log`。
  三项跳过仍是已有的 macOS 大小写和两项 Windows PowerShell 用例。

## 独立 daemon 的无模型产品短流程

- 用 `uv run --no-sync python scripts/validate_incident_product.py --output
  /var/tmp/amadeus-stage7-product-smoke-20260928.json` 在 WSL2 的临时 `TAU_HOME`
  启动真实 incident daemon，生成只供本次使用的本地入口和 Webhook 凭证。
  经远程 CLI `connect`、`inbox`、`query`、`associate`、`import-alert`、`timeline`、`handoff`，以及实际 HTTP
  `/webhook/alertmanager`，检查 firing 投递、同 ID 同内容去重、同 ID 改内容拒绝、
  resolved 关联原案且不自动宣布业务恢复；另以未匹配的 resolved 告警触发
  `needs_association`，经远程 CLI 手动关联原案。案件保留三条观察，时间线有 17 条执行；
  重启 daemon 后远程查询仍得到同一案件与观察。交接中实际模型请求数为 0，
  重启后再从远程 CLI 导入另一条 firing 告警，同内容重复导入被去重，并创建与原案
  不同的案件；新案也只有一条告警观察、0 次模型请求。
  不将这个无模型产品短流程冒充告警启动后的完整调查。结果 JSON 通过
  `uv run --no-sync python -m json.tool` 解析，退出 0；临时进程退出且未残留
  `tau incident serve` 进程。脚本 Ruff 与单文件 mypy 通过。
- 这补充了入口实测，但尚未覆盖固定模型的告警闭环、晚到恢复及历史修正场景；
  因而 V9/V10 和整体验收仍不能标为通过。

## 固定模型续测的当前前置条件

- 2026-09-27 21:06 UTC 再次只读查询案件 `92159813b809535ba94650fe83f2f75e`
  的预算：9 次调用、163152 token、可用调用 0、可用 token 35824，原案件截止
  时间 21:00 UTC 已过。全局保守记账仍为 **3981002 / 4000000 token**；没有
  在剩余 18998 token 中启动可能无法完成审查的模型流程。用户尚未确认将本次总上限
  提高至 5000000 token。收到确认后，恢复同案前还需以显式预算命令延长案件
  截止时间并增加调用数，随后核对新回执；不能将总额度批准自动当作案件预算变更。

## 真实案件的 TUI 只读验收

- `uv run --no-sync python scripts/validate_incident_tui.py
  92159813b809535ba94650fe83f2f75e --environment amadeus-demo
  --evidence-id 317da37c9f304d0da93f58dc82cd0583
  --host-config examples/incident-demo/host.json
  --output /var/tmp/amadeus-stage7-real-tui-20260928.json` 在 WSL2 用 Textual Pilot
  打开已保存的固定模型案件。概览显示原症状、证据页显示指定指标 Evidence ID、
  任务页显示真实 Task ID；时间线分页后得到 **186 条执行**，筛选出 **9 条
  model_request**。在工作区输入框回取指定证据并于 Details 显示同一 ID，
  时间线呈现 **186 条 Jaeger 导航链接**，其中一条实际路由返回 HTTP 200；
  这验证了 UI 链接和服务路由，不证明每条 trace 均有后端 span。
  无 UI 错误，案件前后仍为 v51、8 条观察且整个 Case 对象相同。脚本 Ruff 与
  单文件 mypy 通过。此为真实数据的只读 TUI 验收，未调用模型或证明诊断正确；
  固定模型告警/晚到/历史组合流程仍待验证。

## 可选模型步数检查点（2026-09-28，离线修改）

用户要求把累计硬 `call_limit` 改为可选的阶段性刹车，并明确本轮**不跑真实模型复测**。
新建 Case 默认没有累计调用上限，也不再预留确定性进展报告用不到的模型调用；
`--checkpoint-steps N` 按本次 `run`/`resume` 中规划、worker、审查和格式修复的模型请求统一计数。
达到 N 次后原子地拒绝下一次请求，保存进展报告并暂停 Case。再次 `resume` 开始新的步数周期，
但累计 usage、token 预算和 deadline 不清零。一次请求的最后模型响应不提供工具，
并要求角色提交结构化结果或明确缺口。新任务不再继承由 `max_turns` 推导的累计调用上限。

已存在 Case 的持久硬 `call_limit`、报告调用预留、deadline 和旧任务预算保持原值；
`run`/`resume` 参数不能暗中扩张它们。因此本报告前述真实案件仍受原有硬调用上限和
已到期的 deadline 约束，此次改动不会使其自动继续。`--call-limit` 仍可显式为新 Case
设置累计硬上限，`budget --add-calls` 只适用于已有该硬上限的 Case。
`--max-tasks` 也改为可选的单次派发/规划上限；每角色 `--max-turns`（默认 8）
仍限制角色响应数，并要求最后一步输出结构化结果或缺口。

新增复现测试覆盖默认无硬调用上限、并发请求的原子检查点、达到检查点时任务回到 ready、
继续运行后重新计数但不清账、最后一步无效工具响应时不把任务误判为已完成，以及 CLI 参数。
取消默认 `max_tasks` 后，定向测试发现已审查的诊断还会多发一次规划请求；
现于独立审查满足报告完成条件后直接提交确定性诊断报告，补充断言
`stop_reason=case_completed` 和无多余请求。新 Case 上 `budget --add-calls`
被拒绝而 `--add-tokens` 仍可用，也有单独复现测试。

WSL2 设置已配置的 `UV_PROJECT_ENVIRONMENT`、`UV_CACHE_DIR`，并将 uv 可执行目录
加入 `PATH` 后运行：

```sh
uv run --no-sync pytest -q --tb=short -o cache_dir=/var/tmp/amadeus-checkpoint-verified-full-cache
uv run --no-sync pytest tests/incident -q --tb=short -o cache_dir=/var/tmp/amadeus-checkpoint-verified-incident-cache
uv run --no-sync mypy src
```

最终 incident 全组 **97 passed**。全量 Python 测试 **2110 passed、3 skipped**；三项跳过为仅适用 macOS 的文件系统
大小写用例与仅适用 Windows/PowerShell 的两项更新器用例。`mypy src` 为
**169 个源文件无错误**；相关源码和测试的 Ruff check、改动文件 format check 通过。
最后补入的单个旧预算兼容性测试在全量测试收集后新增，已用 `uv run --no-sync pytest
tests/incident/test_budget_recovery.py` 单独复测 **8 passed**；随后以上述 incident 全组命令
再次核对。WSL2 初次定向用例中 1 项因子进程 PATH 缺少 uv 而失败，
修正 PATH 后 WSL2 incident 全组通过；这不是产品行为缺陷。

本轮没有调用真实模型或获得新的诊断证据；现有真实案件不自动迁移旧的持久
`call_limit`、任务调用上限或过期的 deadline。V9/V10 与 Stage 7 总体验收仍为待验证。
本次改动与此前未提交的 Stage 代码同处工作树，没有单独修复提交。
