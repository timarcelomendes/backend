import pandas as pd
from sqlalchemy import text
from database import get_engine, exec_sql
import traceback

CATS = ["Promotor", "Neutro", "Detrator"]

def read_df(sql: str, params: dict = None) -> pd.DataFrame:
    engine = get_engine()
    with engine.connect() as conn:
        result = conn.execute(text(sql), params or {})
        rows = result.fetchall()
        cols = list(result.keys())
    return pd.DataFrame(rows, columns=cols)

def load_respostas(q: str, companhia: str, empresa: str, categoria: str, perfil: str, incluir_excluidas: bool, topn: int) -> pd.DataFrame:
    where = []
    params = {}

    if (q or "").strip():
        where.append("(LOWER(r.motivo) LIKE :like OR LOWER(c.nome) LIKE :like OR LOWER(COALESCE(e.nome, r.empresa, c.empresa)) LIKE :like)")
        params["like"] = f"%{q.strip().lower()}%"
        
    # 🏢 Filtro de Companhia
    if companhia and companhia != "Todas":
        where.append("comp.nome = :companhia")
        params["companhia"] = companhia
        
    # 🏢 Filtro de Empresa
    if (empresa or "").strip() and empresa != "Todas":
        where.append("LOWER(COALESCE(e.nome, r.empresa, c.empresa)) LIKE :empresa")
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
        
        COALESCE(
            NULLIF(LTRIM(RTRIM(e.nome)), ''), 
            NULLIF(LTRIM(RTRIM(r.empresa)), ''), 
            NULLIF(LTRIM(RTRIM(c.empresa)), '')
        ) AS empresa,
        
        e.id AS empresa_id,
        e.gestor_id AS gestor_id,
        
        comp.nome AS companhia,
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
    LEFT JOIN dbo.nps_empresas e ON r.empresa_id = e.id
    LEFT JOIN dbo.nps_companhias comp ON e.companhia_id = comp.id
    {where_sql}
    ORDER BY COALESCE(r.data_resposta, r.created_at) DESC, r.resposta_id DESC;
    """
    
    df = read_df(sql, params)
    
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

def processar_acao_automatica(resposta_id, nota, empresa_id, motivo):
    try:
        # 1. Tratamento seguro do ID do n8n
        try:
            eid_val = int(empresa_id) if empresa_id else 0
        except:
            eid_val = 0
            
        # 2. Busca Inteligente da Empresa
        engine = get_engine()
        with engine.connect() as conn:
            if eid_val == 0:
                sql_busca = text("""
                    SELECT e.id, e.nome, e.gestor_id 
                    FROM dbo.nps_respostas r
                    LEFT JOIN dbo.nps_empresas e ON r.empresa = e.nome
                    WHERE r.resposta_id = :rid
                """)
                empresa_data = conn.execute(sql_busca, {"rid": str(resposta_id)}).mappings().first()
            else:
                sql_busca = text("""
                    SELECT id, nome, gestor_id 
                    FROM dbo.nps_empresas 
                    WHERE id = :eid
                """)
                empresa_data = conn.execute(sql_busca, {"eid": eid_val}).mappings().first()

        # 3. Extração dos dados
        id_real = empresa_data["id"] if empresa_data else None
        nome_emp = empresa_data["nome"] if empresa_data else "Conta Geral"
        id_gestor = empresa_data["gestor_id"] if empresa_data else None

        # 4. A GRAVAÇÃO À FORÇA (COM COMMIT EXPLÍCITO)
        sql_insert = text("""
            INSERT INTO dbo.nps_acoes 
            (resposta_id, empresa_id, gestor_id, titulo, descricao, status, prioridade)
            VALUES 
            (:rid, :eid, :gid, :t, :d, 'Pendente', 'Alta')
        """)
        
        params = {
            "rid": str(resposta_id),
            "eid": id_real,
            "gid": id_gestor,
            "t": f"🔥 Ação Automática: {nome_emp}",
            "d": f"Nota: {nota}. Motivo: {motivo}"
        }
        
        # Abriremos uma nova conexão, inserimos e forçamos o Commit
        with engine.connect() as conn:
            conn.execute(sql_insert, params)
            conn.commit()  # 👈 ESTA É A LINHA MÁGICA QUE SALVA DEFINITIVAMENTE!
            
        print(f"✅ SUCESSO ABSOLUTO E GRAVADO COM COMMIT: {nome_emp}!")
        
    except Exception as e:
        print(f"❌ ERRO AO GRAVAR AÇÃO NO BANCO: {e}")
        import traceback
        print(traceback.format_exc())