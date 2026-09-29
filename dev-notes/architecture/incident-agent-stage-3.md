# Ariadne Stage 3：并行、等待、预算与恢复

状态：**实现完成，待统一验证**。日期：2026-09-27。

基线为 `c66fb87`，在工作区已有 Stage 1–2 实现上继续修改；没有创建本阶段 Git commit。
本期只做语法、纯导入、lint、格式和类型检查，没有编写或运行测试，也没有运行模型、
Fixture 调查、并发、故障注入或恢复演练。以下描述实现的协议，行为验证统一在 Stage 7 完成。

## 1. 为什么需要这一层

Stage 2 已有一次任务的 Executor、工具、Finding 和提交回执。长期调查还需要回答：
谁可以继续这个 Case，哪些任务现在能执行，旧执行返回后能否提交，以及等待和中断
期间如何保留预算。Stage 3 在既有执行流程外增加这些控制，不改变 Harness 的职责。

`tau_agent` 仍只负责可复用执行；`tau_incident` 拥有案件控制；`tau_coding` 创建 provider、
提供路径和生命周期。没有向 Harness 引入数据库、Textual、案件或 provider 配置依赖。

## 2. 模块和完成标准的对应关系

| 要求 | 实现位置与数据 |
| --- | --- |
| 唯一有效协调者、续租、代次接管 | `coordinator.py` 的 acquire/renew/release；`store/control.py` 的 owners 与事务内身份检查 |
| 依赖就绪、有界派发 | `claim_ready_tasks()`、DispatchTasks；任务、独立 attempt、额度预留与回执在同一提交事务写入 |
| 独立消息、取消、provider 会话 | 复用 `executor.py`；每个 attempt 独立 Harness 和 trace；应用 `investigation.py` 的 provider factory 与关闭边界 |
| 固定契约、实际读取和复验 | `context.py` 按派发 Case 版本重建；request snapshots、manifests、RecordRead、Finding 引用及原生目录版本 |
| 结果接受、待复核、迟到拒绝 | `submission.py`、`store._finding_manifest()`、Reducer；所有权、active_attempt_id、契约版本、读取版本再次检查 |
| Case/任务/attempt/request 预算 | `budget.py` 与 store 的 policies、attempt_reservations、request_usage、request_results；保留报告额度 |
| WaitCondition 与唤醒 | `planner.py` 的结构化等待、`recovery.check_wait()`、Fixture 水位检查、RecordWait 与超时动作 |
| 暂停、取消、接管和有限收尾 | `investigation.py` 主循环、`recovery.py`、生命周期事件、host 的 embedded/daemon 契约 |
| CLI 和可追溯进展 | `tau_coding/incident/cli.py` 的 resume/pause/cancel/retry/budget、owner/manifest 查询；确定性进展包含任务状态和等待 |

SQLite schema 从 2 升为 3；保留旧事件、回执和证据。迁移按兼容 Reducer 重建旧任务
投影，补全旧 Finding 的对象版本。Stage 1 的人工观察命令 hash 和 Stage 2 的缺省字段
按旧格式规范化，所有权代次不参与稳定命令内容 hash。迁移与跨版本幂等尚待统一验证。

## 3. 所有权与派发时序

1. Runtime 在 `BEGIN IMMEDIATE` 内取得过期或空闲 Case 的租约，增加 generation。
   有效租约不能被另一个入口接管。续租与派发都重验 owner ID、generation 和到期时间。
2. 启动恢复先核对旧执行。新的 run 不接管仍有效的前端或后台调查。
3. Planner 提交任务契约；有等待条件时，与计划在同一事件中保存。派发另行进行。
4. 派发检查依赖的任务 ID、契约版本和完成状态、Case 状态、并发额度、任务/Case
   预算和 deadline。任务切换到 running，新 attempt 与其预留在同一事务产生。
5. 事务结束后才创建 worker 和等待 provider/工具。每个 attempt 有新 execution token、
   provider session ID、trace 和取消状态，重跑通过 predecessor 与 span link 连接旧执行。

默认 Case 并发为 2。`--global-concurrency` 默认 8，约束同一项目 SQLite 中所有有效
协调者的角色和 attempts；多个活动配置取最严格的上限。这不是跨项目、跨数据库或
跨机器的集群限额。独立角色（包括 Planner）也占一个 slot；未来摘要、审查等调用
复用 RoleRunner 同一入口。本阶段没有实现 Reviewer 或修复策略。

应用为规划与每个 worker 分别创建 provider，禁止默认共享有状态的供应商实例。
领域调用方若只提供一个 RoleRunner 而不提供工厂，则串行运行。工具仍沿用 Harness
原有顺序工具循环，没有引入第二套并行工具执行器。

## 4. 如何处理并行结果

TaskContext 从 `starting_case_version` 重建，不在下一轮换成最新 Case 前提。派发依据、
每次准备交给 provider 的选择清单、动态 evidence_read 和 Finding 显式引用分别记录。
`show --view manifest --attempt-id ...` 也能查询没有最终 Finding 的中断 attempt。
准备后确定未发送的请求从实际读取清单排除；发送结果不明时保守保留请求关联。
这些记录描述模型拿到的材料及其声明的依据，不声称捕获全部隐式推理依赖。

最终提交在短事务内重验：有效租约、attempt generation/token、任务当前的
active_attempt_id 与 contract_version、允许读取的引用，以及未结清预留。依据之外的
Case 更新不要求全案版本相等。实际读取版本变化、新的执行约束，或相关目录/配置
原生版本变化，会保留 Finding、添加 ReviewIssue，并把任务标为 needs_review。
全部候选 Claim 延续 Stage 2 的 needs_review，语义审查属于 Stage 4。

`ReviseTask` 必须明确提供下一契约版本和理由，取消旧执行及其等待；`RetryTask`
显式将停止任务重新排队，下一次派发创建新 attempt。任务累计消耗不因重试清零。
旧代次、取消或替代的 attempt 不能提交最终结果，也不能改变新任务的完成状态。
此前已开始工具查询所得的晚到观测允许保留，但必须关联原工具操作及授权 scope；
新的解释需要从显式读取和新执行走原提交协议。幂等控制命令重放不再次触发取消。

## 5. 预算和请求结果

Case 额度首次运行时持久化。之后的 `run/resume` 不重置累计调用、tokens、deadline 或
报告预留；额度增加通过 ExtendBudget 事件提交，可延长 deadline。新的运行配置仍可
设置并发、上下文和有界执行参数。

派发先预留一次调用和一个完整上下文窗口的 tokens，保守保证每个获准 attempt 有
启动额度。每次请求再用实际投影输入估算加输出 allowance 预留，将所属 attempt 的
启动预留转入请求账本，同时保护其他 attempt 的额度。后续轮次、格式修正及显式
重试都走同一入口；任务额度累计该任务所有 attempts。

请求状态分为 reserved、settled、unknown 和确定未发送的 not_sent。正常返回先保存
响应 artifact 与 request_results，并在同一短事务结算 usage，再结束模型 span。
没有 usage、默认全零 usage 或中断时，保留原估算与 unknown；不能退为零。
只有实际调用前的确定失败可以释放为 not_sent。按 request ID 去重；恢复只根据同一
响应核对，不再计费。执行记录与未来 tracing 导出读取这份账本，不结算第二次。

`show --view budget` 区分已知 tokens、请求预留、attempt 预留、未知额度、可用额度，
并单独报告供应商提供的 cost 和金额未知的调用数。默认保留 1 次调用和 1024 tokens
用于报告策略；当前部分进展是确定性输出，不需要模型。

token 估算与输出 allowance 不是精确计费上限；报告的实际 usage 可以超过预留。
Codex provider 不具备显式输出 cap 的适配参数，快照保留这一限制。未实现可靠货币
单价映射，因此不接受 task 的 monetary hard limit；默认零 cost 不表示免费。
应用禁用可配置的 SDK 重试，所有可见的再次模型调用都有新的 request ID。

## 6. 等待、暂停与恢复

WaitCondition 保存 source、scope、下一检查时间、deadline、检查间隔和超时动作。
支持时间、已登记证据及注入的来源水位 probe；Fixture 实现按 source、signal、范围、
available_at 和目标 watermark 检查。水位检查不创建事实证据，也不调用模型；实际
调查仍需工具登记观测。未知来源或 probe 失败保存 last_error，并有界重查直到 deadline。

等待任务不占执行 slot，其他就绪任务继续；没有可执行依赖且没有在途 attempt 时，
才进入 Case waiting。每次检查是新 span，连接原 WaitCondition 操作；不把等待时间
计入一个长期开放的模型或工具 span。超时明确 resume、cancel 或 pause。

暂停先持久化禁止旧执行完成的状态，停止新请求和派发，再取消并有限收尾。暂停保留
等待条件；resume 立即检查已到期条件。取消任务与暂停区别保存；重试需要明确动作。
主循环自然退出也保存暂停状态。CLI 中断和 embedded 前端退出走同一暂停路径。

接管仅针对已失效 generation：旧 running attempt 标为 interrupted，释放未使用启动
预留，未返回 usage 的请求保持 unknown。按命令回执、请求响应、已登记证据核对未完成
操作，有权威结果才补记对应结果；无依据则保留 interrupted/unknown。记录恢复时间
及来源，原始结束时间和耗时继续为空，不伪造正常完成。请求不会从模型生成一半的位置续写。

Host 的 `mode="daemon"` 仅定义生命周期：frontend_detached 不停止运行；embedded
断开则暂停。外层关闭活动 host 要 `await host.shutdown()`；同步 close 拒绝关闭仍有
活动 owner task 的存储。收尾超时显式报错并保留存储，随后可核对持久状态。本阶段
没有常驻进程管理器、连接协议或 UI 重连服务，这些留到 Stage 6。

独立 CLI 写入口取得短租约；遇到有效协调者明确返回所有权冲突。运行中的调查可以
在所属 Runtime 调用 pause/cancel，CLI 进程用 Ctrl+C 暂停。这里没有提前添加跨进程
控制信箱或后台连接。

## 7. 静态检查

使用现有项目 `.venv` 和 `%TEMP%/amadeus-stage1-tools/uv.exe`。PATH 没有 uv，沿用
前两阶段的工具；`UV_CACHE_DIR=%TEMP%/amadeus-stage1-tools/cache`，`--no-sync`
不安装依赖、不改 PATH 或 lockfile。以下 `uv` 表示该绝对路径。

```text
uv run --no-sync ruff check src/tau_incident src/tau_coding/incident
uv run --no-sync ruff format --check src/tau_incident src/tau_coding/incident
uv run --no-sync mypy -p tau_incident -p tau_coding.incident
uv run --no-sync python -m compileall -q src/tau_incident src/tau_coding/incident
uv run --no-sync python -c "import tau_incident.models; import tau_incident.events; import tau_incident.store; import tau_incident.store.control; import tau_incident.budget; import tau_incident.context; import tau_incident.submission; import tau_incident.executor; import tau_incident.planner; import tau_incident.coordinator; import tau_incident.investigation; import tau_incident.recovery; import tau_incident.telemetry; import tau_incident.telemetry.tools; import tau_incident.reporting; import tau_incident.execution.provider; import tau_coding.incident.host; import tau_coding.incident.investigation; import tau_coding.incident.cli"
```

结果：上述检查通过，mypy 覆盖 24 个源文件。检查期间修正了类型、导入、格式及闭包
捕获问题。纯导入没有实例化 Host/Store/Provider，没有创建案件数据库、解析 CLI，
也没有执行迁移、调查或任何并发和恢复路径。静态通过不等同于正确性测试通过。

## 8. Stage 7 必须验证的时序

- **V1**：schema 1→2→3，旧命令 hash 与事件重放；任务/attempt/预留/回执同事务；
  COMMIT 未知后的原 ID 查询；两个最终 Finding；任务修订与重复暂停命令。
- **V3**：默认两个 worker；跨 Case 的全局额度及 Planner slot；依赖阻塞；启动预留
  转请求与别的 attempt 抢占；实际超额、缺失/零 usage、迟到 usage、重复结算；预算
  扩展不清零；未来摘要/审查/修复共用入口；低额度时确定性报告仍可输出。
- **V3/V4**：派发到首请求之间有更新；中途 evidence_read；无关更新合并；实际前提
  或目录版本改变；取消、契约替代、新 attempt 和旧 Finding/观测交错返回。
- **V4**：租约续租/到期边界、双协调者争用、失效代次接管、暂停后重新派发、等待
  与另一任务完成交错、时间/证据/水位检查、检查异常、deadline 的三种动作。
- **V4/V8**：快照已写但未调用、调用已开始未返回、响应已存但 span 未结束、提交已
  完成但执行记录失败；按响应/证据/回执恢复，不补造结束时刻；取消不响应时有限收尾。
- **V8**：独立 provider/session/trace 不串线；dispatch/retry/wait/recovery links；
  request manifest 与全文 artifact 一致；本地记录失败不被误报成成功。
- **V10**：同一 Case 的 run→暂停→resume→等待→继续→部分进展；CLI 参数、返回状态、
  Ctrl+C、host embedded/daemon 生命周期。实际后台连接与前端重连在 Stage 6 后合验。

以上均为待验证事项，没有执行结果。Stage 4–6 未在本期提前实现。
