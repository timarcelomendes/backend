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
        
        -- 👇 CORREÇÃO: Enviamos para o Vue diretamente como "empresa"
        COALESCE(
            NULLIF(LTRIM(RTRIM(e.nome)), ''), 
            NULLIF(LTRIM(RTRIM(r.empresa)), ''), 
            NULLIF(LTRIM(RTRIM(c.empresa)), '')
        ) AS empresa,
        
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

def processar_acao_automatica(resposta_id: str, nota: int, empresa_id: int, motivo: str):
    """
    Gatilho que verifica a configuração do sistema e cria uma Ação Automática no Kanban.
    """
    # Determina a categoria da nota
    categoria = "Promotor" if nota >= 9 else "Neutro" if nota >= 7 else "Detrator"
    
    try:
        from database import get_engine
        from sqlalchemy import text
        engine = get_engine()
        with engine.begin() as conn:
            # 1. Descobre quais categorias têm a automação ligada
            sql_config = text("SELECT valor FROM dbo.nps_configuracoes WHERE chave = 'auto_acao_categorias'")
            categorias_str = conn.execute(sql_config).scalar() or ""
            categorias_ativas = [c.strip() for c in categorias_str.split(",")]
            
            # 2. Se a categoria atual (ex: Detrator) estiver na lista ativa...
            if categoria in categorias_ativas:
                
                # 3. Verifica se já existe para evitar duplicados caso a API seja chamada 2x
                sql_check = text("SELECT COUNT(*) FROM dbo.nps_acoes WHERE resposta_id = :rid")
                existe = conn.execute(sql_check, {"rid": resposta_id}).scalar()
                
                if not existe:
                    # 4. Cria a Ação (Ticket) automaticamente!
                    prioridade = "Alta" if categoria == "Detrator" else "Média"
                    titulo = f"🔥 Ação Automática: Análise de {categoria}"
                    
                    texto_motivo = motivo if motivo and str(motivo).strip() else "O cliente não deixou comentário."
                    desc = f"Ticket gerado automaticamente pelo sistema.\n\nNota atribuída: {nota}\nComentário Original: \"{texto_motivo}\"\n\nPor favor, entre em contacto com o cliente para fechar o loop."
                    
                    sql_insert = text("""
                        INSERT INTO dbo.nps_acoes (resposta_id, empresa_id, titulo, descricao, prioridade, status)
                        VALUES (:rid, :eid, :t, :d, :p, 'Pendente')
                    """)
                    conn.execute(sql_insert, {
                        "rid": resposta_id, 
                        "eid": empresa_id,
                        "t": titulo, 
                        "d": desc, 
                        "p": prioridade
                    })
                    print(f"✅ Ticket automático criado para resposta {resposta_id} ({categoria})")
                    
    except Exception as e:
        print(f"❌ Erro na automação de ações: {e}")