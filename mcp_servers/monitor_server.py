"""智能运维监控 MCP Server

本地实现的监控服务 MCP Server，提供：
- 监控数据查询（CPU、内存、磁盘、网络等）
- 进程信息查询
- 历史工单查询
- 服务信息查询

用于支持运维 Agent 的故障排查场景。
"""

import logging
import functools
import json
import random
import re
from typing import Dict, Any, Optional
from datetime import datetime, timedelta, timezone
from fastmcp import FastMCP

import httpx

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("Monitor_MCP_Server")

mcp = FastMCP("Monitor")


def log_tool_call(func):
    """装饰器：记录工具调用的日志，支持同步和异步函数"""
    import asyncio
    import functools

    if asyncio.iscoroutinefunction(func):
        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            method_name = func.__name__
            logger.info(f"=" * 80)
            logger.info(f"调用方法: {method_name}")
            if kwargs:
                try:
                    params_str = json.dumps(kwargs, ensure_ascii=False, indent=2)
                except (TypeError, ValueError):
                    params_str = str(kwargs)
                logger.info(f"参数信息:\n{params_str}")
            else:
                logger.info("参数信息: 无")
            try:
                result = await func(*args, **kwargs)
                logger.info(f"返回状态: SUCCESS")
                if isinstance(result, dict):
                    summary = {k: v if not isinstance(v, (list, dict)) else f"<{type(v).__name__} with {len(v)} items>"
                              for k, v in list(result.items())[:5]}
                    logger.info(f"返回结果摘要: {json.dumps(summary, ensure_ascii=False)}")
                else:
                    logger.info(f"返回结果: {result}")
                logger.info(f"=" * 80)
                return result
            except Exception as e:
                logger.error(f"返回状态: ERROR")
                logger.error(f"错误信息: {str(e)}")
                logger.error(f"=" * 80)
                raise
        return async_wrapper
    else:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            method_name = func.__name__
            logger.info(f"=" * 80)
            logger.info(f"调用方法: {method_name}")
            if kwargs:
                try:
                    params_str = json.dumps(kwargs, ensure_ascii=False, indent=2)
                except (TypeError, ValueError):
                    params_str = str(kwargs)
                logger.info(f"参数信息:\n{params_str}")
            else:
                logger.info("参数信息: 无")
            try:
                result = func(*args, **kwargs)
                logger.info(f"返回状态: SUCCESS")
                if isinstance(result, dict):
                    summary = {k: v if not isinstance(v, (list, dict)) else f"<{type(v).__name__} with {len(v)} items>"
                              for k, v in list(result.items())[:5]}
                    logger.info(f"返回结果摘要: {json.dumps(summary, ensure_ascii=False)}")
                else:
                    logger.info(f"返回结果: {result}")
                logger.info(f"=" * 80)
                return result
            except Exception as e:
                logger.error(f"返回状态: ERROR")
                logger.error(f"错误信息: {str(e)}")
                logger.error(f"=" * 80)
                raise
        return wrapper


# ============================================================
# 辅助函数
# ============================================================

def parse_time_or_default(time_str: Optional[str], default_offset_hours: int = 0) -> datetime:
    """解析时间字符串或返回默认时间。

    Args:
        time_str: 时间字符串（格式：YYYY-MM-DD HH:MM:SS）
        default_offset_hours: 默认时间偏移（小时）

    Returns:
        datetime: 解析后的时间对象
    """
    if time_str:
        try:
            return datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    # 返回默认时间（当前时间 + 偏移）
    return datetime.now() + timedelta(hours=default_offset_hours)


def generate_time_series(base_time: datetime, minutes_offset: int, format_str: str = "%Y-%m-%d %H:%M:%S") -> str:
    """生成时间序列字符串。

    Args:
        base_time: 基准时间
        minutes_offset: 分钟偏移量
        format_str: 时间格式字符串

    Returns:
        str: 格式化的时间字符串
    """
    result_time = base_time + timedelta(minutes=minutes_offset)
    return result_time.strftime(format_str)


# ============================================================
# Prometheus 对接配置
# ============================================================

PROMETHEUS_URL = "http://localhost:9090"

# 显式指定时区（避免 Docker 容器时区与宿主机不一致）
SHANGHAI_TZ = timezone(timedelta(hours=8))

# service_name 参数校验正则（防止 PromQL 注入）
SERVICE_NAME_PATTERN = re.compile(r'^[a-zA-Z0-9_-]+$')


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


def parse_interval_to_step(interval: str) -> str:
    """将 interval 参数转换为 Prometheus step 格式

    Args:
        interval: "1m", "5m", "1h" 等

    Returns:
        str: Prometheus step 值，如 "15s", "5m"
    """
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


def _calc_p95(values: list) -> float:
    """计算 P95 百分位数"""
    if not values:
        return 0
    sorted_vals = sorted(values)
    idx = int(len(sorted_vals) * 0.95)
    if idx >= len(sorted_vals):
        idx = len(sorted_vals) - 1
    return round(sorted_vals[idx], 2)


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


def adapt_prometheus_to_agent_format(
    prom_response: dict,
    service_name: str,
    interval: str
) -> dict:
    """将 Prometheus query_range 响应转换为 Agent 兼容格式

    转换规则：
    1. values 中的 [unix_seconds, "string_value"] -> {"timestamp": "HH:MM", "value": float}
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
        # 时间戳：unix秒 -> Asia/Shanghai 时区的 "HH:MM" 字符串
        dt = datetime.fromtimestamp(ts_seconds, tz=SHANGHAI_TZ)
        timestamp = dt.strftime("%H:%M")
        # CPU 值：已是百分比(0-100)，直接转换
        cpu_percent = round(float(value_str), 1)
        data_points.append({
            "timestamp": timestamp,
            "value": cpu_percent,
            "process_id": service_name
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



# ============================================================
# Memory 指标 adapter（新增）
# ============================================================

MEMORY_THRESHOLD = 70.0


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
    4. data_points 含 used_gb / total_gb 字段
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


# ============================================================
# 监控数据查询工具
# ============================================================

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
            示例: "2026-02-14 10:00:00"
            默认值: 如果不传，默认为当前时间的1小时前
            注意: 必须使用字符串格式，而非时间戳
        
        end_time: 结束时间（可选，字符串类型）
            格式: "YYYY-MM-DD HH:MM:SS"
            示例: "2026-02-14 11:00:00"
            默认值: 如果不传，默认为当前时间
            注意: 必须使用字符串格式，而非时间戳
        
        interval: 数据聚合间隔（可选）
            可选值: "1m" (1分钟), "5m" (5分钟), "1h" (1小时)
            默认值: "1m"
            说明: 控制数据点的时间间隔

    Returns:
        Dict: CPU 监控数据
            - service_name: 服务名称
            - metric_name: 指标名称 (cpu_usage_percent)
            - interval: 数据聚合间隔
            - data_points: 数据点列表，每个点包含:
                * timestamp: 时间点（格式: HH:MM）
                * value: CPU 使用率百分比
            - statistics: 统计信息
                * avg: 平均值
                * max: 最大值
                * min: 最小值
                * p95: P95 百分位数
                * spike_detected: 是否检测到突增
            - alert_info: 告警信息
                * triggered: 是否触发告警
                * threshold: 告警阈值
                * message: 告警消息
    
    使用示例:
        # 示例1: 使用默认时间（最近1小时）
        query_cpu_metrics(service_name="demo-service")
        
        # 示例2: 指定时间范围
        query_cpu_metrics(
            service_name="demo-service",
            start_time="2026-02-14 10:00:00",
            end_time="2026-02-14 11:00:00",
            interval="5m"
        )
    """
    # 1. 参数校验（防止 PromQL 注入）
    if not service_name or not SERVICE_NAME_PATTERN.match(service_name):
        return _error_response(service_name or "unknown", "服务名格式无效")

    try:
        # 2. 时间解析：字符串 "YYYY-MM-DD HH:MM:SS" -> Unix 秒
        prom_start = parse_time_to_unix(start_time, default_offset_hours=-1)
        prom_end = parse_time_to_unix(end_time, default_offset_hours=0)
        step = parse_interval_to_step(interval)

        # 3. 构造 PromQL 查询（service_name 已校验，安全拼接）
        query = f'order_cpu_usage_percent{{service="{service_name}"}}'

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

        # 6.5 如果返回空数据，自动用最近 5 分钟重新查询
        # （Agent 可能传入错误的时间范围，导致查不到数据）
        if not prom_data.get("data", {}).get("result", []):
            logger.info("Prometheus 返回空数据，用最近 5 分钟重新查询...")
            prom_start = parse_time_to_unix(None, default_offset_hours=-1) - 4 * 3600  # 5 分钟前
            prom_end = parse_time_to_unix(None, default_offset_hours=0)
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    f"{PROMETHEUS_URL}/api/v1/query_range",
                    params={"query": query, "start": prom_start, "end": prom_end, "step": step}
                )
            if resp.status_code == 200:
                prom_data = resp.json()

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
async def query_memory_metrics(
    service_name: str,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    interval: str = "1m"
) -> Dict[str, Any]:
    """查询服务的内存使用监控数据。

    Args:
        service_name: 服务名称（必填）
            示例: "order-service"
        
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
        Dict: 内存监控数据
            - service_name: 服务名称
            - metric_name: 指标名称 (memory_usage_percent)
            - data_points: 数据点列表 (timestamp, value, used_gb, total_gb)
            - statistics: 统计信息 (avg, max, min, p95, memory_pressure)
            - alert_info: 告警信息 (triggered, threshold, message)
    """
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

        # 如果返回空数据，自动用最近 5 分钟重新查询
        if not prom_data.get("data", {}).get("result", []):
            logger.info("Prometheus 内存查询返回空数据，用最近 5 分钟重新查询...")
            prom_start = parse_time_to_unix(None, default_offset_hours=-1) - 4 * 3600
            prom_end = parse_time_to_unix(None, default_offset_hours=0)
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    f"{PROMETHEUS_URL}/api/v1/query_range",
                    params={"query": query, "start": prom_start, "end": prom_end, "step": step}
                )
            if resp.status_code == 200:
                prom_data = resp.json()

        return adapt_prometheus_memory_to_agent_format(prom_data, service_name, interval)

    except httpx.TimeoutException:
        return _error_memory_response(service_name, "监控查询超时（5秒）")
    except httpx.ConnectError:
        return _error_memory_response(service_name, "监控服务不可用")
    except Exception as e:
        return _error_memory_response(service_name, f"查询异常: {str(e)}")


# ============================================================
# 服务列表查询工具
# ============================================================

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
    
    使用示例:
        # 查询当前被监控的服务
        result = list_monitored_services()
        # result = {"total": 1, "services": ["demo-service"], "message": "..."}
    """
    # POC 阶段返回固定的服务列表
    # 后续可改为查询 Prometheus 的 /api/v1/series 或 /api/v1/targets
    services = ["order-service"]
    return {
        "total": len(services),
        "services": services,
        "message": f"当前有 {len(services)} 个服务正在被监控"
    }




if __name__ == "__main__":
    # 使用 streamable-http 模式，运行在 8004 端口
    mcp.run(transport="streamable-http", host="127.0.0.1", port=8004, path="/mcp")
