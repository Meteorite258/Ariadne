# Ariadne Stage 1 实现记录

状态：**实现完成，待统一验证**。日期：2026-09-26。

实现位于当前工作区，未创建 Git 提交；保留了进入任务前已有的设计文档改动。
对应[阶段计划](../design/incident-agent-stage-1.md)与[总索引](../design/incident-agent-implementation-plan.md)。

## 接通的流程与分层

`tau incident` 沿用原 `tau_coding.cli` 的 positional 参数分派。案件参数仅在
`incident` 分支中解析；原 print-mode/RPC 分派条件保留。此分支不创建 provider、
CodingSession、Harness、TUI 或扩展，也不调用模型。

`tau_coding.incident` 负责配置、项目/环境检查、路径与 Host 生命周期；
`tau_incident` 负责人工 Command、领域状态、SQLite、artifact 和执行记录。
领域层只复用 `tau_agent.types.JSONValue`，不导入应用层或供应商实现。
这是 Pi 的“可复用核心 / 应用环境 / 前端”分工在新增领域层的延续；
没有为此修改 Tau Harness 的行为。

| 模块 | 实际实现 |
| --- | --- |
| `models.py` | 严格 Pydantic 类型、显式 scope/source、带时区时间窗、版本引用；Case、Observation、候选解释、约束及后续阶段所需 Claim/Hypothesis/Task/Attempt/Finding/Review/Wait/Request/Report/Memory/Execution 类型 |
| `events.py` | 四种人工命令、四种已接受事件、Receipt、规范 JSON 内容 hash、纯函数 `reduce_case` |
| `store/` | schema v1 迁移、WAL/FULL、短写事务、投影/事件/回执、证据和对象版本表、分页查询、执行记录独立表 |
| `evidence/` | SHA-256 artifact、临时文件/flush/fsync/原子发布、ID 受控回取和内容校验；EvidenceRecorder 准备来源，统一 commit 登记 |
| `execution/` | start/finish/link；trace/span、父子与重试关联；墙钟时间和单调时钟耗时；独立更新游标 |
| `coordinator.py` | `execute/get_case/events`、提交前查询幂等回执、人工/登记/提交记录、失败与未知结果处理 |
| `reporting/` | 只从已提交 Case 构造 CaseBrief 和确定性进展报告；不生成根因或恢复判断 |
| `tau_coding/incident/` | IncidentConfig、IncidentHost、CLI；读写接口和生命周期；项目与环境检查 |
| `paths.py`、`pyproject.toml` | 应用生成项目数据目录，新增 wheel 包、mypy 范围与 `py.typed` |

后续领域类型在本期只有 schema，无写命令、调度、预算结算、所有权接管或诊断逻辑。
Manual Case 维持 `open`，影响状态维持 `unknown`。报告是版本化输入的只读视图；
持久诊断报告修订留给后续 Stage。

## 一次写入如何完成

1. Host 根据显式项目、环境与 TauPaths 组装 Runtime。新案 ID 由项目与稳定
   command ID 确定；命令 ID 在 CLI 提交前输出，便于响应丢失后核对。
2. Runtime 先持久化人工操作和提交操作身份，查询同 ID 回执。相同内容返回
   原回执并写 `duplicate_of` link，不重复提交领域事件。
3. 对新观察，建立登记操作，原文保存为 artifact。EvidenceRecorder 生成
   Observation，来源操作由 Runtime 提供；实际覆盖默认未知，不从请求范围推断。
   人工输入 `raw_text` 不去除空白，UTF-8 文件不转换换行。
4. CaseStore 在事务外校验 artifact。`BEGIN IMMEDIATE` 后再次检查幂等、
   操作身份、版本、环境、原文 hash、准备好的观察元数据与引用。
5. 在同一事务内执行纯 Reducer，更新 Case/对象版本，登记证据和 artifact 引用，
   追加事件，写入回执。拒绝/版本冲突仅保存回执，不产生已接受事件。
   外键与唯一约束约束证据引用、操作引用、命令身份和每案事件版本。
6. 提交后完成执行记录，引用实际 Receipt/Event/Evidence/Artifact。登记、
   提交、人工操作各有独立操作 ID。人工流程不虚构 TaskAttempt。

`CaseStore.commit` 还要求由 Runtime 提供 `source_operation_id`，观察命令另带
准备好的 Observation。ArtifactStore/EvidenceRecorder 不提供绕过 commit 修改
案件的接口。Store 执行记录接口只能修改独立执行表。

## 幂等、版本和错误语义

- 内容 hash 包含 Command 与显式 expected_versions，不包含新生成的操作 ID、
  证据 ID、采集默认时间。拒绝回执同样占用 command ID；改变内容/前提需新 ID。
- `VersionRef` 为 `(case_id, kind, object_id, version)`，用于 Case、证据、
  人工输入，以及后续 Task/Budget/Report 等对象。后续种类目前没有投影或写入口，
  引用未登记对象会拒绝，不会默认为有效。
- 未指定 expected_versions 的人工追加在事务中读取当前状态并加一；提供
  `--expected-version` 则做乐观版本校验。Receipt 分别保留期望版本和实际
  读到的版本。派生观察的 inputs 也必须是同案当前合法版本。
- 进程竞争时 commit 内二次幂等检查仍返回原回执，后来准备的观察不会登记。
- SQLite COMMIT 异常保留未知语义并尝试查回执。记录完成失败抛出
  `LocalRecordingError`，携带已知回执；CLI 返回非零并展示回执，不误报全流程成功。
  已提交 Case 不因执行记录失败而再次写入。
- 受控取消记录 cancelled；进程硬中断可能留下 running 记录。本期没有自动
  接管、补写或恢复 sweep，后续 Stage 3 负责结合权威记录处理。
- 同一个操作的 start/finish/link 都推进 execution cursor；查询返回当前记录，
  客户端按 operation_id 合并。领域事件 cursor 则表示不可变的提交次序。
- 文件系统与 SQLite 不是一个事务。失败可留下孤立文件，本期不做清理；
  数据库只接受已经完整发布并校验的引用。外部删除或损坏回取时报错。

## 入口与文档

`tau incident new/show/observe/report` 已连接 Host。`observe --kind` 区分
观察、候选解释、约束；完整观察元数据可用 `--observation-json` 提交。
`show --view` 支持 brief/case/events/executions/evidence/receipt；结构化页面
带 next_cursor，执行记录可按 attempt 与操作类型筛选。report 支持 Markdown/JSON。
JSON stdout 可由 shell 导出；本期没有导出服务。

默认目录为 `TAU_HOME/incidents/<project_key>/`，内含 `cases.sqlite3` 与 artifacts。
项目 key 由 canonical 绝对目录和 slug/hash 生成，Windows 路径大小写标准化。
案件 scope 存环境；Host 检查项目/环境，SQLite 按项目保存，未实现多租户鉴权。
Session 和 Case 相互独立。

用户接口详见 [website 指南](../../website/content/guides/incidents.md)；CLI 与
configuration reference 已同步。指南中的命令是接口说明，本期没有执行它们。

## 检查记录

环境：Windows、CPython 3.12.14，项目 `.venv`，现有 `uv.lock` 未修改。
初始 PATH 无 uv，WSL 也未发现 uv；从官方发行包将 uv 准备在临时目录
`%TEMP%/amadeus-stage1-tools/`，通过该目录的 `uv.exe run` 使用项目环境。
未修改系统 Python 或 PATH。依赖准备产生 editable package 构建，不是打包行为验收。

以下命令中的 `uv` 指上述绝对路径；检查范围包括新增模块及修改的两个应用文件：

```text
uv run ruff check src/tau_incident src/tau_coding/incident src/tau_coding/cli.py src/tau_coding/paths.py
uv run ruff format --check src/tau_incident src/tau_coding/incident src/tau_coding/cli.py src/tau_coding/paths.py
uv run mypy -p tau_incident -p tau_coding.incident -m tau_coding.paths -m tau_coding.cli
uv run python -m compileall -q src/tau_incident src/tau_coding/incident src/tau_coding/cli.py src/tau_coding/paths.py
uv run python -c "import tau_incident.models; import tau_incident.events; import tau_incident.store; import tau_incident.evidence; import tau_incident.execution; import tau_incident.coordinator; import tau_incident.reporting; import tau_coding.incident.config; import tau_coding.incident.host; import tau_coding.incident.cli; import tau_coding.cli; import tau_coding.paths"
```

结果：上述最终检查全部通过，mypy 检查 14 个源文件。检查中修正了格式、
类型注解、argparse Never 返回类型以及缺少的 py.typed。纯导入不实例化 Host、
不创建案件库、不解析命令、不启动调查。

**没有编写或执行任何单元、回归、集成或端到端测试，也没有行为演练。**
静态检查不能证明 SQL、事务、并发、CLI 参数传递或恢复行为正确。

## 留给 Stage 7 的关注项

- V1：四种命令的事务结果、拒绝回执、ID 内容冲突、并发重复、版本检查、
  事件重放等价、未知 schema 拒绝；提交前后失败不得出现已提交悬空引用。
- V1：原始 UTF-8 空白/CRLF/hash/大小保真；同内容并发发布、未知覆盖、
  空结果与失败的区别、引用同案/版本限制；外部损坏、路径越界与符号链接。
- V8：父子/links、回执/事件/证据关联、操作更新游标分页、重复回执的关联、
  COMMIT 未知、记录失败、软取消、硬中断残留，未知不能被误记成功。
- V11：Windows 与 POSIX 路径、项目隔离/环境校验、TAU_HOME、wheel 包内容、
  原 CLI positional/print/RPC 语义、incident help/参数位置/退出码、JSON 导出与回取。
- 完整人工开案 → 多类补充 → 统一提交 → 进展/执行/回执查询的贯通验收；
  不调用模型、不把人工候选解释或无匹配结果当成根因结论。

限制：本期为单机同步人工操作；不执行后续调查任务，不结案，不自动恢复，
不调用外部遥测或模型，不提供常驻运行。SQLite 和文件系统持久性边界需要
在最终验证环境中实际检查。
