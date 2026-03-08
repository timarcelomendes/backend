import pandas as pd
from sqlalchemy import text
from database import get_engine, exec_sql

CATS = ["Promotor", "Neutro", "Detrator"]

def read_df(sql: str, params: dict = None) -> pd.DataFrame:
    engine = get_engine()
    with engine.connect() as conn:
        result = conn.execute(text(sql), params or {})
        rows = result.fetchall()
        cols = list(result.keys())
    return pd.DataFrame(rows, columns=cols)

def load_respostas(q: str, empresa: str, categoria: str, perfil: str, incluir_excluidas: bool, topn: int) -> pd.DataFrame:
    where = []
    params = {}

    if (q or "").strip():
        where.append("(LOWER(r.motivo) LIKE :like OR LOWER(c.nome) LIKE :like OR LOWER(c.empresa) LIKE :like)")
        params["like"] = f"%{q.strip().lower()}%"
        
    if (empresa or "").strip():
        where.append("LOWER(c.empresa) LIKE :empresa")
        params["empresa"] = f"%{empresa.strip().lower()}%"
        
    if categoria and categoria != "Todas":
        where.append("r.categoria = :cat")
        params["cat"] = categoria
        
    if perfil and perfil != "Todos":
        where.append("c.perfil_decisor = :perf")
        params["perf"] = perfil

    # 💡 LÓGICA DE ARQUIVAMENTO
    if not incluir_excluidas:
        where.append("r.excluido = 0")

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    # 💡 O SEGREDO ESTÁ AQUI: Nomes de colunas 100% únicos para o Pandas não dar erro
    sql = f"""
    SELECT TOP ({int(topn)})
        r.resposta_id, 
        r.cliente_id AS resposta_cliente_id,  -- Renomeado para não chocar com o cliente_id da tabela clientes
        c.nome AS cliente_nome, 
        c.empresa, 
        c.perfil_decisor AS perfil_cliente,
        r.nota, 
        r.motivo, 
        r.categoria, 
        r.canal, 
        r.expectativas, 
        r.o_que_faltava, 
        r.jira_issue_url,
        r.created_at,
        r.excluido
    FROM dbo.nps_respostas r
    LEFT JOIN dbo.nps_clientes c ON r.cliente_id = c.cliente_id
    {where_sql}
    ORDER BY r.created_at DESC;
    """
    
    # Executa e converte para Pandas
    return read_df(sql, params)

def update_resposta(resposta_id: str, nota: int, categoria: str, motivo: str, canal: str, expectativas: str, o_que_faltava: str):
    sql = """
    UPDATE dbo.nps_respostas SET 
        nota=:nota, categoria=:categoria, motivo=:motivo, canal=:canal,
        expectativas=:expectativas, o_que_faltava=:o_que_faltava
    WHERE resposta_id=:resposta_id;
    """
    exec_sql(sql, {
        "resposta_id": resposta_id, "nota": int(nota), "categoria": categoria,
        "motivo": (motivo or "").strip() or None, "canal": (canal or "").strip() or None,
        "expectativas": (expectativas or "").strip() or None, "o_que_faltava": (o_que_faltava or "").strip() or None,
    })

def soft_delete(resposta_id: str):
    exec_sql("UPDATE dbo.nps_respostas SET deleted_at = SYSUTCDATETIME() WHERE resposta_id=:resposta_id;", {"resposta_id": resposta_id})

def restore(resposta_id: str):
    exec_sql("UPDATE dbo.nps_respostas SET deleted_at = NULL WHERE resposta_id=:resposta_id;", {"resposta_id": resposta_id})