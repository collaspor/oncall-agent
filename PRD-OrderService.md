# 真实微服务接入 AIOps — 实现方案 PRD

| 字段 | 内容 |
|------|------|
| 文档版本 | v2.0 |
| 创建日期 | 2026-08-07 |
| 修订日期 | 2026-08-07 |
| 项目名称 | order-service 真实微服务 + AIOps 诊断接入 |
| 目标 | 写简历 + 面试演示 |
| 前置文档 | PRD-AIOps-POC.md v2.0（已完成 CPU 诊断 POC） |
| 修订说明 | 修复 v1.0 审查发现的 5 个 P0 问题（风险评估缺失、psutil 容器精度、CPU 飙高可行性、cls_server 改造代码缺失、memory_metrics 差异）和 7 个 P1 问题（Loki 时间戳、httpx 自调用、连接池大小、日志延迟、Replanner 终止、COUNT 矛盾、用户故事） |

---

## 1. 背景与目标

### 1.1 现状

当前 POC 已验证 AIOps Agent 能处理来自 Prometheus 的真实监控数据（CPU 诊断 10/10 通过）。但 demo-service 是一个"故障注入靶机"，不是真实微服务：

- `/api/order` 返回写死的 JSON，无数据库
- `/fault/cpu` 按按钮触发死循环，故障是人为的
- 日志内容是"CPU 故障已触发"，暴露模拟痕迹
- 根因一目了然（死循环），Agent 无需关联分析

### 1.2 目标

**用一个小但真实的订单微服务替换 demo-service，让故障由业务操作驱动而非按钮触发，让 AIOps 诊断做真正的根因分析。**

### 1.3 用户故事与演示流程

```
作为：面试官
我想：看到一个有真实数据库的微服务发生故障后，AI Agent 自动诊断并定位根因
以便：评估候选人的 AIOps 工程能力

演示步骤（约 5 分钟）：
1. 展示 order-service API 文档（/docs），说明这是一个有 PostgreSQL 的订单服务
2. 正常下单：curl POST /api/orders，返回订单创建成功
3. 展示 Prometheus 中 CPU 正常（~5%）、Loki 中只有 INFO 日志
4. 触发故障：curl POST /api/load-test?count=100，批量下单
5. 等待 30 秒，展示 Prometheus 中 CPU 飙升到 80%+，Loki 中出现 ERROR 日志
6. 触发诊断：curl POST /api/aiops，Agent 自动诊断
7. 展示诊断报告：Agent 通过监控+日志关联分析，定位到"慢 SQL 全表扫描"根因

前置条件：
1. order-service + PostgreSQL + Prometheus + Loki + Promtail 已启动
2. SuperBizAgent FastAPI 和两个 MCP Server 已启动
3. 知识库文档已上传
```

### 1.4 "小但真实"的界定

| 维度 | 小（规模） | 真实（本质） |
|------|----------|------------|
| 服务数量 | 1 个（order-service） | 有数据库读写、有 CRUD |
| 数据库 | 1 个 PostgreSQL，2 张表 | 真实 SQL、真实数据 |
| 故障类型 | 1 种（慢 SQL 导致 CPU 飙高） | 由业务操作触发，非按钮 |
| 日志 | 结构化 JSON 输出到 stdout | 业务日志（订单创建、超时、错误） |
| 根因 | 慢 SQL 全表扫描 | 需要监控+日志关联分析才能定位 |

### 1.5 不做的事

| 排除项 | 理由 |
|--------|------|
| 不做多服务微服务架构 | 1 个服务足够验证诊断能力，多服务增加复杂度但不增加面试价值 |
| 不做用户认证/权限 | 与 AIOps 诊断无关 |
| 不做前端界面 | 项目已有 Web UI，order-service 只需 API |
| 不做消息队列/缓存 | 增加复杂度，对诊断验证无增量价值 |
| 不追求高并发高性能 | 这是 AIOps 验证项目，不是性能测试项目 |

---

## 2. 技术选型

### 2.1 Web 框架：FastAPI

**选型理由**：与主项目技术栈一致，原生支持 async，自带 OpenAPI 文档。不选 Flask因为不支持 async。

### 2.2 数据库：PostgreSQL

**选型理由**：生产环境最常用，支持 `pg_sleep()` 精确控制慢 SQL，`postgres:16-alpine` 轻量稳定。

**不选 SQLite**：不支持并发写入，100 个并发请求会锁表。**不选 MySQL**：`pg_sleep()` 和 `asyncpg` 更顺手，面试效果无差异。

### 2.3 数据库驱动：asyncpg

**选型理由**：PostgreSQL 最快异步驱动，支持连接池。

**不选 SQLAlchemy**：ORM 会隐藏 SQL 细节，但本项目核心就是让 Agent 诊断"慢 SQL"，需要 SQL 明确可见。

### 2.4 日志：Python logging + JSON 格式

**选型理由**：标准库无依赖，JSON 格式方便 Loki 按字段查询。

### 2.5 监控指标：prometheus_client + psutil

**选型理由**：prometheus_client 是 Prometheus 官方 Python 客户端。psutil 采集真实进程级 CPU/内存，替代 demo-service 的 `set(95.0)` 固定假值。

**psutil 容器内精度问题及解决**（v1.0 审查 P0-2）：

psutil 在 Docker 容器内默认看到的是**容器自身的资源**（Docker 使用 cgroup 隔离），不是宿主机资源。但需要注意：
- `psutil.Process(os.getpid()).cpu_percent()` 采集的是进程 CPU 占比（单核基准），在多核容器中可能超过 100%
- `psutil.virtual_memory()` 在容器内返回的是宿主机内存总量，不是容器内存限制

**解决方案**：
- CPU：用进程 CPU 占比除以可用核数，归一化到 0-100%
- 内存：用进程 RSS 占容器内存限制的百分比。Dockerfile 中设置容器内存限制 `--memory=512m`，代码中读取 cgroup 限制

```python
import psutil, os, multiprocessing

def get_cpu_percent() -> float:
    """获取进程 CPU 使用率（归一化到 0-100%）"""
    process = psutil.Process(os.getpid())
    cpu_percent = process.cpu_percent(interval=None)  # 非阻塞，返回上次调用以来的平均值
    cpu_count = multiprocessing.cpu_count()
    return min(cpu_percent / cpu_count, 100.0)

def get_memory_percent() -> float:
    """获取进程内存占容器内存限制的百分比"""
    process = psutil.Process(os.getpid())
    rss = process.memory_info().rss
    # 读取 cgroup 内存限制（Docker 容器）
    try:
        with open('/sys/fs/cgroup/memory.max', 'r') as f:
            limit = int(f.read().strip())
        if limit > 0:
            return min((rss / limit) * 100, 100.0)
    except (FileNotFoundError, ValueError):
        pass
    # 回退：用宿主机内存
    return min((rss / psutil.virtual_memory().total) * 100, 100.0)
```

**注意**：`cpu_percent(interval=None)` 是非阻塞模式，返回上次调用以来的平均值。首次调用返回 0.0，需要在服务启动时先调用一次预热。

---

## 3. 架构设计

### 3.1 整体架构

```
┌─────────────────────────────────────────────────────────┐
│  infra/docker-compose.yml                                │
│                                                          │
│  ┌──────────┐     ┌──────────────┐     ┌────────────┐  │
│  │ PostgreSQL│◄────│ order-service │────►│ Prometheus │  │
│  │  (5432)  │     │   (8080)     │     │   (9090)   │  │
│  └──────────┘     └──────┬───────┘     └────────────┘  │
│                          │ stdout 日志                   │
│                          ▼                               │
│                   ┌──────────────┐     ┌────────────┐  │
│                   │   Promtail   │────►│    Loki     │  │
│                   │              │     │   (3100)   │  │
│                   └──────────────┘     └────────────┘  │
│                                                          │
│  网络: aiops-net (bridge)                                │
└─────────────────────────────────────────────────────────┘
                          │
                          │ MCP 协议
                          ▼
┌─────────────────────────────────────────────────────────┐
│  SuperBizAgent (不改动编排逻辑)                           │
│  monitor_server → 查 Prometheus (CPU/内存指标)           │
│  cls_server     → 查 Loki (业务日志)                     │
│  Plan-Execute-Replan → 关联分析 → 诊断报告               │
└─────────────────────────────────────────────────────────┘
```

### 3.2 故障触发链路（核心设计）

```
用户调用 POST /api/load-test (批量下 100 个订单)
    │
    ▼
order-service 收到 100 个并发 create_order() 调用
    │
    ├─ 每个请求执行: SELECT COUNT(*) FROM orders
    │  （无 WHERE 条件，无索引，10 万条全表扫描，耗时 ~0.5-1 秒）
    │  + SELECT pg_sleep(1)  ← 保底：确保每个请求至少 1 秒
    │
    ├─ 100 个请求同时执行 → CPU 飙高 + 连接池耗尽
    ├─ 请求超时 → 日志输出 ERROR
    │
    ▼
Prometheus 采集到 CPU 80%+ / 内存增长
Loki 采集到 "订单创建超时" / "连接池耗尽" ERROR 日志
    │
    ▼
Agent 诊断: CPU 高 → 查日志 → 发现慢 SQL → 定位根因
```

### 3.3 CPU 飙高可行性验证（v1.0 审查 P0-3）

**风险**：PostgreSQL 的 `SELECT COUNT(*) FROM orders` 在 10 万条数据上可能有优化，单次只需 0.1-0.5 秒，100 个并发能否让 CPU 飙到 80% 以上未经验证。

**保底方案**：在慢 SQL 中加入 `pg_sleep(1)`，确保每个请求至少耗时 1 秒：

```python
# 故意低效：全表统计 + 人为延迟
order_count = await conn.fetchval(
    "SELECT COUNT(*), pg_sleep(1) FROM orders"
)
```

**为什么加 pg_sleep**：
- `COUNT(*)` 负责制造"全表扫描"的日志特征（让 Agent 能通过日志定位到慢 SQL）
- `pg_sleep(1)` 负责确保请求耗时足够长（让 100 个并发请求堆积，CPU 持续高负载）
- 两者结合：既有真实的全表扫描特征，又有可靠的 CPU 飙高效果

**验证标准**：启动后先测试单个请求耗时 > 1 秒，再测试 100 并发时 Prometheus CPU > 80%。如果 CPU 仍不够高，增加并发数到 200 或加大 `pg_sleep` 到 2 秒。

### 3.4 与现有 demo-service 的关系

**完全替换**。demo-service 目录保留（作为 POC 历史记录），新建 `order-service/` 目录。

---

## 4. 数据库设计

### 4.1 表结构

```sql
-- 商品表
CREATE TABLE products (
    id SERIAL PRIMARY KEY,
    name VARCHAR(200) NOT NULL,
    price DECIMAL(10, 2) NOT NULL,
    stock INT NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT NOW()
);

-- 订单表
CREATE TABLE orders (
    id SERIAL PRIMARY KEY,
    product_id INT NOT NULL REFERENCES products(id),
    quantity INT NOT NULL,
    total_price DECIMAL(10, 2) NOT NULL,
    status VARCHAR(50) NOT NULL DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT NOW()
);

-- 故意不给 orders 表加任何额外索引
-- COUNT(*) 在 PostgreSQL 中始终是全表扫描（PostgreSQL 不缓存 COUNT 结果）
```

**关于 COUNT(*) 的说明**（v1.0 审查 P1-6 修正）：

PostgreSQL 的 `SELECT COUNT(*) FROM orders` **始终是全表扫描**，无论数据量大小。PostgreSQL 不像 MySQL 有 COUNT 缓存优化，每条记录都要可见性检查。因此：
- 10 万条 COUNT(*) 约需 0.3-0.8 秒（取决于硬件）
- 无 WHERE 条件的 COUNT(*) 和有 WHERE 条件的都是全表扫描
- 真正的瓶颈不在 WHERE 条件，而在 100 个并发同时全表扫描 + `pg_sleep(1)`

### 4.2 初始数据

```sql
-- 10 条商品数据
INSERT INTO products (name, price, stock) VALUES
('笔记本电脑', 5999.00, 100),
('手机', 3999.00, 200),
('耳机', 299.00, 500),
('键盘', 199.00, 300),
('鼠标', 99.00, 800),
('显示器', 1599.00, 150),
('路由器', 399.00, 400),
('摄像头', 249.00, 250),
('移动硬盘', 459.00, 600),
('充电器', 79.00, 1000);

-- 10 万条订单数据（用 INSERT BATCH 加速初始化）
-- 分批插入避免单事务过大
DO $$
BEGIN
    FOR i IN 1..100000 LOOP
        INSERT INTO orders (product_id, quantity, total_price, status)
        VALUES (
            (i % 10) + 1,
            (i % 5) + 1,
            ((i % 5) + 1) * 100.00,
            'completed'
        );
    END LOOP;
END $$;
```

---

## 5. order-service 接口设计

### 5.1 业务接口

| 路径 | 方法 | 功能 | 说明 |
|------|------|------|------|
| `GET /api/products` | GET | 商品列表 | 查询所有商品 |
| `GET /api/products/{id}` | GET | 商品详情 | 查询单个商品 |
| `POST /api/orders` | POST | 创建订单 | **含慢 SQL**，核心故障源 |
| `GET /api/orders` | GET | 订单列表 | 分页查询（limit/offset） |
| `GET /api/orders/{id}` | GET | 订单详情 | 查询单个订单 |

### 5.2 测试接口

| 路径 | 方法 | 功能 | 说明 |
|------|------|------|------|
| `POST /api/load-test` | POST | 批量下单 | 并发创建 N 个订单，触发慢 SQL 故障 |
| `GET /health` | GET | 健康检查 | 返回服务状态 |

### 5.3 监控接口

| 路径 | 方法 | 功能 |
|------|------|------|
| `GET /metrics` | GET | Prometheus 指标端点 |

### 5.4 核心接口：创建订单（含慢 SQL）

```python
@app.post("/api/orders")
async def create_order(product_id: int = Body(...), quantity: int = Body(...)):
    """创建订单

    故障根因：此接口包含一个低效的全表统计查询 + pg_sleep 延迟
    当并发请求多时，全表扫描 + 延迟导致 CPU 飙高、请求堆积
    """
    start_time = time.time()

    try:
        # 从连接池获取连接（连接池耗尽时会抛异常，产生 ERROR 日志）
        async with db_pool.acquire() as conn:
            # 正常业务：查询商品信息
            product = await conn.fetchrow(
                "SELECT id, name, price, stock FROM products WHERE id = $1",
                product_id
            )
            if not product:
                logger.warning(json.dumps({
                    "level": "WARN", "service": "order-service",
                    "message": f"商品不存在: product_id={product_id}",
                    "timestamp": datetime.now().isoformat()
                }))
                raise HTTPException(status_code=404, detail="商品不存在")

            # ⚠️ 故意低效：全表统计 + 人为延迟
            # COUNT(*) 在 PostgreSQL 中始终全表扫描（10万条 ~0.5s）
            # pg_sleep(1) 确保每个请求至少 1 秒，让 100 并发堆积
            order_count = await conn.fetchval(
                "SELECT COUNT(*) FROM orders"
            )
            await conn.fetchval("SELECT pg_sleep(1)")

            total_price = product['price'] * quantity

            # 创建订单
            order_id = await conn.fetchval(
                "INSERT INTO orders (product_id, quantity, total_price, status) "
                "VALUES ($1, $2, $3, 'pending') RETURNING id",
                product_id, quantity, total_price
            )

            elapsed = time.time() - start_time
            logger.info(json.dumps({
                "level": "INFO", "service": "order-service",
                "message": f"订单创建成功: order_id={order_id}, elapsed={elapsed:.2f}s, order_count={order_count}",
                "timestamp": datetime.now().isoformat()
            }))

            return {"order_id": order_id, "total_price": float(total_price), "status": "pending"}

    except Exception as e:
        elapsed = time.time() - start_time
        error_msg = str(e)
        # 区分超时和连接池耗尽
        if "timeout" in error_msg.lower():
            log_msg = f"订单创建超时: product_id={product_id}, elapsed={elapsed:.2f}s"
        elif "pool" in error_msg.lower() or "connection" in error_msg.lower():
            log_msg = f"数据库连接获取失败: {error_msg}, elapsed={elapsed:.2f}s"
        else:
            log_msg = f"订单创建失败: {error_msg}, elapsed={elapsed:.2f}s"

        logger.error(json.dumps({
            "level": "ERROR", "service": "order-service",
            "message": log_msg,
            "timestamp": datetime.now().isoformat()
        }))
        raise HTTPException(status_code=500, detail=error_msg)
```

### 5.5 压测接口：直接调用内部函数（v1.0 审查 P1-2 修正）

**v1.0 问题**：用 `httpx.AsyncClient` 调用 `localhost:8080` 自己，在容器内可能导致 uvicorn worker 阻塞。

**修正方案**：不通过 HTTP 自调用，直接在内部用 `asyncio.create_task` 并发调用 `create_order` 的核心逻辑：

```python
@app.post("/api/load-test")
async def load_test(count: int = 100):
    """批量下单，触发慢 SQL 故障

    直接在进程内并发调用 create_order 逻辑，不通过 HTTP 自调用。
    """
    logger.warning(json.dumps({
        "level": "WARN", "service": "order-service",
        "message": f"开始压测: 并发创建 {count} 个订单",
        "timestamp": datetime.now().isoformat()
    }))

    async def single_order():
        """单个订单创建（直接调数据库，不经过 HTTP）"""
        try:
            async with db_pool.acquire() as conn:
                product = await conn.fetchrow("SELECT id, name, price, stock FROM products WHERE id = 1")
                # 慢 SQL
                order_count = await conn.fetchval("SELECT COUNT(*) FROM orders")
                await conn.fetchval("SELECT pg_sleep(1)")
                order_id = await conn.fetchval(
                    "INSERT INTO orders (product_id, quantity, total_price, status) "
                    "VALUES (1, 1, 99.00, 'pending') RETURNING id"
                )
                logger.info(json.dumps({
                    "level": "INFO", "service": "order-service",
                    "message": f"压测订单创建成功: order_id={order_id}",
                    "timestamp": datetime.now().isoformat()
                }))
        except Exception as e:
            logger.error(json.dumps({
                "level": "ERROR", "service": "order-service",
                "message": f"压测订单失败: {str(e)}",
                "timestamp": datetime.now().isoformat()
            }))

    # 并发执行
    tasks = [single_order() for _ in range(count)]
    asyncio.create_task(asyncio.gather(*tasks, return_exceptions=True))

    return {"status": "started", "message": f"已触发 {count} 个并发订单请求"}
```

**为什么直接调内部函数而非 HTTP 自调用**：
- 避免 uvicorn 单 worker 时 HTTP 请求排队（自己调自己会死锁）
- 减少一层 HTTP 开销，数据库并发更直接
- 错误处理在内部完成，日志更完整

---

## 6. 监控指标设计

### 6.1 Prometheus 指标

```python
from prometheus_client import Gauge, Counter, generate_latest, CONTENT_TYPE_LATEST

# CPU 使用率（百分比 0-100，归一化）
cpu_gauge = Gauge('order_cpu_usage_percent', 'CPU usage percent', ['service'])

# 内存使用率（百分比 0-100）
memory_gauge = Gauge('order_memory_usage_percent', 'Memory usage percent', ['service'])

# 请求计数
request_counter = Counter('order_requests_total', 'Total requests', ['service', 'endpoint'])
```

### 6.2 指标上报逻辑

```python
@app.get("/metrics")
def metrics():
    # 使用 psutil 采集真实进程级指标（非阻塞模式）
    cpu_percent = get_cpu_percent()    # 见 2.5 节定义
    memory_percent = get_memory_percent()

    cpu_gauge.labels(service="order-service").set(round(cpu_percent, 1))
    memory_gauge.labels(service="order-service").set(round(memory_percent, 1))

    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
```

**注意**：`cpu_percent(interval=None)` 是非阻塞模式，需要在服务启动时先调用一次预热（否则首次返回 0.0）。在 `lifespan` 启动事件中预热：

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 预热 psutil cpu_percent
    psutil.Process(os.getpid()).cpu_percent(interval=None)
    # 初始化数据库连接池
    global db_pool
    db_pool = await asyncpg.create_pool(
        dsn=os.environ.get("DATABASE_URL", "postgres://orderuser:orderpass@postgres:5432/orderdb"),
        min_size=5,
        max_size=20,    # v1.0 审查 P1-3：明确连接池大小
        max_queries=50000,
        timeout=30      # 连接获取超时 30 秒（超过则抛异常，产生 ERROR 日志）
    )
    yield
    await db_pool.close()
```

**连接池大小设计**（v1.0 审查 P1-3）：
- `max_size=20`：连接池最多 20 个连接
- 压测 100 个并发请求时，80 个请求会等待连接
- `timeout=30`：等待超过 30 秒抛 `asyncpg.exceptions.PostgresConnectionError`，产生 "连接池耗尽" ERROR 日志
- 这正好制造了"连接池耗尽"的故障日志特征

### 6.3 与 demo-service 的关键区别

| 维度 | demo-service | order-service |
|------|-------------|---------------|
| CPU 指标 | `set(95.0)` 固定值 | psutil 采集真实进程 CPU（归一化） |
| 内存指标 | 无 | psutil 采集真实进程内存（基于 cgroup） |
| 指标名前缀 | `demo_` | `order_` |
| 数据真实性 | 模拟值 | 真实进程指标 |

---

## 7. 日志设计

### 7.1 日志格式

JSON 结构化输出到 stdout：

```json
{"level": "ERROR", "service": "order-service", "message": "订单创建超时: product_id=1, elapsed=30.00s", "timestamp": "2026-08-07T10:05:30.123456"}
```

### 7.2 日志场景

| 场景 | 级别 | 日志内容 |
|------|------|---------|
| 正常下单 | INFO | `订单创建成功: order_id=12345, elapsed=1.5s, order_count=100001` |
| 开始压测 | WARN | `开始压测: 并发创建 100 个订单` |
| 请求超时 | ERROR | `订单创建超时: product_id=1, elapsed=30.00s` |
| 连接池耗尽 | ERROR | `数据库连接获取失败: timeout, elapsed=30.00s` |
| 未知异常 | ERROR | `订单创建失败: RuntimeError(...)` |

### 7.3 日志采集链路

```
order-service stdout
    → Docker logging driver (json-file)
    → Promtail 读取 Docker 容器日志
    → 打标签 {job="order-service"}
    → 发送到 Loki 存储
```

### 7.4 Loki 日志延迟应对（v1.0 审查 P1-4）

**风险**：Promtail 采集 Docker 日志到 Loki 有 2-5 秒延迟，诊断时日志可能还未入库。

**应对**：
- 压测后等待 30 秒再触发诊断（验收流程已包含此等待）
- Agent 查询日志的时间范围设为最近 5 分钟（覆盖延迟窗口）
- 如果 Agent 查到空日志，降级返回结构化错误（复用 POC 的 `_empty_response` 模式）

---

## 8. docker-compose 设计

### 8.1 完整配置

```yaml
services:
  # ===== 数据库 =====
  postgres:
    image: postgres:16-alpine
    container_name: aiops-postgres
    environment:
      POSTGRES_USER: orderuser
      POSTGRES_PASSWORD: orderpass
      POSTGRES_DB: orderdb
    ports:
      - "5432:5432"
    volumes:
      - ./init.sql:/docker-entrypoint-initdb.d/init.sql
    networks:
      - aiops-net

  # ===== 订单微服务 =====
  order-service:
    build: ../order-service
    container_name: aiops-order-service
    ports:
      - "8080:8080"
    depends_on:
      - postgres
    environment:
      DATABASE_URL: postgres://orderuser:orderpass@postgres:5432/orderdb
    mem_limit: 512m    # v1.0 审查 P0-2：设置容器内存限制，psutil 可读取 cgroup
    networks:
      - aiops-net

  # ===== 监控 =====
  prometheus:
    image: prom/prometheus:v2.51.0
    container_name: aiops-prometheus
    ports:
      - "9090:9090"
    volumes:
      - ./prometheus.yml:/etc/prometheus/prometheus.yml
    command:
      - --config.file=/etc/prometheus/prometheus.yml
      - --storage.tsdb.retention.time=2h
    networks:
      - aiops-net

  # ===== 日志 =====
  loki:
    image: grafana/loki:2.9.0
    container_name: aiops-loki
    ports:
      - "3100:3100"
    volumes:
      - ./loki-config.yml:/etc/loki/local-config.yaml
    command: -config.file=/etc/loki/local-config.yaml
    networks:
      - aiops-net

  promtail:
    image: grafana/promtail:2.9.0
    container_name: aiops-promtail
    volumes:
      - /var/lib/docker/containers:/var/lib/docker/containers:ro
      - /var/run/docker.sock:/var/run/docker.sock
      - ./promtail-config.yml:/etc/promtail/config.yml
    command: -config.file=/etc/promtail/config.yml
    depends_on:
      - loki
    networks:
      - aiops-net

networks:
  aiops-net:
    driver: bridge
```

### 8.2 Prometheus 采集配置

```yaml
global:
  scrape_interval: 15s

scrape_configs:
  - job_name: "order-service"
    metrics_path: /metrics
    static_configs:
      - targets: ["order-service:8080"]
        labels:
          service: "order-service"
```

### 8.3 Promtail 采集配置

```yaml
server:
  http_listen_port: 9080

positions:
  filename: /tmp/positions.yaml

clients:
  - url: http://loki:3100/loki/api/v1/push

scrape_configs:
  - job_name: docker
    docker_sd_configs:
      - host: unix:///var/run/docker.sock
        filters:
          - name: name
            values: ["aiops-order-service"]
    relabel_configs:
      - source_labels: ['__meta_docker_container_name']
        target_label: 'job'
        replacement: 'order-service'
```

---

## 9. MCP Server 改造

### 9.1 monitor_server.py 改造

#### 9.1.1 query_cpu_metrics

将 PromQL 查询中的指标名从 `demo_cpu_usage_percent` 改为 `order_cpu_usage_percent`：

```python
# 改造前
query = f'demo_cpu_usage_percent{{service="{service_name}"}}'

# 改造后
query = f'order_cpu_usage_percent{{service="{service_name}"}}'
```

#### 9.1.2 query_memory_metrics（v1.0 审查 P0-5 修正）

**v1.0 问题**：现有 `query_memory_metrics` 是同步 `def` 且返回结构含 `used_gb`/`total_gb`，与 CPU 不同。PRD 说"复用 CPU 的 adapter"但没指出差异。

**修正方案**：

1. 改为 `async def`（与 CPU 一致）
2. 新增 `adapt_prometheus_memory_to_agent_format` 函数（基于 CPU adapter 修改 metric_name 和 threshold）
3. 返回结构保持和现有 mock 一致（含 `used_gb`/`total_gb`）

```python
SHANGHAI_TZ = timezone(timedelta(hours=8))
MEMORY_THRESHOLD = 70.0  # 内存告警阈值（与 mock 一致）

def _empty_memory_response(service_name: str, interval: str) -> dict:
    """内存空数据降级返回"""
    return {
        "service_name": service_name,
        "metric_name": "memory_usage_percent",
        "interval": interval,
        "data_points": [],
        "statistics": {
            "avg": 0, "max": 0, "min": 0, "p95": 0,
            "memory_pressure": False
        },
        "alert_info": {
            "triggered": False, "threshold": MEMORY_THRESHOLD,
            "message": "该时间范围内无内存监控数据"
        }
    }

def _error_memory_response(service_name: str, message: str) -> dict:
    """内存错误降级返回"""
    return {
        "service_name": service_name,
        "metric_name": "memory_usage_percent",
        "interval": "1m",
        "data_points": [],
        "statistics": {
            "avg": 0, "max": 0, "min": 0, "p95": 0,
            "memory_pressure": False
        },
        "alert_info": {
            "triggered": False, "threshold": MEMORY_THRESHOLD,
            "message": f"内存监控查询失败: {message}"
        }
    }

def adapt_prometheus_memory_to_agent_format(
    prom_response: dict, service_name: str, interval: str
) -> dict:
    """将 Prometheus 内存指标响应转换为 Agent 兼容格式

    与 CPU adapter 的差异：
    1. metric_name = "memory_usage_percent"
    2. 阈值 70%（CPU 是 80%）
    3. spike_detected 字段名为 memory_pressure
    4. data_points 含 used_gb / total_gb 字段（与 mock 一致）
    """
    result = prom_response.get("data", {}).get("result", [])
    if not result:
        return _empty_memory_response(service_name, interval)

    values = result[0].get("values", [])
    total_gb = 0.5  # 容器内存限制 512m = 0.5GB

    data_points = []
    for ts_seconds, value_str in values:
        dt = datetime.fromtimestamp(ts_seconds, tz=SHANGHAI_TZ)
        timestamp = dt.strftime("%H:%M")
        memory_percent = round(float(value_str), 1)
        used_gb = round((memory_percent / 100.0) * total_gb, 2)
        data_points.append({
            "timestamp": timestamp,
            "value": memory_percent,
            "used_gb": used_gb,
            "total_gb": total_gb
        })

    all_values = [dp["value"] for dp in data_points]
    max_val = max(all_values) if all_values else 0
    memory_pressure = max_val > MEMORY_THRESHOLD

    return {
        "service_name": service_name,
        "metric_name": "memory_usage_percent",
        "interval": interval,
        "data_points": data_points,
        "statistics": {
            "avg": round(sum(all_values) / len(all_values), 2) if all_values else 0,
            "max": max_val,
            "min": min(all_values) if all_values else 0,
            "p95": _calc_p95(all_values),
            "memory_pressure": memory_pressure
        },
        "alert_info": {
            "triggered": memory_pressure,
            "threshold": MEMORY_THRESHOLD,
            "message": "内存使用率超过 70% 阈值，存在内存压力" if memory_pressure else "内存使用率正常"
        }
    }


@mcp.tool()
@log_tool_call
async def query_memory_metrics(
    service_name: str,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    interval: str = "1m"
) -> Dict[str, Any]:
    """查询服务的内存使用监控数据。（工具签名和文档不变）"""
    if not service_name or not SERVICE_NAME_PATTERN.match(service_name):
        return _error_memory_response(service_name or "unknown", "服务名格式无效")

    try:
        prom_start = parse_time_to_unix(start_time, default_offset_hours=-1)
        prom_end = parse_time_to_unix(end_time, default_offset_hours=0)
        step = parse_interval_to_step(interval)

        query = f'order_memory_usage_percent{{service="{service_name}"}}'

        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{PROMETHEUS_URL}/api/v1/query_range",
                params={"query": query, "start": prom_start, "end": prom_end, "step": step}
            )

        if resp.status_code != 200:
            return _error_memory_response(service_name, f"Prometheus 返回 HTTP {resp.status_code}")

        prom_data = resp.json()
        if prom_data.get("status") != "success":
            return _error_memory_response(service_name, "Prometheus 查询失败")

        return adapt_prometheus_memory_to_agent_format(prom_data, service_name, interval)

    except httpx.TimeoutException:
        return _error_memory_response(service_name, "监控查询超时（5秒）")
    except httpx.ConnectError:
        return _error_memory_response(service_name, "监控服务不可用")
    except Exception as e:
        return _error_memory_response(service_name, f"查询异常: {str(e)}")
```

#### 9.1.3 list_monitored_services

```python
services = ["order-service"]  # 从 "demo-service" 改为 "order-service"
```

### 9.2 cls_server.py 改造（v1.0 审查 P0-4 + P1-1 补充完整代码）

#### 9.2.1 Loki API 时间戳转换（v1.0 审查 P1-1）

**关键事实**：现有 `cls_server.py` 的 `search_log` 工具入参 `start_time`/`end_time` 是**毫秒时间戳**（int 类型），而 Loki API 用**纳秒时间戳**。转换：`毫秒 * 1_000_000 = 纳秒`。

#### 9.2.2 search_log 改造

```python
import httpx
from datetime import datetime, timezone, timedelta

LOKI_URL = "http://localhost:3100"
SHANGHAI_TZ = timezone(timedelta(hours=8))

def adapt_loki_to_agent_format(
    loki_response: dict,
    topic_id: str,
    start_time: int,
    end_time: int,
    query: str,
    limit: int
) -> dict:
    """将 Loki query_range 响应转换为 Agent 兼容格式

    Loki 返回格式：
    {"status":"success","data":{"resultType":"streams","result":[
      {"stream":{"job":"order-service"},"values":[["1708012345000000000","{\"level\":\"ERROR\",...}"]]}
    ]}}

    转换为 mock 兼容格式：
    {"topic_id":"...","logs":[{"timestamp":"10:05","level":"ERROR","message":"..."}]}
    """
    result = loki_response.get("data", {}).get("result", [])
    if not result:
        return {
            "topic_id": topic_id, "start_time": start_time, "end_time": end_time,
            "query": query, "limit": limit, "total": 0, "logs": [],
            "took_ms": 0, "message": "未查询到日志"
        }

    logs = []
    for stream in result:
        stream_labels = stream.get("stream", {})
        for ts_ns, log_line in stream.get("values", []):
            # 纳秒时间戳 → "HH:MM:SS" 字符串
            ts_seconds = int(ts_ns) / 1_000_000_000
            dt = datetime.fromtimestamp(ts_seconds, tz=SHANGHAI_TZ)
            timestamp = dt.strftime("%H:%M:%S")

            # 尝试解析 JSON 日志（order-service 输出的是 JSON）
            try:
                log_obj = json.loads(log_line)
                level = log_obj.get("level", "INFO")
                message = log_obj.get("message", log_line)
            except (json.JSONDecodeError, TypeError):
                # 非 JSON 日志，尝试从文本提取级别
                level = "INFO"
                message = log_line
                if "ERROR" in log_line.upper():
                    level = "ERROR"
                elif "WARN" in log_line.upper():
                    level = "WARN"

            logs.append({
                "timestamp": timestamp,
                "level": level,
                "message": message
            })

    # 按时间排序
    logs.sort(key=lambda x: x["timestamp"])

    # 限制返回数量
    if len(logs) > limit:
        logs = logs[:limit]

    return {
        "topic_id": topic_id, "start_time": start_time, "end_time": end_time,
        "query": query, "limit": limit, "total": len(logs), "logs": logs,
        "took_ms": 50, "message": f"成功查询 {len(logs)} 条日志"
    }


@mcp.tool()
@log_tool_call
async def search_log(
    topic_id: str,
    start_time: int,
    end_time: int,
    query: Optional[str] = None,
    limit: int = 100
) -> Dict[str, Any]:
    """基于提供的查询参数搜索日志。（工具签名和文档不变）

    改造：从 mock 改为查 Loki API
    时间戳转换：入参毫秒 → Loki 纳秒（* 1_000_000）
    """
    try:
        # 毫秒 → 纳秒
        loki_start = str(start_time * 1_000_000)
        loki_end = str(end_time * 1_000_000)

        # 构造 LogQL
        logql = query if query else '{job="order-service"}'

        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{LOKI_URL}/loki/api/v1/query_range",
                params={
                    "query": logql,
                    "start": loki_start,
                    "end": loki_end,
                    "limit": limit
                }
            )

        if resp.status_code != 200:
            return {
                "topic_id": topic_id, "total": 0, "logs": [],
                "message": f"Loki 查询失败: HTTP {resp.status_code}"
            }

        loki_data = resp.json()
        return adapt_loki_to_agent_format(
            loki_data, topic_id, start_time, end_time, query, limit
        )

    except httpx.TimeoutException:
        return {"topic_id": topic_id, "total": 0, "logs": [], "message": "日志查询超时"}
    except httpx.ConnectError:
        return {"topic_id": topic_id, "total": 0, "logs": [], "message": "日志服务不可用"}
    except Exception as e:
        return {"topic_id": topic_id, "total": 0, "logs": [], "message": f"查询异常: {str(e)}"}
```

#### 9.2.3 search_topic_by_service_name 改造

```python
@mcp.tool()
@log_tool_call
async def search_topic_by_service_name(
    service_name: str,
    region_code: Optional[str] = None,
    fuzzy: bool = True
) -> Dict[str, Any]:
    """根据服务名称搜索相关的日志主题信息。（工具签名不变）

    改造：从 mock 改为查 Loki labels API
    """
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            # 查询 Loki 中有哪些 job 标签
            resp = await client.get(f"{LOKI_URL}/loki/api/v1/label/job/values")

        if resp.status_code == 200:
            jobs = resp.json().get("data", [])
            # 模糊匹配
            if fuzzy:
                matched = [j for j in jobs if service_name.lower() in j.lower() or j.lower() in service_name.lower()]
            else:
                matched = [j for j in jobs if j == service_name]

            topics = []
            for job in matched:
                topics.append({
                    "topic_id": f"loki-{job}",
                    "topic_name": f"{job} 日志",
                    "service_name": job,
                    "region_code": region_code or "local",
                    "create_time": "2026-01-01 00:00:00",
                    "log_count": 0,
                    "description": f"{job} 服务的应用日志"
                })

            return {
                "total": len(topics), "topics": topics,
                "query": {"service_name": service_name, "region_code": region_code, "fuzzy": fuzzy},
                "message": f"找到 {len(topics)} 个匹配的日志主题" if topics else f"未找到服务 '{service_name}' 的日志主题"
            }
        else:
            return {"total": 0, "topics": [], "message": f"Loki 查询失败: HTTP {resp.status_code}"}

    except Exception as e:
        return {"total": 0, "topics": [], "message": f"查询异常: {str(e)}"}
```

### 9.3 aiops_service.py 改造

```python
# 服务名从 demo-service 改为 order-service
aiops_task = dedent("""诊断当前系统是否存在告警，如果存在告警请详细分析告警原因并生成诊断报告。
            请重点关注 order-service 服务的 CPU、内存等监控指标。
            ...（报告格式不变）""")
```

---

## 10. 风险评估（v1.0 审查 P0-1 新增）

| 风险 | 概率 | 影响 | 应对措施 |
|------|------|------|---------|
| **CPU 飙高不达标** | 中 | 高 | 加 `pg_sleep(1)` 保底；如仍不够增加并发数到 200 或 pg_sleep 到 2 秒；先验证单请求耗时 > 1s |
| **psutil 容器内精度问题** | 中 | 中 | CPU 归一化到 0-100%（除以核数）；内存读取 cgroup `/sys/fs/cgroup/memory.max`；设置 `mem_limit: 512m` |
| **Loki 日志采集延迟** | 中 | 中 | 压测后等 30s 再诊断；查询时间范围设最近 5 分钟；空日志时降级返回 |
| **httpx 自调用死锁** | — | — | 已修正：压测接口直接调内部函数，不经 HTTP |
| **asyncpg 连接池耗尽** | 低 | 中 | 连接池 `max_size=20`，100 并发时 80 个等待，`timeout=30s` 后抛异常产生 ERROR 日志 |
| **Replanner 提前终止** | 中 | 高 | Replanner `MAX_STEPS=8`，步骤 >= 5 禁止 replan。如果查日志消耗步骤导致提前终止，需调优提示词或增加 MAX_STEPS。监控诊断日志确认步骤数 |
| **MCP 客户端重试延迟** | 低 | 中 | 3 次重试 1+2+4=7s + 超时 5s×3，最坏 22s。延迟要求 < 5 分钟，可接受 |
| **Docker 网络不通** | 低 | 中 | 所有容器在同一 `aiops-net` 网络 |
| **PostgreSQL 初始化慢** | 低 | 低 | 10 万条 INSERT 约 10-30 秒，容器启动后等 30s 再验证 |
| **Loki 配置错误导致采集失败** | 中 | 中 | 启动后先用 curl 验证 Loki API 可用，再查 labels 确认有数据 |

---

## 11. 验收方案

### 11.1 验收流程

```
步骤 1: 启动基础设施
  docker compose -f infra/docker-compose.yml up -d --build
  → 等待 30 秒让 PostgreSQL 初始化 10 万条数据
  → 确认 5 个容器运行：postgres/order-service/prometheus/loki/promtail

步骤 2: 验证 order-service 业务正常
  curl http://localhost:8080/api/products  → 返回 10 条商品
  curl -X POST http://localhost:8080/api/orders -H "Content-Type: application/json" -d '{"product_id":1,"quantity":1}'
  → 返回订单创建成功（注意：单次请求应耗时 > 1s 因 pg_sleep）

步骤 3: 验证 Prometheus 采集
  curl "http://localhost:9090/api/v1/query?query=order_cpu_usage_percent"
  → 返回 CPU 值
  curl "http://localhost:9090/api/v1/query?query=order_memory_usage_percent"
  → 返回内存值

步骤 4: 验证 Loki 日志
  curl -G "http://localhost:3100/loki/api/v1/query" --data-urlencode 'query={job="order-service"}'
  → 返回日志（可能需要等 5 秒让 Promtail 采集）

步骤 5: 触发故障
  curl -X POST "http://localhost:8080/api/load-test?count=100"
  → 等待 30 秒让压测完成 + Prometheus 采集 + Loki 入库

步骤 6: 验证故障数据
  curl "http://localhost:9090/api/v1/query?query=order_cpu_usage_percent"
  → 确认 CPU > 80%
  curl -G "http://localhost:3100/loki/api/v1/query_range" --data-urlencode 'query={job="order-service"} |= "ERROR"' --data-urlencode 'start=...' --data-urlencode 'end=...'
  → 确认有 ERROR 日志

步骤 7: 运行 AIOps 诊断
  curl -X POST http://localhost:9900/api/aiops -d '{"session_id":"test"}' --no-buffer
  → 收集 SSE 事件流，检查无 error 事件

步骤 8: 验收诊断报告
  → 检查报告是否满足验收清单
```

### 11.2 验收检查清单

| # | 检查项 | 通过标准 | 结果 |
|---|--------|---------|------|
| 1 | order-service 业务正常 | `/api/products` 返回商品列表 | ☐ |
| 2 | 数据库有 10 万条订单 | `SELECT COUNT(*) FROM orders` 返回 100000 | ☐ |
| 3 | 单次下单耗时 > 1 秒 | 创建订单返回的 elapsed > 1.0s | ☐ |
| 4 | Prometheus 采集到 CPU 指标 | query API 返回 `order_cpu_usage_percent` | ☐ |
| 5 | Prometheus 采集到内存指标 | query API 返回 `order_memory_usage_percent` | ☐ |
| 6 | Loki 采集到日志 | query API 返回 order-service 日志 | ☐ |
| 7 | 压测触发 CPU 飙高 | Prometheus 中 CPU > 80% | ☐ |
| 8 | 压测产生 ERROR 日志 | Loki 中有 level:ERROR 日志 | ☐ |
| 9 | Agent 诊断无报错 | SSE 流无 error 事件 | ☐ |
| 10 | 报告识别 CPU 故障 | 报告包含"CPU"相关表述 | ☐ |
| 11 | 报告定位到 order-service | 报告包含"order-service" | ☐ |
| 12 | 报告引用真实监控数据 | CPU 数值与 Prometheus 一致 | ☐ |
| 13 | 报告引用真实日志 | 报告包含日志内容（非空，非"未找到"） | ☐ |
| 14 | 报告包含根因分析 | 报告提及"慢SQL"或"全表扫描"或"COUNT"或"pg_sleep"或"请求堆积"或"数据库查询缓慢"之一 | ☐ |
| 15 | 报告包含处置建议 | 报告有可操作建议（如"优化SQL""添加索引""限制并发"等） | ☐ |
| 16 | 诊断耗时 < 5 分钟 | — | ☐ |

### 11.3 与 POC 验收的关键差异

| 维度 | POC 验收 | 本次验收 |
|------|---------|---------|
| 服务 | demo-service（无数据库） | order-service（有 PostgreSQL） |
| CPU 指标 | `set(95.0)` 固定值 | psutil 采集真实进程 CPU |
| 内存指标 | mock 算法 | psutil 采集真实进程内存 |
| 日志 | 空（cls_server 是 mock） | 真实业务日志（Loki 采集） |
| 根因 | 死循环（显而易见） | 慢 SQL（需关联分析） |
| 报告日志证据 | 空 | 有真实 ERROR 日志 |
| 报告根因结论 | 靠猜 | 有日志+监控证据支撑 |

---

## 12. 实施计划

### 12.1 任务分解

| 序号 | 任务 | 产出物 | 预估时间 |
|------|------|--------|---------|
| T1 | 创建 order-service 应用 | `order-service/app.py` + `requirements.txt` + `Dockerfile` | 2h |
| T2 | 编写数据库初始化脚本 | `infra/init.sql`（建表 + 10万条数据） | 0.5h |
| T3 | 更新 docker-compose | `infra/docker-compose.yml`（+postgres +loki +promtail） | 0.5h |
| T4 | 编写 Loki/Promtail 配置 | `infra/loki-config.yml` + `infra/promtail-config.yml` | 0.5h |
| T5 | 启动基础设施并验证 | 业务正常 + Prometheus 有数据 + Loki 有日志 | 1h |
| T6 | 改造 monitor_server.py | CPU/内存指标名改为 order_，内存接真实数据 | 1h |
| T7 | 改造 cls_server.py | search_topic/search_log 接 Loki | 2h |
| T8 | 修改 aiops_service.py | 任务描述服务名改为 order-service | 0.1h |
| T9 | 端到端验证 + 调试 | 压测 → 诊断 → 验收清单 | 1.5h |
| **合计** | | | **~9.5h** |

### 12.2 依赖关系

```
T1 (order-service) ──┐
T2 (init.sql) ───────┼──> T5 (启动验证) ──┬──> T6 (改monitor) ──┐
T3 (docker-compose) ─┤                   ├──> T7 (改cls) ──────┼──> T9 (验证)
T4 (loki/promtail) ──┘                   │                      │
                                         T8 (改aiops) ─────────┘
```

T1/T2/T3/T4 可并行。T6/T7 依赖 T5。T9 依赖 T6/T7/T8。

### 12.3 里程碑

| 里程碑 | 标志 |
|--------|------|
| M1 | order-service 业务正常 + 数据库有 10 万条数据 |
| M2 | Prometheus 采集到真实 CPU/内存 + Loki 采集到日志 |
| M3 | 压测触发 CPU > 80% + 产生 ERROR 日志 |
| M4 | Agent 诊断报告通过验收清单（16 项） |

---

## 13. 文件清单

### 13.1 新增文件

| 文件 | 说明 |
|------|------|
| `order-service/app.py` | 订单微服务主程序 |
| `order-service/requirements.txt` | Python 依赖 |
| `order-service/Dockerfile` | 容器构建文件 |
| `infra/init.sql` | 数据库初始化脚本（建表+10万条数据） |
| `infra/loki-config.yml` | Loki 配置 |
| `infra/promtail-config.yml` | Promtail 采集配置 |

### 13.2 修改文件

| 文件 | 修改内容 |
|------|---------|
| `infra/docker-compose.yml` | 替换 demo-service 为 order-service + postgres + loki + promtail |
| `infra/prometheus.yml` | 采集目标改为 order-service |
| `mcp_servers/monitor_server.py` | 指标名 demo_ → order_，内存接真实数据（async + adapter） |
| `mcp_servers/cls_server.py` | search_topic/search_log 接 Loki（含纳秒时间戳转换 + adapter） |
| `app/services/aiops_service.py` | 任务描述服务名改为 order-service |

### 13.3 保留不动的文件

| 文件 | 说明 |
|------|------|
| `demo-service/` | 保留作为 POC 历史记录 |
| `app/agent/` 全部 | Agent 编排逻辑零改动 |
| `app/agent/mcp_client.py` | MCP 客户端不变 |
| `app/services/rag_agent_service.py` | RAG Agent 不变 |
| `app/config.py` | MCP 服务地址不变（localhost:8003/8004） |
