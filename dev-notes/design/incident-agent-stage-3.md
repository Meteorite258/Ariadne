# Stage 3：并行、等待、预算与恢复

状态：**实现完成，待统一验证**。前置依赖：[Stage 2](incident-agent-stage-2.md)实现完成。

[总计划](incident-agent-implementation-plan.md) · 下一阶段：[Stage 4](incident-agent-stage-4.md)

## 目标与贯通路径

把既有调查循环扩展为可持续运行的 Runtime：多个独立任务并行，结果按依据合并；数据未就绪时等待，取消或进程中断后从持久状态继续。任务执行仍使用 Stage 2 的 Executor。

## 模块与关键接口

| 模块 | 实施约定 |
| --- | --- |
| `coordinator.py`、`store/` | `acquire_owner(case_id)`、`claim_ready_tasks(owner_generation)`；有效所有权代次、任务契约版本和 attempt 身份 |
| `budget.py` | `reserve/settle/reconcile`，区分 Case 额度、attempt 预留、实际 usage、估算与未知消耗 |
| `submission.py` | `ReadManifest` 与依据版本复验；晚到、重复、取消及过期提交使用统一回执 |
| 新增 `recovery.py`，扩展等待模型 | `recover(owner_generation)`、`check_wait(condition, clock)`、Runtime 的 `pause/resume/shutdown` |
| 应用 host 与 CLI | 所有权与 provider 生命周期、取消收尾、继续调查入口；为 Stage 6 提供 embedded/daemon 两种生命周期契约 |

## 实现步骤

1. **建立有效协调者。** 通过存储登记 Case 的所有者、代次和接管条件；每次任务派发、续租及提交检查有效身份。一个 Case 同时只有一个有效协调者。其他入口须交给协调者处理或明确返回所有权冲突，不能绕过它改调查状态。
2. **实现有界派发。** 从依赖已满足且有预算的任务中选择，默认并发上限为 2，并有配置化全局限制。事务内创建 attempt 和预算预留，事务外启动独立 Harness；规划、审查、修复和重试也受相应额度约束。并行 worker 使用独立调用上下文和 provider 会话身份，provider 实例的创建与关闭由 host 管理，不能默认共享有状态的供应商会话。避免在原 loop 中另建一套并行工具执行器。
3. **按实际读取处理结果。** 持久化派发依据、每轮请求材料、动态证据读取和 Finding 引用。提交时重验相关版本；无关 Case 更新可合并，前提变化或语义冲突进入复核。取消或旧 attempt 不能完成替代任务；其有效观测可保存，采纳解释必须走明确流程。
4. **完成预算账本。** 所有模型调用经同一预留/结算入口，覆盖规划、摘要、审查、修复与可观测重试。中断无 usage 时保留估算和未知额度，不能自动退为零；重复提交和 tracing 导出不重复结算。保留 token 安全余量，耗尽时仍可输出确定性进展。模型步数检查点可选，按一次 run/resume 计算，触发时保存进展并暂停；Case 的累计 token 和已存在的硬调用上限仍有效。
5. **实现等待与恢复。** WaitCondition 保存范围、数据水位或轻量检查、下次时间、期限和超时处理。某任务等待不阻断其他就绪任务；全案等待须考虑在途工作。暂停、取消和关闭停止新派发并有限收尾；启动时接管失效代次，旧执行标为中断，重跑分配新 attempt。
6. **接通执行关联和入口。** 每个 attempt 形成独立 trace，等待、唤醒和恢复通过 ID/links 连接。按回执、请求和证据核对未完成操作，保留未知结果。CLI 增加暂停/继续所需的命令模型；host 区分交互退出与后台前端断开，实际常驻连接由 Stage 6 接入。

## 交付物与实现完成标准

- [x] 同一 Runtime 的循环包含派发、接收、复核标记、等待、预算停止和恢复分支。
- [x] 所有会改变任务完成状态的提交均校验所有权、attempt 和相关依据版本。
- [x] 模型与工具等待不持有数据库事务；并发额度、预留和未知成本有明确账目。
- [x] 取消、关闭与恢复均能追溯到原执行，CLI 沿用原 Case；静态检查完成，行为覆盖交给 Stage 7 的 V1、V3、V4、V8、V10。

## 阶段检查与记录

2026-09-27：实现与静态检查完成，未编写或执行测试，未运行并发、模型调用、故障注入或恢复演练。勾选项表示实现完成，不表示行为验证通过。

实现基于 `c66fb87` 工作区中已有的 Stage 1–2；本阶段未单独提交 Git commit。模块、检查命令、边界与关键时序详见[Stage 3 实现记录](../architecture/incident-agent-stage-3.md)。新增 SQLite schema 3、租约与代次、独立派发、读取清单、版本复验、预算与响应账本、等待和恢复；复用原 Executor、Command/Receipt、Reducer 与 ExecutionRecorder。

CLI 增加 `resume/pause/cancel/retry/budget`，保留 `run` 和原 Case ID，增加 owner/manifest 查询。`tau_coding` 按角色创建和关闭独立 provider，提供 embedded/daemon 生命周期边界；实际常驻服务、连接和 TUI/RPC 留到 Stage 6。语义 Reviewer、修复策略和最终诊断留到 Stage 4。
