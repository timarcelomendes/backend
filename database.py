import os
import urllib.parse
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv()

# 🎯 CRÍTICO: Esta variável TEM de estar aqui, no topo do ficheiro
_engine_instance = None

def get_engine():
    # 🎯 E esta linha TEM de ser a primeira dentro da função
    global _engine_instance
    
    if _engine_instance is not None:
        return _engine_instance

    server = os.getenv("SQL_SERVER", "").strip()
    database = os.getenv("SQL_DB", "").strip()
    username = os.getenv("SQL_USER", "").strip()
    password = os.getenv("SQL_PASSWORD", "").strip()

    # Limpeza do servidor
    server = server.replace("tcp:", "").replace(",1433", "").replace("https://", "")
    
    if "@" in username:
        username = username.split("@")[0]
        
    if "database.windows.net" in server:
        short_server = server.split(".")[0]
        usuario_final = f"{username}@{short_server}"
    else:
        usuario_final = username

    senha_codificada = urllib.parse.quote_plus(password)
    usuario_codificado = urllib.parse.quote_plus(usuario_final)
    
    conn_url = f"mssql+pymssql://{usuario_codificado}:{senha_codificada}@{server}:1433/{database}"
    
    print(f"🔌 Criando nova conexão... Servidor: {server}")
    
    # 🎯 Atribuição à variável global
    _engine_instance = create_engine(
        conn_url,
        pool_size=15,
        max_overflow=25,
        pool_pre_ping=True,
        pool_recycle=300,
        future=True,
        connect_args={
            "login_timeout": 30,
            "timeout": 30
        }
    )
    return _engine_instance

def exec_sql(sql: str, params: dict | None = None):
    # Agora o exec_sql consegue chamar o get_engine sem erro
    engine = get_engine()
    with engine.begin() as conn:
        return conn.execute(text(sql), params or {})