---
title: Incident investigation
description: Open a case, investigate replay telemetry, and inspect evidence and unverified progress.
---

Ariadne Stages 1–6 are **implemented, pending unified verification**. Stage 7 has
run deterministic tests, full Python tests in WSL2, actual container and telemetry
checks, and selected CLI and service operations. A fixed-model investigation has
not yet produced a complete, independently reviewed diagnosis.

Manual commands record input without calling a model. `run` uses the configured
model to plan and investigate live telemetry or a queryable replay dataset. Cases retain observations,
candidate explanations, and execution constraints separately. Reports summarize
committed input, reviewed interpretations and unresolved questions. Review acceptance
does not prove a root cause. Business recovery is recorded separately using observations.

## Open a case and add input

Use the same project directory and explicit environment for every command. Put
incident flags **after** the incident action. `--project` defaults
to the current directory, or Tau's `--cwd` when supplied before `incident`.

```sh
tau incident new "Checkout requests are timing out" --environment production --impact "Purchases affected" --entity checkout --command-id checkout-open-1
tau incident observe CASE_ID "Timeouts observed at 10:05 UTC" --environment production --command-id checkout-observation-1
tau incident observe CASE_ID "The payment endpoint may have changed" --kind explanation --environment production
tau incident observe CASE_ID "Only inspect the checkout deployment" --kind constraint --environment production
```

Replace `CASE_ID` with the returned case ID. Write commands print their command
and case IDs to stderr before submission and a JSON receipt to stdout. A command
ID is generated if omitted. Keep it to reconcile an interrupted response.

`new` accepts `--impact`, repeated `--entity`, `--start`, `--end`, `--actor`,
and `--source-ref`. Times use ISO 8601 with an explicit timezone. Missing incident
times/entities remain unspecified. Observation scope defaults to the case's scope;
`--entity`, `--start`, and `--end` can describe a different observed scope in the
same environment. Requested scope does not establish actual coverage.

`observe` defaults to `--kind observation`. Its summary can accompany
`--raw-file PATH` containing UTF-8 source text. The raw bytes, including line
endings and surrounding whitespace, are preserved through UTF-8 decoding and
encoding. `--result` accepts `complete`, `partial`, `no_match`, `failed`, or
`unknown` (the default). These describe the submitted observation, not a query
executed by Tau. For example, registering a human report of a failed query is a
successful registration with an observation result of `failed`.

For full provenance, use `--observation-json PATH` with an `ObservationInput`
object. Required fields are `summary`, `raw_text`, `scope` (including
`environment`), and `source` (`kind: "human"`, `actor`, optional `reference`).
Optional fields are:

- `actual_query`: an object containing the actual query or collection parameters.
- `data_time`: `{ "start": ..., "end": ... }`; `available_at` and `collected_at`:
  timezone-aware timestamps. If omitted, collection time is the registration time;
  data and availability times remain unknown.
- `actual_coverage`: a scope with `environment`, `entities`, and `time_window`,
  or `null` for unknown coverage (default).
- `result`, `units`, `sampled`, `truncated`, and `completeness_note`.
- `method`, `baseline`, and `inputs` for derived observations. Each input reference
  specifies `case_id`, `kind`, `object_id`, and `version`; references must exist at
  that exact current version in the same case.

JSON input replaces positional text and observation metadata flags. It does not
accept caller-assigned evidence IDs, artifact paths, operation IDs, or attempt IDs.
The application generates those identities. Candidate explanations stay
unverified. Constraints are included in planning and worker context. Their free-form
meaning is interpreted by the model; runtime enforcement uses explicit task scope,
tool permissions and request budgets.

## Run an investigation

`run` requires a new-format case and configured live telemetry or a replay dataset.
Configure a Tau provider first. The v2 refactor is verified with fake providers;
the fixed-model complete-diagnosis acceptance remains pending.

```sh
tau incident new "Checkout requests are timing out" --environment demo --entity checkout --start 2026-09-26T00:00:00Z --end 2026-09-26T01:00:00Z
tau --provider PROVIDER --model MODEL incident run CASE_ID --environment demo --fixture examples/incident/replay.json --fixture-now 2026-09-26T00:10:00Z --checkpoint-steps 12
tau incident resume CASE_ID --environment demo --fixture examples/incident/replay.json --format json
```

The runtime acquires a case lease, recovers expired executions, obtains structured
plans, and dispatches ready tasks within the concurrency and budget limits. Each
attempt uses an independent worker and provider session. Workers register evidence
and submit structured Findings through the same transaction and receipt protocol.
Workers receive only
`telemetry_query`, `telemetry_capabilities`, `service_catalog`, and `evidence_read`
as permitted by their task. They do not load coding tools, project instructions,
extensions, main-session history or CodingSession compaction.

Replay rows are filtered by signal, environment, entities, inclusive event-time
range, optional substring, availability time and pagination. Signals include
metrics, logs, business traces, deployments and configuration. The product format
and sample are in `examples/incident/`; they contain no grading answers. Actual
coverage remains unknown unless the source explicitly establishes it. No matches,
sampled or paged data, delayed data and query failures retain their distinct meaning.
`--fixture-now` uses a fixed timezone-aware replay availability time. Runtime records
use the application clock independently.

| Option | Default | Meaning |
| --- | --- | --- |
| `--fixture PATH` | one source required | UTF-8 `ReplayData` JSON |
| `--services-config PATH` | alternative to `--fixture` | live/fixture JSON, optional analysis and OTLP export |
| `--fixture-now TIME` | application time | Replay availability clock |
| `--provider NAME`, `--model NAME` | Tau settings | Model selection; top-level equivalents also work |
| `--concurrency` | 2 | Case concurrency, including standalone planning roles |
| `--global-concurrency` | 8 | Shared capacity across active cases in this project's database |
| `--deadline TIME` | none | Initial case budget deadline, with timezone |
| `--format-repairs` | 2 | Additional structured-output correction passes |
| `--max-repair-rounds` | 2 | Local repairs per issue and evidence cycle |
| `--checkpoint-steps` | none | Optional model-request count for this `run`/`resume`; save progress and pause when reached |
| `--token-limit` | none | Optional case cumulative estimated/charged token envelope |
| `--context-tokens` | 32768 | Requested input plus output window, clamped to model metadata |
| `--output-tokens` | 4096 | Output reservation, clamped to model metadata |
| `--format` | markdown | `markdown` or `json` |

Dispatch reserves concurrency capacity. Each model request records its input
estimate plus output allowance atomically before sending. An optional case cap
checks this amount; there is no task quota or reporting reserve. Unknown usage
retains its estimate. A checkpoint counts planning, worker, review, retry and format
repair requests in this invocation. It prevents further requests, drains requests
already sent, saves progress and pauses the case. An incomplete final JSON at a
checkpoint does not fail or complete a task. `resume` starts a fresh checkpoint
interval without clearing cumulative usage. Token estimates are approximate, and
actual reported usage may exceed the reservation. Supported
transports receive the output cap; the Codex transport currently records this as
an allowance only because its adapter has no output-cap setting. Adapter retries
are disabled for investigation calls. The incident runtime performs at most two
explicit transport retries, with separate linked request IDs and usage records.
There is no monetary hard-limit enforcement.

Run returns a stop reason, accepted task count, cumulative usage, progress and a
versioned report. It saves a paused state on exit unless a reviewed diagnosis is
complete. Use `resume` to continue the same Case;
`run` does not implicitly unpause it. Interrupted executions from an expired lease
are reconciled with receipts, responses and evidence; reruns get new attempt IDs.
The old execution cannot complete a replacement task. Partial findings make a task
ready to continue. Failures record a concrete execution error and preserve accepted
progress; other ready work can continue. A blocked finding must name an external
condition, user input or execution obstacle.

Finding acceptance checks identity, authorized versioned references, scope and
declared completion conditions. It does not prove that a natural-language condition
or causal interpretation is true. Completion conditions have runtime-assigned IDs.
Tentative interpretations support further investigation without immediately opening
review tasks. Diagnoses, important exclusions, contradictions and adopted causal
dependencies require review. Related independent judgments can share a review task.
Exploration and quality work alternate when both are ready. Review and repair use
the same task dispatch, tools, request accounting,
ownership and command receipts. Accepted judgments become `current` but remain
`unverified`. A `diagnose` task synthesizes a diagnosis claim, which also requires
review before a complete report can be committed.

Workers keep their dispatch context. Request selections, dynamic evidence reads and
Finding references are rechecked at submission. Unrelated case updates can merge;
changed premises or catalog revisions preserve the Finding for review and withhold
task completion. Evidence from an already started, cancelled query can be retained
with its original provenance. Using it in a new interpretation requires a new
authorized read and submission.

Workers can call `save_progress` repeatedly without ending their attempt. Task progress
holds a versioned summary, evidence/claim references, open questions, next actions and
recorded operation/output references. Runtime validates execution facts. `SubmitFinding`
freezes a formal result. Context rebuilding keeps the attempt active; process recovery
creates a successor attempt using progress and execution facts recorded after it.
`context_read` retrieves domain records or a paginated index; `evidence_read` retrieves
original bytes. Raw evidence is not copied into progress.

One diagnosis policy is shared by report building and transactional submission. It
requires reviewed dependencies, valid evidence, handled counterevidence and resolved
blocking work. Planner `defer` records why a stopped branch is unrelated follow-up;
the diagnosis reviewer must assess that reason. Reports list these follow-ups separately.
New relevant evidence can invalidate a report and reopen investigation.

## Pause, wait, cancel and continue

```sh
tau incident pause CASE_ID --environment demo --command-id pause-1
tau incident resume CASE_ID --environment demo --fixture examples/incident/replay.json
tau incident cancel CASE_ID --environment demo --task-id TASK_ID --command-id cancel-1
tau incident retry CASE_ID --environment demo --task-id TASK_ID --reason "Retry after source became available"
tau incident budget CASE_ID --environment demo --token-limit 200000 --command-id budget-1
tau incident budget CASE_ID --environment demo --add-tokens 50000 --command-id budget-2
tau incident budget CASE_ID --environment demo --clear-token-limit --command-id budget-3
```

`cancel` without `--task-id` cancels active/queued investigation work. `retry`
explicitly queues a stopped task; `resume` then dispatches a new attempt. Original
attempts and cumulative task costs remain recorded. Budget changes are events;
`budget --deadline TIME` extends the case deadline. Set, increase or clear an optional
case token cap explicitly; none of these operations clears accounting. Later run
options do not reset the established policy. Control commands accept `--reason` and
`--command-id`. Checkpoints retain authorized tools in the last permitted request.
A tool call absent from that request's offered tool list is blocked before execution.

Only one live coordinator owns a Case. Standalone writes, including pause/cancel,
return an ownership conflict while another coordinator is active. In the process
running the CLI, Ctrl+C pauses it; embedded clients can call that Runtime's controls.
Cross-process control connections are a later stage.

The planner may persist time, evidence or source-watermark waits, including a wait
on a newly planned task. Other ready tasks keep running. The Case waits only after
in-flight and dependency-ready work is exhausted. Checks are bounded and use no
model calls. Each condition includes scope, next check, deadline, interval, source
and an explicit resume/cancel/pause timeout action. Fixture watermarks check the
named replay source and signal against the requested timestamp and availability.
`--fixture-now` freezes replay availability, so advancing real time will not make
later fixture rows available with that option set.

Wait conditions survive pause/exit; overdue conditions are checked on resume.
Checks run only while the host is running. The host defines embedded and daemon
lifecycle modes, but this stage does not install or launch a background service.
Daemon frontend detach preserves host ownership; embedded detach pauses it.
Application shutdown cancels work and drains within a bound; incomplete cleanup
is explicit and old execution identities remain fenced.

## Read progress and provenance

```sh
tau incident show CASE_ID --environment production
tau incident show CASE_ID --environment production --view case
tau incident show CASE_ID --environment production --view evidence --evidence-id EVIDENCE_ID
tau incident report CASE_ID --environment production
tau incident report CASE_ID --environment production --format json
```

`show` defaults to a JSON CaseBrief containing the full case and unresolved items.
`--view case` returns the committed aggregate. Evidence retrieval checks that the
ID belongs to the case and verifies the artifact hash and size. `report` produces
Markdown by default. When a persisted report exists, it returns the latest revision
and its validity state; otherwise it returns a deterministic progress view.
Redirect stdout using your shell to export these views.

## Review, repair, versioned reports and historical memory

Review issues identify a target revision, concrete gap, impact and required action.
Judgment dependencies distinguish evidence support, evidence opposition and derivation
from another judgment. Task rationale is a separate relation. Changing a premise
marks dependent interpretations for review; it does not delete independent observations.
Scope overlap identifies candidates, not proof that they are invalid. Reviewers inspect
remaining independent support before recommending narrower claims, more evidence or replacement.

Each issue has a stable identity, evidence cycle and repair count. The default is
two repairs, including repairs whose output revises the target claim. No progress,
repeated failure, the round limit or insufficient budget leaves an unresolved issue.
New relevant evidence or an explicit new check can open another cycle. All calls
share the same case accounting. An optional `--checkpoint-steps` pauses investigation,
review and synthesis at a resumable checkpoint. Normal work has no cumulative call
ceiling, role duration limit or maximum task count.

```sh
tau incident report CASE_ID --environment demo --commit
tau incident report CASE_ID --environment demo --version 1 --format json
tau incident memory CASE_ID --environment demo
tau incident memory CASE_ID --environment demo --cross-environment
tau incident memory CASE_ID --environment demo --rebuild
tau incident recheck CASE_ID --environment demo --review-id REVIEW_ID --new-check "Compare the corrected deployment window" --reason "New check supplied"
tau incident evidence-applicability CASE_ID --environment demo --evidence-id EVIDENCE_ID --applicable no --reason "Wrong collection window"
tau incident withdraw-report CASE_ID --environment demo --version 2 --reason "Source interpretation withdrawn"
tau incident reopen CASE_ID --environment demo --reason "New incident information"
tau incident impact CASE_ID --environment demo --status recovered --evidence-id EVIDENCE_ID --reason "Recovery observation supplied"
tau incident revise-task CASE_ID --environment demo --task-json revised-task.json --reason "Narrow the investigation scope"
```

These are interface examples, not executed validation. `report --commit` builds a
version from committed judgments without an extra model call. A complete diagnosis
requires a reviewed diagnosis claim, resolved issues and no outstanding tasks or waits.
Otherwise the report is partial. Reports include supporting/opposing references,
propagation edges, alternative explanations, gaps, recommendations and verification
sources. Model-generated judgments remain `unverified`; review approval is not an
environment verification source. Recovery requires a separate `impact` command with
applicable, complete observation references and never follows from report completion.

Reports and cards preserve supersession references. Relevant changes mark derived
content stale in the same case transaction; rebuilding occurs after that transaction.
`report --commit` also rebuilds the card. `memory --rebuild` requires a current report.
Report withdrawal withdraws its derived cards. An incomplete investigation produces an
`incomplete` card rather than a diagnosis card. Paths marked effective describe completed
investigation tasks, not proven effective production fixes.

Memory retrieval is isolated by project and environment. `--cross-environment` explicitly
allows other environments within the same project and reports differences. Ranking uses
entities, symptom terms and time context. Retrieval and request persistence recheck
source report/card versions. Historical cards enter requests as optional leads, never
as current Evidence IDs. Snapshots keep `memory_retrieval` and `memory_selection`
separately from current evidence selection. Optional history is dropped before essential
current material when the request budget is tight.

`revise-task` accepts a complete `InvestigationTask` JSON with the next contract version,
unchanged budget, `ready` status and no active attempt; the existing contract revision
path cancels old execution. It supports investigation/diagnosis tasks. Quality tasks are
managed by their issue cycle; use `recheck` after an unresolved stop. Quality control
commands accept `--command-id`, require a reason, and retain a command receipt.

## Events, execution records, and retry receipts

```sh
tau incident show CASE_ID --environment production --view events --after 0 --limit 100
tau incident show CASE_ID --environment production --view executions --operation submission --after 0
tau incident show CASE_ID --environment production --view receipt --command-id checkout-observation-1
```

Events and execution records are JSON pages with a `next_cursor`; pass it as
`--after` to continue. Limits are 1–1000. Execution queries optionally filter by
`--attempt-id` or `--operation manual_command|evidence_registration|submission|planning|investigation|context|model_request|tool|output_validation|dispatch|wait_check|lifecycle|recovery|review|report|memory`.
Stage 1 manual operations have no task attempt. An execution cursor advances on
start, finish and link changes, so a previously returned operation can reappear
with updated metadata; merge by `operation_id`. This is a current-record feed,
not a history of every intermediate span update. Domain events are append-only.

Each command has its own trace with child registration/submission operations.
Records distinguish operation status from result, retain durations and links,
and reference evidence, artifacts, events and receipts. A rejected submission can
have operation status `succeeded`: the check completed, with result `rejected`.
Investigation traces associate attempts, context construction, model requests,
tool calls, output validation and submissions. A tool operation keeps the actual
query artifact, and each accepted observation links to its registration and raw
artifact. Model-call records are created only after context validation, reservation
and request snapshot persistence. These records establish execution provenance;
they do not establish investigation accuracy.

```sh
tau incident show CASE_ID --environment demo --view executions --operation model_request
tau incident show CASE_ID --environment demo --view request --request-id REQUEST_ID
tau incident show CASE_ID --environment demo --view budget
tau incident show CASE_ID --environment demo --view owner
tau incident show CASE_ID --environment demo --view manifest --attempt-id ATTEMPT_ID
```

`request` returns the snapshot, neutral provider input and usage/reservation record.
It contains the system text, messages, tool schemas, model/configuration, selected
versions and omissions. It is not the provider adapter's final wire payload.
`budget` separates settled tokens, request and attempt reservations, unknown usage,
available allowance, reported monetary cost and calls with unknown monetary cost.
`owner` shows the lease and generation. `manifest` includes dispatch, request,
dynamic-read and final-Finding references, including interrupted attempts. Request
snapshots describe prepared input; uncertainty about sending is retained conservatively.
Context projection can summarize raw tool details with evidence provenance and
omit complete older exchanges; it preserves call groups and error status. If the
fixed contract and critical evidence cannot fit, the request fails before any
model call. Raw evidence remains retrievable by its ID.

Reusing a command ID with the same payload and expected versions returns the
original receipt. Different content with the same ID fails. Rejected/conflicting
receipts are also stable: use a new ID when changing input or preconditions.
Creation derives the same case ID from project and command ID on retries.
`observe --expected-version N` adds a case version precondition; without it,
manual input appends to the current case within a serialized transaction.

Exit status is 0 for accepted writes/reads, 2 for a rejected/conflicting receipt,
and 1 for validation, ID reuse, storage or recording errors. A missing receipt
query returns `null`; absence alone does not prove an in-flight command failed.
After an ambiguous commit, the runtime attempts receipt lookup. If the local
execution record cannot be completed, the CLI reports failure and includes any
known receipt: an accepted write may already exist. Reconcile by command ID
before retrying; do not create a new ID merely because the response was lost.

## Storage and current limits

New data lives under `TAU_HOME/incidents/<project_key>/v2/` (`~/.tau` by default):

- `cases.sqlite3` stores schema version 6, cases, events, receipts, object versions,
  evidence metadata, artifact references, execution records, request snapshots
  and the request accounting ledger, leases, role/attempt slots and response records.
  Read manifests are derived from request snapshots and explicit reads. Reports,
  review cycles, applicability revisions and cards live in the versioned case
  projection and accepted event log. Domain projection replay does not recreate
  request usage or execution records. Application and domain event schema is v2.
- `artifacts/<sha256>` stores immutable raw content. Files are flushed and
  published before their references enter SQLite. A failed submission can leave
  an unreferenced file; this stage does not delete or garbage-collect artifacts.

Old databases and artifacts remain in their original directory. A read-only adapter
supports case, reports, evidence, requests, timeline and hash-checked export, retaining
old JSON shapes. It reads a private SQLite snapshot so opening an archive cannot
modify its original WAL/shared-memory files. Old cases cannot run, resume or accept
writes, and are never automatically copied into the new workflow. Before rollout,
stop the old daemon and back up its database, WAL and artifacts. Rollback uses the
old program with that archive; never open the v2 database with an old program.

`tau incident show CASE_ID --environment ENV --view export` returns original JSON
records and referenced artifacts as a base64 file mapping with a SHA256 manifest.
The same export is available through `incident.query` and `/incident export`.

The project key combines a readable directory slug with a hash of its canonical
absolute path. Environments are explicit case fields checked by the host, within
the project's database. Sessions and cases have separate storage; these commands
do not create coding sessions or load skills or extensions. `run` and `resume`
construct separate providers per planning/worker role, using application-owned
configuration and closing them on exit. Shared concurrency is scoped to this
database; the strictest live coordinator setting applies, not a machine-wide limit.

SQLite and artifact files are not one filesystem transaction. Concurrent writers
and interrupted commits have deterministic tests. External deletion, filesystem
corruption and power loss still need separate fault testing. Stop writers before copying the whole
data directory, including any SQLite WAL files and artifacts. Do not edit the
database or remove referenced files manually.

Background services, alert receivers and incident TUI/RPC are implemented in Stage 6.
Real telemetry, replay export, isolated analysis and execution export are implemented.
Review, repair, report and memory behavior have deterministic tests; the fixed
model has not yet completed an independently reviewed diagnosis. Recovery records
the time and basis of reconciliation and leaves unknown original end times empty.
It does not resume a model in the middle of generation. Concurrency, lease races,
interruption and recovery have deterministic tests and a real daemon restart check;
the full fixed-model interruption scenario still awaits validation.

## Live telemetry, datasets and Python analysis

Stage 5 adds the Prometheus range-query API, Jaeger business-span queries and
OpenSearch SSO log queries behind the same worker and evidence interfaces. Choose
`--services-config PATH` on `run` or `resume`. Configuration schema 1 selects
`mode: live` with `live` and `history`, or `mode: fixture` with `fixture` pointing
to replay JSON or an exported dataset directory. File paths resolve relative to
the configuration file. The existing `--fixture` shortcut remains supported.

The configured service allowlist and environment restrict business queries;
Agent traces use a separate service and environment. Capabilities expose the
configured signals, metric names, scope limits and source identity. Queries have
bounded windows, response sizes and row limits. `contains` filters normalized
rows using a literal substring. Unknown retention, sampling, failed queries and
truncation remain explicit; an empty result does not establish health.

Export with `uv run python -m tau_coding.incident.data_cli --services-config PATH
--scope SCOPE_JSON --output NEW_DIRECTORY`. This command queries telemetry and
must only be run when collection is intended. It records source query responses,
hashes, coverage gaps, topology versions and deployment/configuration history.
Fixture queries filter the dataset independently of original call order;
`--fixture-now` controls availability, including late data. Controller-only
fault switches and expected answers are excluded from the export.

`analysis_image` enables `python_analysis` only when it contains a fixed
`repository@sha256:...` digest. Task tool permission and deadlines still apply.
Only selected Evidence IDs are staged read-only; the model cannot choose Docker
flags, host mounts, credentials or image. The default policy is no network,
non-root, read-only root, dropped capabilities, bounded CPU/memory/pids/time and
tmpfs output. The script, image, selected evidence, outputs, exit code and cleanup
status are recorded. Output files become derived evidence with input references.
The analysis image contains Python's standard library; no runtime package install
is exposed. Set `analysis_image` to null to disable it.

Optional `otel` configuration exports local execution metadata using a bounded
OTLP/HTTP JSON queue. IDs, statuses and evidence/artifact references are allowed;
prompts, responses, arbitrary error text and credentials stay out of spans.
Export gaps are appended to `otel-gaps.jsonl` in the incident data directory.
Export does not change Case state or settle usage again.

The repository's `examples/incident-demo/README.md` documents the pinned Demo,
WSL2 deployment parameters, actual deployment versus flag controls, export and
replay format, image digest preparation and resource limits. No containers were
built or started and no live/replay investigation or tests were run in Stage 5.

## Stage 6：案件入口与后台服务

Stage 6 **实现完成，待统一验证**。下面是已实现接口的使用说明。入口集成测试、部分真实 CLI
操作及真实案件的只读 TUI 工作区检查已经执行；固定模型的告警启动闭环及完整产品操作
验收仍待完成。

`examples/incident-demo/host.json` 提供 Host 配置。两个凭证通过环境变量配置：
`AMADEUS_INCIDENT_TOKEN` 用于用户操作，`AMADEUS_ALERT_TOKEN` 用于 webhook；
每个值至少 24 字符。配置文件只记录变量名。默认监听 127.0.0.1:8765，自动调查 allowlist 为空。

```sh
tau incident serve --config examples/incident-demo/host.json
tau incident connect --config examples/incident-demo/host.json
tau incident query --config examples/incident-demo/host.json --remote --case-id CASE_ID
tau incident timeline --config examples/incident-demo/host.json --remote --case-id CASE_ID --after 0
tau incident handoff --config examples/incident-demo/host.json --remote --case-id CASE_ID --output handoff.json
```

`serve` 前台运行常驻进程，部署时由终端或进程管理器维持。`connect` 查询服务能力；
后续命令可使用 `--remote`。客户端退出不停止服务拥有的 Runtime。嵌入模式退出会暂停并收尾。
不要把运行 `serve` 的终端退出与关闭一个连接客户端混为一谈。

`dispatch --json-file action.json` 使用统一动作：

```json
{"request_id":"run-checkout-1","operation":"run","case_id":"CASE_ID","limits":{"checkpoint_steps":8,"token_limit":200000}}
```

`resume` 使用新的稳定 request_id；重试相同请求保留原 ID 和完整内容。
动作 `command` 携带领域 Command，并要求 command_id 等于 request_id；支持创建、三种人工输入、
暂停/取消、预算、重试和质量控制。`expected_versions` 可携带 Case 版本。
`query --json-file query.json` 支持 case/brief/events/timeline/evidence/request/receipt/budget/
report/handoff/provenance/action；后四类需要引用时使用 `reference` 字段。
事件与时间线 cursor 独立，按返回的 next_cursor 继续；时间线按 operation_id 合并变化。

### TUI 与 Session

启动 Tau 前设置 `AMADEUS_INCIDENT_CONFIG` 为 Host 配置路径，
`AMADEUS_INCIDENT_MODE=connect`（默认）或 `embedded`。

- `/incident new 症状描述`：开案并绑定当前聊天。
- `/incident bind CASE_ID`、`/incident workspace`：绑定和打开案件工作区。
- `/incident run`、`pause`、`resume`、`cancel`：控制调查。
- `/incident observe 文本`、`explain 文本`、`constraint 文本`：记录观察、候选解释和约束。
- `/incident budget 10 50000`：增加调用/token 预算；不清零历史消耗。
- `/incident report`、`handoff`、`timeline`：查看报告、交接和执行。
- `/incident evidence ID`、`request ID`、`receipt COMMAND_ID`：回取原始材料。
- `/incident coding`：解除案件绑定并恢复编码输入。

案件模式普通聊天作为人工观察记录，不自动当成证据支持的结论。调查方向用解释或约束表达，
然后运行/继续。工作区提供状态、解释、证据、任务、等待、预算和报告，以及可展开和过滤的
时间线；References 可从报告/判断追溯提交及请求。Details 页完整展示查询结果。
Overview 同时显示执行导出缺口；定时刷新保留时间线已展开的操作。工作区内切换案件会重置
该视图的执行游标，避免混入前一案件记录。
新建/切换/fork/清空/压缩聊天只改变交互与绑定，不回滚 Case。恢复绑定读取案件当前状态。

### 告警与交接

```sh
tau incident import-alert --config examples/incident-demo/host.json --remote --json-file alert.json
tau incident inbox --config examples/incident-demo/host.json --remote
tau incident associate --config examples/incident-demo/host.json --remote --update-id UPDATE_ID --case-id CASE_ID
```

Alertmanager v4 webhook 地址为 `/webhook/alertmanager`，需 Bearer 接收凭证；
每条告警必须具有 environment、service 和 alertname 标签。原始投递写入 inbox 后才确认。
同一投递 ID 不同内容拒绝；投递 ID 缺省时用载荷 hash。分组 ID 不作为案件 ID，
fingerprint 和开始时间区分更新与复发。外部 incident_id 优先关联；范围匹配有歧义时保留供人工处理。
resolved 只更新告警记录，不自动宣告业务恢复或结束诊断。

### 验证配置与预算恢复

LongCat 示例位于 `examples/incident/longcat/`。将 `catalog.toml` 和 `providers.json`
放进独立的 `TAU_HOME`，通过环境变量 `LONGCAT_API_KEY` 提供凭证。示例固定
`LongCat-2.5-Preview`、关闭 thinking、请求超时 180 秒、自动重试 0 次；调查命令仍需
显式提供 token、调用次数、输出长度和截止时间。这是正在验收的配置，完整调查尚未通过。

预算停止后先查看案件与任务的累计消耗。`budget` 增加案件额度，不会清除历史消耗，
也不会增加原任务额度；`retry` 同样保留已消耗的任务预算。原任务额度耗尽时，
继续重复 retry 不能恢复执行，应让 Planner 在案件剩余额度内选择后续工作。

自动调查同时检查环境、服务和级别 allowlist，受案件并发、worker 并发、队列、调用和 token
预算约束。关联完成但尚未排队的输入持久保留，背压解除后可继续派发。
交接 JSON 保存 Case、事件、回执、执行、请求引用、预算及导出缺口；原始 artifact 继续通过受控
证据/请求 ID 回取。报告仍保留验证状态，静态检查不证明诊断正确。

