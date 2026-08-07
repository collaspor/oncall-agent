# AIOps 智能运维 Agent 落地 POC — 产品需求文档 (PRD)

| 字段 | 内容 |
|------|------|
| 文档版本 | v2.0 |
| 创建日期 | 2026-08-07 |
| 修订日期 | 2026-08-07 |
| 项目名称 | SuperBizAgent AIOps 真实数据源接入 POC |
| 文档性质 | 实施落地 PRD（含需求边界定义 + 最小 POC 方案） |
| 修订说明 | 修复 v1.0 中 5 个 P0 问题（时间参数类型、CPU 值转换、step 转换、服务名引导、示例统一）和 7 个 P1 问题（async httpx、PromQL 注入、空数据结构、延迟假设、Replanner 风险、时区、用户故事） |

---

## 1. 背景与目标

### 1.1 现状

SuperBizAgent 项目当前的两个 MCP Server（`cls_server.py` 日志服务、`monitor_server.py` 监控服务）返回的是 **mock 假数据**：

- `monitor_server.py` 的 CPU 数据由算法生成（从 10% 线性增长到 95%），与真实系统状态无关
- `cls_server.py` 的日志内容写死为"正在同步元数据……"

现有 `monitor_server.py` 的 `query_cpu_metrics` 工具签名（**POC 改造的基础事实**）：

```python
def query_cpu_metrics(
    service_name: str,
    start_time: Optional[str] = None,   # 字符串格式 "YYYY-MM-DD HH:MM:SS"
    end_time: Optional[str] = None,     # 字符串格式 "YYYY-MM-DD HH:MM:SS"
    interval: str = "1m"
) -> Dict[str, Any]:
```

**关键事实**：`start_time` / `end_time` 是**字符串**类型（`"YYYY-MM-DD HH:MM:SS"` 格式），通过 `parse_time_or_default` 函数解析。项目中时间工具为 `get_current_time`（`app/tools/time_tool.py`），返回的也是 `"YYYY-MM-DD HH:MM:SS"` 字符串。**不存在毫秒时间戳入参**。

这导致 AIOps 诊断的输入数据是假的，诊断结论不可信，无法用于真实运维场景。

### 1.2 POC 目标

**用最小成本验证一个核心假设：把 MCP Server 从 mock 切换到真实数据源后，现有的 Agent（LangGraph Plan-Execute-Replan）能否正确理解真实数据并给出合理的诊断结论。**

具体验证三个问题：

1. **数据兼容性**：Prometheus 返回的数据结构，经过 adapter 转换后，Agent 能否正确解析？
2. **诊断有效性**：基于真实 CPU 监控数据，Agent 能否准确识别故障并给出合理建议？
3. **工程可行性**：改造 MCP Server 的成本和风险是否可控？

### 1.3 POC 成功标准

触发一次真实的 CPU 故障后，Agent 自动完成诊断，输出的 Markdown 报告满足：

- ✅ 正确识别"CPU 使用率过高"
- ✅ 正确关联到目标服务（demo-service）
- ✅ 提供至少一条合理的处置建议
- ✅ 全程无报错，数据格式 Agent 能正确解析
- ✅ 诊断耗时 < 5 分钟（含 MCP 重试等待，详见 2.4 节）

### 1.4 明确不做的事（POC 范围排除）

| 排除项 | 理由 |
|--------|------|
| 不接 Loki（日志） | POC 只验证监控数据链路，减少变量 |
| 不接 Alertmanager（告警） | POC 手动触发诊断，不验证告警触发链路 |
| 不改 Agent 编排逻辑 | Planner/Executor/Replanner 代码不变，验证 MCP 改造后 Agent 能否直接使用 |
| 不做鉴权/持久化/审计 | 属于工程加固阶段，POC 不涉及 |
| 不做内存/磁盘/网络诊断 | POC 只做 CPU 一种故障，控制范围 |
| 不追求量化准确率 | POC 定性验收，不建测试集 |

**关于"不改 Agent"的精确界定**：

| 允许修改 | 不允许修改 |
|---------|----------|
| `aiops_service.py` 中 `diagnose()` 方法的**任务描述文本**（引导 Agent 诊断 demo-service） | Planner/Executor/Replanner 的**编排逻辑代码** |
| `aiops-docs/` 知识库文档内容 | `mcp_client.py` 的 MCP 客户端逻辑 |
| `monitor_server.py` 的 MCP 工具实现 | `rag_agent_service.py` 的 RAG Agent 逻辑 |

### 1.5 用户故事

```
作为：运维工程师
我想：在 demo-service 发生 CPU 飙高故障时，通过调用 AIOps API 自动获取诊断报告
以便：快速定位故障根因并获取处置建议，减少人工排查时间

前置条件：
1. demo-service 和 Prometheus 已启动并正常运行
2. 已通过访问 /fault/cpu 触发了 CPU 故障，且等待至少 30 秒让 Prometheus 采集到数据
3. SuperBizAgent FastAPI 服务和改造后的 MCP Server 已启动

触发方式：
POST http://localhost:9900/api/aiops  {"session_id": "poc-test"}

预期结果：
SSE 流式返回诊断过程，最终输出 Markdown 诊断报告
```

---

## 2. 需求边界定义

### 2.1 诊断范围

| 维度 | POC 范围 | 后续扩展 |
|------|---------|---------|
| 故障类型 | CPU 使用率过高（1 种） | 内存、磁盘、网络、服务不可用（5+ 种） |
| 数据源 | Prometheus（1 个） | + Loki + Alertmanager（3 个） |
| 诊断模式 | 手动触发（调用 `/api/aiops`） | 告警自动触发 |
| 输出格式 | Markdown 报告（现有格式） | + 结构化 JSON（供自动修复） |

### 2.2 诊断结论消费者

**消费者**：运维工程师（人）

**输出格式**：Markdown 报告（与现有 `aiops_service.py` 的 `diagnose` 方法输出格式一致）

**理由**：POC 阶段不需要对接自动修复系统。项目现有的诊断报告模板（告警清单 + 根因分析 + 处理方案）已经满足人工阅读需求。

### 2.3 准确率验收标准

POC 采用 **定性验收**（不建量化测试集）：

| 验收项 | 标准 | 验证方式 |
|--------|------|---------|
| 故障识别 | 报告中明确提及"CPU 使用率过高"或等价表述 | 人工检查报告 |
| 服务定位 | 报告中提及目标服务名 `demo-service` | 人工检查报告 |
| 处置建议 | 报告中包含至少 1 条可操作的处置建议（如"重启服务""限制并发""扩容"等） | 人工检查报告 |
| 数据引用 | 报告中引用了真实的 CPU 数值（非编造，与 Prometheus 原始数据一致） | 对比 Prometheus 查询结果 |

### 2.4 延迟要求

| 指标 | 要求 | 理由 |
|------|------|------|
| 单次诊断总耗时 | < 5 分钟 | Plan-Execute-Replan 约 4-6 步，每步含 1 次 LLM 调用（~20s）+ 工具调用（含重试最坏 ~21s），总计约 3-4 分钟，留 1 分钟余量 |
| MCP 工具单次响应 | < 5 秒 | Prometheus 查询通常 < 1 秒 |
| MCP 工具最坏响应（含重试） | < 21 秒 | MCP 客户端有 3 次指数退避重试（1s + 2s + 4s = 7s 等待）+ 每次 5s 超时，最坏 7+5+5+5=22s，取 21s 上界 |
| LLM 单次响应 | < 30 秒 | DashScope qwen-max 正常响应 10-20 秒 |

**注意**：MCP 客户端 `mcp_client.py` 中的 `retry_interceptor` 会在工具调用失败时重试 3 次（指数退避：1s、2s、4s）。如果 Prometheus 持续不可用，单次工具调用的实际耗时可达 ~21 秒。POC 验收时需关注此延迟。

---

## 3. 最小 POC 架构设计

### 3.1 架构总览

```
┌──────────────────────────────────────────────────────────┐
│  demo-service（FastAPI 故障注入微服务）                    │
│  - /api/order     正常业务接口                             │
│  - /fault/cpu     触发 CPU 飙高（死循环）                  │
│  - /fault/stop    停止故障注入                            │
│  - /metrics       Prometheus 指标端点（上报百分比 0-100）  │
│  - /health        健康检查                                │
└────────────────────────┬─────────────────────────────────┘
                         │ /metrics (pull, 15s间隔)
                         ▼
┌──────────────────────────────────────────────────────────┐
│  Prometheus（Docker 容器, 端口 9090）                     │
│  - 采集 demo-service 指标                                 │
│  - 存储 time series 数据                                  │
│  - HTTP API: /api/v1/query_range                         │
│  - 指标名: demo_cpu_usage_percent (Gauge, 百分比 0-100)   │
└────────────────────────┬─────────────────────────────────┘
                         │ HTTP GET (query_range, async)
                         ▼
┌──────────────────────────────────────────────────────────┐
│  monitor_server.py（改造后的 MCP Server, 端口 8004）       │
│  ┌──────────────────────────────────────────────────────┐│
│  │  query_cpu_metrics(service, start, end, interval)   ││
│  │    1. 解析字符串时间 → Unix 秒                        ││
│  │    2. 异步调用 Prometheus API 查真实数据              ││
│  │    3. adapter 转换为 Agent 兼容格式（百分比不转换）    ││
│  │    4. 返回和 mock 相同的数据结构                      ││
│  ├──────────────────────────────────────────────────────┤│
│  │  list_monitored_services()  [新增工具]               ││
│  │    返回当前被监控的服务列表 ["demo-service"]          ││
│  │    → 引导 Agent 知道该诊断哪个服务                    ││
│  └──────────────────────────────────────────────────────┘│
│  其他工具（query_memory_metrics 等）→ 暂时保留 mock       │
└────────────────────────┬─────────────────────────────────┘
                         │ MCP 协议 (streamable-http, :8004)
                         ▼
┌──────────────────────────────────────────────────────────┐
│  SuperBizAgent（编排逻辑不改动）                           │
│  - LangGraph Plan-Execute-Replan                         │
│  - Planner → Executor → Replanner                        │
│  - 调用 list_monitored_services 发现目标服务              │
│  - 调用 query_cpu_metrics 获取真实 CPU 数据               │
│  - 生成 Markdown 诊断报告                                 │
│  [仅修改] aiops_service.py 中的诊断任务描述文本            │
└──────────────────────────────────────────────────────────┘
```

### 3.2 POC 数据流

```
1. 用户访问 http://localhost:8080/fault/cpu
   → demo-service 启动 CPU 死循环，CPU 使用率飙升至 ~95%

2. Prometheus 每 15s 拉取 /metrics
   → 存储 demo-service 的 demo_cpu_usage_percent 指标（值 ≈ 95.0）

3. 用户调用 POST http://localhost:9900/api/aiops
   → Agent 启动 Plan-Execute-Replan 诊断流程
   → 诊断任务描述中已包含 demo-service 的引导信息

4. Planner 制定计划
   → 计划包含"查询被监控服务列表"和"查询 demo-service 的 CPU 监控数据"步骤

5. Executor 执行步骤
   → 调用 list_monitored_services 获取服务列表
   → 调用 query_cpu_metrics(service_name="demo-service", start_time=..., end_time=...)
   → MCP Server 解析字符串时间 → 查 Prometheus API → adapter 转换 → 返回真实 CPU 数据

6. Replanner 评估结果
   → 发现 CPU 使用率过高（max > 80%），决定生成最终报告

7. 输出 Markdown 诊断报告
   → 包含 CPU 过高识别 + 服务定位 + 处置建议
```

### 3.3 Agent 诊断目标服务名引导方案

**问题**：现有 `aiops_service.py` 的 `diagnose()` 方法中，诊断任务描述是通用的"诊断当前系统是否存在告警"，没有指定 demo-service。如果 Planner 不知道该查哪个服务，可能查到 mock 中的 `data-sync-service`，导致 Prometheus 查不到数据。

**解决方案**（双重引导，不改 Agent 编排逻辑）：

1. **新增 MCP 工具 `list_monitored_services`**：返回当前 Prometheus 监控的服务列表 `["demo-service"]`。Planner 可在计划中先查服务列表，再逐个诊断。

2. **修改诊断任务描述文本**：在 `aiops_service.py` 的 `diagnose()` 方法中，将任务描述修改为包含 demo-service 的引导（仅改文本，不改编排逻辑）：

```python
# aiops_service.py diagnose() 方法中修改任务描述
aiops_task = dedent("""诊断当前系统是否存在告警，如果存在告警请详细分析告警原因并生成诊断报告。
                请重点关注 demo-service 服务的 CPU、内存等监控指标。
                ...（后续报告格式要求不变）""")
```

---

## 4. 核心设计：Adapter 层

### 4.1 问题陈述

现有 `monitor_server.py` 的 `query_cpu_metrics` 工具返回的数据结构（Agent 的提示词和逻辑基于此调优）：

```json
{
  "service_name": "data-sync-service",
  "metric_name": "cpu_usage_percent",
  "interval": "1m",
  "data_points": [
    {"timestamp": "10:05", "value": 85.3, "process_id": "pid-12345"},
    {"timestamp": "10:06", "value": 91.2, "process_id": "pid-12345"}
  ],
  "statistics": {
    "avg": 75.2, "max": 96.0, "min": 10.0, "p95": 94.1,
    "spike_detected": true
  },
  "alert_info": {
    "triggered": true, "threshold": 80.0,
    "message": "CPU 使用率持续超过 80% 阈值"
  }
}
```

Prometheus `query_range` API 返回的数据结构（**指标名和值语义已统一**）：

```json
{
  "status": "success",
  "data": {
    "resultType": "matrix",
    "result": [{
      "metric": {"__name__": "demo_cpu_usage_percent", "service": "demo-service"},
      "values": [
        [1708012345, "5.0"],
        [1708012360, "95.0"]
      ]
    }]
  }
}
```

**关键事实**：demo-service 通过 `prometheus_client.Gauge` 直接上报**百分比**值（0-100），Prometheus 存储的 `values` 中的值就是百分比（如 `"95.0"`），**不是比率**。因此 adapter **不需要做 `* 100` 转换**。

**如果不做 adapter，Agent 拿到 Prometheus 原始格式，LLM 可能无法正确解析 values 数组中的 `[timestamp, "string_value"]` 结构，导致诊断失败。**

### 4.2 Adapter 设计

在 MCP Server 内部增加 adapter 函数，将 Prometheus 返回转换为和 mock 完全一致的结构：

```python
# monitor_server.py 内部新增 adapter
from datetime import datetime, timezone, timedelta

# 显式指定时区（避免 Docker 容器时区与宿主机不一致）
SHANGHAI_TZ = timezone(timedelta(hours=8))

def adapt_prometheus_to_agent_format(
    prom_response: dict,
    service_name: str,
    interval: str
) -> dict:
    """将 Prometheus query_range 响应转换为 Agent 兼容格式

    转换规则：
    1. values 中的 [unix_seconds, "string_value"] → {"timestamp": "HH:MM", "value": float}
    2. CPU 值已是百分比(0-100)，直接 float() 转换，不做 * 100
    3. 时间戳使用 Asia/Shanghai 时区转换
    4. 计算 statistics (avg/max/min/p95)
    5. 判断 spike_detected (max > 80)
    6. 生成 alert_info
    """
    result = prom_response.get("data", {}).get("result", [])
    if not result:
        return _empty_response(service_name, interval)

    values = result[0].get("values", [])

    data_points = []
    for ts_seconds, value_str in values:
        # 时间戳：unix秒 → Asia/Shanghai 时区的 "HH:MM" 字符串
        dt = datetime.fromtimestamp(ts_seconds, tz=SHANGHAI_TZ)
        timestamp = dt.strftime("%H:%M")
        # CPU 值：已是百分比(0-100)，直接转换
        cpu_percent = round(float(value_str), 1)
        data_points.append({
            "timestamp": timestamp,
            "value": cpu_percent,
            "process_id": service_name  # POC 阶段用服务名作为 process_id
        })

    # 计算统计信息
    all_values = [dp["value"] for dp in data_points]
    max_val = max(all_values) if all_values else 0
    spike_detected = max_val > 80.0

    return {
        "service_name": service_name,
        "metric_name": "cpu_usage_percent",
        "interval": interval,
        "data_points": data_points,
        "statistics": {
            "avg": round(sum(all_values) / len(all_values), 2) if all_values else 0,
            "max": max_val,
            "min": min(all_values) if all_values else 0,
            "p95": _calc_p95(all_values),
            "spike_detected": spike_detected
        },
        "alert_info": {
            "triggered": spike_detected,
            "threshold": 80.0,
            "message": "CPU 使用率持续超过 80% 阈值" if spike_detected else "CPU 使用率正常"
        }
    }


def _empty_response(service_name: str, interval: str) -> dict:
    """空数据降级返回（结构完整，与正常返回一致）"""
    return {
        "service_name": service_name,
        "metric_name": "cpu_usage_percent",
        "interval": interval,
        "data_points": [],
        "statistics": {
            "avg": 0, "max": 0, "min": 0, "p95": 0,
            "spike_detected": False
        },
        "alert_info": {
            "triggered": False, "threshold": 80.0,
            "message": "该时间范围内无监控数据"
        }
    }


def _error_response(service_name: str, message: str) -> dict:
    """错误降级返回（结构完整，包含错误信息）"""
    return {
        "service_name": service_name,
        "metric_name": "cpu_usage_percent",
        "interval": "1m",
        "data_points": [],
        "statistics": {
            "avg": 0, "max": 0, "min": 0, "p95": 0,
            "spike_detected": False
        },
        "alert_info": {
            "triggered": False, "threshold": 80.0,
            "message": f"监控查询失败: {message}"
        }
    }


def _calc_p95(values: list) -> float:
    """计算 P95 百分位数"""
    if not values:
        return 0
    sorted_vals = sorted(values)
    idx = int(len(sorted_vals) * 0.95)
    if idx >= len(sorted_vals):
        idx = len(sorted_vals) - 1
    return round(sorted_vals[idx], 2)
```

### 4.3 Adapter 设计原则

| 原则 | 说明 |
|------|------|
| **输出格式 100% 兼容 mock** | Agent 的提示词和逻辑零改动 |
| **值语义一致** | demo-service 上报百分比(0-100)，Prometheus 存储百分比，adapter 直接 `float()` 转换，**不做 `* 100`** |
| **时间格式转换** | Prometheus 用 unix 秒 → adapter 转为 Asia/Shanghai 时区的 "HH:MM" 字符串 |
| **统计信息计算** | Prometheus 不返回统计，adapter 自己算 avg/max/min/p95 |
| **空数据返回结构完整** | 空数据时返回所有字段（service_name/metric_name/interval/data_points/statistics/alert_info），与正常返回结构一致 |
| **错误返回结构完整** | 错误时返回完整结构，alert_info.message 包含错误原因 |
| **时区显式指定** | 使用 `timezone(timedelta(hours=8))` 显式指定 Asia/Shanghai，避免容器时区不一致 |

---

## 5. 时间戳处理规范

### 5.1 问题

POC 涉及 3 套时间表示：

| 组件 | 时间格式 | 示例 |
|------|---------|------|
| MCP 工具入参（`start_time` / `end_time`） | 字符串 `"YYYY-MM-DD HH:MM:SS"` | `"2026-08-07 10:05:00"` |
| Prometheus API | Unix 秒（浮点数） | `1708012345.0` |
| 返回给 Agent 的 timestamp 字段 | `"HH:MM"` 字符串 | `"10:05"` |

### 5.2 规范

```
MCP 工具入参（字符串 "YYYY-MM-DD HH:MM:SS"）
    │
    ├─→ 查 Prometheus 时：datetime.strptime → timestamp() → Unix 秒
    │
    └─→ 返回 Agent 时：Prometheus Unix 秒 → datetime.fromtimestamp(tz=SHANGHAI) → "HH:MM" 字符串
```

**所有转换在 MCP Server 内部完成，Agent 永远只接触字符串时间（入参 `"YYYY-MM-DD HH:MM:SS"`，返回 `"HH:MM"`）。**

**注意**：入参为 `Optional[str]`，可能为 `None`。当为 `None` 时，使用默认值（start 默认 1 小时前，end 默认当前时间），与现有 `parse_time_or_default` 逻辑一致。

### 5.3 转换代码

```python
from datetime import datetime, timezone, timedelta

SHANGHAI_TZ = timezone(timedelta(hours=8))

def parse_time_to_unix(time_str: str, default_offset_hours: int = 0) -> float:
    """将字符串时间解析为 Unix 时间戳（秒）

    Args:
        time_str: "YYYY-MM-DD HH:MM:SS" 格式字符串，None 时使用默认时间
        default_offset_hours: 默认时间偏移（小时），0=当前时间，-1=1小时前

    Returns:
        float: Unix 时间戳（秒）
    """
    if time_str:
        dt = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
    else:
        dt = datetime.now() + timedelta(hours=default_offset_hours)
    return dt.timestamp()


# 使用示例
prom_start = parse_time_to_unix(start_time, default_offset_hours=-1)  # 默认1小时前
prom_end = parse_time_to_unix(end_time, default_offset_hours=0)       # 默认当前时间

# Prometheus 返回的 values 中是 Unix 秒，转回 "HH:MM"（显式指定时区）
for ts_seconds, value_str in values:
    dt = datetime.fromtimestamp(ts_seconds, tz=SHANGHAI_TZ)
    timestamp_str = dt.strftime("%H:%M")
```

### 5.4 interval 参数转换

`interval` 参数格式为 `"1m"` / `"5m"` / `"1h"`，需转换为 Prometheus `query_range` 的 `step` 参数。

```python
def parse_interval_to_step(interval: str) -> str:
    """将 interval 参数转换为 Prometheus step 格式

    Args:
        interval: "1m", "5m", "1h" 等

    Returns:
        str: Prometheus step 值，如 "15s", "30s", "60s"
    """
    if not interval:
        return "15s"  # 默认 15 秒

    # 解析数字和单位
    unit = interval[-1].lower()
    try:
        num = int(interval[:-1])
    except ValueError:
        return "15s"  # 解析失败用默认值

    if unit == 'm':
        # 分钟 → 转为秒数
        total_seconds = num * 60
        # 如果 <= 30s 用秒，否则用分钟
        if total_seconds <= 30:
            return f"{total_seconds}s"
        else:
            return f"{num}m"
    elif unit == 'h':
        return f"{num * 60}m"
    elif unit == 's':
        return interval
    else:
        return "15s"  # 未知单位用默认值

# 示例
# "1m" → "15s"  (1分钟间隔，Prometheus step 用 15s 保证有足够数据点)
# "5m" → "5m"
# "1h" → "60m"
```

**设计说明**：当 interval 为 `"1m"` 时，step 设为 `"15s"` 而非 `"60s"`，是因为 Prometheus 的 step 是数据点间隔，15s 间隔能在 1 分钟内产生 4 个数据点，比 1 个数据点更有诊断价值。这与现有 mock 代码中 1 分钟生成 1 个数据点的行为略有不同，但更合理。

---

## 6. 降级处理策略

### 6.1 数据源异常处理

MCP Server 调用 Prometheus API 时可能遇到以下情况：

| 异常场景 | 处理方式 | 返回给 Agent 的内容 |
|---------|---------|-------------------|
| Prometheus 不可用（连接失败） | 不重试（非瞬时错误），返回错误信息 | 完整结构，`alert_info.message` = "监控查询失败: 监控服务不可用" |
| 查询超时（>5s） | 返回超时信息 | 完整结构，`alert_info.message` = "监控查询失败: 监控查询超时（5秒）" |
| 查询返回空数据 | 返回空结构（格式完整） | 完整结构，`data_points=[]`，`alert_info.message` = "该时间范围内无监控数据" |
| 查询语法错误 | 返回错误信息 | 完整结构，`alert_info.message` = "监控查询失败: Prometheus 查询失败" |
| HTTP 非 200 | 返回错误信息 | 完整结构，`alert_info.message` = "监控查询失败: Prometheus 返回 HTTP {status}" |

**所有降级返回都使用 `_error_response()` 或 `_empty_response()` 函数，保证返回结构与正常返回完全一致**（包含 service_name/metric_name/interval/data_points/statistics/alert_info 全部字段）。

### 6.2 设计原则

- **永远不抛异常给 Agent**：MCP 工具返回错误信息，让 Replanner 决定是否跳过该步骤或换工具
- **错误信息语义化**：`alert_info.message` 字段告诉 Agent"为什么失败"，让它能做出决策（如"监控不可用，改查日志"）
- **空数据返回结构完整**：即使没数据，返回结构也和有数据时一致（data_points 为空数组，statistics 全为 0）
- **MCP 客户端重试影响**：`mcp_client.py` 的 `retry_interceptor` 会重试 3 次（1+2+4=7s 等待 + 3×5s 超时 = 最坏 22s）。Prometheus 不可用时，Agent 会等待约 22s 才收到错误返回

---

## 7. 技术实现方案

### 7.1 基础设施：Prometheus 最小化部署

**docker-compose 配置**（新增 `infra/docker-compose.yml`）：

```yaml
services:
  prometheus:
    image: prom/prometheus:v2.51.0
    container_name: aiops-prometheus
    ports:
      - "9090:9090"
    volumes:
      - ./prometheus.yml:/etc/prometheus/prometheus.yml
    command:
      - --config.file=/etc/prometheus/prometheus.yml
      - --storage.tsdb.retention.time=2h  # POC 只保留 2 小时数据
    networks:
      - aiops-net

  demo-service:
    build: ../demo-service
    container_name: aiops-demo-service
    ports:
      - "8080:8080"
    networks:
      - aiops-net

networks:
  aiops-net:
    driver: bridge
```

**Prometheus 采集配置**（`infra/prometheus.yml`）：

```yaml
global:
  scrape_interval: 15s
  evaluation_interval: 15s

scrape_configs:
  - job_name: "demo-service"
    static_configs:
      - targets: ["demo-service:8080"]
        labels:
          service: "demo-service"
```

**选型理由**：
- 只部署 Prometheus + demo-service 两个容器，最小化
- `retention.time=2h` 控制磁盘占用（POC 不需要长期存储）
- `scrape_interval: 15s` 平衡数据密度和资源消耗
- 显式定义 `aiops-net` 网络，确保容器间通信

**demo-service Dockerfile**（`demo-service/Dockerfile`）：

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8080
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
```

### 7.2 故障注入服务：demo-service

**技术栈**：FastAPI + prometheus_client + uvicorn

**核心接口**：

| 路径 | 方法 | 功能 |
|------|------|------|
| `/api/order` | GET | 正常业务接口（模拟订单查询） |
| `/fault/cpu` | GET | 触发 CPU 飙高（启动后台死循环线程） |
| `/fault/stop` | GET | 停止所有故障注入 |
| `/metrics` | GET | Prometheus 指标端点（上报百分比 0-100） |
| `/health` | GET | 健康检查 |

**CPU 故障注入实现**：

```python
import threading
import math
from fastapi import FastAPI
from prometheus_client import Gauge, generate_latest, CONTENT_TYPE_LATEST
from fastapi.responses import Response

app = FastAPI(title="Demo Fault Service")

cpu_fault_active = False
# 指标名: demo_cpu_usage_percent，值为百分比(0-100)
cpu_gauge = Gauge('demo_cpu_usage_percent', 'CPU usage percent of demo service', ['service'])

@app.get("/fault/cpu")
def trigger_cpu_fault():
    """触发 CPU 飙高：启动后台线程执行死循环计算"""
    global cpu_fault_active
    cpu_fault_active = True

    def cpu_burn():
        while cpu_fault_active:
            # 数学运算消耗 CPU
            _ = math.sqrt(123456789.123456789) * math.pi

    # 启动 2 个线程拉满 CPU
    for _ in range(2):
        threading.Thread(target=cpu_burn, daemon=True).start()

    return {"status": "triggered", "message": "CPU 故障已触发"}

@app.get("/fault/stop")
def stop_fault():
    """停止所有故障注入"""
    global cpu_fault_active
    cpu_fault_active = False
    return {"status": "stopped", "message": "故障已停止"}

@app.get("/metrics")
def metrics():
    # 上报当前 CPU 使用率（百分比 0-100）
    # 故障激活时上报 95.0，正常时上报 5.0
    cpu_value = 95.0 if cpu_fault_active else 5.0
    cpu_gauge.labels(service="demo-service").set(cpu_value)
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

@app.get("/health")
def health():
    return {"status": "ok", "cpu_fault_active": cpu_fault_active}

@app.get("/api/order")
def get_order():
    """正常业务接口"""
    return {"order_id": "ORD-001", "status": "completed", "amount": 99.9}
```

**设计说明**：

- **为什么用后台线程而不是阻塞接口**：故障触发后接口要立即返回，CPU 消耗在后台线程持续进行。这样 Prometheus 能持续采集到高 CPU 指标。
- **为什么 CPU 值直接 set 而不是真实采集**：POC 阶段简化，直接上报模拟的 CPU 百分比给 Prometheus。真实场景应该用 `psutil` 采集进程级 CPU。POC 关注的是"Agent 能否处理来自 Prometheus 的数据"，而非"CPU 采集是否精确"。
- **为什么上报百分比(0-100)而非比率(0-1)**：与现有 mock 数据格式一致（mock 的 value 就是百分比），adapter 不需要做 `* 100` 转换，减少出错点。
- **为什么有 `/fault/stop`**：方便反复测试，触发故障后可停止，避免一直烧 CPU。

### 7.3 MCP Server 改造：monitor_server.py

**改造范围**：只改 `query_cpu_metrics` 一个工具 + 新增 `list_monitored_services` 工具，其他工具保留 mock。

**改造后代码**：

```python
import httpx
import re
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional

# 显式指定时区（避免 Docker 容器时区与宿主机不一致）
SHANGHAI_TZ = timezone(timedelta(hours=8))
PROMETHEUS_URL = "http://localhost:9090"

# service_name 参数校验正则（防止 PromQL 注入）
SERVICE_NAME_PATTERN = re.compile(r'^[a-zA-Z0-9_-]+$')


def parse_time_to_unix(time_str: str, default_offset_hours: int = 0) -> float:
    """将字符串时间解析为 Unix 时间戳（秒）"""
    if time_str:
        dt = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
    else:
        dt = datetime.now() + timedelta(hours=default_offset_hours)
    return dt.timestamp()


def parse_interval_to_step(interval: str) -> str:
    """将 interval 参数转换为 Prometheus step 格式"""
    if not interval:
        return "15s"
    unit = interval[-1].lower()
    try:
        num = int(interval[:-1])
    except ValueError:
        return "15s"
    if unit == 'm':
        total_seconds = num * 60
        return f"{total_seconds}s" if total_seconds <= 30 else f"{num}m"
    elif unit == 'h':
        return f"{num * 60}m"
    elif unit == 's':
        return interval
    return "15s"


@mcp.tool()
@log_tool_call
async def query_cpu_metrics(
    service_name: str,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    interval: str = "1m"
) -> Dict[str, Any]:
    """查询服务的 CPU 使用率监控数据。

    Args:
        service_name: 服务名称（必填）
            示例: "demo-service"
        start_time: 开始时间（可选，字符串类型）
            格式: "YYYY-MM-DD HH:MM:SS"
            默认值: 如果不传，默认为当前时间的1小时前
        end_time: 结束时间（可选，字符串类型）
            格式: "YYYY-MM-DD HH:MM:SS"
            默认值: 如果不传，默认为当前时间
        interval: 数据聚合间隔（可选）
            可选值: "1m" (1分钟), "5m" (5分钟), "1h" (1小时)
            默认值: "1m"

    Returns:
        Dict: CPU 监控数据（结构与 mock 版本完全一致）
    """
    # 1. 参数校验（防止 PromQL 注入）
    if not service_name or not SERVICE_NAME_PATTERN.match(service_name):
        return _error_response(service_name or "unknown", "服务名格式无效")

    try:
        # 2. 时间解析：字符串 "YYYY-MM-DD HH:MM:SS" → Unix 秒
        prom_start = parse_time_to_unix(start_time, default_offset_hours=-1)
        prom_end = parse_time_to_unix(end_time, default_offset_hours=0)
        step = parse_interval_to_step(interval)

        # 3. 构造 PromQL 查询（service_name 已校验，安全拼接）
        query = f'demo_cpu_usage_percent{{service="{service_name}"}}'

        # 4. 异步调用 Prometheus API（不阻塞事件循环）
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{PROMETHEUS_URL}/api/v1/query_range",
                params={
                    "query": query,
                    "start": prom_start,
                    "end": prom_end,
                    "step": step
                }
            )

        # 5. 检查 HTTP 状态
        if resp.status_code != 200:
            return _error_response(service_name, f"Prometheus 返回 HTTP {resp.status_code}")

        prom_data = resp.json()

        # 6. 检查查询状态
        if prom_data.get("status") != "success":
            return _error_response(service_name, "Prometheus 查询失败")

        # 7. Adapter 转换
        return adapt_prometheus_to_agent_format(prom_data, service_name, interval)

    except httpx.TimeoutException:
        return _error_response(service_name, "监控查询超时（5秒）")
    except httpx.ConnectError:
        return _error_response(service_name, "监控服务不可用")
    except Exception as e:
        return _error_response(service_name, f"查询异常: {str(e)}")


@mcp.tool()
@log_tool_call
def list_monitored_services() -> Dict[str, Any]:
    """获取当前被 Prometheus 监控的服务列表。

    此工具用于了解系统中有哪些服务正在被监控，便于诊断时选择目标服务。

    Returns:
        Dict: 服务列表
            - total: 服务数量
            - services: 服务名称列表
            - message: 状态消息
    """
    # POC 阶段返回固定的服务列表
    # 后续可改为查询 Prometheus 的 /api/v1/series 或 /api/v1/targets
    services = ["demo-service"]
    return {
        "total": len(services),
        "services": services,
        "message": f"当前有 {len(services)} 个服务正在被监控"
    }
```

**改造要点**：

| 要点 | 说明 |
|------|------|
| 工具签名不变 | `service_name, start_time, end_time, interval` 参数类型和默认值与原代码完全一致 |
| 工具文档不变 | docstring 保持与 mock 版本一致，Agent 的 Planner 看到的工具描述不变 |
| 返回结构不变 | 经 adapter 转换后，返回结构和 mock 完全一致 |
| **改同步为异步** | `def` → `async def`，`httpx.get` → `httpx.AsyncClient`，避免阻塞 MCP Server 事件循环 |
| **时间入参为字符串** | 与现有代码一致，`start_time`/`end_time` 是 `"YYYY-MM-DD HH:MM:SS"` 字符串，非毫秒时间戳 |
| **CPU 值不转换** | demo-service 上报百分比，adapter 直接 `float()` 转换，不做 `* 100` |
| **step 正确转换** | `parse_interval_to_step` 函数正确解析 `"1m"` → `"15s"` |
| **PromQL 注入防护** | `service_name` 用正则校验 `^[a-zA-Z0-9_-]+$}`，防止恶意拼接 |
| **时区显式指定** | adapter 中使用 `SHANGHAI_TZ` 显式指定 Asia/Shanghai 时区 |
| 新增依赖 | `httpx`（项目 `pyproject.toml` 已有 `httpx>=0.26.0`） |
| 配置化 | `PROMETHEUS_URL` 应从 `.env` 读取（POC 阶段先硬编码，后续优化） |

### 7.4 Agent 侧改动

**允许修改的文件**（仅改任务描述文本，不改编排逻辑）：

| 文件 | 改动内容 | 理由 |
|------|---------|------|
| `app/services/aiops_service.py` | `diagnose()` 方法中的任务描述文本加入 demo-service 引导 | 引导 Agent 诊断目标服务，不改 Plan-Execute-Replan 编排逻辑 |
| `aiops-docs/` | 新增/更新文档描述 demo-service 的故障特征 | RAG 检索时为 Planner 提供参考 |

**明确不改的文件**：

| 文件 | 理由 |
|------|------|
| `app/agent/aiops/planner.py` | 计划制定逻辑不变 |
| `app/agent/aiops/executor.py` | 步骤执行逻辑不变 |
| `app/agent/aiops/replanner.py` | 重规划逻辑不变 |
| `app/agent/mcp_client.py` | MCP 客户端不变 |
| `app/services/rag_agent_service.py` | RAG Agent 逻辑不变 |

**这是 POC 的核心验证点**：如果 Agent 编排逻辑零改动就能正确处理真实数据，说明 adapter 设计成功，方案可行。

### 7.5 诊断任务描述修改

在 `aiops_service.py` 的 `diagnose()` 方法中，修改任务描述（仅加一行引导，报告格式要求不变）：

```python
# 修改前
aiops_task = dedent("""诊断当前系统是否存在告警，如果存在告警请详细分析告警原因并生成诊断报告，诊断报告输出格式要求：
                ```...（报告格式不变）```""")

# 修改后（仅增加一行服务引导）
aiops_task = dedent("""诊断当前系统是否存在告警，如果存在告警请详细分析告警原因并生成诊断报告。
                请重点关注 demo-service 服务的 CPU、内存等监控指标。
                诊断报告输出格式要求：
                ```...（报告格式不变）```""")
```

---

## 8. 验收方案

### 8.1 POC 验收流程

```
步骤 1: 启动基础设施
  docker compose -f infra/docker-compose.yml up -d
  → 确认 Prometheus (9090) 和 demo-service (8080) 正常运行
  → 访问 http://localhost:8080/health 确认 demo-service 健康

步骤 2: 启动 Agent 服务
  python mcp_servers/monitor_server.py   # 改造后的 MCP（端口 8004）
  python mcp_servers/cls_server.py       # CLS MCP（端口 8003，保留 mock）
  python -m uvicorn app.main:app --port 9900
  → 确认 http://localhost:9900/health 返回正常

步骤 3: 触发 CPU 故障
  curl http://localhost:8080/fault/cpu
  → 等待 30 秒，让 Prometheus 采集到足够数据点

步骤 4: 验证 Prometheus 数据
  curl "http://localhost:9090/api/v1/query?query=demo_cpu_usage_percent"
  → 确认返回 CPU 值 ≈ 95.0（百分比）

步骤 5: 触发 Agent 诊断
  curl -X POST http://localhost:9900/api/aiops \
    -H "Content-Type: application/json" \
    -d '{"session_id":"poc-test"}' --no-buffer
  → 收集 SSE 事件流

步骤 6: 验收诊断报告
  → 检查报告是否满足 2.3 节的验收标准
```

### 8.2 验收检查清单

| # | 检查项 | 通过标准 | 结果 |
|---|--------|---------|------|
| 1 | Prometheus 采集到数据 | `query` API 返回 CPU ≈ 95.0 | ☐ |
| 2 | MCP 工具返回真实数据 | `query_cpu_metrics` 返回非空 data_points | ☐ |
| 3 | MCP 返回格式兼容 | 返回结构包含 service_name/metric_name/interval/data_points/statistics/alert_info | ☐ |
| 4 | Agent 未报错 | SSE 流中无 error 事件 | ☐ |
| 5 | Agent 查询的服务名正确 | SSE 流中 Agent 调用了 `query_cpu_metrics(service_name="demo-service")` | ☐ |
| 6 | 报告识别 CPU 故障 | 报告文本包含"CPU"相关表述 | ☐ |
| 7 | 报告定位到服务 | 报告文本包含"demo-service" | ☐ |
| 8 | 报告包含处置建议 | 报告包含可操作的建议内容（如重启、限流、扩容等） | ☐ |
| 9 | 报告引用真实数据 | 报告中的 CPU 数值与 Prometheus 一致（≈95.0） | ☐ |
| 10 | 诊断耗时 | < 5 分钟 | ☐ |

### 8.3 失败场景与应对

| 失败场景 | 原因分析 | 应对措施 |
|---------|---------|---------|
| Agent 报"无法获取监控数据" | MCP 查 Prometheus 失败 | 检查容器网络连通性、Prometheus URL |
| Agent 查了错误的服务名 | Planner 未使用 demo-service | 检查诊断任务描述是否包含引导；检查 `list_monitored_services` 工具是否被调用 |
| Agent 报数据格式错误 | adapter 转换有 bug | 检查 adapter 输出格式是否和 mock 一致 |
| 报告未识别 CPU 故障 | CPU 值未超过阈值或提示词问题 | 检查 Prometheus 数据是否为 95.0；调优 Planner 提示词 |
| 报告编造数据 | Agent 未正确使用工具返回的数据 | 检查 Executor 是否正确传递工具结果 |
| 诊断超时 | LLM 响应慢或 Replanner 提前终止 | 检查 Replanner 是否在步骤 < 5 时就 respond；调优提示词 |
| 诊断步骤过少 | Replanner 在步骤 >= 3 时倾向 respond | 检查 Replanner 决策日志；必要时调优提示词 |

---

## 9. 实施计划

### 9.1 任务分解

| 序号 | 任务 | 产出物 | 预估时间 |
|------|------|--------|---------|
| T1 | 编写 demo-service | `demo-service/app.py` + `Dockerfile` + `requirements.txt` | 2h |
| T2 | 编写 infra docker-compose | `infra/docker-compose.yml` + `prometheus.yml` | 0.5h |
| T3 | 启动基础设施并验证 | Prometheus UI 可见 demo-service 指标 | 0.5h |
| T4 | 改造 monitor_server.py | adapter + query_cpu_metrics + list_monitored_services | 2h |
| T5 | 验证 MCP 工具返回格式 | 手动调用工具，对比 mock 格式 | 0.5h |
| T6 | 修改诊断任务描述 + 知识库文档 | `aiops_service.py` 任务描述 + `aiops-docs/` 文档 | 0.5h |
| T7 | 启动 Agent 服务 | FastAPI + MCP Server 运行 | 0.5h |
| T8 | 触发故障 + 运行诊断 | SSE 事件流 + 诊断报告 | 0.5h |
| T9 | 验收检查 | 填写验收清单 | 0.5h |
| **合计** | | | **~7.5h** |

### 9.2 依赖关系

```
T1 (demo-service) ──┐
                     ├──> T3 (启动验证) ──> T4 (改造MCP) ──> T5 (验证格式) ──┐
T2 (docker-compose) ─┘                                                    ├──> T8 (运行诊断) ──> T9 (验收)
                                                          T6 (改任务描述) ──┤
                                                          T7 (启动Agent) ───┘
```

T1 和 T2 可并行。T4 依赖 T3（需要 Prometheus 有数据才能验证 adapter）。T6 可与 T4/T5 并行。T8 依赖 T5、T6、T7。

### 9.3 验证里程碑

| 里程碑 | 标志 | 阶段 |
|--------|------|------|
| M1 | Prometheus UI 可见 demo-service 的 `demo_cpu_usage_percent` 指标（值≈95.0） | 基础设施就绪 |
| M2 | 手动调用 `query_cpu_metrics` 返回格式正确的真实数据（与 mock 结构一致） | MCP 改造成功 |
| M3 | Agent 诊断报告通过验收清单（10 项全部 ☑） | **POC 成功** |

---

## 10. 风险评估

| 风险 | 概率 | 影响 | 应对 |
|------|------|------|------|
| Agent 无法理解真实 CPU 数据格式 | 中 | 高 | adapter 保证格式 100% 兼容 mock；M2 里程碑先验证格式 |
| Agent 查询了错误的服务名 | 中 | 高 | 双重引导：`list_monitored_services` 工具 + 任务描述文本；验收清单第 5 项检查 |
| Replanner 提前终止诊断 | 中 | 高 | Replanner 在步骤 >= 3 时倾向 respond、>= 5 禁止 replan、>= 8 强制终止。如果诊断不充分，需调优提示词或调整 MAX_STEPS |
| MCP 重试导致延迟过高 | 低 | 中 | MCP 客户端 3 次重试（1+2+4=7s）+ 超时（5s×3），最坏 22s。延迟要求已调整为 < 5 分钟 |
| Docker 网络导致容器间无法通信 | 中 | 中 | demo-service 和 Prometheus 放同一 `aiops-net` docker network |
| Prometheus 采集间隔太长导致数据点不足 | 低 | 中 | scrape_interval 设 15s，故障后等 30s 再诊断 |
| LLM 把真实数据当噪声忽略 | 低 | 高 | 检查报告是否引用了真实数值；必要时调优提示词 |
| CPU 故障线程影响宿主机性能 | 低 | 低 | 用 daemon 线程，`/fault/stop` 可随时停止 |
| Docker 容器时区与宿主机不一致 | 低 | 中 | adapter 显式使用 `SHANGHAI_TZ` 时区转换 |
| PromQL 注入（service_name 含特殊字符） | 低 | 中 | 正则校验 `^[a-zA-Z0-9_-]+$`，拒绝非法字符 |

---

## 11. POC 成功后的后续规划

POC 验证通过后，按以下顺序扩展（非本文档范围，仅作规划参考）：

| 阶段 | 内容 | 依赖 POC 的什么 |
|------|------|----------------|
| 扩展 1 | 接入 Loki（日志），改造 cls_server.py | 复用 adapter 模式和时间戳处理规范 |
| 扩展 2 | 接入 Alertmanager，新增 alert_server | 复用降级处理策略和 PromQL 参数校验 |
| 扩展 3 | 扩展故障类型（内存/磁盘/网络） | 复用 demo-service 故障注入框架 |
| 扩展 4 | 工程加固（鉴权/持久化/审计） | 数据链路已验证可行 |
| 扩展 5 | 测试体系（场景集 + 自动评估） | 诊断流程已稳定 |

---

## 附录 A：现有 mock 数据结构参考

### query_cpu_metrics 返回结构（当前 mock）

```json
{
  "service_name": "data-sync-service",
  "metric_name": "cpu_usage_percent",
  "interval": "1m",
  "data_points": [
    {"timestamp": "10:05", "value": 12.5, "process_id": "pid-12345"},
    {"timestamp": "10:06", "value": 21.0, "process_id": "pid-12345"}
  ],
  "statistics": {
    "avg": 65.3, "max": 96.0, "min": 10.5, "p95": 94.1,
    "spike_detected": true
  },
  "alert_info": {
    "triggered": true, "threshold": 80.0,
    "message": "CPU 使用率持续超过 80% 阈值"
  }
}
```

### query_memory_metrics 返回结构（当前 mock，POC 不改）

```json
{
  "service_name": "data-sync-service",
  "metric_name": "memory_usage_percent",
  "interval": "1m",
  "data_points": [
    {"timestamp": "10:05", "value": 31.0, "used_gb": 2.48, "total_gb": 8.0}
  ],
  "statistics": {
    "avg": 58.2, "max": 85.0, "min": 30.0, "p95": 83.5,
    "memory_pressure": true
  },
  "alert_info": {
    "triggered": true, "threshold": 70.0,
    "message": "内存使用率超过 70% 阈值，存在内存压力"
  }
}
```

---

## 附录 B：现有代码关键事实（POC 改造依据）

### monitor_server.py 工具签名（v1.0 审查发现的 P0 问题修正依据）

```python
# 实际代码签名（字符串时间，非毫秒时间戳）
def query_cpu_metrics(
    service_name: str,
    start_time: Optional[str] = None,   # "YYYY-MM-DD HH:MM:SS"
    end_time: Optional[str] = None,     # "YYYY-MM-DD HH:MM:SS"
    interval: str = "1m"
) -> Dict[str, Any]:
```

### 时间工具（app/tools/time_tool.py）

```python
# 工具名为 get_current_time（非 get_current_timestamp）
# 返回 "YYYY-MM-DD HH:MM:SS" 字符串（非毫秒时间戳）
# 使用 Asia/Shanghai 时区
```

### MCP 客户端重试机制（app/agent/mcp_client.py）

```python
# retry_interceptor: 3 次重试，指数退避
# 第1次失败 → 等待 1s → 第2次失败 → 等待 2s → 第3次失败 → 等待 4s → 返回错误
# 加上每次 5s 超时，最坏总耗时: 5+1+5+2+5+4 = 22s
```

### Replanner 终止条件（app/agent/aiops/replanner.py）

```python
MAX_STEPS = 8                    # 步骤 >= 8 强制生成响应
# 步骤 >= 5: 禁止 replan，只能 respond
# 步骤 >= 3: 提示词倾向 respond（"信息足够就响应"）
```

---

## 附录 C：Prometheus API 参考

### query_range（范围查询）

```
GET /api/v1/query_range?query=...&start=...&end=...&step=...
```

**参数**：
- `query`: PromQL 表达式，如 `demo_cpu_usage_percent{service="demo-service"}`
- `start`: 开始时间（Unix 秒，浮点数）
- `end`: 结束时间（Unix 秒，浮点数）
- `step`: 步长，如 `15s`、`1m`、`5m`

**返回示例**（demo-service 上报百分比值）：

```json
{
  "status": "success",
  "data": {
    "resultType": "matrix",
    "result": [{
      "metric": {"__name__": "demo_cpu_usage_percent", "service": "demo-service"},
      "values": [
        [1708012345, "5.0"],
        [1708012360, "95.0"]
      ]
    }]
  }
}
```

**注意**：
- values 中的值是**字符串**类型，需要 `float()` 转换
- 值是**百分比**(0-100)，不是比率(0-1)，adapter 不做 `* 100` 转换
- 时间戳是 **Unix 秒**，adapter 转换时需显式指定 `Asia/Shanghai` 时区

### query（瞬时查询）

```
GET /api/v1/query?query=...
```

用于验收步骤 4 验证 Prometheus 是否采集到数据。
