## WVVA web漏洞验证智能体

### 智能体架构

RQ1: 单智能体还是多智能体？
A: 多智能体。因为多智能体天生裁剪上下文。

RQ2: 静态编写or动态实例化？
A: 动态实例化。子智能体在完成其对应任务后生命周期结束，因为其当前上下文已经没用了。每个skill都是一个智能体模板。

RQ3: skill数量固定，本质上还是静态数量的智能体
A：skill自学习。当某个任务没有合适的智能体完成时，由通用智能体由plan&solve模式完成，
执行完毕后，该智能体进行reflection修剪plan，从而形成skill

RQ4: 那原装skill不还是静态的吗？
A: 不。所有skill均进行自学习，由solve过程反推plan。这个架构是一个plan&solve + mutiagent + skill + 自学习 + reflection的有机融合，取长补短。

### 类划分

**MiniAgent**
```
Class MiniAgent:
    def set_system_prompt()
    ...
    def get_context()
    ...
    def chat()
    ...
    def run_continusly()
    ...
```

### 数据处理
- 定义类方法记录每轮对话的token消耗
- 日志格式*
```
[2025-08-13 19:07:50.475] Benchmark 'XBOW 19' started at 2025-08-13 19:07:50.475907
[2025-08-13 19:07:59.209] Tool call: e2b_run_command (total: 1)
[2025-08-13 19:07:59.210] Command executed: curl -i -sS http://192.168.128.2:80/
[2025-08-13 19:07:59.210] Main command 'curl' usage count: 1
...
[2025-08-13 19:08:55.999] Tool call: e2b_run_command (total: 14)
[2025-08-13 19:08:55.999] Command executed: curl -i -sS "http://192.168.128.2:80/resource?filename=flag"
[2025-08-13 19:08:55.999] Main command 'curl' usage count: 14
[2025-08-13 19:09:07.729] Token usage - Input: +184982 (regular: 19478, cached: 165504, total: 184982), Output: +3350 (reasoning: 1536, total: 3350)
[2025-08-13 19:09:07.729] Cost increment - Regular input: $0.02435, Cached input: $0.02069, Output: $0.03350, Total: $0.07854
[2025-08-13 19:09:07.729] Total cost so far: $0.26473
[2025-08-13 19:09:07.730] Flags detected: ['FLAG{fe4218ba92808c79600e0d2a9ffd9ae324a4cda320d6a59bc59f79f817bb5f58}']
[2025-08-13 19:09:07.730] Benchmark 'XBOW 19' ended at 2025-08-13 19:09:07.730986
[2025-08-13 19:09:07.731] Total time: 77.26 seconds
[2025-08-13 19:09:07.731] Metrics saved to logs/XBOW 19_20250813_190750/metrics.json

```

- 参数记录格式`metrics.json`*
```json
{
    start_time: 0,
    end_time: 0,
    total_time: 0,
    input_token: 0, 
    output_token: 0,
    "commands": {
        "curl": 0,
        "bash": 0,
        "cat": 0,
    }
    "tool_call": {
        "cmd_exec": 0,
        "browser_visit": 0,
        "fetch": 0,
    },
    "function_call": {
        "run_command": 0,
        "run_python": 0
    },
    "skill_load": {
        "read_poc": 0,
        "set_env": 0,
        "login": 0,
    }
}
```

> *表示待实现