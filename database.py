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

_engine_instance = None

def get_engine():
    global _engine_instance
    if _engine_instance is not None:
        return _engine_instance

    # 1. Pega as variáveis do Azure ou do .env local
    server = os.getenv("SQL_SERVER", "").strip()
    database = os.getenv("SQL_DB", "").strip()
    username = os.getenv("SQL_USER", "").strip()
    password = os.getenv("SQL_PASSWORD", "").strip()

    # 2. Limpeza absoluta do nome do servidor (remove tcp:, porta e https://)
    server = server.replace("tcp:", "").replace(",1433", "").replace("https://", "")
    
    # 3. Limpa o nome do utilizador (remove o @ se você já tiver tentado colocar antes)
    if "@" in username:
        username = username.split("@")[0]
        
    # 4. O Truque Mágico: Só adiciona o sufixo obrigatório se for um banco do Azure
    if "database.windows.net" in server:
        short_server = server.split(".")[0] # Pega apenas o primeiro nome
        usuario_final = f"{username}@{short_server}"
    else:
        usuario_final = username # Mantém normal se for um banco local

    # 5. Protege senhas e utilizadores que tenham caracteres especiais (como #, %, $, etc)
    senha_codificada = urllib.parse.quote_plus(password)
    usuario_codificado = urllib.parse.quote_plus(usuario_final)
    
    # 6. Monta a string de conexão blindada
    conn_url = f"mssql+pymssql://{usuario_codificado}:{senha_codificada}@{server}:1433/{database}"
    
    print(f"🔌 Conectando ao Banco... Servidor: {server} | Utilizador: {usuario_final}")
    
    _engine_instance = create_engine(
        conn_url,
        pool_size=10,
        max_overflow=20,
        pool_pre_ping=True,
        pool_recycle=1800,
        future=True,
    )
    return _engine_instance