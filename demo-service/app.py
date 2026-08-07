"""Demo Fault Service - 故障注入微服务

用于 POC 验证 AIOps Agent 能否诊断真实故障。
提供 CPU 故障注入接口和 Prometheus 指标端点。
"""

import threading
import math
from fastapi import FastAPI
from prometheus_client import Gauge, generate_latest, CONTENT_TYPE_LATEST
from fastapi.responses import Response

app = FastAPI(title="Demo Fault Service", description="AIOps POC 故障注入服务")

# 故障状态
cpu_fault_active = False

# Prometheus 指标：CPU 使用率（百分比 0-100）
cpu_gauge = Gauge(
    'demo_cpu_usage_percent',
    'CPU usage percent of demo service',
    ['service']
)


@app.get("/api/order")
def get_order():
    """正常业务接口（模拟订单查询）"""
    return {"order_id": "ORD-001", "status": "completed", "amount": 99.9}


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

    return {"status": "triggered", "message": "CPU 故障已触发，CPU 使用率将飙升至 ~95%"}


@app.get("/fault/stop")
def stop_fault():
    """停止所有故障注入"""
    global cpu_fault_active
    cpu_fault_active = False
    return {"status": "stopped", "message": "故障已停止，CPU 使用率将恢复正常"}


@app.get("/metrics")
def metrics():
    """Prometheus 指标端点

    上报当前 CPU 使用率（百分比 0-100）：
    - 故障激活时上报 95.0
    - 正常时上报 5.0
    """
    cpu_value = 95.0 if cpu_fault_active else 5.0
    cpu_gauge.labels(service="demo-service").set(cpu_value)
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/health")
def health():
    """健康检查"""
    return {"status": "ok", "cpu_fault_active": cpu_fault_active}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
