import os
import urllib.parse
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

# Carrega as variáveis do arquivo .env
load_dotenv()

def exec_sql(sql: str, params: dict | None = None):
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text(sql), params or {})

def _build_conn_str(driver: str, server: str, database: str, username: str, password: str) -> str:
    return (
        f"Driver={{{driver}}};"
        f"Server=tcp:{server},1433;"
        f"Database={database};"
        f"Uid={username};"
        f"Pwd={password};"
        "Encrypt=yes;"
        "TrustServerCertificate=yes;"
        "MARS_Connection=yes;"
        "Connection Timeout=30;"
    )

# Engine instanciado de forma global para aproveitar o Pool de conexões do SQLAlchemy
_engine_instance = None

def get_engine():
    global _engine_instance
    if _engine_instance is not None:
        return _engine_instance

    # Agora buscamos do ambiente, não mais do st.secrets
    server = os.getenv("SQL_SERVER")
    database = os.getenv("SQL_DB")
    username = os.getenv("SQL_USER")
    password = os.getenv("SQL_PASSWORD")

    driver = "ODBC Driver 18 for SQL Server" # Pode manter a lógica de fallback se quiser
    conn_str = _build_conn_str(driver, server, database, username, password)
    params = urllib.parse.quote_plus(conn_str)
    
    _engine_instance = create_engine(
        f"mssql+pyodbc:///?odbc_connect={params}",
        pool_pre_ping=True,
        pool_recycle=1800,
        future=True,
    )
    return _engine_instance