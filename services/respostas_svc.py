import pandas as pd
from sqlalchemy import text
from database import get_engine, exec_sql
import traceback
import uuid
import requests
from datetime import datetime, timedelta

CATS = ["Promotor", "Neutro", "Detrator"]

def obter_regras_dinamicas():
    """Lê as parametrizações de negócio da base de dados"""
    from database import get_engine
    from sqlalchemy import text
    
    regras = {
        "sla_detrator_dias": 2,
        "sla_neutro_dias": 5,
        "sla_promotor_dias": 7,
        "fillout_campos": "clienteId,email,nome,empresa,empresa_id",
        "email_template_html": ""
    }
    
    try:
        engine = get_engine()
        with engine.connect() as conn:
            query = text("SELECT chave, valor FROM dbo.nps_configuracoes WHERE chave IN ('sla_detrator_dias', 'sla_neutro_dias', 'sla_promotor_dias', 'fillout_campos', 'email_template_html')")
            for linha in conn.execute(query).fetchall():
                if linha.chave in ['sla_detrator_dias', 'sla_neutro_dias', 'sla_promotor_dias']:
                    regras[linha.chave] = int(linha.valor) if linha.valor else regras[linha.chave]
                else:
                    regras[linha.chave] = linha.valor
    except Exception as e:
        print(f"⚠️ Usando regras padrão. Erro ao ler banco: {e}")
        
    return regras

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
        
    if companhia and companhia != "Todas":
        where.append("comp.nome = :companhia")
        params["companhia"] = companhia
        
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
        r.excluido,
        
        (SELECT TOP 1 id FROM dbo.nps_acoes WHERE resposta_id = r.resposta_id) AS acao_vinculada
        
    FROM BaseHistorico r
    LEFT JOIN dbo.nps_clientes c ON r.cliente_id = c.cliente_id
    LEFT JOIN dbo.nps_empresas e ON r.empresa_id = e.id
    LEFT JOIN dbo.nps_companhias comp ON e.companhia_id = comp.id
    {where_sql}
    ORDER BY COALESCE(r.data_resposta, r.created_at) DESC, r.resposta_id DESC;
    """
    
    # Executa o sql gigante de cima
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

from datetime import datetime, timedelta
from sqlalchemy import text
from database import get_engine

def processar_acao_automatica(resposta_id: str, nota: int, empresa_id: int, empresa_nome: str, motivo: str):
    """
    Gera tickets automáticos no Kanban interno para TODAS as respostas.
    """
    if nota is None:
        return

    # 1. Carregar as regras (SLA)
    regras = obter_regras_dinamicas()
    
    # 2. Descobrir Categoria, SLA e a Prioridade apropriada
    if nota <= 6:
        categoria = "Detrator"
        prioridade = "Alta"
        dias_prazo = int(regras.get("sla_detrator_dias", 2))
    elif nota <= 8:
        categoria = "Neutro"
        prioridade = "Média"
        dias_prazo = int(regras.get("sla_neutro_dias", 5))
    else:
        categoria = "Promotor"
        prioridade = "Baixa"
        dias_prazo = int(regras.get("sla_promotor_dias", 7)) # SLA garantido para Promotores

    engine = get_engine()
    try:
        with engine.begin() as conn:
            # 3. Roteamento Inteligente (Gestor da Conta)
            gestor_id_encontrado = None
            emp_id_real = empresa_id

            if emp_id_real and emp_id_real > 0:
                query_gestor = text("SELECT gestor_id FROM dbo.nps_empresas WHERE id = :eid")
                res = conn.execute(query_gestor, {"eid": emp_id_real}).fetchone()
                if res: 
                    gestor_id_encontrado = res.gestor_id

            elif empresa_nome:
                query_gestor = text("SELECT id, gestor_id FROM dbo.nps_empresas WHERE nome = :nome")
                res = conn.execute(query_gestor, {"nome": empresa_nome}).fetchone()
                if res:
                    emp_id_real = res.id
                    gestor_id_encontrado = res.gestor_id

            prazo_limite = (datetime.now() + timedelta(days=dias_prazo)).strftime("%Y-%m-%d %H:%M:%S")
            titulo = f"[{categoria} NPS {nota}] Ação Requerida: {empresa_nome or 'Cliente Indefinido'}"
            
            # 4. Gauge AI (Plano de Ação)
            descricao_txt = f"🚨 Ticket gerado automaticamente via sistema NPS.\n\nComentário Original:\n\"{motivo or 'O cliente não deixou comentários de texto.'}\""
            
            if motivo and len(motivo.strip()) > 3:
                import os
                from openai import OpenAI
                try:
                    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
                    prompt_ai = f"O cliente '{empresa_nome}' deu nota {nota} no NPS. Comentário: '{motivo}'. Como especialista em Customer Success, crie um plano de ação direto, prático e em bullet points (máximo 3 passos curtos) para a nossa equipa recuperar/fidelizar este cliente. Comece exatamente com a frase: '🤖 Análise Gauge AI:'"
                    
                    resposta_ai = client.chat.completions.create(
                        model="gpt-4o-mini",
                        messages=[{"role": "user", "content": prompt_ai}],
                        temperature=0.7,
                        max_tokens=200
                    )
                    plano_ai = resposta_ai.choices[0].message.content
                    descricao_txt = f"🚨 Ticket gerado via sistema NPS.\n\n💬 Comentário Original:\n\"{motivo}\"\n\n{plano_ai}"
                except Exception as e_ai:
                    print(f"⚠️ Aviso: Falha ao gerar plano com Gauge AI. Erro: {e_ai}")

            # 5. Inserir na Tabela (CORRIGIDO: Apenas colunas que existem fisicamente na tabela nps_acoes)
            sql_insert = text("""
                INSERT INTO dbo.nps_acoes 
                (resposta_id, empresa_id, gestor_id, titulo, descricao, prioridade, prazo_limite, status, created_at, updated_at)
                VALUES 
                (:rid, :eid, :gid, :t, :d, :p, :pl, 'Pendente', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """)

            conn.execute(sql_insert, {
                "rid": resposta_id,
                "eid": emp_id_real if emp_id_real and emp_id_real > 0 else None,
                "gid": gestor_id_encontrado,
                "t": titulo,
                "d": descricao_txt,
                "p": prioridade,
                "pl": prazo_limite
            })

            print(f"🎫 Ticket automático ({categoria}) no Kanban criado com sucesso para a resposta {resposta_id}!")

    except Exception as e:
        print(f"❌ Erro crítico ao processar ação automática no Kanban: {e}")
        import traceback
        traceback.print_exc()

def processar_webhook_fillout(payload: dict):
    """Recebe o JSON nativo do Fillout, grava a resposta e gera a ação no Kanban"""
    try:
        engine = get_engine()
        
        submission = payload.get("submission", {})
        form_id = str(payload.get("formId", ""))
        submission_id = str(submission.get("submissionId", ""))
        
        url_params = {str(p.get("name", "")).lower().replace("_", ""): p.get("value") for p in submission.get("urlParameters", [])}
        
        by_label = {}
        nota = None
        motivo = ""
        expectativas = ""
        o_que_faltava = ""
        
        for q in submission.get("questions", []):
            tipo = q.get("type")
            nome_pergunta = str(q.get("name", "")).lower()
            valor = q.get("value")
            
            by_label[nome_pergunta] = valor
            
            if tipo == "OpinionScale" and valor not in (None, ""):
                try:
                    nota = int(float(valor))
                except:
                    pass
            elif "expectativas" in nome_pergunta:
                expectativas = str(valor or "")
            elif "faltando" in nome_pergunta or "melhorar" in nome_pergunta:
                o_que_faltava = str(valor or "")
            elif tipo == "LongAnswer" and not motivo:
                motivo = str(valor or "")

        def extrair_dado_seguro(chaves):
            for chave in chaves:
                if chave in url_params and url_params[chave] not in (None, ""):
                    return str(url_params[chave]).strip()
            for key_pergunta, val_pergunta in by_label.items():
                if val_pergunta not in (None, ""):
                    for chave in chaves:
                        if chave in key_pergunta:
                            return str(val_pergunta).strip()
            return ""

        cliente_id = extrair_dado_seguro(["clienteid", "id cliente"])
        email = extrair_dado_seguro(["email"])
        nome = extrair_dado_seguro(["nome"])
        empresa = extrair_dado_seguro(["empresa"])
        empresa_id_str = extrair_dado_seguro(["empresaid"])
        empresa_id = int(empresa_id_str) if empresa_id_str.isdigit() else 0
                
        categoria = "Indefinido"
        if nota is not None:
            if nota <= 6: categoria = "Detrator"
            elif nota <= 8: categoria = "Neutro"
            else: categoria = "Promotor"
            
        resposta_id = f"F-{cliente_id}-{uuid.uuid4().hex[:8].upper()}"

        with engine.begin() as conn:
            sql_check = text("SELECT 1 FROM dbo.nps_respostas WHERE submission_id = :sub_id")
            if conn.execute(sql_check, {"sub_id": submission_id}).scalar():
                print(f"⚠️ Webhook ignorado: Submissão {submission_id} já existe.")
                return {"status": "ignorado", "motivo": "duplicado"}

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
            
            if cliente_id:
                sql_update_cli = text("""
                    UPDATE dbo.nps_clientes 
                    SET status_envio = 'Respondido', updated_at = SYSUTCDATETIME() 
                    WHERE cliente_id = :cid
                """)
                conn.execute(sql_update_cli, {"cid": cliente_id})

        print(f"✅ Nova Resposta Guardada! Cliente: {nome} | Empresa: {empresa} | Nota: {nota}")

        from services.respostas_svc import processar_acao_automatica
        processar_acao_automatica(
            resposta_id=resposta_id,
            nota=nota,
            empresa_id=empresa_id,
            empresa_nome=empresa,
            motivo=motivo
        )

        try:
            enviar_alerta_teams(
                resposta_id=resposta_id,
                cliente_id=cliente_id,
                nome=nome,
                email=email,
                empresa=empresa,
                nota=nota,
                categoria=categoria,
                motivo=motivo,
                expectativas=expectativas,
                o_que_faltava=o_que_faltava,
                form_id=form_id,
                submission_id=submission_id
            )
        except Exception as erro_teams:
            print(f"⚠️ Erro ao enviar alerta Teams: {erro_teams}")

        try:
            from services.email_svc import enviar_email_resposta
            enviar_email_resposta(
                email_destino=email, 
                nome=nome, 
                empresa=empresa, 
                nota=nota, 
                categoria=categoria,
                motivo=motivo,
                expectativas=expectativas,
                o_que_faltava=o_que_faltava
            )
        except Exception as erro_email:
            print(f"⚠️ Erro ao enviar o e-mail de agradecimento: {erro_email}")

        return {"status": "success", "resposta_id": resposta_id, "nota": nota}

    except Exception as e:
        print(f"❌ Erro ao processar Webhook Fillout: {e}")
        import traceback
        traceback.print_exc()
        return {"status": "error", "message": str(e)}


def enviar_alerta_teams(resposta_id: str, cliente_id: str, nome: str, email: str, empresa: str, nota: int, categoria: str, motivo: str, expectativas: str, o_que_faltava: str, form_id: str, submission_id: str):
    """Monta um Adaptive Card com layout avançado e envia para o Teams"""
    try:
        engine = get_engine()
        with engine.connect() as conn:
            query_webhook = text("SELECT valor FROM dbo.nps_configuracoes WHERE chave = 'teams_webhook_url'")
            webhook_url = conn.execute(query_webhook).scalar()
            
            perfil, segmento = "-", "-"
            if cliente_id:
                query_cli = text("SELECT perfil_decisor, segmento FROM dbo.nps_clientes WHERE cliente_id = :cid")
                res_cli = conn.execute(query_cli, {"cid": cliente_id}).fetchone()
                if res_cli:
                    perfil = res_cli.perfil_decisor or "-"
                    segmento = res_cli.segmento or "-"

        if not webhook_url:
            print("⚠️ Webhook do Teams não configurado. Alerta ignorado.")
            return

        if categoria == 'Detrator':
            emoji, cor_nota = '🚨', 'Attention'
        elif categoria == 'Neutro':
            emoji, cor_nota = '⚠️', 'Warning'
        elif categoria == 'Promotor':
            emoji, cor_nota = '✅', 'Good'
        else:
            emoji, cor_nota = '📊', 'Default'

        motivo_txt = motivo if motivo else "Sem comentário."
        expectativas_txt = expectativas if expectativas else "Não respondido."
        data_hoje = datetime.now().strftime("%Y-%m-%d")

        url_painel = f"https://build.fillout.com/editor/{form_id}/results"
        url_resposta = f"https://build.fillout.com/editor/{form_id}/results?sessionId={submission_id}"

        card_body = [
            {
                "type": "ColumnSet",
                "columns": [
                    {
                        "type": "Column",
                        "width": "stretch",
                        "items": [
                            { "type": "TextBlock", "text": f"{emoji} NPS Fillout — {categoria}", "weight": "Bolder", "size": "Large", "wrap": True },
                            { "type": "TextBlock", "text": f"Empresa: {empresa if empresa else '-'}", "wrap": True, "spacing": "None", "isSubtle": True }
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
                    { "title": "Data:", "value": data_hoje },
                    { "title": "ClienteId:", "value": cliente_id if cliente_id else "-" },
                    { "title": "Perfil:", "value": perfil },
                    { "title": "Segmento:", "value": segmento },
                    { "title": "RespostaId:", "value": resposta_id }
                ]
            },
            { "type": "TextBlock", "text": f"**Motivo da nota:**\n{motivo_txt}", "wrap": True, "spacing": "Medium" },
            { "type": "TextBlock", "text": f"**Atendeu às expectativas?**\n{expectativas_txt}", "wrap": True, "spacing": "Small" }
        ]

        if o_que_faltava:
            card_body.append({ "type": "TextBlock", "text": f"**O que estava faltando?**\n{o_que_faltava}", "wrap": True, "spacing": "Small" })

        card_body.append({
            "type": "TextBlock", 
            "text": "🤖 *Enviado automaticamente pelo Hub de NPS*", 
            "wrap": True, 
            "spacing": "Large", 
            "size": "Small", 
            "isSubtle": True
        })

        payload_teams = {
            "type": "message",
            "attachments": [{
                "contentType": "application/vnd.microsoft.card.adaptive",
                "contentUrl": None,
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": card_body,
                    "actions": [
                        { "type": "Action.OpenUrl", "title": "Ver Painel Geral", "url": url_painel },
                        { "type": "Action.OpenUrl", "title": "Ver Resposta Específica", "url": url_resposta }
                    ]
                }
            }]
        }

        resposta = requests.post(webhook_url, json=payload_teams, headers={"Content-Type": "application/json"})
        
        if resposta.status_code in (200, 201, 202):
            print(f"📣 Alerta Teams enviado com sucesso para {nome}!")
        else:
            print(f"❌ Falha ao enviar para o Teams. HTTP {resposta.status_code}: {resposta.text}")

    except Exception as e:
        print(f"❌ Erro interno ao enviar alerta do Teams: {e}")