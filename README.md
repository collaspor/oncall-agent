# SuperBizAgent

> 企业级智能对话和 AIOps 运维诊断系统 — 支持 RAG 知识库问答、Plan-Execute-Replan 自动故障诊断、真实微服务故障注入

## 核心特性

- **AIOps 智能诊断** — LangGraph Plan-Execute-Replan 自动故障诊断，接入真实 Prometheus 监控 + Loki 日志
- **RAG 知识库问答** — Milvus 向量检索增强，支持文档上传、自动建立向量索引
- **真实微服务故障注入** — order-service（FastAPI + PostgreSQL），通过批量下单触发慢 SQL + 连接池耗尽
- **MCP 工具协议** — 监控数据（Prometheus）和日志数据（Loki）通过 MCP Server 统一接入
- **流式输出** — SSE 流式返回诊断过程和结果

## 技术栈

| 层次 | 技术 |
|------|------|
| Web 框架 | FastAPI + uvicorn |
| LLM 编排 | LangChain + LangGraph (Plan-Execute-Replan) |
| LLM 模型 | 阿里云通义千问 (ChatQwen / qwen-max) |
| 向量数据库 | Milvus v2.5.10 (1024 维, IVF_FLAT) |
| 监控 | Prometheus + Grafana |
| 日志 | Loki + Promtail |
| 订单微服务 | FastAPI + asyncpg + PostgreSQL 16 |
| 工具协议 | MCP (FastMCP + langchain-mcp-adapters) |

## 快速开始

### 环境要求

- Python 3.11+
- Docker Desktop（运行 Milvus + Prometheus + Loki + PostgreSQL）
- 阿里云 DashScope API Key

### 安装和启动

#### Windows 环境

```powershell
# 1. 创建虚拟环境并安装依赖
uv venv
.venv\Scripts\activate
uv pip install -e .

# 2. 编辑 .env 文件，填入 DASHSCOPE_API_KEY
notepad .env

# 3. 启动基础设施（Milvus + order-service + Prometheus + Loki）
docker compose -f vector-database.yml up -d
docker compose -f infra/docker-compose.yml up -d --build

# 4. 启动 MCP 服务和 FastAPI
.\start-windows.bat

# 5. 上传知识库文档
python -c "import requests, os, time; [requests.post('http://localhost:9900/api/upload', files={'file': open(f'aiops-docs/{f}', 'rb')}) or time.sleep(1) for f in os.listdir('aiops-docs') if f.endswith('.md')]"
```

#### Linux/macOS 环境

```bash
make init    # 一键初始化（Docker + 服务 + 文档）
make start   # 启动所有服务
```

### 访问服务

- **Web 界面**: http://localhost:9900
- **API 文档**: http://localhost:9900/docs
- **Prometheus**: http://localhost:9090
- **Loki**: http://localhost:3100
- **Attu (Milvus UI)**: http://localhost:8000

## 验证 AIOps 诊断

```bash
# 1. 触发故障（100 个并发下单 → CPU 飙高 + 连接池耗尽 + ERROR 日志）
curl -X POST "http://localhost:8080/api/load-test?count=100"

# 2. 等待 15 秒让 Prometheus 采集 + Loki 入库

# 3. 触发 AIOps 诊断
curl -X POST "http://localhost:9900/api/aiops" \
  -H "Content-Type: application/json" \
  -d '{"session_id":"test"}' --no-buffer

# 4. 诊断报告将流式返回，包含：
#    - CPU/内存监控数据（来自 Prometheus）
#    - ERROR 日志证据（来自 Loki）
#    - 根因分析（连接池耗尽）
#    - 处置建议
```

## API 接口

| 功能 | 方法 | 路径 | 说明 |
|------|------|------|------|
| AIOps 诊断 | POST | `/api/aiops` | Plan-Execute-Replan 自动诊断（SSE 流式） |
| 普通对话 | POST | `/api/chat` | RAG 问答（一次性返回） |
| 流式对话 | POST | `/api/chat_stream` | RAG 问答（SSE 流式） |
| 文件上传 | POST | `/api/upload` | 上传文档并自动向量化 |
| 健康检查 | GET | `/health` | 服务状态 + Milvus 连接状态 |

## 项目结构

```
super_biz_agent_py/
├── app/                          # AIOps Agent 主应用
│   ├── api/                      # API 路由（aiops/chat/file/health）
│   ├── services/                 # 业务服务（aiops_service/rag_agent_service/...）
│   ├── agent/aiops/              # Plan-Execute-Replan（planner/executor/replanner）
│   ├── tools/                    # LangChain 工具（knowledge_tool/time_tool）
│   ├── core/                     # Milvus 客户端 + LLM 工厂
│   └── models/                   # 请求/响应模型
├── mcp_servers/                  # MCP Server
│   ├── monitor_server.py         # 监控 MCP（查 Prometheus，含 adapter）
│   └── cls_server.py             # 日志 MCP（查 Loki，含 adapter）
├── order-service/                # 订单微服务（故障源）
│   └── app.py                    # FastAPI + PostgreSQL + 故障注入
├── infra/                        # 基础设施
│   ├── docker-compose.yml        # PostgreSQL + order-service + Prometheus + Loki + Promtail
│   ├── init.sql                  # 建表 + 10万条订单数据
│   ├── prometheus.yml            # 监控采集配置
│   ├── loki-config.yml           # 日志存储配置
│   └── promtail-config.yml       # 日志采集配置
├── aiops-docs/                   # 运维知识库（5个 Markdown 文档）
├── static/                       # Web 前端
├── docs/                         # 项目文档
│   ├── ARCHITECTURE.md           # 架构文档
│   └── diagnosis/                # 诊断报告存档
├── vector-database.yml           # Milvus Docker Compose
├── pyproject.toml                # 项目配置
└── .env                          # 环境变量
```

详细架构说明请参阅 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 诊断流程

```
1. POST /api/load-test (触发 100 并发下单)
   → 连接池耗尽 → CPU 飙高 85%+ → ERROR 日志 "数据库连接获取超时"

2. POST /api/aiops (触发诊断)
   → Planner 制定 6-7 步计划
   → Executor 逐步执行：
     ├─ get_current_time → 获取当前时间
     ├─ list_monitored_services → 确认 order-service 在监控中
     ├─ query_cpu_metrics → Prometheus 查到 CPU 88%
     ├─ query_memory_metrics → Prometheus 查到内存 77%
     └─ search_log → Loki 查到 ERROR 日志
   → Replanner 评估 → 生成报告

3. 输出 Markdown 诊断报告
   - 活跃告警：CPU + 内存超标
   - 日志证据：数据库连接获取超时
   - 根因分析：连接池耗尽 + 资源争用
   - 处置建议：优化代码、增加资源、优化数据库查询
```

## 配置说明

通过 `.env` 文件配置：

```bash
# 阿里云 DashScope（必填）
DASHSCOPE_API_KEY=your-api-key
DASHSCOPE_API_BASE=https://dashscope.aliyuncs.com/compatible-mode/v1
DASHSCOPE_MODEL=qwen-max

# Milvus
MILVUS_HOST=localhost
MILVUS_PORT=19530

# RAG
RAG_TOP_K=3
CHUNK_MAX_SIZE=800
CHUNK_OVERLAP=100

# MCP 服务
MCP_CLS_URL=http://localhost:8003/mcp
MCP_MONITOR_URL=http://localhost:8004/mcp
```

## 停止服务

```powershell
# Windows
.\stop-windows.bat

# 或手动停止 Docker
docker compose -f infra/docker-compose.yml down
docker compose -f vector-database.yml down
```

## 技术文档

- [架构文档](docs/ARCHITECTURE.md) — 完整架构说明、模块详解、数据流
- [POC PRD](PRD-AIOps-POC.md) — POC 阶段需求文档
- [OrderService PRD](PRD-OrderService.md) — 真实微服务接入方案
- [日志修复 PRD](PRD-LogFix.md) — 日志查询链路修复方案
- [诊断报告存档](docs/diagnosis/) — 历次诊断输出

## 许可证

MIT License

author: chief
