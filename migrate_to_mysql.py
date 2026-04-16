"""
Migração de SQL Server (Azure) → MySQL
Garante integridade por validação de contagem em cada tabela.
"""

import os
import sys
import pymysql
import pymysql.cursors
from datetime import datetime
from decimal import Decimal
import pyodbc

# ──────────────────────────────────────────
# CONFIGURAÇÃO
# ──────────────────────────────────────────
MSSQL = {
    "server":   os.getenv("MSSQL_SERVER",   "svd-nps-gauge.database.windows.net"),
    "database": os.getenv("MSSQL_DATABASE", "free-sql-db-nps-gauge"),
    "user":     os.getenv("MSSQL_USER",     "app_user"),
    "password": os.getenv("MSSQL_PASSWORD", "AdminG@uge280"),
    "port":     int(os.getenv("MSSQL_PORT", "1433")),
}

MYSQL = {
    "host":     os.getenv("MYSQL_HOST",     "localhost"),
    "port":     int(os.getenv("MYSQL_PORT", "3306")),
    "database": os.getenv("MYSQL_DATABASE", "nps"),
    "user":     os.getenv("MYSQL_USER",     "nps_user"),
    "password": os.getenv("MYSQL_PASSWORD", "NpsMySQL2026!"),
    "charset":  "utf8mb4",
}

BATCH_SIZE = 500


# ──────────────────────────────────────────
# MAPEAMENTO DE TIPOS SQL SERVER → MYSQL
# ──────────────────────────────────────────
def map_type(data_type: str, char_max: int | None, precision: int | None, scale: int | None) -> str:
    t = data_type.lower()

    if t in ("int",):
        return "INT"
    if t == "bigint":
        return "BIGINT"
    if t == "smallint":
        return "SMALLINT"
    if t == "tinyint":
        return "TINYINT"
    if t == "bit":
        return "TINYINT(1)"
    if t == "float":
        return "DOUBLE"
    if t == "real":
        return "FLOAT"
    if t in ("decimal", "numeric"):
        p = precision or 18
        s = scale or 0
        return f"DECIMAL({p},{s})"
    if t == "money":
        return "DECIMAL(19,4)"
    if t == "smallmoney":
        return "DECIMAL(10,4)"
    if t in ("nvarchar", "nchar"):
        if char_max is None or char_max == -1:
            return "LONGTEXT CHARACTER SET utf8mb4"
        return f"VARCHAR({char_max}) CHARACTER SET utf8mb4"
    if t in ("varchar", "char"):
        if char_max is None or char_max == -1:
            return "LONGTEXT"
        return f"VARCHAR({char_max})"
    if t in ("text", "ntext"):
        return "LONGTEXT CHARACTER SET utf8mb4"
    if t == "datetime2":
        return "DATETIME(6)"
    if t in ("datetime", "smalldatetime"):
        return "DATETIME"
    if t == "date":
        return "DATE"
    if t == "time":
        return "TIME"
    if t == "uniqueidentifier":
        return "VARCHAR(36)"
    if t in ("image", "varbinary"):
        return "LONGBLOB"
    if t == "xml":
        return "LONGTEXT"

    return "TEXT"


# ──────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────
def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def clean_default(default: str | None) -> str | None:
    """Remove parênteses duplos e funções SQL Server incompatíveis com MySQL."""
    if default is None:
        return None
    d = default.strip()
    # Remove wrapping parens e.g. ((0)) -> 0
    while d.startswith("(") and d.endswith(")"):
        d = d[1:-1].strip()

    normalized = d.lower().replace(" ", "")

    # Remove funções SQL Server sem equivalente direto no DEFAULT do MySQL
    if normalized in (
        "getdate",
        "getdate()",
        "getutcdate",
        "getutcdate()",
        "sysdatetime",
        "sysdatetime()",
        "sysutcdatetime",
        "sysutcdatetime()",
        "sysdatetimeoffset",
        "sysdatetimeoffset()",
        "newid",
        "newid()",
        "newsequentialid",
        "newsequentialid()",
    ):
        return None

    # SQL Server unicode literal N'abc' -> MySQL 'abc'
    if d.startswith("N'"):
        return d[1:]

    return d


def sanitize_value(val):
    """Converte tipos Python que o MySQL não aceita nativamente."""
    if isinstance(val, Decimal):
        return float(val)
    if isinstance(val, bytes):
        return val
    return val


# ──────────────────────────────────────────
# SCHEMA: Lê do SQL Server
# ──────────────────────────────────────────
def get_tables(mssql_conn) -> list[str]:
    cur = mssql_conn.cursor()
    cur.execute("""
        SELECT TABLE_NAME
        FROM information_schema.tables
        WHERE TABLE_TYPE = 'BASE TABLE'
        ORDER BY TABLE_NAME
    """)
    result = [row[0] for row in cur.fetchall()]
    cur.close()
    return result


def get_columns(mssql_conn, table: str) -> list[dict]:
    cur = mssql_conn.cursor()
    cur.execute("""
        SELECT
            c.COLUMN_NAME,
            c.DATA_TYPE,
            c.CHARACTER_MAXIMUM_LENGTH,
            c.NUMERIC_PRECISION,
            c.NUMERIC_SCALE,
            c.IS_NULLABLE,
            c.COLUMN_DEFAULT,
            COLUMNPROPERTY(OBJECT_ID(c.TABLE_NAME), c.COLUMN_NAME, 'IsIdentity') AS IS_IDENTITY
        FROM information_schema.columns c
        WHERE c.TABLE_NAME = ?
        ORDER BY c.ORDINAL_POSITION
    """, (table,))
    cols = []
    for row in cur.fetchall():
        cols.append({
            "name":        row[0],
            "data_type":   row[1],
            "char_max":    row[2],
            "precision":   row[3],
            "scale":       row[4],
            "nullable":    row[5] == "YES",
            "default":     row[6],
            "is_identity": bool(row[7]),
        })
    cur.close()
    return cols


def get_pk_columns(mssql_conn, table: str) -> list[str]:
    cur = mssql_conn.cursor()
    cur.execute("""
        SELECT kcu.COLUMN_NAME
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
            ON tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
            AND tc.TABLE_NAME = kcu.TABLE_NAME
        WHERE tc.CONSTRAINT_TYPE = 'PRIMARY KEY'
          AND tc.TABLE_NAME = ?
        ORDER BY kcu.ORDINAL_POSITION
    """, (table,))
    result = [row[0] for row in cur.fetchall()]
    cur.close()
    return result


# ──────────────────────────────────────────
# DDL: Gera CREATE TABLE para MySQL
# ──────────────────────────────────────────
def build_create_table(table: str, columns: list[dict], pk_cols: list[str]) -> str:
    lines = []
    for col in columns:
        mysql_type = map_type(col["data_type"], col["char_max"], col["precision"], col["scale"])
        null_clause = "NULL" if col["nullable"] else "NOT NULL"
        auto_inc = "AUTO_INCREMENT" if col["is_identity"] else ""

        default_val = clean_default(col["default"])
        if default_val is not None and not col["is_identity"]:
            # Tenta usar o default SQL Server no MySQL quando compatível
            default_clause = f"DEFAULT {default_val}"
        else:
            default_clause = ""

        parts = [f"  `{col['name']}` {mysql_type}", null_clause]
        if auto_inc:
            parts.append(auto_inc)
        if default_clause and not auto_inc:
            parts.append(default_clause)

        lines.append(" ".join(p for p in parts if p))

    if pk_cols:
        pk_list = ", ".join(f"`{c}`" for c in pk_cols)
        lines.append(f"  PRIMARY KEY ({pk_list})")

    body = ",\n".join(lines)
    return f"CREATE TABLE IF NOT EXISTS `{table}` (\n{body}\n) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;"


# ──────────────────────────────────────────
# MIGRAÇÃO DE DADOS
# ──────────────────────────────────────────
def count_rows_mssql(mssql_conn, table: str) -> int:
    cur = mssql_conn.cursor()
    cur.execute(f"SELECT COUNT(*) FROM dbo.[{table}]")
    result = cur.fetchone()[0]
    cur.close()
    return result


def count_rows_mysql(mysql_conn, table: str) -> int:
    with mysql_conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM `{table}`")
        return cur.fetchone()["COUNT(*)"]


def migrate_table(mssql_conn, mysql_conn, table: str, columns: list[dict]) -> tuple[int, int]:
    col_names = [c["name"] for c in columns]
    col_list  = ", ".join(f"[{c}]" for c in col_names)
    placeholders = ", ".join(["%s"] * len(col_names))
    mysql_cols = ", ".join(f"`{c}`" for c in col_names)

    insert_sql = f"INSERT IGNORE INTO `{table}` ({mysql_cols}) VALUES ({placeholders})"

    total_source = count_rows_mssql(mssql_conn, table)
    if total_source == 0:
        log(f"  ↳ {table}: vazia, pulando.")
        return 0, 0

    inserted = 0
    offset = 0

    # Desativa verificação de FK e auto-increment durante bulk insert
    with mysql_conn.cursor() as cur:
        cur.execute("SET FOREIGN_KEY_CHECKS=0;")
        cur.execute(f"SET SESSION sql_mode='NO_AUTO_VALUE_ON_ZERO';")

    mssql_cur = mssql_conn.cursor()
    mssql_cur.execute(f"SELECT {col_list} FROM dbo.[{table}]")

    while True:
        rows = mssql_cur.fetchmany(BATCH_SIZE)
        if not rows:
            break

        clean_rows = [
            tuple(sanitize_value(val) for val in row)
            for row in rows
        ]

        with mysql_conn.cursor() as mysql_cur:
            mysql_cur.executemany(insert_sql, clean_rows)
        mysql_conn.commit()

        inserted += len(rows)
        offset += len(rows)
        pct = int((offset / total_source) * 100)
        log(f"  ↳ {table}: {offset}/{total_source} ({pct}%)")

    mssql_cur.close()

    with mysql_conn.cursor() as cur:
        cur.execute("SET FOREIGN_KEY_CHECKS=1;")
    mysql_conn.commit()

    return total_source, inserted


# ──────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────
def main():
    log("=" * 60)
    log("Iniciando migração SQL Server → MySQL")
    log("=" * 60)

    # ── Conexão SQL Server
    log("Conectando ao SQL Server (Azure)...")
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
    mssql_conn = pyodbc.connect(conn_str)
    log("✓ SQL Server conectado.")

    # ── Conexão MySQL
    log("Conectando ao MySQL...")
    mysql_conn = pymysql.connect(
        host=MYSQL["host"],
        port=MYSQL["port"],
        db=MYSQL["database"],
        user=MYSQL["user"],
        password=MYSQL["password"],
        charset=MYSQL["charset"],
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )
    log("✓ MySQL conectado.")

    tables = get_tables(mssql_conn)
    log(f"\nTabelas encontradas no SQL Server: {len(tables)}")
    for t in tables:
        log(f"  • {t}")

    # ── Fase 1: Criar schema no MySQL
    log("\n── FASE 1: Criando schema no MySQL ──")
    with mysql_conn.cursor() as cur:
        cur.execute("SET FOREIGN_KEY_CHECKS=0;")

    for table in tables:
        columns = get_columns(mssql_conn, table)
        pk_cols = get_pk_columns(mssql_conn, table)
        ddl = build_create_table(table, columns, pk_cols)

        with mysql_conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS `{table}`;")
            cur.execute(ddl)
        mysql_conn.commit()
        log(f"  ✓ {table} criada.")

    with mysql_conn.cursor() as cur:
        cur.execute("SET FOREIGN_KEY_CHECKS=1;")
    mysql_conn.commit()

    # ── Fase 2: Migrar dados
    log("\n── FASE 2: Migrando dados ──")
    results = {}
    errors  = []

    for table in tables:
        log(f"\n[{table}]")
        try:
            columns = get_columns(mssql_conn, table)
            src_count, dst_count = migrate_table(mssql_conn, mysql_conn, table, columns)
            results[table] = {"source": src_count, "destination": dst_count}
        except Exception as e:
            log(f"  ✗ ERRO: {e}")
            errors.append((table, str(e)))
            mysql_conn.rollback()

    # ── Fase 3: Validação
    log("\n── FASE 3: Validação de integridade ──")
    log(f"{'Tabela':<35} {'SQL Server':>12} {'MySQL':>10} {'Status':>8}")
    log("-" * 70)

    all_ok = True
    for table in tables:
        src = count_rows_mssql(mssql_conn, table)
        try:
            dst = count_rows_mysql(mysql_conn, table)
        except Exception:
            dst = -1

        status = "✓ OK" if src == dst else "✗ DIFF"
        if src != dst:
            all_ok = False

        log(f"{table:<35} {src:>12} {dst:>10} {status:>8}")

    log("-" * 70)

    if errors:
        log(f"\n✗ Tabelas com erro ({len(errors)}):")
        for table, err in errors:
            log(f"  • {table}: {err}")

    if all_ok and not errors:
        log("\n✅ Migração concluída com sucesso! Todos os dados validados.")
    else:
        log("\n⚠️  Migração concluída com divergências. Revise os itens acima.")
        sys.exit(1)

    mssql_conn.close()
    mysql_conn.close()


if __name__ == "__main__":
    main()
