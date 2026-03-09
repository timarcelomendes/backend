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

# Engine instanciado de forma global para aproveitar o Pool de conexões do SQLAlchemy
_engine_instance = None

def get_engine():
    global _engine_instance
    if _engine_instance is not None:
        return _engine_instance

    # Busca das variáveis de ambiente
    server = os.getenv("SQL_SERVER")
    database = os.getenv("SQL_DB")
    username = os.getenv("SQL_USER")
    password = os.getenv("SQL_PASSWORD")

    # Codificamos a senha e o usuário para evitar que caracteres como @ ou # quebrem a URL
    senha_codificada = urllib.parse.quote_plus(password)
    usuario_codificado = urllib.parse.quote_plus(username)
    
    # 👇 A MÁGICA: Usamos mssql+pymssql. Sem necessidade de drivers do Windows/Linux!
    conn_url = f"mssql+pymssql://{usuario_codificado}:{senha_codificada}@{server}:1433/{database}"
    
    print("🔌 Conectando ao banco de dados com pymssql (Driver Embutido)...")
    
    _engine_instance = create_engine(
        conn_url,
        pool_pre_ping=True,
        pool_recycle=1800,
        future=True,
    )
    return _engine_instance