# Tau dev notes (contributor build-log)

These are the internal, phase-by-phase build journals and design records for Tau.
They are **not** published on the docs site — they live here for contributors who
want to trace how the system was assembled.

User-facing documentation lives in `website/content/` and is published at
<https://twotimespi.dev/>.

## Contents

- [Tau 架构与二次开发速读（中文）](quickstart-architecture-zh.md) — a concise source-based guide to the package boundaries, request flow, persistence, and extension points.
- [Amadeus 架构与改进事项地图](architecture/incident-agent-architecture-map.md) — Tau 基础与新增机制、两层循环、职责区域及长期调查问题归属。
- [Amadeus 持续调查 v2](architecture/incident-agent-v2.md) — 统一状态、持续执行、新旧存储隔离的实现与离线验收记录；Stage 7 真实模型验收仍待验证。
- [Amadeus 状态归属与简化设计草案](design/incident-agent-state-ownership.md) — 改造前审计与决策来源，现行语义以 v2 记录为准。
- `design/` — high-level design docs written alongside the build:
  - [Incident Agent 目标设计](design/incident-agent.md) and [总实现计划与 Stage 索引](design/incident-agent-implementation-plan.md) — the complete incident-investigation design and its staged implementation and final verification plan.
  - [Amadeus Stage 1 实现记录](architecture/incident-agent-stage-1.md) — 人工案件、SQLite 提交、证据、执行记录与 CLI；实现完成，待统一验证。
  - [Amadeus Stage 2 实现记录](architecture/incident-agent-stage-2.md) — 规划、工具调查、Finding 提交、请求上下文与预算、CLI 闭环；实现完成，待统一验证。
  - [Amadeus Stage 3 实现记录](architecture/incident-agent-stage-3.md) — 所有权与代次、有界并行、读取复验、预算账本、等待和恢复；实现完成，待统一验证。
  - [Amadeus Stage 4 实现记录](architecture/incident-agent-stage-4.md) — 重要结论审查、类型依赖、局部修复周期、版本化诊断与历史记忆；实现完成，待统一验证。
  - [Amadeus Stage 5 实现记录](architecture/incident-agent-stage-5.md) — 固定 Demo、真实遥测、拓扑/变更、导出回放、隔离 Python 和 OTel 接线；实现完成，待统一验证。
  - [Amadeus Stage 6 实现记录](architecture/incident-agent-stage-6.md) — 案件工作区、统一 Host、常驻服务、CLI/RPC、告警与交接；实现完成，待统一验证。
  - `00-roadmap.md` — phased roadmap
  - `01-architecture.md` — the three-layer split
  - `02-agent-loop.md` — agent loop responsibilities
  - `03-tools.md` — built-in tool design
  - `04-sessions.md` — session tree / persistence design
  - `05-core-types-and-events.md` — provider-neutral types and events
  - `project-trust.md` — researched, implementation-ready project-trust design
    (design only; enforcement is not implemented)
  - `agent-loop.md`, `harness.md` — harness/loop reference notes
- `architecture/` — per-phase implementation notes (`phase-1` … `phase-24`, plus
  hardening and feature notes). Each answers: what was added, why it exists, how
  later phases use it.
- `adr/` — architecture decision records.
- `catalog-model-safety.md` — checklist for adding providers and models to the built-in catalog safely.
- `google-stream-completion.md` — why native Google streams require an explicit
  `finishReason` before Tau reports successful completion.
- `startup-thinking-level-fallback.md` — why startup resolves a valid thinking
  level per model instead of assuming the global `medium` default.
- `models-dev-catalog.md` — Pi-compatible build-time models.dev catalog
  generation, offline fallback, and snapshot refresh workflow.
- `llama-cpp-phase-5.md` — built-in llama.cpp connection, safe state, `/local`,
  failure handling, and Phase 5 validation.
- `architecture/phase-6-local-inference-hardening.md` — second-backend contract
  validation, lifecycle hardening, migration, and security decisions.

Amadeus work is tracked in this repository's
[issues](https://github.com/Meteorite258/Ariadne/issues); the reusable Tau core
still follows Tau's upstream
[GitHub issue #1](https://github.com/huggingface/tau/issues/1).
