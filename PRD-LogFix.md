# P0 修复方案：日志查询链路 — 实施方案 PRD

| 字段 | 内容 |
|------|------|
| 文档版本 | v1.0 |
| 创建日期 | 2026-08-07 |
| 项目名称 | 修复 AIOps 诊断日志查询链路 |
| 目标 | 让 Agent 诊断报告中"日志证据"部分能引用真实日志 |

---

## 1. 问题分析

### 1.1 当前现象

Agent 诊断报告中"日志证据"始终为空，报告原文：

> "由于缺少具体的日志搜索结果，无法提供确切的日志条目。"
> "由于缺乏具体的日志内容，我们只能基于已知的监控信息进行初步判断。"

### 1.2 根因定位

通过分析 `cls_server.py` 的 MCP 调用日志（`mcp_cls_err.log`），发现 Agent 调用 `search_log` 时传入的参数有问题：

```json
{
  "topic_id": "application-logs",
  "start_time": 1696509600000,
  "end_time": 1696513200000,
  "query": "{job=\"app-service\"} |= \"error\"",
  "limit": 100
}
```

**3 个问题**：

| # | 问题 | 说明 |
|---|------|------|
| 1 | **job 名错误** | Agent 传 `{job="app-service"}`，Loki 中实际是 `{job="order-service"}` |
| 2 | **时间戳错误** | Agent 传 `1696509600000`（2023 年 10 月），应为当前时间 |
| 3 | **topic_id 虚构** | Agent 传 `"application-logs"`，`search_topic_by_service_name` 返回的是 `"loki-order-service"` |

问题 2 已通过时间兜底逻辑修复（`search_log` 内部检测到时间偏差超过 1 小时时自动改为最近 5 分钟）。问题 1 和 3 仍未解决。

### 1.3 为什么 Agent 会传错参数

Agent 的 Executor 执行步骤时，LLM 根据**工具的 docstring**和**上下文**决定传什么参数。当前 `search_log` 的 docstring 示例中写的是 `topic_id="topic-001"`、`query` 参数说明是"CLS 查询语法"。Agent 不了解 Loki 的 LogQL 语法，所以自己编了一个 job 名。

**根本原因**：MCP 工具的 docstring 没有给 Agent 足够明确的指引，Agent 不知道 job 名必须是 `order-service`。

### 1.4 为什么不靠调优提示词解决

调优 Planner 提示词或知识库文档可以让 Agent "知道"正确的 job 名，但这种方式：
- 依赖 LLM 理解能力，不稳定（有时传对有时传错）
- 需要反复调试提示词，耗时不可控
- 换一个 LLM 可能又要重新调

**更可靠的方案**：在 MCP 代码层面保证无论 Agent 传什么参数，都能查到正确日志。

---

## 2. 解决方案

### 2.1 核心思路

**在 `search_log` 内部强制覆盖 Agent 传的错误参数**：

1. **忽略 `query` 参数中的 job 名**：无论 Agent 传什么 query，都替换为 `{job="order-service"}` + 保留关键词过滤
2. **忽略 `topic_id`**：不依赖 topic_id 做查询（当前实现已如此，但 docstring 仍误导 Agent）
3. **更新 docstring**：让 Agent 知道不需要传精确的 query，工具会自动处理

### 2.2 改动清单

| 文件 | 改动 | 行数 |
|------|------|------|
| `mcp_servers/cls_server.py` | `search_log` 函数：强制覆盖 query 中的 job 名 + 更新 docstring | ~20 行 |
| `mcp_servers/cls_server.py` | `search_topic_by_service_name` 函数：更新 docstring 中的示例 | ~5 行 |

**不改的文件**：
- `order-service/app.py`（不涉及）
- `monitor_server.py`（不涉及）
- `aiops_service.py`（不涉及）
- Agent 编排逻辑（不涉及）

### 2.3 `search_log` 改造详情

#### 2.3.1 LogQL 构造逻辑（核心改动）

当前逻辑（v1.0 审查后已部分修复）：

```python
# 当前：如果 query 包含 {，直接使用
if '{' in query:
    logql = query  # ← 问题：Agent 传 {job="app-service"} 会直接用
```

改造后：

```python
# 改造后：无论 Agent 传什么 query，都强制用 order-service 作为 job
if query and query.strip():
    if 'level:' in query.lower():
        # CLS 语法 "level:ERROR" → 提取级别关键词
        level_part = query.split('level:')[-1].strip().strip('"').strip("'")
        if ' or ' in level_part.lower():
            parts = [p.strip().strip('"').strip("'") for p in level_part.lower().split(' or ')]
            logql = '{job="order-service"}'
            for part in parts:
                logql += f' |= "{part}"'
        else:
            logql = '{job="order-service"} |= "' + level_part + '"'
    elif '{' in query:
        # Agent 传了 LogQL，但 job 名可能不对
        # 提取 |= 后面的关键词，重新构造
        import re
        keywords = re.findall(r'\|=\s*"([^"]+)"', query)
        logql = '{job="order-service"}'
        for kw in keywords:
            logql += f' |= "{kw}"'
        if not keywords:
            # 没有关键词，查全部
            logql = '{job="order-service"}'
    else:
        # 纯关键词，作为文本搜索
        logql = '{job="order-service"} |= "' + query + '"'
else:
    logql = '{job="order-service"}'
```

**关键设计**：无论 Agent 传 `{job="app-service"} |= "error"` 还是 `{job="system"} |= "high_cpu_usage"`，都提取关键词（`"error"`、`"high_cpu_usage"`），然后用正确的 job 名 `order-service` 重新构造 LogQL。

#### 2.3.2 docstring 更新

当前 docstring 误导 Agent 传 `topic_id="topic-001"` 和 `query="level:ERROR"`。更新为：

```python
async def search_log(
    topic_id: str,
    start_time: int,
    end_time: int,
    query: Optional[str] = None,
    limit: int = 100
) -> Dict[str, Any]:
    """搜索 order-service 服务的日志。

    注意：此工具会自动查询 order-service 服务的日志，topic_id 参数不影响查询结果。
    如果需要过滤特定级别的日志，可以在 query 参数中传入级别关键词，如 "ERROR" 或 "WARN"。

    Args:
        topic_id: 主题ID（可传任意值，不影响查询，工具自动查 order-service）
        start_time: 开始时间戳（毫秒，int类型）
            如果时间距当前超过1小时，会自动改为最近5分钟
        end_time: 结束时间戳（毫秒，int类型）
        query: 查询关键词（可选）
            传入 "ERROR" 可过滤错误级别日志
            传入 "WARN" 可过滤警告级别日志
            不传则返回全部日志
        limit: 返回结果数量限制（默认100）

    Returns:
        Dict: 日志查询结果
            - total: 日志条数
            - logs: 日志列表，每条包含 timestamp, level, message
            - message: 查询状态消息
    """
```

**关键变化**：
- 明确告诉 Agent "topic_id 不影响查询"
- 明确告诉 Agent "传 ERROR 或 WARN 即可过滤"
- 不再展示复杂的 LogQL 示例（Agent 不需要知道 LogQL 语法）

### 2.4 `search_topic_by_service_name` docstring 更新

当前 docstring 的示例用 `service_name="data-sync-service"`，应改为 `service_name="order-service"`，并简化示例：

```python
async def search_topic_by_service_name(
    service_name: str,
    region_code: Optional[str] = None,
    fuzzy: bool = True
) -> Dict[str, Any]:
    """搜索日志主题信息。

    此工具返回当前被 Loki 采集日志的服务列表。

    Args:
        service_name: 服务名称
            示例: "order-service"
        region_code: 地区代码（可选，不影响查询）
        fuzzy: 是否模糊匹配（默认 True）

    Returns:
        Dict: 匹配的日志主题列表
    """
```

---

## 3. 验收方案

### 3.1 验收流程

```
步骤 1: 触发压测
  curl -X POST "http://localhost:8080/api/load-test?count=50"
  → 等待 20 秒让 Prometheus 采集 + Loki 入库

步骤 2: 运行诊断
  curl -X POST "http://localhost:9900/api/aiops" -d '{"session_id":"log-test"}'
  → 收集诊断报告

步骤 3: 验收报告中的日志证据
```

### 3.2 验收检查清单

| # | 检查项 | 通过标准 |
|---|--------|---------|
| 1 | Agent 调用了 search_log | MCP 日志中有 search_log 调用记录 |
| 2 | search_log 返回了日志 | 返回 total > 0，logs 非空 |
| 3 | 报告"日志证据"非空 | 报告中引用了具体日志内容（非"无法提供"） |
| 4 | 日志内容是真实的 | 日志内容包含"订单""压测""超时"等业务关键词 |
| 5 | Agent 诊断无报错 | SSE 流无 error 事件 |

### 3.3 失败场景与应对

| 失败场景 | 原因 | 应对 |
|---------|------|------|
| search_log 仍返回空 | Loki 中确实没有日志（Promtail 未采集到） | 检查 Promtail 配置和 Loki labels |
| 日志返回了但报告中仍为空 | Agent 没有正确引用日志内容 | 调优 Replanner 提示词，强调"必须引用日志证据" |
| search_log 报错 | Loki API 不通 | 检查 Loki 容器状态和网络连通性 |

---

## 4. 实施计划

| 序号 | 任务 | 预估时间 |
|------|------|---------|
| T1 | 改造 `search_log` 的 LogQL 构造逻辑 | 0.5h |
| T2 | 更新 `search_log` 和 `search_topic_by_service_name` 的 docstring | 0.3h |
| T3 | 重启 MCP 服务 + 触发压测 + 运行诊断 | 0.5h |
| T4 | 验收检查 | 0.2h |
| **合计** | | **~1.5h** |

---

## 5. 风险评估

| 风险 | 概率 | 影响 | 应对 |
|------|------|------|------|
| Agent 仍传错误参数导致查不到日志 | 低 | 高 | LogQL 强制覆盖 job 名；时间兜底已实现 |
| Loki 中没有日志（Promtail 未采集） | 低 | 高 | 压测后等 20 秒再诊断；验收前先手动查 Loki 确认有数据 |
| Agent 拿到日志但不引用到报告中 | 中 | 中 | 调优 Replanner 提示词（本次不做，作为后续迭代） |
| 关键词提取正则有 bug | 低 | 低 | 用简单字符串操作替代正则 |

---

## 6. 不做的事

| 排除项 | 理由 |
|--------|------|
| 不改 Agent 编排逻辑 | adapter 思路不变 |
| 不改 Planner 提示词 | 依赖 LLM 理解能力，不稳定；代码层面保证更可靠 |
| 不改 order-service | 日志输出已正常，问题在查询侧 |
| 不改 monitor_server | 日志问题与监控无关 |
