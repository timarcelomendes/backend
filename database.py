import os
import re
import urllib.parse
from sqlalchemy import create_engine, event, text
from dotenv import load_dotenv

load_dotenv()

# 🎯 CRÍTICO: Esta variável TEM de estar aqui, no topo do ficheiro
_engine_instance = None


def _apply_top_limit(statement: str) -> str:
    """Converte SELECT TOP ... para LIMIT ... no final da query."""
    sql = statement

    # SELECT TOP (expr) ...
    m = re.search(r"(?is)^\s*select\s+top\s*\(\s*([^\)]+)\s*\)\s+", sql)
    if m:
        top_expr = m.group(1).strip()
        sql = re.sub(r"(?is)^\s*select\s+top\s*\(\s*([^\)]+)\s*\)\s+", "SELECT ", sql, count=1)
    else:
        # SELECT TOP expr ...
        m2 = re.search(r"(?is)^\s*select\s+top\s+([^\s,]+)\s+", sql)
        if not m2:
            return sql
        top_expr = m2.group(1).strip()
        sql = re.sub(r"(?is)^\s*select\s+top\s+([^\s,]+)\s+", "SELECT ", sql, count=1)

    if re.search(r"(?is)\blimit\b", sql):
        return sql

    sql = sql.rstrip()
    if sql.endswith(";"):
        sql = f"{sql[:-1]} LIMIT {top_expr};"
    else:
        sql = f"{sql} LIMIT {top_expr}"
    return sql


def _translate_tsql_to_mysql(statement: str) -> str:
    """Traduz os principais padrões T-SQL usados no projeto para MySQL."""
    sql = statement

    # Prefixo de schema SQL Server
    sql = re.sub(r"(?i)\bdbo\.", "", sql)

    # Funções de data/hora
    sql = re.sub(r"(?i)\bgetdate\s*\(\s*\)", "NOW()", sql)
    sql = re.sub(r"(?i)\bsysutcdatetime\s*\(\s*\)", "UTC_TIMESTAMP(6)", sql)

    # Nulos e strings
    sql = re.sub(r"(?i)\bisnull\s*\(", "IFNULL(", sql)
    sql = re.sub(r"(?i)\blen\s*\(", "CHAR_LENGTH(", sql)
    sql = re.sub(r"(?is)CAST\s*\((.*?)\s+AS\s+NVARCHAR\s*\(\s*MAX\s*\)\s*\)", r"CAST(\1 AS CHAR)", sql)
    sql = re.sub(r"(?is)CAST\s*\((.*?)\s+AS\s+NVARCHAR\s*\(\s*(\d+)\s*\)\s*\)", r"CAST(\1 AS CHAR(\2))", sql)
    sql = re.sub(r"(?is)CAST\s*\((.*?)\s+AS\s+BIT\s*\)", r"CAST(\1 AS UNSIGNED)", sql)

    # Casts tolerantes
    sql = re.sub(r"(?is)TRY_CAST\s*\((.*?)\s+AS\s+INT\s*\)", r"CAST(\1 AS SIGNED)", sql)

    # DATEDIFF(day, a, b) -> TIMESTAMPDIFF(DAY, a, b)
    sql = re.sub(
        r"(?is)DATEDIFF\s*\(\s*day\s*,\s*([^,]+?)\s*,\s*([^\)]+?)\s*\)",
        r"TIMESTAMPDIFF(DAY, \1, \2)",
        sql,
    )

    # IF EXISTS ... UPDATE ... ELSE INSERT ... (caso recorrente de configuracoes)
    if re.search(r"(?is)^\s*if\s+exists\s*\(.*nps_configuracoes.*\)\s*update\s+nps_configuracoes", sql):
        sql = (
            "INSERT INTO nps_configuracoes (chave, valor, updated_at) "
            "VALUES (%(chave)s, %(valor)s, NOW()) "
            "ON DUPLICATE KEY UPDATE valor = VALUES(valor), updated_at = VALUES(updated_at)"
        )

    # TOP/LIMIT
    sql = _apply_top_limit(sql)

    return sql

def get_engine():
    global _engine_instance

    if _engine_instance is not None:
        return _engine_instance

    host = os.getenv("MYSQL_HOST", "localhost").strip()
    port = os.getenv("MYSQL_PORT", "3306").strip()
    database = (os.getenv("MYSQL_DATABASE") or os.getenv("MYSQL_DB") or "nps").strip()
    username = os.getenv("MYSQL_USER", "nps_user").strip()
    password = os.getenv("MYSQL_PASSWORD", "").strip()

    senha_codificada = urllib.parse.quote_plus(password)
    usuario_codificado = urllib.parse.quote_plus(username)

    conn_url = (
        f"mysql+pymysql://{usuario_codificado}:{senha_codificada}@{host}:{port}/{database}"
        "?charset=utf8mb4"
    )

    print(f"Criando nova conexao MySQL... Host: {host}:{port}, DB: {database}")

    _engine_instance = create_engine(
        conn_url,
        pool_size=15,
        max_overflow=25,
        pool_pre_ping=True,
        pool_recycle=300,
        future=True,
        connect_args={
            "connect_timeout": 30,
            "read_timeout": 30,
            "write_timeout": 30,
        }
    )

    @event.listens_for(_engine_instance, "before_cursor_execute", retval=True)
    def _mysql_compatibility_layer(conn, cursor, statement, parameters, context, executemany):
        translated = _translate_tsql_to_mysql(statement)
        return translated, parameters

    return _engine_instance

def exec_sql(sql: str, params: dict | None = None):
    engine = get_engine()
    with engine.begin() as conn:
        return conn.execute(text(sql), params or {})