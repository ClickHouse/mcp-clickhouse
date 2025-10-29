"""chDB MCP 服务实现。"""

import logging
import json
import concurrent.futures
import atexit

import chdb.session as chs
from fastmcp import FastMCP
from fastmcp.tools import Tool
from fastmcp.prompts import Prompt

from ..config import get_chdb_config, get_mcp_config
from .prompts import CHDB_PROMPT

logger = logging.getLogger("mcp-clickhouse.chdb")

# 查询执行器
QUERY_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=10)
atexit.register(lambda: QUERY_EXECUTOR.shutdown(wait=True))

# 全局 chDB 客户端
_chdb_client = None


def _init_chdb_client():
    """初始化全局 chDB 客户端实例。"""
    try:
        if not get_chdb_config().enabled:
            logger.info("chDB 已禁用，跳过客户端初始化")
            return None

        client_config = get_chdb_config().get_client_config()
        data_path = client_config["data_path"]
        logger.info(f"创建 chDB 客户端，data_path={data_path}")
        client = chs.Session(path=data_path)
        logger.info(f"成功连接到 chDB，data_path={data_path}")
        return client
    except Exception as e:
        logger.error(f"初始化 chDB 客户端失败：{e}")
        return None


def create_chdb_client():
    """创建 chDB 客户端连接。"""
    if not get_chdb_config().enabled:
        raise ValueError("chDB 未启用。设置 CHDB_ENABLED=true 以启用它。")
    return _chdb_client


def execute_chdb_query(query: str):
    """使用 chDB 客户端执行查询。"""
    client = create_chdb_client()
    try:
        res = client.query(query, "JSON")
        if res.has_error():
            error_msg = res.error_message()
            logger.error(f"执行 chDB 查询时出错：{error_msg}")
            return {"error": error_msg}

        result_data = res.data()
        if not result_data:
            return []

        result_json = json.loads(result_data)

        return result_json.get("data", [])

    except Exception as err:
        logger.error(f"执行 chDB 查询时出错：{err}")
        return {"error": str(err)}


def run_chdb_select_query(query: str):
    """在 chDB 中运行 SQL，chDB 是一个进程内 ClickHouse 引擎"""
    logger.info(f"执行 chDB SELECT 查询：{query}")
    try:
        future = QUERY_EXECUTOR.submit(execute_chdb_query, query)
        try:
            timeout_secs = get_mcp_config().query_timeout
            result = future.result(timeout=timeout_secs)
            # 检查是否从 execute_chdb_query 收到了错误结构
            if isinstance(result, dict) and "error" in result:
                logger.warning(f"chDB 查询失败：{result['error']}")
                return {
                    "status": "error",
                    "message": f"chDB 查询失败：{result['error']}",
                }
            return result
        except concurrent.futures.TimeoutError:
            logger.warning(
                f"chDB 查询在 {timeout_secs} 秒后超时：{query}"
            )
            future.cancel()
            return {
                "status": "error",
                "message": f"chDB 查询在 {timeout_secs} 秒后超时",
            }
    except Exception as e:
        logger.error(f"run_chdb_select_query 中的意外错误：{e}")
        return {"status": "error", "message": f"意外错误：{e}"}


def chdb_initial_prompt() -> str:
    """此提示帮助用户了解如何在 chDB 中进行交互和执行常见操作"""
    return CHDB_PROMPT


def register_tools(mcp: FastMCP):
    """将 chDB 工具注册到 MCP 实例。"""
    global _chdb_client
    
    _chdb_client = _init_chdb_client()
    if _chdb_client:
        atexit.register(lambda: _chdb_client.close())

    mcp.add_tool(Tool.from_function(run_chdb_select_query))
    chdb_prompt = Prompt.from_function(
        chdb_initial_prompt,
        name="chdb_initial_prompt",
        description="此提示帮助用户了解如何在 chDB 中进行交互和执行常见操作",
    )
    mcp.add_prompt(chdb_prompt)
    logger.info("chDB 工具和提示已注册")

