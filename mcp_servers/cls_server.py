"""腾讯云 CLS (Cloud Log Service) MCP Server

本地实现的 CLS 日志服务 MCP Server，提供日志查询、检索和分析功能。
"""

import logging
import functools
import json
from typing import Dict, Any, Optional
from datetime import datetime, timedelta, timezone
from fastmcp import FastMCP

import httpx

# Loki 配置
LOKI_URL = "http://localhost:3100"
SHANGHAI_TZ = timezone(timedelta(hours=8))

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("CLS_MCP_Server")

mcp = FastMCP("CLS")


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
    return datetime.now() + timedelta(hours=default_offset_hours)


def generate_time_series(base_time: datetime, minutes_offset: int) -> str:
    """生成基于基准时间的时间字符串。

    Args:
        base_time: 基准时间
        minutes_offset: 分钟偏移量

    Returns:
        str: 格式化的时间字符串
    """
    result_time = base_time + timedelta(minutes=minutes_offset)
    return result_time.strftime("%Y-%m-%d %H:%M:%S")


@mcp.tool()
@log_tool_call
def get_current_timestamp() -> int:
    """获取当前时间戳（以毫秒为单位）。
    
    此工具用于获取标准的毫秒时间戳，可用于：
    1. 作为 search_log 的 end_time 参数（查询到现在）
    2. 计算历史时间点作为 start_time 参数
    
    Returns:
        int: 当前时间戳（毫秒），例如: 1708012345000
    
    使用示例:
        # 获取当前时间
        current = get_current_timestamp()
        
        # 计算15分钟前的时间
        fifteen_min_ago = current - (15 * 60 * 1000)
        
        # 计算1小时前的时间
        one_hour_ago = current - (60 * 60 * 1000)
        
        # 用于搜索最近15分钟的日志
        search_log(
            topic_id="loki-order-service",
            start_time=fifteen_min_ago,
            end_time=current
        )
    """
    return int(datetime.now().timestamp() * 1000)


@mcp.tool()
@log_tool_call
def get_region_code_by_name(region_name: str) -> Dict[str, Any]:
    """根据地区名称搜索对应的地区参数。

    Args:
        region_name: 地区名称（如：北京、上海、广州等）

    Returns:
        Dict: 包含地区代码和相关信息的字典
            - region_code: 地区代码
            - region_name: 地区名称
            - available: 是否可用
    """
    # 模拟地区映射表（实际应该从配置或数据库读取）
    region_mapping = {
        "北京": {"region_code": "ap-beijing", "region_name": "北京", "available": True},
        "上海": {"region_code": "ap-shanghai", "region_name": "上海", "available": True},
        "广州": {"region_code": "ap-guangzhou", "region_name": "广州", "available": True},
    }

    result = region_mapping.get(region_name)
    if result:
        return result
    else:
        return {
            "region_code": None,
            "region_name": region_name,
            "available": False,
            "error": f"未找到地区: {region_name}"
        }


@mcp.tool()
@log_tool_call
def get_topic_info_by_name(topic_name: str, region_code: Optional[str] = None) -> Dict[str, Any]:
    """根据主题名称搜索相关的主题信息。

    Args:
        topic_name: 主题名称
        region_code: 地区代码（可选）

    Returns:
        Dict: 包含主题信息的字典
            - topic_id: 主题ID
            - topic_name: 主题名称
            - region_code: 所属地区
            - create_time: 创建时间
            - log_count: 日志数量
    """
    mock_topics = [
        {
            "topic_id": "topic-001",
            "topic_name": "数据同步服务日志",
            "service_name": "data-sync-service",
            "region_code": "ap-beijing",
            "create_time": "2024-01-01 10:00:00",
            "log_count": 0,
            "description": "服务应用日志"
        }
    ]

    # 根据名称和地区筛选
    for topic in mock_topics:
        if topic["topic_name"] == topic_name:
            if region_code is None or topic["region_code"] == region_code:
                return topic

    return {
        "topic_id": None,
        "topic_name": topic_name,
        "region_code": region_code,
        "error": f"未找到主题: {topic_name}"
    }


@mcp.tool()
@log_tool_call
async def search_topic_by_service_name(
    service_name: str,
    region_code: Optional[str] = None,
    fuzzy: bool = True
) -> Dict[str, Any]:
    """根据服务名称搜索相关的日志主题信息，支持模糊搜索。
    
    此工具用于根据服务名称查找对应的日志主题（topic），便于后续进行日志查询。
    
    Args:
        service_name: 服务名称（必填）
            示例: "order-service", "order"
            说明: 当 fuzzy=True 时，支持部分匹配
        
        region_code: 地区代码（可选）
        
        fuzzy: 是否启用模糊搜索（可选，默认 True）
    
    Returns:
        Dict: 搜索结果
            - total: 匹配到的主题数量
            - topics: 主题列表
            - query: 查询条件
    """
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{LOKI_URL}/loki/api/v1/label/job/values")

        if resp.status_code == 200:
            jobs = resp.json().get("data", [])
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
                "total": len(topics),
                "topics": topics,
                "query": {"service_name": service_name, "region_code": region_code, "fuzzy": fuzzy},
                "message": f"找到 {len(topics)} 个匹配的日志主题" if topics else f"未找到服务 '{service_name}' 的日志主题"
            }
        else:
            return {"total": 0, "topics": [], "message": f"Loki 查询失败: HTTP {resp.status_code}"}

    except httpx.TimeoutException:
        return {"total": 0, "topics": [], "message": "日志服务查询超时"}
    except httpx.ConnectError:
        return {"total": 0, "topics": [], "message": "日志服务不可用"}
    except Exception as e:
        return {"total": 0, "topics": [], "message": f"查询异常: {str(e)}"}


@mcp.tool()
@log_tool_call
async def search_log(
    topic_id: str,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    query: Optional[str] = None,
    limit: int = 100
) -> Dict[str, Any]:
    """搜索 order-service 服务的日志。

    注意：此工具会自动查询 order-service 服务的日志，topic_id 参数不影响查询结果。
    start_time 和 end_time 如果不传或传了非整数值，会自动使用最近5分钟的时间范围。
    如果需要过滤特定级别的日志，可以在 query 参数中传入级别关键词，如 "ERROR" 或 "WARN"。

    Args:
        topic_id: 主题ID（可传任意值，不影响查询，工具自动查 order-service）
        start_time: 开始时间戳（毫秒数字，可不传，不传则自动查最近5分钟）
        end_time: 结束时间戳（毫秒数字，可不传，不传则自动用当前时间）
        query: 查询关键词（可选）
            传入 "ERROR" 可过滤错误级别日志
            传入 "WARN" 可过滤警告级别日志
            传入 "ERROR OR WARN" 可同时查询多个级别
            不传则返回全部日志
        limit: 返回结果数量限制（默认100）

    Returns:
        Dict: 日志查询结果
            - total: 日志条数
            - logs: 日志列表，每条包含 timestamp, level, message
            - message: 查询状态消息
    """
    try:
        # 时间范围兜底：处理 Agent 传了模板字符串、0、或错误时间的情况
        now_ms = int(datetime.now().timestamp() * 1000)
        try:
            start_time = int(start_time)
            end_time = int(end_time)
        except (ValueError, TypeError):
            # Agent 传了非整数值（如模板字符串），用最近 5 分钟
            start_time = now_ms - 5 * 60 * 1000
            end_time = now_ms

        if start_time <= 0 or end_time <= 0:
            # Agent 传了 0 或负数，用最近 5 分钟
            start_time = now_ms - 5 * 60 * 1000
            end_time = now_ms
        elif abs(now_ms - end_time) > 3600 * 1000:
            # Agent 传了过期时间（距当前超过 1 小时），用最近 5 分钟
            start_time = now_ms - 5 * 60 * 1000
            end_time = now_ms

        # 毫秒 → 纳秒（Loki 用纳秒时间戳）
        loki_start = str(start_time * 1_000_000)
        loki_end = str(end_time * 1_000_000)

        # 构造 LogQL：强制使用 order-service 作为 job 名
        # 无论 Agent 传什么 query，都提取关键词后用正确的 job 名重新构造
        if query and query.strip():
            if 'level:' in query.lower():
                # CLS 语法 "level:ERROR" 或 "level:ERROR OR level:WARN"
                level_part = query.split('level:')[-1].strip().strip('"').strip("'")
                if ' or ' in level_part.lower():
                    # 多级别查询：用 LogQL 正则匹配 |~ 实现 OR 语义
                    parts = [p.strip().strip('"').strip("'") for p in level_part.lower().split(' or ')]
                    pattern = '|'.join(parts)
                    logql = '{job="order-service"} |~ "' + pattern + '"'
                else:
                    logql = '{job="order-service"} |= "' + level_part + '"'
            elif '{' in query:
                # Agent 传了 LogQL，但 job 名可能不对
                # 提取 |= 后面的关键词，用正确的 job 名重新构造
                import re
                keywords = re.findall(r'\|=\s*"([^"]+)"', query)
                logql = '{job="order-service"}'
                if len(keywords) == 1:
                    logql += f' |= "{keywords[0]}"'
                elif len(keywords) > 1:
                    # 多关键词用正则 OR 匹配
                    pattern = '|'.join(keywords)
                    logql += f' |~ "{pattern}"'
            else:
                # 纯关键词，检查是否包含 OR（如 "ERROR OR WARN"）
                if ' OR ' in query.upper():
                    parts = [p.strip().strip('"').strip("'") for p in query.upper().split(' OR ')]
                    pattern = '|'.join(parts)
                    logql = '{job="order-service"} |~ "' + pattern + '"'
                else:
                    logql = '{job="order-service"} |= "' + query + '"'
        else:
            # 默认查询：所有 order-service 日志
            logql = '{job="order-service"}'

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
        return _adapt_loki_to_agent_format(loki_data, topic_id, start_time, end_time, query, limit)

    except httpx.TimeoutException:
        return {"topic_id": topic_id, "total": 0, "logs": [], "message": "日志查询超时"}
    except httpx.ConnectError:
        return {"topic_id": topic_id, "total": 0, "logs": [], "message": "日志服务不可用"}
    except Exception as e:
        return {"topic_id": topic_id, "total": 0, "logs": [], "message": f"查询异常: {str(e)}"}


def _adapt_loki_to_agent_format(
    loki_response: dict,
    topic_id: str,
    start_time: int,
    end_time: int,
    query: str,
    limit: int
) -> dict:
    """将 Loki query_range 响应转换为 Agent 兼容格式"""
    result = loki_response.get("data", {}).get("result", [])
    if not result:
        return {
            "topic_id": topic_id, "start_time": start_time, "end_time": end_time,
            "query": query, "limit": limit, "total": 0, "logs": [],
            "took_ms": 0, "message": "未查询到日志"
        }

    logs = []
    for stream in result:
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

    logs.sort(key=lambda x: x["timestamp"])

    if len(logs) > limit:
        logs = logs[:limit]

    return {
        "topic_id": topic_id, "start_time": start_time, "end_time": end_time,
        "query": query, "limit": limit, "total": len(logs), "logs": logs,
        "took_ms": 50, "message": f"成功查询 {len(logs)} 条日志"
    }



if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="127.0.0.1", port=8003, path="/mcp")
