# Oncall Agent

企业级智能 oncall 运维系统，基于 Plan-Execute-Replan 模式实现自动故障诊断和根因分析。

## 核心能力

- **智能对话** — LangChain 多轮对话 + 流式输出
- **RAG 问答** — 向量检索增强，支持文档上传与自动索引
- **AIOps 诊断** — Plan-Execute-Replan 自动故障诊断，流式输出诊断过程
- **MCP 工具集成** — 日志查询与监控数据接入

## 技术栈

| 层次 | 技术 |
|------|------|
| 框架 | FastAPI + LangChain + LangGraph |
| LLM | 阿里云 DashScope (通义千问) |
| 向量库 | Milvus |
| 工具协议 | MCP (Model Context Protocol) |

## 快速开始

### 环境要求

- Python 3.10+
- Docker Desktop (用于运行 Milvus)
- 阿里云 DashScope API Key

### 安装启动

```bash
# 安装依赖
pip install uv
uv venv
source .venv/bin/activate   # Linux/macOS
# .venv\Scripts\activate    # Windows
uv pip install -e .

# 编辑 .env 填入 DASHSCOPE_API_KEY
# 然后启动
make init   # Linux/macOS
make start
```

Windows 用户可使用 `start-windows.bat` 一键启动。

### 访问

- Web 界面: http://localhost:9900
- API 文档: http://localhost:9900/docs

## API 概览

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/chat` | 普通对话 |
| POST | `/api/chat_stream` | SSE 流式对话 |
| POST | `/api/aiops` | AIOps 自动故障诊断 (流式) |
| POST | `/api/upload` | 上传文档建立索引 |
| GET  | `/api/health` | 健康检查 |

## 项目结构

```
├── super_biz_agent_py-release-2026-03-16/   # 主项目目录
│   ├── app/           # FastAPI 应用 (API、Agent、服务、模型)
│   ├── mcp_servers/   # MCP 服务 (CLS 日志、监控)
│   ├── static/        # Web 前端
│   ├── aiops-docs/    # 运维知识库
│   └── ...
├── chat_cmd_workflow.json
└── README.md
```

详细文档见 [super_biz_agent_py-release-2026-03-16/README.md](super_biz_agent_py-release-2026-03-16/README.md)。
