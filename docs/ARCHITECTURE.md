# SuperBizAgent 架构文档

> 本文档基于对项目全部代码的完整阅读，记录当前项目架构、核心模块逻辑、数据流和关键设计决策。
> 创建日期：2026-08-07

---

## 1. 项目概述

### 1.1 是什么

SuperBizAgent 是一个基于 LangChain + LangGraph 构建的智能 AIOps 运维诊断系统。它通过 **Plan-Execute-Replan** 工作流模式，自动调用监控工具（查 Prometheus）和日志工具（查 Loki），对微服务故障进行根因分析，并输出结构化的 Markdown 诊断报告。

### 1.2 解决什么问题

传统运维中，当服务出现 CPU 飙高、请求超时等故障时，工程师需要手动查监控面板、翻日志、关联分析，耗时 10-30 分钟。本系统将这个过程自动化：

- **自动制定诊断计划**：Planner 根据 RAG 知识库经验，将"诊断故障"拆解为可执行步骤
- **自动执行工具调用**：Executor 调用 MCP 工具查 Prometheus/Loki 获取真实数据
- **自动重规划**：Replanner 评估已执行结果，决定继续、调整计划或生成报告
- **自动生成报告**：综合监控指标和日志证据，输出包含根因分析和处置建议的 Markdown 报告

### 1.3 核心验证场景

通过 `order-service`（一个有真实 PostgreSQL 数据库的订单微服务）制造故障：

1. 调用 `POST /api/load-test?count=100` 触发 100 个并发下单
2. 每个请求执行 `SELECT COUNT(*) FROM orders`（10 万条全表扫描）+ 持有连接 3 秒
3. 连接池（`max_size=3`）迅速耗尽 → CPU 飙高（85%+）→ ERROR 日志输出（"数据库连接获取超时"）
4. Prometheus 采集 CPU/内存指标，Loki 采集业务日志
5. Agent 自动诊断，通过监控+日志关联分析定位到"连接池耗尽"根因

---

## 2. 整体架构图

```
┌─────────────────────────────────────────────────────────────────────┐
│                        用户 / 前端 Web UI                             │
│                   (static/index.html, SSE 流式接收)                   │
└──────────────┬──────────────────────────────────┬───────────────────┘
               │ POST /api/aiops                  │ POST /api/chat_stream
               ▼                                   ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     SuperBizAgent (FastAPI :9900)                    │
│                                                                      │
│  ┌─────────────┐  ┌─────────────┐  ┌──────────┐  ┌────────────┐    │
│  │ api/aiops   │  │ api/chat    │  │ api/file │  │ api/health │    │
│  │ SSE 流式    │  │ SSE 流式    │  │ 上传索引 │  │ 健康检查   │    │
│  └──────┬──────┘  └──────┬──────┘  └────┬─────┘  └────────────┘    │
│         ▼                ▼               ▼                           │
│  ┌──────────────┐ ┌──────────────┐ ┌──────────────────┐            │
│  │aiops_service │ │rag_agent_    │ │vector_index_     │            │
│  │(Plan-Execute │ │service       │ │service           │            │
│  │ -Replan)     │ │(create_agent)│ │(分块→向量化→存储)│            │
│  └──────┬───────┘ └──────┬───────┘ └────────┬─────────┘            │
│         │                │                   │                      │
│         ▼                ▼                   ▼                      │
│  ┌──────────────────────────────────────────────────┐              │
│  │              LangChain 工具层                      │              │
│  │  ┌────────────┐ ┌──────────────┐ ┌────────────┐  │              │
│  │  │get_current │ │retrieve_     │ │ MCP 工具    │  │              │
│  │  │_time       │ │knowledge     │ │(动态加载)   │  │              │
│  │  └────────────┘ └──────┬───────┘ └─────┬──────┘  │              │
│  └────────────────────────┼────────────────┼─────────┘              │
│                           │                │                         │
│         ┌─────────────────┘                │                         │
│         ▼                                   ▼                         │
│  ┌──────────────┐              ┌──────────────────────┐             │
│  │  Milvus      │              │  MCP Client          │             │
│  │  (:19530)    │              │  (MultiServer)       │             │
│  │  collection: │              │  + retry_interceptor │             │
│  │  "biz"       │              │  (3次指数退避重试)    │             │
│  └──────────────┘              └──────┬───────┬───────┘             │
│                                       │       │                      │
└───────────────────────────────────────┼───────┼─────────────────────┘
                                        │       │
                    ┌───────────────────┘       └──────────────────┐
                    ▼                                              ▼
┌───────────────────────────────────┐         ┌─────────────────────────────────┐
│  Monitor MCP Server (:8004)       │         │  CLS MCP Server (:8003)         │
│  (mcp_servers/monitor_server.py)  │         │  (mcp_servers/cls_server.py)    │
│                                   │         │                                 │
│  工具:                             │         │  工具:                           │
│  - query_cpu_metrics (async)      │         │  - search_log (async)           │
│    → 查 Prometheus API            │         │    → 查 Loki API                │
│  - query_memory_metrics (async)   │         │  - search_topic_by_service_name │
│    → 查 Prometheus API            │         │    → 查 Loki labels API         │
│  - list_monitored_services        │         │  - get_current_timestamp        │
│    → 返回 ["order-service"]       │         │  - get_region_code_by_name(mock)│
│                                   │         │  - get_topic_info_by_name(mock) │
│  adapter 层:                       │         │                                 │
│  adapt_prometheus_to_agent_format │         │  adapter 层:                     │
│  adapt_prometheus_memory_to_*     │         │  _adapt_loki_to_agent_format    │
│  (Prometheus格式→Agent兼容格式)    │         │  (Loki格式→Agent兼容格式)        │
└──────────────┬────────────────────┘         └──────────────┬──────────────────┘
               │                                              │
               ▼                                              ▼
┌───────────────────────────────────┐         ┌─────────────────────────────────┐
│  Prometheus (:9090)               │         │  Loki (:3100)                   │
│  采集 order-service /metrics      │         │  存储 order-service 日志         │
│  指标:                             │         │  Promtail 采集 Docker stdout     │
│  - order_cpu_usage_percent        │         │  label: job=order-service        │
│  - order_memory_usage_percent     │         └─────────────────────────────────┘
└───────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│                     infra/docker-compose.yml                        │
│                                                                     │
│  ┌────────────┐  ┌──────────────┐  ┌────────────┐  ┌───────────┐  │
│  │ PostgreSQL │  │ order-service│  │ Prometheus │  │   Loki    │  │
│  │  (:5432)   │  │   (:8080)    │  │  (:9090)   │  │  (:3100)  │  │
│  │ 10万条订单 │  │ FastAPI+     │  │ 采集指标    │  │ 存储日志  │  │
│  │            │  │ asyncpg      │  │             │  │           │  │
│  └─────┬──────┘  └──────┬───────┘  └──────┬─────┘  └─────┬─────┘  │
│        │                │                  │              │        │
│        └────────────────┘                  │              │        │
│                                           │         ┌─────┴─────┐  │
│                                           │         │ Promtail  │  │
│                                           │         │ 采集stdout│  │
│                                           │         └───────────┘  │
│                                                                     │
│  网络: aiops-net (bridge)                                           │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│                     Milvus Docker (vector-database.yml)             │
│  ┌────────────┐  ┌────────────┐  ┌────────────┐  ┌───────────┐    │
│  │ standalone │  │   etcd     │  │   minio    │  │   attu    │    │
│  │  (:19530)  │  │            │  │ (:9001)    │  │  (:8000)  │    │
│  └────────────┘  └────────────┘  └────────────┘  └───────────┘    │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 3. 技术栈

| 层次 | 技术 | 版本/说明 |
|------|------|----------|
| Web 框架 | FastAPI + uvicorn | Python 3.13, 端口 9900 |
| LLM 编排 | LangChain + LangGraph | create_agent + StateGraph |
| LLM 模型 | ChatQwen (通义千问 qwen-max) | DashScope API, temperature=0 |
| 向量数据库 | Milvus v2.5.10 | collection "biz", 1024 维, IVF_FLAT |
| 监控 | Prometheus v2.51.0 | 采集 order-service /metrics, 15s 间隔 |
| 日志 | Loki v2.9.0 + Promtail v2.9.0 | 采集 Docker stdout 日志 |
| 订单微服务 | FastAPI + asyncpg + PostgreSQL 16 | 端口 8080, 连接池 max_size=3 |
| MCP 协议 | FastMCP 3.4.5 + langchain-mcp-adapters | streamable-http transport |
| 日志库 | Loguru (主项目) + Python logging JSON (order-service) | |
| 配置 | Pydantic Settings + .env | |

---

## 4. 目录结构说明

```
super_biz_agent_py/
├── app/                              # AIOps Agent 主应用
│   ├── main.py                       # FastAPI 入口, lifespan 管理 Milvus 连接
│   ├── config.py                     # Pydantic Settings, 从 .env 读配置
│   ├── api/                          # API 路由层
│   │   ├── aiops.py                  # POST /api/aiops (SSE 流式诊断)
│   │   ├── chat.py                   # POST /api/chat, /api/chat_stream
│   │   ├── file.py                   # POST /api/upload (文档上传+向量化)
│   │   └── health.py                 # GET /health
│   ├── services/                     # 业务服务层
│   │   ├── aiops_service.py          # Plan-Execute-Replan 工作流
│   │   ├── rag_agent_service.py      # RAG Agent (create_agent + MemorySaver)
│   │   ├── vector_store_manager.py   # Milvus VectorStore 封装
│   │   ├── vector_index_service.py   # 文档分块+向量化+存储
│   │   ├── vector_embedding_service.py # DashScope text-embedding-v4
│   │   ├── vector_search_service.py  # 向量检索
│   │   └── document_splitter_service.py # Markdown 分块
│   ├── agent/                        # Agent 模块
│   │   ├── mcp_client.py             # MCP 客户端 (单例+重试拦截器)
│   │   └── aiops/                    # AIOps 核心逻辑
│   │       ├── state.py              # PlanExecuteState (TypedDict)
│   │       ├── planner.py            # 计划制定器 (查知识库+生成步骤)
│   │       ├── executor.py           # 步骤执行器 (ToolNode 自动调工具)
│   │       ├── replanner.py          # 重规划器 (continue/replan/respond)
│   │       └── utils.py              # 工具描述格式化
│   ├── tools/                        # LangChain 工具
│   │   ├── knowledge_tool.py         # retrieve_knowledge (查 Milvus)
│   │   └── time_tool.py              # get_current_time (返回时间字符串)
│   ├── core/                         # 核心组件
│   │   ├── llm_factory.py            # ChatOpenAI 工厂 (DashScope 兼容)
│   │   └── milvus_client.py          # Milvus 连接管理+collection 创建
│   ├── models/                       # 数据模型
│   │   ├── request.py                # ChatRequest, ClearRequest
│   │   ├── response.py               # ApiResponse, SessionInfoResponse
│   │   ├── aiops.py                  # AIOpsRequest
│   │   └── document.py               # 文档模型
│   └── utils/
│       └── logger.py                 # Loguru 日志配置
├── mcp_servers/                      # MCP Server (工具提供方)
│   ├── monitor_server.py             # 监控 MCP (查 Prometheus)
│   ├── cls_server.py                 # 日志 MCP (查 Loki)
│   └── README.md
├── order-service/                    # 订单微服务 (故障源)
│   ├── app.py                        # FastAPI + asyncpg + 故障注入
│   ├── Dockerfile
│   └── requirements.txt
├── infra/                            # 基础设施
│   ├── docker-compose.yml            # PostgreSQL+order-service+Prometheus+Loki+Promtail
│   ├── prometheus.yml                # 采集配置
│   ├── loki-config.yml               # Loki 配置
│   ├── promtail-config.yml           # Promtail 采集 Docker 日志
│   └── init.sql                      # 建表 + 10万条订单数据
├── demo-service/                     # 旧 POC 故障服务 (已弃用,保留历史)
├── aiops-docs/                       # 运维知识库 (5个 Markdown 文档)
├── static/                           # Web 前端
│   ├── index.html
│   ├── app.js
│   └── styles.css
├── docs/                             # 文档目录
│   ├── ARCHITECTURE.md               # 本文档
│   └── diagnosis/                    # 诊断报告输出存档
├── PRD-AIOps-POC.md                  # POC PRD v2.0
├── PRD-OrderService.md               # OrderService PRD v2.0
├── PRD-LogFix.md                     # 日志修复 PRD v1.0
├── vector-database.yml               # Milvus Docker Compose
├── Makefile                          # Linux/macOS 管理命令
├── start-windows.bat                 # Windows 启动脚本
├── stop-windows.bat                  # Windows 停止脚本
├── pyproject.toml                    # 项目配置
└── .env                              # 环境变量 (DASHSCOPE_API_KEY 等)
```

---

## 5. 核心模块详解

### 5.1 AIOps 诊断流程 (app/services/aiops_service.py + app/agent/aiops/)

#### 5.1.1 工作流架构

LangGraph StateGraph 三个节点循环：

```
planner → executor → replanner → (executor | END)
```

#### 5.1.2 状态定义 (state.py)

```python
class PlanExecuteState(TypedDict):
    input: str              # 用户输入（诊断任务描述）
    plan: List[str]         # 执行计划（步骤列表）
    past_steps: Annotated[List[tuple], operator.add]  # 已执行步骤+结果（追加式）
    response: str           # 最终响应/报告
```

#### 5.1.3 Planner (planner.py)

**职责**：将诊断任务拆解为可执行步骤

**流程**：
1. 调用 `retrieve_knowledge` 查 Milvus 知识库，获取相关经验文档
2. 调用 `get_mcp_client_with_retry()` 获取 MCP 工具列表
3. 用 `format_tools_description()` 格式化工具描述
4. 用 `ChatQwen(temperature=0).with_structured_output(Plan)` 生成计划
5. 返回 `{"plan": plan_steps}`

**关键设计**：
- 先查知识库再制定计划，让经验文档指导步骤生成
- 使用 `with_structured_output(Plan)` 强制 LLM 输出 `{"steps": [...]}` 格式
- 异常时返回默认计划 `["收集相关信息", "分析数据", "生成报告"]`

#### 5.1.4 Executor (executor.py)

**职责**：执行计划中的下一个步骤

**流程**：
1. 取出 `plan[0]`（第一个步骤）
2. 合并本地工具 `[get_current_time, retrieve_knowledge]` + MCP 工具
3. 创建 `ChatQwen(temperature=0).bind_tools(all_tools)`
4. 创建 `ToolNode(all_tools)` 自动执行工具调用
5. LLM 决定调哪些工具 → ToolNode 执行 → LLM 生成结果
6. 返回 `{"plan": plan[1:], "past_steps": [(task, result)]}`

**关键设计**：
- `ToolNode` 自动处理工具调用，不需要手动解析 tool_calls
- `past_steps` 用 `operator.add` 追加式更新，不覆盖历史

#### 5.1.5 Replanner (replanner.py)

**职责**：评估已执行结果，决定下一步行动

**三种决策**：
- `continue`：当前计划合理，继续执行下一步
- `replan`：计划需要调整，提供新步骤替换剩余计划
- `respond`：信息充足，生成最终报告

**防护机制**：
- `MAX_STEPS = 8`：超过 8 步强制生成报告
- 步骤 >= 5：禁止 replan，只能 respond
- 步骤 >= 3：提示词倾向 respond（"信息足够就响应"）
- replan 时新步骤数不能超过剩余步骤数

**关键设计**：
- 提示词中明确优先级："优先结束 > 保持不变 > 调整计划"
- 用 `with_structured_output(Act)` 强制输出 `{"action": "...", "new_steps": [...]}`

#### 5.1.6 诊断任务描述 (aiops_service.py diagnose 方法)

当前任务描述（硬编码在 `diagnose()` 方法中）：

```python
aiops_task = """诊断当前系统是否存在告警，如果存在告警请详细分析告警原因并生成诊断报告。
请重点关注 order-service 服务的 CPU、内存等监控指标。

重要要求：
- 必须使用 search_log 工具查询 order-service 的日志（至少查询一次）
- 诊断报告中的"日志证据"部分必须引用 search_log 返回的真实日志内容
- 如果 search_log 返回空结果，在报告中如实说明

诊断报告输出格式要求：
```...（Markdown 报告模板）```"""
```

#### 5.1.7 流式输出 (aiops_service.py execute 方法)

使用 `graph.astream(stream_mode="updates")` 流式输出：
- planner 节点输出 → `type: "plan"` 事件
- executor 节点输出 → `type: "step_complete"` 事件
- replanner 节点输出 → `type: "status"` 或 `type: "report"` 事件
- 最终 → `type: "complete"` 事件

---

### 5.2 RAG Agent 服务 (app/services/rag_agent_service.py)

**职责**：多轮对话 + 知识库问答

**架构**：
- 使用 `langchain.agents.create_agent` 创建 Agent
- LLM: `ChatQwen(model="qwen-max", temperature=0.7, streaming=True)`
- 工具: `[retrieve_knowledge, get_current_time]` + MCP 工具
- 会话管理: `MemorySaver` checkpointer, 按 `session_id` (thread_id) 隔离
- 消息修剪: `trim_messages_middleware` 保留系统消息 + 最近 6 条

**输入输出**：
- 输入: `{"question": "...", "session_id": "..."}`
- 输出: 流式 `{"type": "content", "data": "..."}` 或一次性返回完整答案

---

### 5.3 MCP Server 层

#### 5.3.1 Monitor MCP Server (mcp_servers/monitor_server.py)

**职责**：提供监控数据查询工具，查 Prometheus

**工具列表**：

| 工具名 | 类型 | 作用 |
|--------|------|------|
| `query_cpu_metrics` | async | 查 Prometheus `order_cpu_usage_percent` 指标 |
| `query_memory_metrics` | async | 查 Prometheus `order_memory_usage_percent` 指标 |
| `list_monitored_services` | sync | 返回 `["order-service"]` |

**工具签名（以 query_cpu_metrics 为例）**：
```python
async def query_cpu_metrics(
    service_name: str,                          # "order-service"
    start_time: Optional[str] = None,           # "YYYY-MM-DD HH:MM:SS" 字符串
    end_time: Optional[str] = None,             # 同上
    interval: str = "1m"                        # "1m", "5m", "1h"
) -> Dict[str, Any]
```

**adapter 层设计**：

`adapt_prometheus_to_agent_format()` 函数将 Prometheus 返回格式转换为 Agent 兼容格式：

| Prometheus 返回 | Agent 兼容格式 |
|----------------|---------------|
| `values: [[unix_seconds, "string_value"]]` | `data_points: [{"timestamp": "HH:MM", "value": float}]` |
| 无统计信息 | `statistics: {avg, max, min, p95, spike_detected}` |
| 无告警信息 | `alert_info: {triggered, threshold, message}` |

**关键设计**：
- **空结果重试**：如果 Prometheus 返回空数据，自动用最近 5 分钟重新查询
- **参数校验**：`SERVICE_NAME_PATTERN = re.compile(r'^[a-zA-Z0-9_-]+$')` 防止 PromQL 注入
- **时间转换**：字符串 `"YYYY-MM-DD HH:MM:SS"` → `parse_time_to_unix()` → Unix 秒
- **时区**：`SHANGHAI_TZ = timezone(timedelta(hours=8))` 显式指定
- **降级处理**：`_empty_response()` 和 `_error_response()` 返回完整结构

**log_tool_call 装饰器**：
- 同时支持同步和异步函数（`asyncio.iscoroutinefunction` 检测）
- 记录方法名、参数、返回状态到日志

#### 5.3.2 CLS MCP Server (mcp_servers/cls_server.py)

**职责**：提供日志查询工具，查 Loki

**工具列表**：

| 工具名 | 类型 | 作用 |
|--------|------|------|
| `search_log` | async | 查 Loki 日志（query_range API） |
| `search_topic_by_service_name` | async | 查 Loki labels API 返回服务列表 |
| `get_current_timestamp` | sync | 返回当前毫秒时间戳 |
| `get_region_code_by_name` | sync | mock（返回地区代码） |
| `get_topic_info_by_name` | sync | mock（返回主题信息） |

**search_log 工具签名**：
```python
async def search_log(
    topic_id: str,                              # 可传任意值，不影响查询
    start_time: Optional[str] = None,           # 毫秒时间戳字符串（不传则自动查最近5分钟）
    end_time: Optional[str] = None,             # 同上
    query: Optional[str] = None,                # "ERROR", "WARN", "ERROR OR WARN"
    limit: int = 100
) -> Dict[str, Any]
```

**LogQL 构造逻辑**（核心设计）：

无论 Agent 传什么 query，都强制用 `order-service` 作为 job 名：

| Agent 传入的 query | 构造的 LogQL | 说明 |
|---------------------|-------------|------|
| `"ERROR"` | `{job="order-service"} |= "ERROR"` | 单关键词包含 |
| `"ERROR OR WARN"` | `{job="order-service"} \|~ "ERROR\|WARN"` | 多关键词正则 OR |
| `"level:ERROR"` | `{job="order-service"} |= "ERROR"` | CLS 语法转换 |
| `"level:ERROR OR level:WARN"` | `{job="order-service"} \|~ "ERROR\|WARN"` | 多级别正则 OR |
| `'{job="app-service"} |= "error"'` | `{job="order-service"} |= "error"` | 提取关键词重新构造 |
| `None` 或空 | `{job="order-service"}` | 查全部日志 |

**时间兜底逻辑**：
1. 尝试 `int(start_time)` / `int(end_time)`，失败则用最近 5 分钟
2. 如果值 <= 0，用最近 5 分钟
3. 如果 end_time 距当前超过 1 小时，用最近 5 分钟
4. 毫秒 → 纳秒：`* 1_000_000`（Loki 用纳秒时间戳）

**adapter 层**：
`_adapt_loki_to_agent_format()` 将 Loki 的 streams 格式转换为日志列表：

| Loki 返回 | Agent 兼容格式 |
|-----------|---------------|
| `streams: [{stream: {job:...}, values: [[ns_ts, log_line]]}]` | `logs: [{timestamp: "HH:MM:SS", level: "ERROR", message: "..."}]` |
| 原始日志行（可能 JSON） | 解析 JSON 提取 level/message，非 JSON 则文本匹配 |

---

### 5.4 order-service 订单微服务 (order-service/app.py)

**职责**：提供真实的订单 CRUD 业务 + 故障注入

#### 5.4.1 技术栈
- FastAPI + uvicorn (端口 8080)
- asyncpg (PostgreSQL 异步驱动, 连接池 max_size=3, timeout=2s)
- prometheus_client (指标上报)
- Python logging (JSON 格式输出到 stdout)

#### 5.4.2 数据库
- PostgreSQL 16, 2 张表: products (10条) + orders (10万条)
- orders 表故意不加索引，让 `SELECT COUNT(*)` 全表扫描

#### 5.4.3 业务接口

| 接口 | 功能 |
|------|------|
| `GET /api/products` | 商品列表 |
| `GET /api/products/{id}` | 商品详情 |
| `POST /api/orders` | 创建订单（含慢 SQL: COUNT(*) + sleep 3s） |
| `GET /api/orders` | 订单列表（分页） |
| `GET /api/orders/{id}` | 订单详情 |

#### 5.4.4 故障注入接口

`POST /api/load-test?count=100`：批量并发下单

**故障触发机制**：
1. 100 个协程并发调用 `single_order()`
2. 每个协程用 `asyncio.wait_for(db_pool.acquire(), timeout=2.0)` 获取连接
3. 前 3 个请求拿到连接，各持有 3 秒（`await asyncio.sleep(3)`）
4. 其余 97 个请求等待 2 秒后超时 → `asyncio.TimeoutError`
5. 超时请求输出 ERROR 日志: "数据库连接获取超时: 等待2.00秒后超时，连接池已耗尽"

**CPU/内存指标**：
- 压测激活时 CPU 上报 85%±3，内存 75%±3（持续 10 分钟）
- 非压测时 CPU 5%±1，内存 10%±1
- 说明：Docker 容器内 psutil 采集精度有限，用状态驱动模拟

#### 5.4.5 日志格式

JSON 结构化输出到 stdout：
```json
{"level": "ERROR", "service": "order-service", "message": "数据库连接获取超时: 等待2.00秒后超时，连接池已耗尽", "timestamp": "2026-08-07T05:48:06.092221"}
```

---

### 5.5 基础设施 (infra/)

#### 5.5.1 docker-compose.yml

5 个服务在同一 `aiops-net` 网络：

| 服务 | 镜像 | 端口 | 作用 |
|------|------|------|------|
| postgres | postgres:16-alpine | 5432 | 数据库，自动执行 init.sql |
| order-service | 自构建 | 8080 | 订单微服务, mem_limit=512m |
| prometheus | prom/prometheus:v2.51.0 | 9090 | 监控采集, retention=2h |
| loki | grafana/loki:2.9.0 | 3100 | 日志存储 |
| promtail | grafana/promtail:2.9.0 | - | 日志采集, 读取 Docker 容器日志 |

#### 5.5.2 Prometheus 采集

```yaml
scrape_interval: 15s
scrape_configs:
  - job_name: "order-service"
    metrics_path: /metrics
    targets: ["order-service:8080"]
```

#### 5.5.3 Promtail 采集

通过 Docker SD 自动发现 `aiops-order-service` 容器，读取其 stdout 日志，打标签 `job=order-service`，推送到 Loki。

#### 5.5.4 Milvus (vector-database.yml)

独立 Docker Compose，4 个容器：
- milvus-standalone (:19530) - 向量数据库
- etcd - 元数据存储
- minio (:9001) - 对象存储
- attu (:8000) - Web UI

collection "biz": 1024 维, IVF_FLAT 索引, L2 距离

---

### 5.6 MCP 客户端 (app/agent/mcp_client.py)

**职责**：管理 MCP 客户端单例，连接两个 MCP Server

**配置**（从 config.py 读取）：
```python
mcp_servers = {
    "cls": {"transport": "streamable-http", "url": "http://localhost:8003/mcp"},
    "monitor": {"transport": "streamable-http", "url": "http://localhost:8004/mcp"}
}
```

**重试拦截器** (`retry_interceptor`)：
- 3 次重试，指数退避（1s, 2s, 4s）
- 失败时返回 `CallToolResult(isError=True)` 而非抛异常
- 让 Agent 的 Replanner 决定是否跳过或换工具

---

## 6. 数据流（从压测触发到诊断报告输出的完整链路）

```
1. 用户调用 POST http://localhost:8080/api/load-test?count=100
   → order-service 启动 100 个并发 single_order() 协程
   → 前 3 个获取连接成功, 持有 3 秒
   → 其余 97 个等待 2 秒后超时
   → 输出 ERROR 日志: "数据库连接获取超时: 等待2.00秒后超时，连接池已耗尽"
   → CPU 指标上报 85%, 内存上报 75%

2. Prometheus 每 15s 拉取 /metrics
   → 存储 order_cpu_usage_percent = 85
   → 存储 order_memory_usage_percent = 75

3. Promtail 采集 order-service stdout 日志
   → 打标签 job=order-service
   → 推送到 Loki 存储

4. 用户调用 POST http://localhost:9900/api/aiops {"session_id":"xxx"}
   → aiops_service.diagnose() 启动 Plan-Execute-Replan

5. Planner 制定计划 (6-7 步):
   → 查 Milvus 知识库获取经验文档
   → 生成: [get_time, list_services, query_cpu/mem, search_log, 分析, 报告]

6. Executor 逐步执行:
   步骤1: get_current_time → "2026-08-07 13:55:15"
   步骤2: list_monitored_services → ["order-service"]
   步骤3: query_cpu_metrics(service="order-service") 
           → MCP 查 Prometheus → adapter 转换
           → 返回 {max: 88.0, avg: 36.36, p95: 87.4, spike_detected: true}
           query_memory_metrics(service="order-service")
           → 返回 {max: 77.4, avg: 35.35, p95: 76.7, memory_pressure: true}
   步骤4: search_log(topic_id="...", start_time=..., end_time=..., query="ERROR OR WARN")
           → MCP 查 Loki → adapter 转换
           → 返回 {logs: [{timestamp:"13:54:55", level:"ERROR", message:"数据库连接获取超时..."}]}

7. Replanner 评估: 信息充足 → respond

8. 生成 Markdown 诊断报告:
   - 活跃告警: CPU 88.0% + 内存 77.4%
   - 日志证据: "数据库连接获取超时的问题"
   - 根因结论: "资源争用问题""数据库响应缓慢"
   - 处置建议: 优化代码、增加资源、优化数据库查询

9. SSE 流式输出 → 前端展示
```

---

## 7. 关键设计决策

### 7.1 adapter 层设计

**问题**：Prometheus/Loki 返回的数据格式与 Agent 提示词期望的格式不同。

**方案**：在 MCP Server 内部加 adapter 函数，将外部数据源格式转换为 Agent 兼容格式。

**效果**：Agent 编排逻辑（planner/executor/replanner）零改动就接入了真实数据源。

### 7.2 参数兜底设计

**问题**：Agent (LLM) 传入的参数可能不正确（错误的时间、错误的 job 名、模板字符串而非实际值）。

**方案**：在 MCP 工具内部对每个参数做兜底处理：
- 时间参数：`Optional[str]` 类型，无法解析则用最近 5 分钟
- job 名：强制覆盖为 `order-service`
- query 参数：拆分 OR 关键词，用正则 `|~` 构造 OR 查询

### 7.3 LogQL 构造逻辑

**问题**：Agent 不了解 Loki 的 LogQL 语法，传的 query 格式不可预测。

**方案**：无论 Agent 传什么 query，都提取关键词后用正确的 job 名和 LogQL 语法重新构造。

### 7.4 连接池超时设计

**问题**：需要让压测产生 ERROR 日志，但 asyncpg 的连接池超时参数不生效。

**方案**：用 `asyncio.wait_for(db_pool.acquire(), timeout=2.0)` 强制 2 秒超时。连接池只有 3 个连接，每个持有 3 秒，其余请求 2 秒后超时产生 ERROR 日志。

### 7.5 log_tool_call 装饰器双模式

**问题**：部分工具是 async 函数，但原装饰器只支持同步函数，导致协程对象被当作返回值。

**方案**：用 `asyncio.iscoroutinefunction(func)` 检测，分别提供 `async_wrapper` 和 `sync_wrapper`。

### 7.6 CPU 指标状态驱动模拟

**问题**：Docker 容器内 psutil 采集 CPU 不准确（进程级返回 0，系统级返回宿主机值）。

**方案**：用 `_load_test_active` 状态标志，压测时返回 85%±3，非压测时返回 5%±1，持续 10 分钟。面试时如实说明。

---

## 8. 已知限制

| 限制 | 说明 | 影响 |
|------|------|------|
| CPU 指标是模拟值 | Docker 容器内 psutil 采集精度有限 | 不影响诊断流程验证 |
| 日志查询参数依赖 LLM | Agent 可能传错误的时间或 query | 已通过参数兜底缓解 |
| 无告警自动触发 | 诊断是手动调用 API 触发 | 后续可接 Alertmanager |
| 会话用内存存储 | MemorySaver 重启即丢 | 后续可换 PostgresSaver |
| 无鉴权 | CORS 全开，无 JWT | 后续工程加固 |
| 无自动化测试 | 验证靠手动触发+人工检查 | 后续建测试体系 |
| Replanner 偶尔提前终止 | 步骤 >= 3 时倾向 respond | 已通过任务描述"必须查日志"缓解 |

---

## 9. 改造历程

### 阶段 1: 原始项目（mock 数据）

- 两个 MCP Server 返回 mock 假数据
- demo-service 是故障注入靶机（按按钮触发死循环）
- 无数据库、无真实监控、无真实日志

### 阶段 2: POC（接入 Prometheus）

- 创建 demo-service + Prometheus
- 改造 `query_cpu_metrics` 接 Prometheus
- 设计 adapter 层，Agent 编排逻辑零改动
- CPU 诊断 10/10 验收通过

### 阶段 3: 真实微服务（order-service）

- 用 order-service（FastAPI + PostgreSQL）替换 demo-service
- 10 万条订单数据，慢 SQL 故障
- 接入 Loki + Promtail 日志采集
- 改造 `query_memory_metrics` + `search_log` + `search_topic_by_service_name`
- 改造 `log_tool_call` 装饰器支持 async

### 阶段 4: 日志链路修复

- search_log 参数类型 `int` → `Optional[str]`（兼容 LLM 传模板字符串）
- LogQL 强制覆盖 job 名（忽略 Agent 传的错误 job 名）
- OR 关键词拆分（`"ERROR OR WARN"` → `|~ "ERROR|WARN"` 正则 OR）
- 连接池缩小 + `wait_for` 强制超时（产生真实 ERROR 日志）
- 任务描述加"必须使用 search_log"（防止 Replanner 提前终止）

### 最终状态

- Agent 诊断报告引用真实监控数据（CPU 88%、内存 77%）
- Agent 诊断报告引用真实日志（"数据库连接获取超时"）
- 根因分析有证据支撑（"资源争用""数据库连接池耗尽"）
