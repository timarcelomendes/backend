import os
import sys
from collections import Counter
from datetime import date, datetime, time
from decimal import Decimal

import pyodbc
import pymysql
import pymysql.cursors


MSSQL = {
    "server": os.getenv("MSSQL_SERVER", "svd-nps-gauge.database.windows.net"),
    "database": os.getenv("MSSQL_DATABASE", "free-sql-db-nps-gauge"),
    "user": os.getenv("MSSQL_USER", "app_user"),
    "password": os.getenv("MSSQL_PASSWORD", "AdminG@uge280"),
    "port": int(os.getenv("MSSQL_PORT", "1433")),
}

MYSQL = {
    "host": os.getenv("MYSQL_HOST", "mysql"),
    "port": int(os.getenv("MYSQL_PORT", "3306")),
    "database": os.getenv("MYSQL_DATABASE", "nps"),
    "user": os.getenv("MYSQL_USER", "nps_user"),
    "password": os.getenv("MYSQL_PASSWORD", "NpsMySQL2026!"),
    "charset": "utf8mb4",
}


def log(msg: str) -> None:
    print(msg, flush=True)


def connect_mssql():
    conn_str = (
        "DRIVER={ODBC Driver 18 for SQL Server};"
        f"SERVER={MSSQL['server']},{MSSQL['port']};"
        f"DATABASE={MSSQL['database']};"
        f"UID={MSSQL['user']};"
        f"PWD={MSSQL['password']};"
        "Encrypt=yes;"
        "TrustServerCertificate=no;"
        "Connection Timeout=30;"
    )
    return pyodbc.connect(conn_str)


def connect_mysql():
    return pymysql.connect(
        host=MYSQL["host"],
        port=MYSQL["port"],
        db=MYSQL["database"],
        user=MYSQL["user"],
        password=MYSQL["password"],
        charset=MYSQL["charset"],
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )


def get_tables_mssql(conn):
    cur = conn.cursor()
    cur.execute(
        """
        SELECT TABLE_NAME
        FROM information_schema.tables
        WHERE TABLE_TYPE = 'BASE TABLE'
        ORDER BY TABLE_NAME
        """
    )
    rows = [r[0] for r in cur.fetchall()]
    cur.close()
    return rows


def get_tables_mysql(conn):
    cur = conn.cursor()
    cur.execute(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = %s
        ORDER BY table_name
        """,
        (MYSQL["database"],),
    )
    rows = [r.get("table_name", r.get("TABLE_NAME")) for r in cur.fetchall()]
    cur.close()
    return rows


def get_columns_mssql(conn, table):
    cur = conn.cursor()
    cur.execute(
        """
        SELECT COLUMN_NAME
        FROM information_schema.columns
        WHERE TABLE_NAME = ?
        ORDER BY ORDINAL_POSITION
        """,
        (table,),
    )
    rows = [r[0] for r in cur.fetchall()]
    cur.close()
    return rows


def get_columns_mysql(conn, table):
    cur = conn.cursor()
    cur.execute(
        """
        SELECT COLUMN_NAME
        FROM information_schema.columns
        WHERE table_schema = %s
          AND table_name = %s
        ORDER BY ORDINAL_POSITION
        """,
        (MYSQL["database"], table),
    )
    rows = [r.get("COLUMN_NAME", r.get("column_name")) for r in cur.fetchall()]
    cur.close()
    return rows


def normalize_value(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return 1 if v else 0
    if isinstance(v, int):
        return v
    if isinstance(v, Decimal):
        return format(v, "f")
    if isinstance(v, datetime):
        # SQL Server keeps fractional seconds more often than MySQL DATETIME.
        # Compare at second precision to avoid false positives from truncation.
        return v.replace(microsecond=0).isoformat(sep=" ")
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, time):
        return v.replace(microsecond=0).isoformat()
    if isinstance(v, bytes):
        return v.hex()
    s = str(v)
    return s.strip()


def fetch_rows_mssql(conn, table, columns):
    col_list = ", ".join(f"[{c}]" for c in columns)
    cur = conn.cursor()
    cur.execute(f"SELECT {col_list} FROM dbo.[{table}]")
    rows = [tuple(normalize_value(v) for v in row) for row in cur.fetchall()]
    cur.close()
    return rows


def fetch_rows_mysql(conn, table, columns):
    col_list = ", ".join(f"`{c}`" for c in columns)
    cur = conn.cursor()
    cur.execute(f"SELECT {col_list} FROM `{table}`")
    db_rows = cur.fetchall()
    rows = []
    for r in db_rows:
        rows.append(tuple(normalize_value(r[c]) for c in columns))
    cur.close()
    return rows


def main():
    log("=== VALIDACAO COMPLETA SQL SERVER x MYSQL ===")
    mssql = connect_mssql()
    mysql = connect_mysql()

    source_tables = get_tables_mssql(mssql)
    target_tables = get_tables_mysql(mysql)

    src_set = set(source_tables)
    dst_set = set(target_tables)

    missing_in_mysql = sorted(src_set - dst_set)
    extra_in_mysql = sorted(dst_set - src_set)

    if missing_in_mysql:
        log("Tabelas faltando no MySQL:")
        for t in missing_in_mysql:
            log(f"  - {t}")
    if extra_in_mysql:
        log("Tabelas extras no MySQL:")
        for t in extra_in_mysql:
            log(f"  - {t}")

    common_tables = sorted(src_set & dst_set)
    if not common_tables:
        log("Nenhuma tabela em comum para comparar.")
        sys.exit(1)

    total_tables = 0
    failed_tables = 0

    for table in common_tables:
        total_tables += 1
        log(f"\n--- Tabela: {table} ---")

        src_cols = get_columns_mssql(mssql, table)
        dst_cols = get_columns_mysql(mysql, table)

        if src_cols != dst_cols:
            failed_tables += 1
            log("  [ERRO] Colunas diferentes entre origem e destino")
            log(f"  SQL Server: {src_cols}")
            log(f"  MySQL    : {dst_cols}")
            continue

        src_rows = fetch_rows_mssql(mssql, table, src_cols)
        dst_rows = fetch_rows_mysql(mysql, table, dst_cols)

        src_count = len(src_rows)
        dst_count = len(dst_rows)
        log(f"  Linhas SQL Server: {src_count}")
        log(f"  Linhas MySQL     : {dst_count}")

        if src_count != dst_count:
            failed_tables += 1
            log("  [ERRO] Quantidade de linhas divergente")
            continue

        src_counter = Counter(src_rows)
        dst_counter = Counter(dst_rows)

        if src_counter != dst_counter:
            failed_tables += 1
            log("  [ERRO] Conteudo divergente")

            missing_rows = list((src_counter - dst_counter).elements())
            extra_rows = list((dst_counter - src_counter).elements())

            if missing_rows:
                log("  Exemplo de linha faltando no MySQL:")
                log(f"    {missing_rows[0]}")
            if extra_rows:
                log("  Exemplo de linha extra no MySQL:")
                log(f"    {extra_rows[0]}")
            continue

        log("  [OK] Conteudo identico")

    mssql.close()
    mysql.close()

    log("\n=== RESUMO ===")
    log(f"Tabelas comparadas: {total_tables}")
    log(f"Tabelas com erro  : {failed_tables}")

    if missing_in_mysql or extra_in_mysql or failed_tables > 0:
        log("RESULTADO FINAL: FALHA")
        sys.exit(1)

    log("RESULTADO FINAL: SUCESSO - nada ficou para tras")


if __name__ == "__main__":
    main()