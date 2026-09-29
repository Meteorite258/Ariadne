# Ariadne 持续调查 v2：实现与离线验证

日期：2026-09-28。**统一改造与离线验证完成。**
**Stage 7 仍为验证中；固定模型完整诊断的有效性待验证。**

本轮真实模型请求为 **0**。此前 LongCat 验证记账保持 **3,981,002 / 4,000,000 token**。
没有 benchmark、多模型比较或消融；Python、测试和构建命令均通过 `uv`。

## 状态归属

Tau 分层不变：`tau_ai` 负责 provider，`tau_agent` 提供通用 Harness、工具、事件和请求投影。
案件、数据库和质量规则在 `tau_incident`；路径、Docker、HTTP/RPC/CLI/TUI 组装在 `tau_coding`。

| 内容 | 权威记录 | 派生和写入边界 |
| --- | --- | --- |
| 范围、症状、用户控制 | Case 与领域命令事件 | Case 展示由控制意图、有效执行、任务、等待和报告生成 |
| 工作目标、契约、进度 | Task 与版本化 `Task.progress` | Task 状态由 reducer 生成，带 `status_source_version` |
| 一次执行 | Attempt 身份、代次、execution token、结果 | 后继执行链接前次执行；旧执行不能提交新任务结果 |
| 数据、来源、覆盖 | Evidence + 不可变 artifact | Progress 只引用原始材料 |
| 推理 | Claim 类型化 `dependencies` | support/opposition/premises 是属性；删除未接线 Hypothesis/Assessment |
| 审查 | ReviewIssue 对指定判断版本的意见和依据周期 | 不复制修复任务执行状态；批审仍逐项给出结果 |
| 正式提交 | Finding 冻结快照 | 与 Progress 共用 WorkContent；生命周期分开 |
| 交付 | Report / MemoryCard 的来源版本 | 新相关证据、判断或任务变化使交付失效 |
| 请求和用量 | request_id 账本、RequestSnapshot.selection | Case/Task/Attempt 用量由同一账本聚合；读取清单由快照和显式读取生成，移除重复 manifests 写入 |
| 执行和租约 | ExecutionRecord、请求结果、租约账本 | 不宣称领域事件能独立重建执行事实 |

没有新增 PlannerMemory、WorkLog 或另一套假设数据库。Planner 提出 Decision；ContextBuilder
组织输入；UI/tracing 消费记录。`rebuild_case_projection` 重建领域投影和版本索引，保留 usage 及执行账本。

## 按六步实现

| 步骤 | 已接入行为 | 主要代码 |
| --- | --- | --- |
| 1：边界和状态 | `v2/` 隔离、旧库只读快照和原始导出、统一投影 | history、projection、store、应用 config/host |
| 2：进度和恢复（P1/P2/P7） | 非终结进度、版本/代次/幂等校验、契约修订保留进度、错误分类、移除角色限制 | progress、failure、events、executor、recovery |
| 3：用量（P6） | 默认无 cap，仅案件级政策；发送前原子记账；初始与变更政策事件；恢复不清账 | budget、store/control、execution/provider、coordinator |
| 4：规划和质量（P3/P4/P9/P10） | 显式动作、信息比较、公平派发、批审、依赖先审查、统一完成规则 | planner、information、investigation、review、readiness、quality |
| 5：上下文和分析（P5/P8） | 稳定完成条件 ID、相关材料、反证保留、完整交互整理、回取索引、隔离助手 | context、context_read、telemetry/tools、analysis、Demo analysis |
| 6：入口和验收 | 统一 Host 的 CLI/HTTP/RPC/TUI/daemon/告警；配置和文档；回归及实际容器 | tau_coding/incident、tests/incident、website/content |

### 持续执行

- Incident Harness 使用 `max_turns=None`。移除 Ariadne 的 max_turns、role_timeout_seconds、max_tasks、call_limit、任务额度和报告预留。
- 保留单请求网络超时、工具超时、容器资源/输出限制、租约、用户控制、请求窗口，以及可选案件 token cap、deadline、运行 checkpoint_steps。
- `save_progress` 校验契约版本、进度版本、有效 attempt、执行 token、所有权代次和幂等 ID。
  执行成功状态、artifact 来源及执行游标由运行时核对；保存不结束 attempt。
- `partial` Finding 让任务继续；`blocked` 必须说明外部条件、用户输入或执行障碍。
- 检查点阻止新请求，允许已发请求完成并记账，再保存报告和暂停；最终 JSON 不完整不误判任务失败或完成。
- 进程恢复创建后继 attempt，使用最新进度及之后的执行事实；不补造崩溃前未保存的解释。
- 网络错误最多两次显式重试，以新 request_id 和 retry_of 关联，未知 usage 保守记账。
  截断/无效 JSON 进入有界修复；输入不足先重建，仍超限则给出任务 ID、所需大小与拆分要求。
  本地存储错误/提交未知暂停并按回执核对；普通角色故障不终结其他独立任务。

### 规划、审查、上下文

Planner 区分创建、修订、继续、等待、补充输入、记录进展、暂停、综合和 `defer` 无关分支。
进展不再隐含结束 run；相关进度、证据或任务结果触发重新规划，tracing 不触发。
信息比较使用查询范围、来源版本、覆盖、结果内容和语义进度；重复任务记录不自动算新增信息。

普通暂定解释不逐条发起审查。诊断、重要排除、冲突及采用的未审查依赖必须审查。
同一诊断的独立前提可批量审查，前提完成后才审查诊断。取证/质量队列交替派发，质量内部优先诊断依赖。
修复按问题与依据周期管理，不因重述归零。

`diagnosis_readiness` 供调度、ReportBuilder 和事务提交共同使用：判断须完成审查，证据有效且版本一致，
反证冲突已处理，阻塞任务/缺口/等待已解决。分支处置理由及任务版本必须经过诊断审查。
缺少依赖边不能隐藏已知冲突。报告分别列出依据、阻塞项和非阻塞后续事项。

任务输入保留契约、最新 progress、相关判断闭包、关键反证/冲突/约束及最近完整工具交互。
旧材料按需读取；`context_read` 提供分页索引和指定版本内容，`evidence_read` 提供原始材料。
可选旧历史先移除或变为有来源的索引，必要材料仍放不下则要求拆分，不伪造摘要。

隔离 runner 提供 `load_evidence(id)`、`describe_evidence(id)`：JSON、NDJSON、CSV、TSV、文本；描述输出有界。
只访问本次授权挂载，未知 ID/越界路径失败；没有扩大网络、宿主文件、凭证和 Docker socket 权限。
基础镜像和构建产物见 `examples/incident-demo/analysis/image.lock.json`，产物仅在本机，未发布 registry。

## 存储和接口切换

- 新数据：`TAU_HOME/incidents/<project_key>/v2/cases.sqlite3` 和 `v2/artifacts/`；SQLite schema 6，事件/应用接口 schema 2。
- 旧 DB/artifacts 保留原位置。HistoricalCases 复制 DB/WAL 到私有临时目录后只读打开，防止 SQLite 修改原始 SHM/WAL。
  返回旧 JSON，不用新模型默认值或 reducer 改写旧状态。
- 支持旧案件查看、报告、证据、请求、时间线、事件、带 hash 的导出；拒绝 run/resume 和全部写操作。
  不复制旧案为新案，不将旧报告当当前证据，不参加新自动调度。
- CLI `show --view export`、HTTP/RPC `incident.query(view=export)`、会话 `/incident export` 共享查询。
  导出保留原始事件及执行账本；旧事件的语义重放使用原程序版本，v2 不用新 reducer 解释旧事件。
- 旧限制配置明确报错；新示例默认 token_limit/checkpoint_steps 为 null，并发仍为 2/8。
- 正式 rollout 前停止旧 daemon、确认无写入者并备份 DB/WAL/artifacts。回退使用原程序及原库，旧程序不能打开 v2 库。
  本轮实际产品验收使用独立临时目录，没有迁移或恢复执行此前的真实模型案件。

## 离线验证矩阵

| 范围 | 行为测试 / 实际验证 |
| --- | --- |
| 状态一致性 | test_store*、test_read_versions、test_v2_progress：重复命令、冲突、并发提交、旧执行隔离、暂停竞争、回放和投影重建后 usage 不变 |
| 长期执行 | test_role_response_budget：任务/非任务角色连续 13 次请求，受控时钟 312 秒，跨原 8 轮/300 秒边界 |
| 恢复 | test_v2_progress、test_dispatch_wait、test_budget_recovery：多次保存、partial、契约修订保留、后继 attempt、等待和迟到结果 |
| 检查点 | test_v2_checkpoint：两个已发请求，先结束者触发检查点，另一请求仍完成；进度、Finding 和用量保留 |
| 故障 | test_v2_errors、test_recorded_provider、test_store：显式网络重试/未知 usage、截断、无效 JSON、超限、存储失败和未知提交核对 |
| 预算 | test_budget_recovery、test_v2_limits、test_v2_scheduling：默认不限、政策事件、解除不清账、小请求可运行、两个独立连接竞争 cap |
| 质量 | test_v2_review、test_v2_readiness、test_review_repair、test_reports_memory：暂定解释无风暴、批审、公平派发、分支理由审查、依据变更和交付失效 |
| 上下文 | test_context*、test_v2_context：60 条额外观察后的窗口约束、关键反证、旧材料回取和原文不变、明确拆分需求 |
| 分析 | test_v2_analysis_helpers + 实际容器：五种格式、授权边界、截断、超时、非零退出、OOM、取消和清理、派生证据来源 |
| 入口/历史 | test_v2_history、test_intake_frontends、test_service*、test_workspace、test_session_binding：原始旧形状、DB/WAL/SHM 字节不变、写入拒绝、导出 hash、统一 Host |
| Tau 回归 | 完整 Python 套件覆盖原 provider、Harness、工具历史、CLI/RPC/TUI、会话、扩展等 |

### 命令与环境

WSL2 Ubuntu、Python 3.12.3、uv 0.12.19、Docker Engine 29.6.2。Windows 默认 pytest 临时目录发生权限错误后改用 WSL2，未增加跳过。

```sh
export PATH=/var/tmp/amadeus-stage7-tools/uv-x86_64-unknown-linux-gnu:/usr/local/bin:/usr/bin:/bin
export UV_PROJECT_ENVIRONMENT=/var/tmp/amadeus-stage7-venv
export UV_CACHE_DIR=/var/tmp/amadeus-stage7-cache
uv run pytest tests/incident -q -o cache_dir=/var/tmp/amadeus-v2-pytest-cache --tb=short
uv run pytest -q -o cache_dir=/var/tmp/amadeus-v2-pytest-cache --tb=short
uv run mypy
uv run ruff check .
uv run ruff format --check .
uv build --out-dir /var/tmp/amadeus-v2-dist
uv run python scripts/validate_incident_product.py --output /var/tmp/amadeus-v2-product.json
uv run python scripts/validate_incident_containers.py \
  --image amadeus-analysis@sha256:397f3038f805f3b754bc38170827c76fa22b7880dd483648891183d31f6704e2 \
  --output /var/tmp/amadeus-v2-containers.json
```

镜像构建命令：`DOCKER_CONFIG=/var/tmp/amadeus-v2-docker-config docker build -t amadeus-analysis:v2 examples/incident-demo/analysis`。
独立空 Docker 配置避开本机 WSL 缺失的 credential helper，没有修改用户凭证配置。

最终完整 Python **2144 passed / 3 skipped**（220.12 秒），incident 全组 **130 passed**（72.41 秒）。
之后补齐按任务跨 attempt 汇总 usage，预算/进度/调度受影响范围 **18 passed**；没有清账或新建任务预算。
批审到综合修复再到正式报告的延伸用例单独复测通过，并已包含在最终完整套件中。
mypy **181 文件通过**，全仓库 Ruff lint/format（304 文件）通过，sdist/wheel 构建及包内容检查通过；实际容器 **8 项通过**；
真实 daemon/远程 CLI/告警产品验收通过，模型请求 **0**。
锁文件 SHA256：`e7d5af5e90db24e03836a5921771a515132cbe5ae7f1cd38bac51c267f484bfc`，与本轮开始时一致。
3 项跳过均为已有平台条件：macOS 大小写 1 项，Windows/PowerShell updater 2 项；没有新增跳过。

## 修复、提交与限制

测试发现并修正：网络异常绕过重试；进度 schema 前向引用；并发检查点过早取消 sibling；契约修订覆盖进度；
处置引用遗漏；读取清单重复存储；初始限额缺少政策事件；重复任务记录误当进展；旧 bind 回包不一致。
最终审计补上前提审查升级版本后的综合修复调度，避免一直等待；批审用例已延伸到最终报告提交。
整理字符串时引入的租约 SQL 拼接错误已修正并通过全量复测。

工作区开始前已有大量未提交修改和未跟踪的 Stage 1–6 实现。本轮没有创建 Git 提交，避免把原有实现
误记为本轮新增；不虚构修复提交号。交付在当前工作树，原 Stage 历史不重编号。
检查时 HEAD 为 `a194b5c`；它是此前 provider 回归测试修复，不是本轮统一改造提交。

离线验证证明协议、恢复、调度和入口行为，不证明真实模型会形成有效诊断。Stage 7 的固定模型完整场景
仍待验证；未将任何 Stage 或总索引改为“验证通过”。
