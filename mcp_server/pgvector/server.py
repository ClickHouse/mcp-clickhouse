"""pgvector MCP 服务实现。"""

import logging
import concurrent.futures
from typing import Optional

from fastmcp import FastMCP
from fastmcp.tools import Tool
from fastmcp.prompts import Prompt
from fastmcp.exceptions import ToolError

from ..config import get_pgvector_config, get_mcp_config
from .prompts import PGVECTOR_PROMPT

logger = logging.getLogger("mcp-clickhouse.pgvector")

# 查询执行器
QUERY_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=10)


def create_pgvector_client():
    """创建带有 pgvector 支持的 PostgreSQL 客户端连接。"""
    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor
    except ImportError:
        raise ImportError(
            "psycopg2 未安装。使用以下命令安装：pip install psycopg2-binary"
        )

    if not get_pgvector_config().enabled:
        raise ValueError("pgvector 未启用。设置 PGVECTOR_ENABLED=true 以启用它。")

    client_config = get_pgvector_config().get_client_config()
    logger.info(
        f"创建 PostgreSQL 客户端连接到 {client_config['host']}:{client_config['port']} "
        f"用户 {client_config['user']} (database={client_config['database']}, "
        f"sslmode={client_config['sslmode']})"
    )

    try:
        conn = psycopg2.connect(**client_config, cursor_factory=RealDictCursor)
        logger.info("成功连接到 PostgreSQL 服务器")
        return conn
    except Exception as e:
        logger.error(f"连接到 PostgreSQL 失败：{str(e)}")
        raise


def execute_pgvector_query(query: str):
    """在 PostgreSQL 上执行查询，支持 pgvector。"""
    conn = None
    cursor = None
    try:
        conn = create_pgvector_client()
        cursor = conn.cursor()
        cursor.execute(query)

        # 检查查询是否返回结果
        if cursor.description:
            columns = [desc[0] for desc in cursor.description]
            rows = cursor.fetchall()
            # 将 RealDictRow 转换为常规字典
            rows = [dict(row) for row in rows]
            logger.info(f"查询返回 {len(rows)} 行")
            return {"columns": columns, "rows": rows}
        else:
            # 对于非 SELECT 查询
            conn.commit()
            return {"message": "查询执行成功", "rowcount": cursor.rowcount}
    except Exception as err:
        logger.error(f"执行查询时出错：{err}")
        if conn:
            conn.rollback()
        raise ToolError(f"查询执行失败：{str(err)}")
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def run_pgvector_select_query(query: str):
    """在带有 pgvector 支持的 PostgreSQL 上运行 SELECT 查询"""
    logger.info(f"执行 pgvector SELECT 查询：{query}")
    try:
        future = QUERY_EXECUTOR.submit(execute_pgvector_query, query)
        try:
            timeout_secs = get_mcp_config().query_timeout
            result = future.result(timeout=timeout_secs)
            if isinstance(result, dict) and "error" in result:
                logger.warning(f"查询失败：{result['error']}")
                return {
                    "status": "error",
                    "message": f"查询失败：{result['error']}",
                }
            return result
        except concurrent.futures.TimeoutError:
            logger.warning(f"查询在 {timeout_secs} 秒后超时：{query}")
            future.cancel()
            raise ToolError(f"查询在 {timeout_secs} 秒后超时")
    except ToolError:
        raise
    except Exception as e:
        logger.error(f"run_pgvector_select_query 中的意外错误：{str(e)}")
        raise RuntimeError(f"查询执行期间发生意外错误：{str(e)}")


def list_pgvector_tables():
    """列出 PostgreSQL 数据库中的所有表"""
    logger.info("列出所有 PostgreSQL 表")
    query = """
        SELECT 
            table_schema,
            table_name,
            table_type
        FROM information_schema.tables
        WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
        ORDER BY table_schema, table_name;
    """
    try:
        result = execute_pgvector_query(query)
        logger.info(f"找到 {len(result.get('rows', []))} 个表")
        return result
    except Exception as e:
        logger.error(f"列出表时出错：{str(e)}")
        raise ToolError(f"列出表失败：{str(e)}")


def list_pgvector_vectors():
    """列出所有表中的所有向量列及其维度"""
    logger.info("列出 PostgreSQL 数据库中的所有向量列")
    query = """
        SELECT 
            c.table_schema,
            c.table_name,
            c.column_name,
            c.data_type,
            c.udt_name,
            CASE 
                WHEN c.udt_name = 'vector' THEN 
                    substring(c.data_type from 'vector\\((\\d+)\\)')
                ELSE NULL
            END as vector_dimensions
        FROM information_schema.columns c
        WHERE c.udt_name = 'vector'
            AND c.table_schema NOT IN ('pg_catalog', 'information_schema')
        ORDER BY c.table_schema, c.table_name, c.column_name;
    """
    try:
        result = execute_pgvector_query(query)
        logger.info(f"找到 {len(result.get('rows', []))} 个向量列")
        return result
    except Exception as e:
        logger.error(f"列出向量列时出错：{str(e)}")
        raise ToolError(f"列出向量列失败：{str(e)}")


def search_similar_vectors(
    table_name: str,
    vector_column: str,
    query_vector: str,
    limit: int = 10,
    distance_function: str = "l2",
):
    """使用向量嵌入执行相似性搜索。
    
    参数：
        table_name: 要搜索的表名
        vector_column: 向量列名
        query_vector: 查询向量字符串（例如，'[1,2,3]'）
        limit: 返回的结果数（默认：10）
        distance_function: 要使用的距离函数 - 'l2'、'cosine' 或 'inner_product'（默认：'l2'）
    """
    logger.info(f"在 {table_name}.{vector_column} 上执行向量相似性搜索")
    
    # 将距离函数映射到运算符
    operators = {
        "l2": "<->",  # L2 距离（欧几里得）
        "cosine": "<=>",  # 余弦距离
        "inner_product": "<#>",  # 内积（负点积）
    }
    
    if distance_function not in operators:
        raise ToolError(
            f"无效的距离函数 '{distance_function}'。"
            f"有效选项：{', '.join(operators.keys())}"
        )
    
    operator = operators[distance_function]
    
    query = f"""
        SELECT *, {vector_column} {operator} '{query_vector}' AS distance
        FROM {table_name}
        ORDER BY {vector_column} {operator} '{query_vector}'
        LIMIT {limit};
    """
    
    try:
        result = execute_pgvector_query(query)
        logger.info(f"向量搜索返回 {len(result.get('rows', []))} 个结果")
        return result
    except Exception as e:
        logger.error(f"执行向量搜索时出错：{str(e)}")
        raise ToolError(f"执行向量搜索失败：{str(e)}")


def pgvector_initial_prompt() -> str:
    """此提示帮助用户了解如何与 pgvector 进行交互和执行操作"""
    return PGVECTOR_PROMPT


def register_tools(mcp: FastMCP):
    """将 pgvector 工具注册到 MCP 实例。"""
    mcp.add_tool(Tool.from_function(run_pgvector_select_query))
    mcp.add_tool(Tool.from_function(list_pgvector_tables))
    mcp.add_tool(Tool.from_function(list_pgvector_vectors))
    mcp.add_tool(Tool.from_function(search_similar_vectors))
    
    pgvector_prompt = Prompt.from_function(
        pgvector_initial_prompt,
        name="pgvector_initial_prompt",
        description="此提示帮助用户了解如何与 pgvector 进行交互和执行操作",
    )
    mcp.add_prompt(pgvector_prompt)
    logger.info("pgvector 工具和提示已注册")

