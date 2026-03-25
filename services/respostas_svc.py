import pandas as pd
from sqlalchemy import text
from database import get_engine, exec_sql
import traceback
import uuid
import requests

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

def processar_acao_automatica(resposta_id, nota, empresa_id, empresa_nome, motivo):
    engine = get_engine()
    
    id_real = None
    nome_emp = "Conta Geral"
    id_gestor = None
        
    try:
        with engine.connect() as conn:
            # TENTATIVA 1: Pelo ID oficial
            try:
                eid_val = int(empresa_id) if empresa_id else 0
            except:
                eid_val = 0

            if eid_val > 0:
                sql_busca = text("SELECT id, nome, gestor_id FROM dbo.nps_empresas WHERE id = :eid")
                empresa_data = conn.execute(sql_busca, {"eid": eid_val}).mappings().first()
                if empresa_data:
                    id_real = empresa_data["id"]
                    nome_emp = empresa_data["nome"]
                    id_gestor = empresa_data["gestor_id"]

            # TENTATIVA 2: Buscar usando o nome em texto enviado pelo n8n
            if not id_real and empresa_nome and str(empresa_nome).strip() != "":
                sql_busca_nome = text("""
                    SELECT TOP 1 id, nome, gestor_id 
                    FROM dbo.nps_empresas 
                    WHERE LOWER(LTRIM(RTRIM(nome))) LIKE :nome_busca
                """)
                param_nome = f"%{str(empresa_nome).strip().lower()}%"
                empresa_data = conn.execute(sql_busca_nome, {"nome_busca": param_nome}).mappings().first()
                
                if empresa_data:
                    id_real = empresa_data["id"]
                    nome_emp = empresa_data["nome"]
                    id_gestor = empresa_data["gestor_id"]
                else:
                    nome_emp = empresa_nome 

        # Gravação na Tabela de Ações
        with engine.begin() as conn:
            sql_insert = text("""
                INSERT INTO dbo.nps_acoes 
                (resposta_id, empresa_id, gestor_id, titulo, descricao, prioridade)
                VALUES 
                (:rid, :eid, :gid, :t, :d, 'Alta')
            """)
            
            params = {
                "rid": str(resposta_id),
                "eid": id_real,
                "gid": id_gestor,
                "t": f"🔥 Ação Automática: {nome_emp}",
                "d": f"Nota: {nota}. Motivo: {motivo}"
            }
            
            conn.execute(sql_insert, params)
            print(f"✅ SUCESSO! Ação criada. Empresa: {nome_emp} | ID Emp: {id_real} | Gestor: {id_gestor}")
            
    except Exception as e:
        print(f"❌ Erro Crítico: {e}")
        print(traceback.format_exc())

def processar_webhook_fillout(payload: dict):
    """Recebe o JSON nativo do Fillout, grava a resposta e gera a ação no Kanban"""
    try:
        engine = get_engine()
        
        # 1. EXTRAÇÃO DE DADOS
        submission = payload.get("submission", {})
        form_id = str(payload.get("formId", ""))
        submission_id = str(submission.get("submissionId", ""))
        
        # Extrair Hidden Fields (URL Parameters)
        url_params = {str(p.get("name", "")).lower(): p.get("value", "") for p in submission.get("urlParameters", [])}
        cliente_id = url_params.get("clienteid", "")
        email = url_params.get("email", "")
        nome = url_params.get("nome", "")
        empresa = url_params.get("empresa", "")
        empresa_id_str = url_params.get("empresa_id", "") or url_params.get("empresaid", "")
        empresa_id = int(empresa_id_str) if empresa_id_str.isdigit() else 0
        
        # Extrair Perguntas (Nota e Motivo)
        nota = None
        motivo = ""
        expectativas = ""
        o_que_faltava = ""
        
        for q in submission.get("questions", []):
            tipo = q.get("type")
            nome_pergunta = str(q.get("name", "")).lower()
            valor = q.get("value")
            
            if tipo == "OpinionScale" and valor is not None:
                nota = int(valor)
            elif tipo == "LongAnswer" and not motivo:
                motivo = str(valor or "")
            elif "expectativas" in nome_pergunta:
                expectativas = str(valor or "")
            elif "faltando" in nome_pergunta or "melhorar" in nome_pergunta:
                o_que_faltava = str(valor or "")
                
        # Classificar Categoria
        categoria = "Indefinido"
        if nota is not None:
            if nota <= 6: categoria = "Detrator"
            elif nota <= 8: categoria = "Neutro"
            else: categoria = "Promotor"
            
        resposta_id = f"F-{cliente_id}-{uuid.uuid4().hex[:8].upper()}"

        with engine.begin() as conn:
            # 2. VERIFICAR DUPLICIDADE (Anti-Spam)
            sql_check = text("SELECT 1 FROM dbo.nps_respostas WHERE submission_id = :sub_id")
            if conn.execute(sql_check, {"sub_id": submission_id}).scalar():
                print(f"⚠️ Webhook ignorado: Submissão {submission_id} já existe.")
                return {"status": "ignorado", "motivo": "duplicado"}

            # 3. GRAVAR A RESPOSTA
            sql_insert = text("""
                INSERT INTO dbo.nps_respostas (
                    resposta_id, cliente_id, email, empresa, empresa_id,
                    data_resposta, nota, categoria, motivo, canal,
                    form_id, submission_id, created_at, expectativas, o_que_faltava
                ) VALUES (
                    :rid, :cid, :email, :emp, :eid,
                    CAST(GETDATE() AS DATE), :nota, :cat, :motivo, 'Fillout',
                    :fid, :sub_id, SYSUTCDATETIME(), :exp, :falta
                )
            """)
            conn.execute(sql_insert, {
                "rid": resposta_id, "cid": cliente_id, "email": email, "emp": empresa, "eid": empresa_id if empresa_id > 0 else None,
                "nota": nota, "cat": categoria, "motivo": motivo,
                "fid": form_id, "sub_id": submission_id, "exp": expectativas, "falta": o_que_faltava
            })
            
            # 4. ATUALIZAR STATUS DO CLIENTE PARA "Respondido"
            if cliente_id:
                sql_update_cli = text("""
                    UPDATE dbo.nps_clientes 
                    SET status_envio = 'Respondido', updated_at = SYSUTCDATETIME() 
                    WHERE cliente_id = :cid
                """)
                conn.execute(sql_update_cli, {"cid": cliente_id})

        print(f"✅ Nova Resposta NPS Guardada! Cliente: {nome} | Nota: {nota}")

        # 5. GERAR AÇÃO NO KANBAN AUTOMATICAMENTE
        # Importação colocada dentro da função para evitar "Circular Imports"
        from services.respostas_svc import processar_acao_automatica
        processar_acao_automatica(
            resposta_id=resposta_id,
            nota=nota,
            empresa_id=empresa_id,
            empresa_nome=empresa,
            motivo=motivo
        )

        # 6. ENVIAR ALERTA TEAMS E E-MAIL DE AGRADECIMENTO
        # Certifique-se de que a função `enviar_alerta_teams` está acessível neste ficheiro
        try:
            enviar_alerta_teams(
                resposta_id=resposta_id,
                nome=nome,
                email=email,
                empresa=empresa,
                nota=nota,
                categoria=categoria,
                motivo=motivo,
                expectativas=expectativas,
                o_que_faltava=o_que_faltava
            )
        except Exception as erro_teams:
            print(f"⚠️ Erro ao tentar enviar alerta para o Teams: {erro_teams}")
            
        # enviar_email_resposta(...)  <-- (Aguardando o próximo passo!)

        return {"status": "success", "resposta_id": resposta_id, "nota": nota}

    except Exception as e:
        print(f"❌ Erro ao processar Webhook Fillout: {e}")
        import traceback
        traceback.print_exc()
        return {"status": "error", "message": str(e)}

def enviar_alerta_teams(resposta_id: str, nome: str, email: str, empresa: str, nota: int, categoria: str, motivo: str, expectativas: str, o_que_faltava: str):
    """Monta um Adaptive Card e envia para o canal do Microsoft Teams configurado"""
    try:
        engine = get_engine()
        with engine.connect() as conn:
            # 1. Vai buscar o URL do webhook que guardámos nas Configurações
            query = text("SELECT valor FROM dbo.nps_configuracoes WHERE chave = 'teams_webhook_url'")
            webhook_url = conn.execute(query).scalar()
            
        if not webhook_url:
            print("⚠️ Webhook do Teams não configurado. Alerta ignorado.")
            return

        # 2. Lógica visual (Emojis e Cores baseadas na Categoria)
        if categoria == 'Detrator':
            emoji, cor_nota = '🚨', 'Attention'  # Vermelho
        elif categoria == 'Neutro':
            emoji, cor_nota = '⚠️', 'Warning'    # Amarelo
        elif categoria == 'Promotor':
            emoji, cor_nota = '✅', 'Good'       # Verde
        else:
            emoji, cor_nota = '📊', 'Default'

        # Textos seguros caso venham vazios
        motivo_txt = motivo if motivo else "Sem comentário adicional."
        expectativas_txt = expectativas if expectativas else "Não respondido."

        # 3. Construção do "Adaptive Card" (O layout oficial da Microsoft)
        card_body = [
            {
                "type": "ColumnSet",
                "columns": [
                    {
                        "type": "Column",
                        "width": "stretch",
                        "items": [
                            { "type": "TextBlock", "text": f"{emoji} NPS Fillout — {categoria}", "weight": "Bolder", "size": "Large", "wrap": True },
                            { "type": "TextBlock", "text": f"Empresa: {empresa if empresa else 'Não identificada'}", "wrap": True, "spacing": "None", "isSubtle": True }
                        ]
                    },
                    {
                        "type": "Column",
                        "width": "auto",
                        "items": [
                            { "type": "TextBlock", "text": f"{nota}/10", "weight": "Bolder", "size": "ExtraLarge", "color": cor_nota, "horizontalAlignment": "Right" }
                        ]
                    }
                ]
            },
            {
                "type": "FactSet",
                "spacing": "Medium",
                "facts": [
                    { "title": "Contato:", "value": nome if nome else "-" },
                    { "title": "E-mail:", "value": email if email else "-" },
                    { "title": "ID Resposta:", "value": resposta_id }
                ]
            },
            { "type": "TextBlock", "text": f"**Motivo da nota:**\n\n{motivo_txt}", "wrap": True, "spacing": "Medium" },
            { "type": "TextBlock", "text": f"**Atendeu às expectativas?**\n\n{expectativas_txt}", "wrap": True, "spacing": "Small" }
        ]

        # Só adiciona a secção "O que faltava" se o cliente tiver preenchido
        if o_que_faltava:
            card_body.append({ "type": "TextBlock", "text": f"**O que estava faltando?**\n\n{o_que_faltava}", "wrap": True, "spacing": "Small" })

        # Embrulha tudo no formato JSON que o Teams exige
        payload_teams = {
            "type": "message",
            "attachments": [{
                "contentType": "application/vnd.microsoft.card.adaptive",
                "contentUrl": None,
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": card_body
                }
            }]
        }

        # 4. Disparo!
        resposta = requests.post(webhook_url, json=payload_teams, headers={"Content-Type": "application/json"})
        
        if resposta.status_code in (200, 201, 202):
            print(f"📣 Alerta Teams enviado com sucesso para {nome}!")
        else:
            print(f"❌ Falha ao enviar para o Teams. HTTP {resposta.status_code}: {resposta.text}")

    except Exception as e:
        print(f"❌ Erro interno ao enviar alerta do Teams: {e}")