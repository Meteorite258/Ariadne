# Tau 架构与二次开发速读

本文基于本地提交 `55df516`（`tau-ai` 0.4.2）梳理，面向第一次读源码的开发者。目录名虽然是 Amadeus，当前代码、包名和命令仍使用 Tau。建议先读第 1～3 节，再按开发需求查第 5 节。

## 1. 项目做什么，代码放在哪

Tau 是一个 Python 编程 Agent：把用户需求和项目上下文交给模型，执行模型请求的文件或终端工具，再把结果交回模型，直到得到回答。它提供 Textual 交互界面、非交互 print 模式和供外部程序控制的 RPC 模式，并支持会话恢复、分支、上下文压缩、技能与 Python 扩展。

| 位置 | 职责 | 首先看什么 |
| --- | --- | --- |
| `src/tau_agent/` | 可复用的 Agent 核心：协议、消息、事件、循环、状态、会话存储原语 | `provider.py`、`messages.py`、`loop.py`、`harness.py` |
| `src/tau_ai/` | 实现不同模型服务的请求与流式响应适配 | `fake.py`、`openai_compatible.py`、`anthropic.py` |
| `src/tau_coding/` | 编程应用：配置、资源、工具、会话编排、扩展、前端 | `cli.py`、`session_preparation.py`、`session.py` |
| `tests/` | 按模块组织的行为与回归测试 | `test_agent_loop.py`、`test_coding_session.py` |
| `examples/extensions/` | 可直接运行的扩展示例 | `hello_tool.py`、`permission_gate.py`、`sidebar_status.py` |
| `website/content/` | 用户指南、接口参考与内部机制说明 | `internals/`、`guides/`、`reference/` |
| `dev-notes/` | 分阶段实现记录与设计说明 | `architecture/`、`design/`、`adr/` |
| `src/tau_coding/data/` | 随 Python 包分发的文档、示例和资源 | `docs/`、`examples/` |

**最关键的职责边界：** `AgentHarness` 管“Agent 怎么运行”，`CodingSession` 管“在什么编程环境中运行”，TUI 管“如何交互和显示”。这是 Tau 对 Pi 设计的主要继承。

实际源码依赖如下（箭头表示 import / 依赖）：

```text
tau_coding ──→ tau_agent ←── tau_ai
     └───────────────────→ tau_ai
```

注意：有些既有架构文档用 `tau_coding → tau_agent → tau_ai` 表达概念层次。当前源码中，`ModelProvider` 和模型流事件定义在 `tau_agent`；`tau_ai/provider.py` 只是重导出接口。循环通过注入的 provider 调模型，核心不必导入具体适配器。

`tau_agent` 不应依赖 Textual、Rich、Typer、用户配置目录或资源发现逻辑。项目当前作为一个 Python 分发包发布，三层是源码职责边界，并非三个独立安装包。

## 2. 一次请求怎样跑完

命令入口由 `pyproject.toml` 指向 `tau_coding.cli:app`，CLI 用 Typer 解析参数。普通启动进入 TUI；`-p` 进入 print；`--mode rpc` 进入 RPC。

```text
CLI / TUI / RPC
    ↓ 准备配置、存储和运行环境
prepare_coding_session() → CodingSession.load() → prepared.adopt()
    ↓ 普通用户输入
CodingSession.prompt()
    ↓ 扩展 input hook → 技能/模板展开 → 上下文检查
AgentHarness.prompt_message()
    ↓
run_agent_loop()
    ├─ provider.stream_response() → 流式模型事件 → Agent 消息事件
    ├─ 有 ToolCall → 执行工具 → ToolResultMessage → 再请求模型
    └─ 无工具调用且无待处理输入 → AgentEndEvent
    ↓ 会话层完成恢复/压缩等收尾
agent_settled → 前端回到空闲状态
```

具体以“读取某个文件并解释”为例：

1. **准备环境**：`CodingSession.load()` 组织项目信任、资源与扩展发现、provider/model 选择、历史恢复、工具与系统提示词。`prepare_coding_session()` 暂缓正式写入；`adopt()` 提交候选会话，失败则 `abort()` 释放它持有的资源。
2. **处理输入**：前端先处理适用的命令；普通提示进入 `CodingSession.prompt()`，经过扩展输入 hook、`expand_prompt_text()` 和自动压缩检查。`/help` 这类命令不必发给模型。
3. **请求模型**：Harness 保存消息与运行状态，调用 `run_agent_loop()`。循环把 `system`、历史 `messages` 和工具 schema 传给 provider。
4. **执行读取**：模型返回 `ToolCall(name="read", arguments=...)`。循环按名称找到 `AgentTool`，执行前后 hook，并调用异步 `execute()`；结果作为 `ToolResultMessage` 加入历史。
5. **继续推理**：包含读取结果的历史再次传给模型；没有工具调用或排队输入后，循环结束。错误、取消及 `max_turns` 也有终止路径。
6. **持久化与显示**：Harness 的事件订阅者负责把结束的消息写入会话；前端消费事件显示增量文字、工具状态与最终结果。保存不依赖某个 UI 是否继续读取输出。

读代码时区分三种数据：

| 数据 | 定义位置 | 用途 |
| --- | --- | --- |
| `UserMessage` / `AssistantMessage` / `ToolResultMessage` | `tau_agent/messages.py` | 发给模型、保留在对话中的内容；助手内容可含文本、思考和工具调用 |
| `AssistantMessageEvent` | `tau_agent/provider_events.py` | provider 的文本、思考、工具调用增量与完成/失败信号 |
| `AgentEvent` / `CodingSessionEvent` | `tau_agent/events.py`、`tau_coding/events.py` | 循环和应用生命周期，供保存、扩展与前端消费 |

当前循环对同一响应中的工具调用**顺序执行**。`steer` 在循环边界插入后续输入，`follow_up` 在当前工具链结束后接续；它们不会立即打断正在执行的工具。应用前端应以 `agent_settled` 判断完整会话工作结束，因为 `agent_end` 后还可能压缩或重试。

## 3. 主要功能如何实现

### 模型接入与配置

`provider_config.py` 管持久配置，`provider_catalog.py` / `catalog_loader.py` 管模型目录，`provider_runtime.py` 把配置和鉴权信息组装成 provider 实例。实际网络协议在 `tau_ai`：OpenAI-compatible、Anthropic、Google、Mistral、OpenAI Codex 等适配器统一输出模型流事件。

新增兼容现有协议的服务，先考虑配置或动态 provider 扩展；只有协议不匹配时才增加底层适配器。目录数据、认证和网络协议是不同职责，不要把厂商判断塞进 Agent 循环。

### 文件和终端工具

`tau_coding/tools.py:create_coding_tools()` 默认注册 `read`、`write`、`edit`、`bash`。`tau_agent/tools.py` 定义公共 `AgentTool`、参数 schema 和 `AgentToolResult`；具体文件读写、图片处理、输出截断、编辑匹配和进程取消属于应用层。

工具返回结构化内容及 `details`；执行异常会变成错误工具结果，模型可以据此继续处理。新增工具时同时考虑参数校验、取消和结果大小。

### 会话保存、恢复与分支

`tau_agent/session/` 把记录建模为带 `id`、`parent_id` 的条目，支持内存和 JSONL 存储。`tree.py` 找到根到活动节点的路径，`memory.py` 将条目重建为 `SessionState`。

`CodingSession` 订阅 `MessageEndEvent` 保存完整消息；`SessionManager` 管会话索引、名称、项目关联和恢复入口，`TauPaths` 决定落盘位置。默认会话在 `~/.tau/sessions/` 下按项目分目录。

**磁盘历史、当前分支、模型上下文不是同一份列表。** 恢复时只重放活动路径；压缩通过追加 `CompactionEntry`，将旧上下文替换成摘要并保留近期消息，不直接删掉原始历史。相关实现集中在 `session.py`、`context_window.py` 和 `tau_agent/session/memory.py`。

### 项目上下文、技能、模板与信任

`resources.py` 发现资源，`context.py` 读取项目指令，`skills.py` / `prompt_templates.py` 负责技能和提示模板；`system_prompt.py` 组合工具说明、技能索引、项目上下文等。

资源可来自用户与项目的 `.tau` / `.agents` 目录，但是否加载还受 `project_trust.py` 的策略控制。技能主要是供模型读取或由显式调用展开的 Markdown 指令，模板用于展开用户输入；Python 扩展则会执行代码并注册能力。

项目信任控制项目资源的自动加载，**不是文件系统或终端沙箱**。若二次开发涉及执行权限，应单独设计工具执行策略，可先参考 `permission_gate.py` 的工具拦截方式。

### 扩展与本地推理

`extensions/loader.py` 加载扩展入口 `setup(tau: ExtensionAPI)`，`api.py` 暴露注册接口，`runtime.py` 管 hook、事件订阅和生命周期。扩展可增加工具、命令、提示词片段、界面能力及动态 provider。

`extensions/provider_registry.py` 管动态 provider；`local_backends.py` 与 `extensions/builtins/llama_cpp/` 管本地后端能力及 llama.cpp 集成。后端状态、生命周期归 `tau_coding`；模型协议适配仍归 `tau_ai`。重载会更换运行时，修改这里要特别检查旧资源关闭与过期异步结果不能覆盖新状态。

### 前端与导出

TUI 的 `app.py` 管交互，`adapter.py:TuiEventAdapter.apply()` 把会话事件映射到 `state.py` 的显示状态，`widgets.py` 渲染组件。print 使用 `rendering/`，RPC 使用 `rpc.py` 的逐行 JSON 控制与事件输出；协议见 `website/content/reference/rpc.md`。HTML 会话导出在 `session_export.py`。

要做新的前端，可消费 `CodingSession` 事件，或通过 RPC 控制 Tau；只需要通用 Agent 时则直接复用 `AgentHarness`。

## 4. 本地启动与阅读顺序

项目要求 Python **3.12+**，Python 命令统一通过 `uv` 运行。在仓库根目录：

```bash
uv sync --dev
uv run tau --help
uv run tau                         # 交互界面，配置 provider/model 后使用
uv run tau -p "解释这个项目"       # 使用已配置的模型
uv run tau -e examples/extensions/hello_tool.py
```

不需要 API key 的核心测试入口：

```bash
uv run pytest tests/test_agent_loop.py tests/test_agent_harness.py
```

建议按 `messages.py` / `tools.py` / `provider.py` → `loop.py` → `harness.py` → `session.py` 的 `load()`、`prompt()`、`_attach_persistence_listener()` → `cli.py:run_print_mode()` 顺序阅读，再看 TUI。`session.py` 较大，按上述函数定位即可。

`tau_ai/fake.py:FakeProvider` 能重放预先定义的模型事件，`tests/pi_event_helpers.py` 帮助构造事件。先看 `test_agent_loop.py` 的“模型 → 工具 → 模型”测试，可以脱离网络理解整条链路。

## 5. 二次开发改哪里

| 需求 | 最小切入点 | 对应测试 |
| --- | --- | --- |
| 新增业务工具 | 从 `examples/extensions/hello_tool.py` 改起，用 `register_tool()` 注册 | `test_example_extensions.py`、`test_extensions.py` |
| 修改内置读写/终端行为 | `tau_coding/tools.py` | `test_coding_tools.py` |
| 新增斜杠命令 | `commands.py` 或扩展命令注册接口；前端处理交互动作 | `test_commands.py`、`test_cli.py` |
| 改默认系统提示词或资源加载 | `system_prompt.py`、`resources.py`、`context.py` | `test_system_prompt.py`、`test_resources.py`、`test_project_trust.py` |
| 接入新模型服务 | 配置/目录 → `extensions/providers.py` → 必要时新增 `tau_ai` 适配器 | `test_provider_runtime.py`、`test_extension_providers.py`、`test_tau_ai.py` |
| 调整工具循环、排队或取消 | `tau_agent/loop.py`、`harness.py` | `test_agent_loop.py`、`test_agent_harness.py` |
| 修改恢复、分支、压缩 | `session.py`、`tau_agent/session/`、`context_window.py` | `test_coding_session.py`、`test_session.py`、`test_context_window.py` |
| 修改 TUI 或新增外部前端 | `tui/adapter.py`、`state.py`、`app.py` 或 `rpc.py` | `test_tui_adapter.py`、`test_tui_app.py`、`test_rpc.py` |

推荐第一个练习：复制 `hello_tool.py`，改成一个只读业务工具，用 `-e` 加载，再用 fake provider 测试工具调用和结果。这样能经过真实扩展与工具链，又不必先修改核心循环。

行为变更先补对应测试；提交前按变更范围运行检查：

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

架构变化记录到 `dev-notes/`，用户行为变化同步到 `website/content/`。阶段顺序与架构意图以 [路线图 issue #1](https://github.com/huggingface/tau/issues/1) 为参考，功能是否已经实现则核对当前源码和测试，避免把早期设计记录当成当前状态。

继续深入可看 `website/content/internals/agent-loop.md`、`website/content/guides/extensions.md` 和 `CONTRIBUTING.md`。
