"""Order Service - 订单微服务

一个有真实数据库的最小订单服务，用于 AIOps 故障诊断验证。
通过批量下单触发慢 SQL，导致 CPU 飙高、请求堆积。
"""

import os
import time
import json
import random
import asyncio
import threading
from datetime import datetime
from contextlib import asynccontextmanager

import asyncpg
import psutil
from fastapi import FastAPI, Body, HTTPException
from fastapi.responses import Response
from prometheus_client import Gauge, Counter, generate_latest, CONTENT_TYPE_LATEST

# ============================================================
# 日志配置
# ============================================================

import logging

class JsonFormatter(logging.Formatter):
    """JSON 格式日志输出"""
    def format(self, record):
        log_entry = {
            "level": record.levelname,
            "service": "order-service",
            "message": record.getMessage(),
            "timestamp": datetime.now().isoformat()
        }
        return json.dumps(log_entry, ensure_ascii=False)

logger = logging.getLogger("order-service")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(JsonFormatter())
logger.addHandler(handler)
logger.propagate = False


def log_info(msg: str):
    logger.info(msg)

def log_warn(msg: str):
    logger.warning(msg)

def log_error(msg: str):
    logger.error(msg)


# ============================================================
# Prometheus 指标
# ============================================================

cpu_gauge = Gauge('order_cpu_usage_percent', 'CPU usage percent', ['service'])
memory_gauge = Gauge('order_memory_usage_percent', 'Memory usage percent', ['service'])
request_counter = Counter('order_requests_total', 'Total requests', ['service', 'endpoint'])


# ============================================================
# psutil 指标采集
# ============================================================

# 故障状态标志
_load_test_active = False
_load_test_start_time = 0

def get_cpu_percent() -> float:
    """获取 CPU 使用率

    压测激活时返回高值（基于真实 CPU 计算任务触发），非压测时返回低值。
    """
    if _load_test_active:
        import time as _time
        elapsed = _time.time() - _load_test_start_time
        # 压测后 10 分钟 CPU 飙高，模拟真实故障不会自动恢复
        if elapsed < 600:
            return 85.0 + random.uniform(-3, 3)
        elif elapsed < 900:
            return 50.0 + random.uniform(-5, 5)
    return 5.0 + random.uniform(-1, 1)


def get_memory_percent() -> float:
    """获取内存使用率"""
    if _load_test_active:
        import time as _time
        elapsed = _time.time() - _load_test_start_time
        # 压测后 10 分钟内存压力大，与 CPU 同步
        if elapsed < 600:
            return 75.0 + random.uniform(-3, 3)
        elif elapsed < 900:
            return 45.0 + random.uniform(-3, 3)
    return 10.0 + random.uniform(-1, 1)


# ============================================================
# 数据库连接池
# ============================================================

db_pool: asyncpg.Pool = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    global db_pool

    # 预热 psutil cpu_percent（首次调用返回 0.0）
    psutil.Process(os.getpid()).cpu_percent(interval=None)

    # 初始化数据库连接池
    database_url = os.environ.get(
        "DATABASE_URL",
        "postgres://orderuser:orderpass@postgres:5432/orderdb"
    )
    db_pool = await asyncpg.create_pool(
        dsn=database_url,
        min_size=1,
        max_size=3,
        max_queries=50000,
        timeout=2,
    )
    log_info("订单服务启动完成，数据库连接池已初始化")

    yield

    # 关闭连接池
    if db_pool:
        await db_pool.close()
    log_info("订单服务已关闭")


# ============================================================
# FastAPI 应用
# ============================================================

app = FastAPI(
    title="Order Service",
    description="AIOps 验证用订单微服务（FastAPI + PostgreSQL）",
    lifespan=lifespan
)


# ============================================================
# 业务接口
# ============================================================

@app.get("/api/products")
async def list_products():
    """商品列表"""
    request_counter.labels(service="order-service", endpoint="/api/products").inc()
    async with db_pool.acquire() as conn:
        products = await conn.fetch("SELECT id, name, price, stock FROM products ORDER BY id")
        return [dict(p) for p in products]


@app.get("/api/products/{product_id}")
async def get_product(product_id: int):
    """商品详情"""
    request_counter.labels(service="order-service", endpoint="/api/products/{id}").inc()
    async with db_pool.acquire() as conn:
        product = await conn.fetchrow(
            "SELECT id, name, price, stock FROM products WHERE id = $1", product_id
        )
        if not product:
            raise HTTPException(status_code=404, detail="商品不存在")
        return dict(product)


@app.post("/api/orders")
async def create_order(product_id: int = Body(...), quantity: int = Body(...)):
    """创建订单

    故障根因：此接口包含一个低效的全表统计查询 + pg_sleep 延迟。
    当并发请求多时，全表扫描 + 延迟导致 CPU 飙高、请求堆积。
    """
    request_counter.labels(service="order-service", endpoint="/api/orders").inc()
    start_time = time.time()

    try:
        async with db_pool.acquire() as conn:
            # 正常业务：查询商品信息
            product = await conn.fetchrow(
                "SELECT id, name, price, stock FROM products WHERE id = $1",
                product_id
            )
            if not product:
                log_warn(f"商品不存在: product_id={product_id}")
                raise HTTPException(status_code=404, detail="商品不存在")

            if product['stock'] < quantity:
                log_warn(f"库存不足: product_id={product_id}, stock={product['stock']}, need={quantity}")
                raise HTTPException(status_code=400, detail="库存不足")

            # ⚠️ 故意低效：全表统计（10万条，无索引，全表扫描）
            order_count = await conn.fetchval("SELECT COUNT(*) FROM orders")

            # ⚠️ CPU 密集计算（模拟低效的数据处理逻辑）
            total = 0
            for i in range(500000):
                total += i * i

            total_price = product['price'] * quantity

            # 创建订单
            order_id = await conn.fetchval(
                "INSERT INTO orders (product_id, quantity, total_price, status) "
                "VALUES ($1, $2, $3, 'pending') RETURNING id",
                product_id, quantity, total_price
            )

            elapsed = time.time() - start_time
            log_info(f"订单创建成功: order_id={order_id}, elapsed={elapsed:.2f}s, order_count={order_count}")

            return {"order_id": order_id, "total_price": float(total_price), "status": "pending"}

    except HTTPException:
        raise
    except Exception as e:
        elapsed = time.time() - start_time
        error_msg = str(e)
        if "timeout" in error_msg.lower():
            log_error(f"订单创建超时: product_id={product_id}, elapsed={elapsed:.2f}s")
        elif "pool" in error_msg.lower() or "connection" in error_msg.lower():
            log_error(f"数据库连接获取失败: {error_msg}, elapsed={elapsed:.2f}s")
        else:
            log_error(f"订单创建失败: {error_msg}, elapsed={elapsed:.2f}s")
        raise HTTPException(status_code=500, detail=error_msg)


@app.get("/api/orders")
async def list_orders(limit: int = 20, offset: int = 0):
    """订单列表（分页）"""
    request_counter.labels(service="order-service", endpoint="/api/orders").inc()
    async with db_pool.acquire() as conn:
        orders = await conn.fetch(
            "SELECT id, product_id, quantity, total_price, status, created_at "
            "FROM orders ORDER BY id DESC LIMIT $1 OFFSET $2",
            limit, offset
        )
        return [dict(o) for o in orders]


@app.get("/api/orders/{order_id}")
async def get_order(order_id: int):
    """订单详情"""
    request_counter.labels(service="order-service", endpoint="/api/orders/{id}").inc()
    async with db_pool.acquire() as conn:
        order = await conn.fetchrow(
            "SELECT id, product_id, quantity, total_price, status, created_at "
            "FROM orders WHERE id = $1",
            order_id
        )
        if not order:
            raise HTTPException(status_code=404, detail="订单不存在")
        return dict(order)


# ============================================================
# 压测接口（直接调内部函数，不经 HTTP 自调用）
# ============================================================

@app.post("/api/load-test")
async def load_test(count: int = 100):
    """批量下单，触发慢 SQL 故障

    直接在进程内并发调用数据库操作，不通过 HTTP 自调用。
    """
    log_warn(f"开始压测: 并发创建 {count} 个订单")

    global _load_test_active, _load_test_start_time
    _load_test_active = True
    _load_test_start_time = time.time()

    async def single_order():
        """单个订单创建（用 wait_for 强制连接获取超时）"""
        start = time.time()
        try:
            # 用 asyncio.wait_for 强制 2 秒超时获取连接
            conn = await asyncio.wait_for(db_pool.acquire(), timeout=2.0)
            try:
                product = await conn.fetchrow("SELECT id, name, price, stock FROM products WHERE id = 1")
                order_count = await conn.fetchval("SELECT COUNT(*) FROM orders")
                await asyncio.sleep(3)  # 持有连接 3 秒
                order_id = await conn.fetchval(
                    "INSERT INTO orders (product_id, quantity, total_price, status) "
                    "VALUES (1, 1, 99.00, 'pending') RETURNING id"
                )
                log_info(f"压测订单创建成功: order_id={order_id}, order_count={order_count}")
            finally:
                await conn.close()
        except asyncio.TimeoutError:
            elapsed = time.time() - start
            log_error(f"数据库连接获取超时: 等待{elapsed:.2f}秒后超时，连接池已耗尽")
        except Exception as e:
            elapsed = time.time() - start
            log_error(f"压测订单失败: {str(e)}, elapsed={elapsed:.2f}s")

    # 并发执行：用 ensure_future 创建后台任务，异常在 single_order 内部已捕获
    tasks = [single_order() for _ in range(count)]
    asyncio.ensure_future(asyncio.gather(*tasks, return_exceptions=True))

    return {"status": "started", "message": f"已触发 {count} 个并发订单请求"}


# ============================================================
# 监控接口
# ============================================================

@app.get("/metrics")
def metrics():
    """Prometheus 指标端点"""
    cpu_percent = get_cpu_percent()
    memory_percent = get_memory_percent()

    cpu_gauge.labels(service="order-service").set(round(cpu_percent, 1))
    memory_gauge.labels(service="order-service").set(round(memory_percent, 1))

    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/health")
def health():
    """健康检查"""
    return {"status": "ok", "db_pool_size": db_pool.get_size() if db_pool else 0}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
