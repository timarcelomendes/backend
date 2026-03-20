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

    if not incluir_excluidas:
        where.append("r.excluido = 0")

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    sql = f"""
    WITH BaseHistorico AS (
        SELECT 
            *,
            LAG(nota) OVER (PARTITION BY cliente_id ORDER BY COALESCE(data_resposta, created_at) ASC, resposta_id ASC) as nota_anterior
        FROM dbo.nps_respostas
        {'WHERE excluido = 0' if not incluir_excluidas else ''}
    )
    SELECT TOP ({int(topn)})
        r.resposta_id, 
        r.cliente_id AS resposta_cliente_id,
        c.nome AS cliente_nome, 
        c.empresa, 
        c.perfil_decisor AS perfil_cliente,
        r.nota, 
        r.nota_anterior,
        r.motivo, 
        r.categoria, 
        r.canal, 
        r.expectativas, 
        r.o_que_faltava, 
        r.jira_issue_url,
        r.created_at,
        r.excluido
    FROM BaseHistorico r
    LEFT JOIN dbo.nps_clientes c ON r.cliente_id = c.cliente_id
    {where_sql}
    ORDER BY COALESCE(r.data_resposta, r.created_at) DESC, r.resposta_id DESC;
    """
    
    df = read_df(sql, params)
    
    # 🟢 CORREÇÃO 2: Força a nota anterior a ser uma String limpa ('8' em vez de 8.0 ou NaN)
    # Isto garante que o v-if do Vue.js funciona na perfeição!
    if 'nota_anterior' in df.columns:
        df['nota_anterior'] = df['nota_anterior'].apply(lambda x: str(int(x)) if pd.notnull(x) else "")
        
    return df

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
    exec_sql("UPDATE dbo.nps_respostas SET excluido = 1 WHERE resposta_id=:resposta_id;", {"resposta_id": resposta_id})

def restore(resposta_id: str):
    exec_sql("UPDATE dbo.nps_respostas SET excluido = 0 WHERE resposta_id=:resposta_id;", {"resposta_id": resposta_id})