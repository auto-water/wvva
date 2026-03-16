# MAPTA 架构说明

本目录包含基于多智能体与沙箱的安全扫描与日志分析系统，主要由 `main.py`（扫描引擎）和 `analyze_logs.py`（指标与可视化分析）组成。

---

## 1. 项目概述

- **main.py**：多智能体安全扫描引擎，通过 OpenAI API 驱动主智能体与沙箱子智能体，对目标 URL 进行漏洞扫描、PoC 验证，并支持邮件（mail.tm）、Slack 告警等集成。
- **analyze_logs.py**：离线分析工具，从各次扫描/基准测试产生的 `metrics.json` 中聚合数据，生成 LaTeX 表格、CDF 图、成本与工具使用分析、Sankey 图等，用于评估 XBOW 等基准的表现。

---

## 2. 目录与文件结构

```
mapta/
├── main.py          # 多智能体扫描主程序（工具定义、主/子智能体循环、沙箱、入口）
├── analyze_logs.py  # 日志与指标分析（加载 metrics、绘图、LaTeX、Sankey 等）
├── LICENSE          # MIT 许可证
└── README.md        # 本架构说明
```

**外部依赖**：`main.py` 依赖同目录或 Python 路径下的 `function_tool` 模块（`from function_tool import function_tool`），用于将 Python 函数声明为可被模型调用的工具。

---

## 3. main.py 架构

### 3.1 整体架构图（概念）

```
                    ┌─────────────────────────────────────────────────────────┐
                    │                     main.py 入口                          │
                    │  (targets.txt / 单目标 → run_parallel_scans / 单次扫描)   │
                    └───────────────────────────┬─────────────────────────────┘
                                                │
                    ┌───────────────────────────▼──────────────────────────────┐
                    │              run_single_target_scan (每目标)               │
                    │  UsageTracker / set_current_sandbox / run_continuously   │
                    └───────────────────────────┬──────────────────────────────┘
                                                │
    ┌───────────────────────────────────────────▼───────────────────────────────┐
    │                     run_continuously (主智能体循环)                        │
    │   OpenAI client.responses.create(model="gpt-5", tools=main_agent_tools)  │
    │   → 解析 function_call → execute_function_call → asyncio.gather 并行执行  │
    └───┬─────────────────────────────────────────────────────────────────────┘
        │
        │ 主智能体可调用的工具（main_agent_tools 子集）：
        │   sandbox_agent, validator_agent, get_registered_emails,
        │   list_account_messages, get_message_by_id, send_slack_alert, send_slack_summary
        │
        ├──► sandbox_agent(instruction, max_rounds)
        │         └── 内部循环：仅 sandbox_run_command / sandbox_run_python
        │
        ├──► validator_agent(instruction, max_rounds)
        │         └── 内部循环：同上，用于 PoC 验证
        │
        └──► 其它工具：邮件、Slack、直接返回结果
```

- **主智能体**：面向“安全扫描”的高层规划，通过 `sandbox_agent` / `validator_agent` 把具体执行下放到沙箱内。
- **沙箱子智能体**：只使用 `sandbox_run_command`、`sandbox_run_python`，在隔离环境中执行命令和 Python，避免递归调用更高层工具。

### 3.2 多智能体协作机制（详细）

本项目的“多智能体”是**两层、工具调用式**的协作：主智能体把子任务交给子智能体，子智能体只和沙箱交互，最后把结果以**字符串**形式返回给主智能体。

#### 3.2.1 角色与分工

| 角色 | 在代码中的体现 | 可见工具 | 职责 |
|------|----------------|----------|------|
| **主智能体（Orchestrator）** | `run_continuously` 里的 `client.responses.create(..., tools=main_agent_tools)` | `sandbox_agent`、`validator_agent`、邮件三件套、Slack 两件套 | 理解用户目标（如“对某 URL 做全量漏洞扫描”），制定步骤：何时探索、何时让沙箱执行、何时验证 PoC、何时发告警/汇总。**不能**直接执行 Shell/Python，只能通过子智能体。 |
| **沙箱智能体（Executor）** | `run_sandbox_agent` 内部的 `responses.create(..., tools=sandbox_tools)` | 仅 `sandbox_run_command`、`sandbox_run_python` | 根据主智能体下发的 **instruction** 在沙箱里自主多轮执行命令/脚本，直到完成或达到 max_rounds，最后返回一段**文本结果**（如命令输出、分析结论）。 |
| **验证智能体（Validator）** | `run_validator_agent` 内部同样一个循环 | 同上，仅两个沙箱工具 | 专门做 PoC 验证：在沙箱中复现漏洞、收集证据、给出是否成立等结论，返回简洁的**验证结果文本**。 |

主智能体**不**看到 `sandbox_run_command` / `sandbox_run_python`，因此不会直接写命令，而是通过“给 sandbox_agent / validator_agent 发一段自然语言指令”来间接驱动沙箱，从而形成**任务分解 + 安全隔离**的协作。

#### 3.2.2 协作流程（单次 delegation）

1. **用户输入**：例如 “对 https://example.com 做全量漏洞扫描，发现漏洞要给出 PoC”。
2. **主智能体回合**：主智能体根据 system prompt + 当前对话，决定下一步；可能输出多个 `function_call`，例如：
   - `sandbox_agent(instruction="在沙箱里用 curl 探测 example.com 的 /api 端点，看返回头与状态码")`
   - 或 `validator_agent(instruction="在沙箱中执行以下 PoC：...，确认是否出现预期输出")`
   - 或 `send_slack_alert(...)`、`get_registered_emails()` 等。
3. **执行工具**：`execute_function_call` 被调用；若调用的是 `sandbox_agent`：
   - 进入 `run_sandbox_agent(instruction, max_rounds)`。
   - 内部维护**独立的** `sandbox_input_list`（只含该子智能体的 system prompt + 上述 instruction），与主智能体的 `input_list` **不共享**。
   - 子智能体循环：`responses.create(model="gpt-5", tools=[sandbox_run_command, sandbox_run_python], input=sandbox_input_list)` → 若返回 function_call（如 `sandbox_run_command("curl -I ...")`），则在本线程内执行 `execute_tool`；此时 `get_current_sandbox()` 取到的是**当前扫描**在 `run_continuously` 里 set 的沙箱，所以子智能体的所有命令/脚本都跑在**同一沙箱**里。
   - 子智能体多轮执行直到不再发起 tool call，或达到 max_rounds；最后从 output 中取出**文本回复**。
4. **结果回传**：`run_sandbox_agent` 的返回值（一段字符串）被 `execute_tool` 转成 JSON 字符串，再被封装成 `function_call_output` 加入**主智能体**的 `input_list`。
5. **主智能体下一轮**：主智能体看到“沙箱智能体的执行结果”作为工具返回值，据此决定下一步（例如再派一个 sandbox_agent 做别的探测，或调用 validator_agent 验证 PoC，或发 Slack 等）。

因此，**协作的本质**是：主智能体通过**工具调用**把“在沙箱里做什么”描述成一段 instruction，子智能体在**独立的对话上下文**中只使用沙箱工具完成任务，最后把**文本结果**作为该次工具调用的返回值，供主智能体继续推理。

#### 3.2.3 共享上下文与隔离

- **共享的**（同一扫描内）：
  - **沙箱实例**：通过 `_thread_local.sandbox` 绑定到当前扫描；主智能体、sandbox_agent、validator_agent 在同一个 run_continuously 里先后执行，因此共用同一个沙箱（同一环境、同一文件系统）。
  - **当前目标 URL**：`_thread_local.current_target_url`，用于日志与 usage 统计。
  - **UsageTracker**：主智能体与子智能体的 API 用量都记在同一个 tracker 里（main_agent_usage / sandbox_agent_usage）。
- **隔离的**：
  - **对话历史**：主智能体的 `input_list` 与 sandbox 的 `sandbox_input_list`、validator 的 `validator_input_list` 完全独立；子智能体看不到主智能体的其他工具调用或用户消息，只能看到自己那轮收到的 instruction 以及自己在沙箱里的多轮输入/输出。
  - **模型调用**：每次 `responses.create` 都是独立请求；子智能体与主智能体可以是同一模型（gpt-5），但通过不同的 `metadata.name`（如 "security_scan" / "sandbox_agent" / "validator_agent"）区分，便于计费或日志分析。

#### 3.2.4 同一轮内的并行

主智能体**一轮**可以发出多个 function_call（例如同时调用 `sandbox_agent(...)` 和 `get_registered_emails()`）。代码中通过 `asyncio.gather(*tasks)` 并行执行所有 `execute_function_call`。因此：
- 多个**不同**工具可以在同一轮并行（例如一个 sandbox_agent + 一个 get_registered_emails）；
- 同一个子智能体（如 sandbox_agent）在一次调用内部仍是**顺序**多轮（子智能体自己的 while 循环），不会出现“主智能体同时发起两个 sandbox_agent 调用并真正并行跑两个子循环”的复杂交错（除非主智能体在同一轮真的调了两次 sandbox_agent，那时会有两个独立的 sandbox_agent 调用并行执行，但它们各自仍顺序多轮）。

#### 3.2.5 小结

- **层级**：主智能体（规划与编排）→ sandbox_agent / validator_agent（沙箱内执行/验证）→ sandbox_run_command / sandbox_run_python（底层能力）。
- **接口**：主与子之间只通过**工具入参（instruction 字符串）**和**工具返回值（子智能体的最终文本）**交换信息；子智能体没有主智能体的完整对话历史。
- **目的**：主智能体专注“扫什么、验证什么、报什么”，子智能体专注“在隔离环境里怎么执行、怎么验证”，既分工清晰，又通过工具边界避免主智能体直接接触底层命令，便于安全和可控。

### 3.3 沙箱与线程本地存储

- **沙箱工厂**：通过环境变量 `SANDBOX_FACTORY` 指定，格式为 `"module_path:function_name"`。该函数应返回一个沙箱实例，支持：
  - `files.write(path, content)`
  - `commands.run(cmd, timeout=..., user=...)`
  - 可选：`set_timeout(ms)`、`kill()`
- **线程本地**：`_thread_local` 保存当前线程/扫描的 `sandbox` 和 `usage_tracker`，便于多目标并行时每个目标使用独立沙箱与用量统计。
- **生命周期**：在 `run_continuously` 的 `finally` 中调用 `sandbox_instance.kill()`，扫描结束即销毁沙箱。

### 3.4 main.py 主要函数与模块划分

| 函数 / 类 | 所属模块 | 作用 |
|-----------|----------|------|
| `get_current_sandbox()` | 沙箱与线程本地 | 从当前线程取当前扫描的沙箱实例 |
| `set_current_sandbox(sandbox)` | 沙箱与线程本地 | 为当前线程/扫描绑定沙箱实例 |
| `create_sandbox_from_env()` | 沙箱与线程本地 | 根据 `SANDBOX_FACTORY` 动态加载并创建沙箱，未配置则返回 None |
| `UsageTracker`（类） | 用量统计 | 记录主智能体与子智能体 API 用量，支持汇总与保存 JSON |
| `get_current_usage_tracker()` | 用量统计 | 取当前线程的 UsageTracker |
| `set_current_usage_tracker(tracker)` | 用量统计 | 为当前线程设置 UsageTracker |
| `execute_function_call(function_call)` | 工具执行 | 解析 API 返回的 function_call，调用 `execute_tool`，返回 `function_call_output` 结构 |
| `execute_tool(name, arguments)` | 工具执行 | 根据工具名从 `_function_tools` 取函数并执行，结果 JSON 序列化返回 |
| `generate_tools_from_function_tools()` | 工具注册 | 从 `_function_tools` 生成 OpenAI API 所需的 `tools` 列表（name/description/parameters） |
| `get_registered_emails()` | 工具·邮件 | 返回已注册的 mail.tm 邮箱列表（只读 `email_token_store`） |
| `list_account_messages(email, limit)` | 工具·邮件 | 按邮箱列出最近邮件（需 JWT） |
| `get_message_by_id(email, message_id)` | 工具·邮件 | 按邮箱与消息 ID 拉取单封邮件内容 |
| `send_slack_security_alert(...)` | 工具·Slack | 发送单条漏洞告警到 Slack（类型、严重程度、描述、证据等） |
| `send_slack_scan_summary(...)` | 工具·Slack | 发送扫描汇总到 Slack（目标、发现数、各严重程度统计等） |
| `run_sandbox_agent(instruction, max_rounds)` | 子智能体 | 沙箱子智能体：仅用 `sandbox_run_command`/`sandbox_run_python` 完成 instruction，多轮直至结束或达 max_rounds，返回文本 |
| `run_validator_agent(instruction, max_rounds)` | 子智能体 | 验证子智能体：同上工具，侧重 PoC 验证与证据收集，返回验证结论文本 |
| `sandbox_run_python(python_code, timeout)` | 沙箱底层工具 | 在沙箱中写临时脚本并执行 Python，返回 exit code / stdout / stderr（超长截断） |
| `sandbox_run_command(command, timeout)` | 沙箱底层工具 | 在沙箱中执行 Shell 命令，返回 exit code / stdout / stderr |
| `read_targets_from_file(file_path)` | 编排与入口 | 从文件读取目标 URL 列表（每行一个，忽略空行与 `#` 注释） |
| `run_continuously(...)` | 编排与入口 | 主智能体循环：创建/绑定沙箱与 target_url，维护 `input_list`，循环调用 API、解析 function_call、并行执行工具，直至无 tool call 或达 max_rounds；finally 中 kill 沙箱 |
| `run_single_target_scan(...)` | 编排与入口 | 单目标扫描：创建沙箱与 UsageTracker，格式化 user_prompt，调用 `run_continuously`，将结果写 .md、用量写 JSON |
| `run_parallel_scans(...)` | 编排与入口 | 多目标并行：为每个 target 创建 `run_single_target_scan` 任务，`asyncio.gather` 执行，汇总完成数/失败数与 usage 文件列表 |

**模块小结**：沙箱与线程本地、用量统计、工具执行与注册、工具实现（邮件 / Slack / 子智能体 / 沙箱底层）、编排与入口（读目标、主循环、单目标扫描、并行扫描）。`__main__` 中根据是否存在 `targets.txt` 决定是否调用 `run_parallel_scans`。

### 3.5 工具层（Function Tools）

所有可被模型调用的能力均通过 `@function_tool` 装饰器声明，并集中注册在 `_function_tools` 中，再由 `generate_tools_from_function_tools()` 转为 OpenAI 所需的 `tools` 列表。

| 工具名 | 说明 |
|--------|------|
| `sandbox_run_command` | 在沙箱中执行 Shell 命令，返回 exit code / stdout / stderr |
| `sandbox_run_python` | 在沙箱中执行 Python 代码（写临时脚本再执行），返回同上 |
| `sandbox_agent` | 子智能体：仅用上述两个沙箱工具完成给定 instruction，多轮直到无 tool call 或达 max_rounds |
| `validator_agent` | 子智能体：同上，但系统提示侧重 PoC 验证与证据收集 |
| `get_registered_emails` | 返回当前已注册的 mail.tm 邮箱列表（来自内存 `email_token_store`） |
| `list_account_messages` | 按邮箱列出最近邮件（需先有 JWT，见 docstring 中 set_email_jwt_token 说明） |
| `get_message_by_id` | 按邮箱和消息 ID 拉取单封邮件内容 |
| `send_slack_alert` | 发送单条漏洞告警到 Slack（类型、严重程度、URL、描述、证据、建议等） |
| `send_slack_summary` | 发送扫描汇总到 Slack（目标、发现数量、各严重程度统计、耗时等） |

- 主智能体看到的工具集是上述列表的**子集**（不含 `sandbox_run_command` / `sandbox_run_python`），避免主智能体直接操作沙箱，而是通过 `sandbox_agent` / `validator_agent` 间接使用。
- `execute_tool` 根据 name 从 `_function_tools` 取函数并执行，结果以 JSON 字符串返回；`execute_function_call` 解析 API 返回的 function_call，调用 `execute_tool` 并把输出封装为 `function_call_output`。

### 3.6 主循环与子智能体循环

- **run_continuously**：
  - 使用 `client.responses.create(..., tools=main_agent_tools, input=input_list, reasoning={"effort": "high"})`。
  - 若 output 中有 `function_call`，则并行 `execute_function_call`，将请求与结果追加到 `input_list`，进入下一轮。
  - 若无 tool call，则从 output 中提取文本并返回，结束该目标扫描。
  - 支持 `max_rounds` 上限；每轮会记录主智能体 usage 到当前线程的 `UsageTracker`。

- **sandbox_agent / validator_agent**：
  - 各自维护 `sandbox_input_list` / `validator_input_list`，仅传入 `sandbox_run_command` 与 `sandbox_run_python` 作为 tools。
  - 内部同样：`responses.create` → 收集 function_call → `execute_function_call`（此时会在当前线程的沙箱上执行）→ 结果追加到 input，直到无 tool call 或达到 max_rounds。
  - 子智能体的 usage 记入同一 `UsageTracker` 的 sandbox_agent 统计。

### 3.7 用量统计与输出

- **UsageTracker**：按线程保存 `main_agent_usage` 与 `sandbox_agent_usage`，可汇总扫描时长、调用次数、usage 详情。
- **run_single_target_scan**：为每个目标创建独立沙箱与 UsageTracker，扫描结束后将结果写入以 URL 派生的 `.md` 文件，并将用量写入 `{site_name}_usage_log_{timestamp}.json`。

### 3.8 入口与运行模式

- **__main__**：
  - 若存在 `targets.txt`：读取每行一个 URL（忽略空行与 `#` 注释），使用固定 `base_user_prompt` 模板调用 `run_parallel_scans`，对多目标并行执行 `run_single_target_scan`。
  - 若无有效目标则提示回退到单目标模式（当前脚本中未实现单目标 CLI，可扩展）。
- **环境变量**：`SYSTEM_PROMPT`、`SANDBOX_FACTORY`、`SLACK_WEBHOOK_URL`、`SLACK_CHANNEL`、`SANDBOX_SYSTEM_PROMPT`、`VALIDATOR_SYSTEM_PROMPT` 等控制行为与集成。

---

## 4. analyze_logs.py 架构

### 4.1 职责与数据流

- **输入**：指定目录下各子目录中的 `metrics.json`（来自扫描/基准运行）。
- **输出**：汇总表（LaTeX）、多类图表（PNG/PDF）、Sankey（HTML/PNG/PDF）、相关性分析等，用于论文或报告。

### 4.2 主要函数与模块划分

| 函数 | 作用 |
|------|------|
| `load_all_metrics(logs_dir)` | 遍历子目录读取 `metrics.json`，解析 benchmark 名得到 challenge 编号，按编号排序返回列表 |
| `generate_latex_table(data)` | 生成 XBOW 等基准的 LaTeX 汇总表（挑战数、成功率、时间/Token/成本/命令统计） |
| `plot_time_cdf(data, output_dir)` | 绘制总耗时 CDF，区分 solved/unsolved，并标出中位数线 |
| `plot_token_cdfs(data, output_dir)` | 多曲线 CDF：Input/Output/Cached/Reasoning/Total Tokens |
| `plot_cost_analysis(data, output_dir)` | 成本 CDF + 按挑战的成本堆叠柱状图（Regular Input / Cached / Output） |
| `plot_tool_usage(data, output_dir)` | 工具调用分布箱线图 + 每挑战总调用数柱状图 |
| `analyze_command_usage(data, output_dir)` | 命令使用统计、LaTeX 表、按挑战×命令的热力图 |
| `extract_challenge_types(benchmarks_dir)` | 从基准目录下 XBEN-* 的 README 中抽取 Type/Category 等信息 |
| `plot_sankey_analysis(data, output_dir)` | 使用 Plotly 绘制 All Benchmarks → Success/Failed → 挑战类型的 Sankey 图 |
| `plot_success_correlation(data, output_dir)` | 成功与否与时间/成本/Token/工具数的相关性分析与散点图 |
| `main()` | 写死 `logs_dir` 与 `output_dir`，依次调用上述加载与生成步骤并打印生成文件列表 |

### 4.3 样式与依赖

- 使用 matplotlib / seaborn，统一 `COLORS` 与 plt 的 font、spines、legend 等配置。
- LaTeX 表使用 booktabs 风格（`\toprule`/`\midrule`/`\bottomrule`）。
- Sankey 依赖 `plotly`（及可选 `kaleido` 用于静态图导出）。

---

## 5. 依赖与配置概要

- **main.py**：`openai`（AsyncOpenAI）、`httpx`、`aiohttp`、`function_tool`；可选通过 `SANDBOX_FACTORY` 接入任意沙箱实现。
- **analyze_logs.py**：`numpy`、`pandas`、`matplotlib`、`seaborn`、`plotly`（及可选 `kaleido`）。
- **环境变量（main）**：`OPENAI_API_KEY`、`SYSTEM_PROMPT`、`SANDBOX_FACTORY`、`SLACK_WEBHOOK_URL`、`SLACK_CHANNEL`、`SANDBOX_SYSTEM_PROMPT`、`VALIDATOR_SYSTEM_PROMPT` 等。

---

## 6. 使用说明（简要）

- **运行扫描**：在 mapta 目录下准备 `targets.txt`（每行一个 URL），配置好环境变量与 `function_tool` 模块，执行 `python main.py`，将并行扫描并生成每目标的 `.md` 与 usage JSON。
- **分析日志**：将各次运行的 `metrics.json` 按子目录放在同一 `logs_dir` 下，修改 `analyze_logs.py` 中 `main()` 的 `logs_dir`/`output_dir`，运行 `python analyze_logs.py`，在 output 目录查看 LaTeX 与图表。

---

*本 README 基于当前 mapta 目录下 Python 代码整理，用于快速理解架构与二次开发。*
