import pandas as pd
from sqlalchemy import text
from database import get_engine, exec_sql
import traceback
import uuid
import requests
from datetime import datetime, timedelta

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

def processar_acao_automatica(resposta_id: str, nota: int, empresa_id: int, empresa_nome: str, motivo: str):
    """
    Substitui o antigo fluxo do Jira. 
    Gera tickets automáticos no Kanban interno para notas de risco (<= 8).
    """
    # 1. Regra de Negócio: Não gerar ticket automático para Promotores (9-10)
    if nota is None or nota >= 9:
        return

    engine = get_engine()
    try:
        with engine.begin() as conn:
            # 2. Roteamento Inteligente (Descobrir o Gestor da Conta)
            gestor_id_encontrado = None
            emp_id_real = empresa_id

            if emp_id_real and emp_id_real > 0:
                query_gestor = text("SELECT gestor_id FROM dbo.nps_empresas WHERE id = :eid")
                res = conn.execute(query_gestor, {"eid": emp_id_real}).fetchone()
                if res: 
                    gestor_id_encontrado = res.gestor_id

            elif empresa_nome:
                # Se o Fillout não enviou o ID, tenta achar pelo Nome exato
                query_gestor = text("SELECT id, gestor_id FROM dbo.nps_empresas WHERE nome = :nome")
                res = conn.execute(query_gestor, {"nome": empresa_nome}).fetchone()
                if res:
                    emp_id_real = res.id
                    gestor_id_encontrado = res.gestor_id

            # 3. Definir Prioridade e SLA (Prazo de Resolução)
            if nota <= 6:
                prioridade = "Alta"
                dias_prazo = 2 # SLA de 48h para detratores
            else:
                prioridade = "Média"
                dias_prazo = 5 # SLA de 5 dias para neutros

            prazo_limite = (datetime.now() + timedelta(days=dias_prazo)).strftime("%Y-%m-%d")
            
            # 4. Formatar o Título e a Descrição do Ticket
            titulo = f"[Risco NPS {nota}] Ação Requerida: {empresa_nome or 'Cliente Indefinido'}"
            descricao_txt = f"🚨 Ticket gerado automaticamente via sistema NPS.\n\nComentário Original da Avaliação:\n\"{motivo or 'O cliente não deixou comentários de texto.'}\""

            # 5. Inserir na Tabela do Kanban
            sql_insert = text("""
                INSERT INTO dbo.nps_acoes 
                (resposta_id, empresa_id, gestor_id, titulo, descricao, prioridade, prazo_limite, status)
                VALUES (:rid, :eid, :gid, :t, :d, :p, :pl, 'Pendente')
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

            print(f"🎫 Ticket automático no Kanban criado com sucesso para a resposta {resposta_id}!")

    except Exception as e:
        print(f"❌ Erro ao tentar criar ação automática no Kanban: {e}")

def processar_webhook_fillout(payload: dict):
    """Recebe o JSON nativo do Fillout, grava a resposta e gera a ação no Kanban"""
    try:
        engine = get_engine()
        
        # 1. EXTRAÇÃO DE DADOS (Dupla Verificação: URL e Formulário)
        submission = payload.get("submission", {})
        form_id = str(payload.get("formId", ""))
        submission_id = str(submission.get("submissionId", ""))
        
        # Mapear parâmetros da URL (Hidden Fields)
        url_params = {str(p.get("name", "")).lower().replace("_", ""): p.get("value") for p in submission.get("urlParameters", [])}
        
        # Mapear as Perguntas
        by_label = {}
        nota = None
        motivo = ""
        expectativas = ""
        o_que_faltava = ""
        
        for q in submission.get("questions", []):
            tipo = q.get("type")
            nome_pergunta = str(q.get("name", "")).lower()
            valor = q.get("value")
            
            # Guardar tudo num dicionário para busca fácil depois
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
                # O primeiro LongAnswer que sobrar é o motivo
                motivo = str(valor or "")

        # Função auxiliar para procurar dados (Primeiro na URL, depois nas Perguntas)
        def extrair_dado_seguro(chaves):
            # 1. Tentar na URL
            for chave in chaves:
                if chave in url_params and url_params[chave] not in (None, ""):
                    return str(url_params[chave]).strip()
            # 2. Tentar nas Perguntas
            for key_pergunta, val_pergunta in by_label.items():
                if val_pergunta not in (None, ""):
                    for chave in chaves:
                        if chave in key_pergunta:
                            return str(val_pergunta).strip()
            return ""

        # Extração blindada
        cliente_id = extrair_dado_seguro(["clienteid", "id cliente"])
        email = extrair_dado_seguro(["email"])
        nome = extrair_dado_seguro(["nome"])
        empresa = extrair_dado_seguro(["empresa"])
        empresa_id_str = extrair_dado_seguro(["empresaid"])
        empresa_id = int(empresa_id_str) if empresa_id_str.isdigit() else 0
                
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

        print(f"✅ Nova Resposta Guardada! Cliente: {nome} | Empresa: {empresa} | Nota: {nota}")

        # 5. GERAR AÇÃO NO KANBAN AUTOMATICAMENTE
        from services.respostas_svc import processar_acao_automatica
        processar_acao_automatica(
            resposta_id=resposta_id,
            nota=nota,
            empresa_id=empresa_id,
            empresa_nome=empresa,
            motivo=motivo
        )

        # 6. ENVIAR ALERTA TEAMS
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

        # =======================================================
        # 7. ENVIAR E-MAIL DE AGRADECIMENTO (CLOSE THE LOOP)
        # =======================================================
        try:
            from services.email_svc import enviar_email_resposta
            enviar_email_resposta(
                email_destino=email, 
                nome=nome, 
                empresa=empresa, 
                nota=nota, 
                categoria=categoria
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
            # 1. URL do webhook
            query_webhook = text("SELECT valor FROM dbo.nps_configuracoes WHERE chave = 'teams_webhook_url'")
            webhook_url = conn.execute(query_webhook).scalar()
            
            # 2. Buscar Perfil e Segmento na base de dados
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

        # 3. Lógica visual (Emojis e Cores baseadas na Categoria)
        if categoria == 'Detrator':
            emoji, cor_nota = '🚨', 'Attention'
        elif categoria == 'Neutro':
            emoji, cor_nota = '⚠️', 'Warning'
        elif categoria == 'Promotor':
            emoji, cor_nota = '✅', 'Good'
        else:
            emoji, cor_nota = '📊', 'Default'

        # Textos seguros caso venham vazios
        motivo_txt = motivo if motivo else "Sem comentário."
        expectativas_txt = expectativas if expectativas else "Não respondido."
        data_hoje = datetime.now().strftime("%Y-%m-%d")

        # 4. URLs dos Botões do Fillout
        url_painel = f"https://build.fillout.com/editor/{form_id}/results"
        url_resposta = f"https://build.fillout.com/editor/{form_id}/results?sessionId={submission_id}"

        # 5. Construção do "Adaptive Card" (O layout exato que pediu)
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

        # Se o cliente preencheu "O que estava faltando", adiciona esse bloco
        if o_que_faltava:
            card_body.append({ "type": "TextBlock", "text": f"**O que estava faltando?**\n{o_que_faltava}", "wrap": True, "spacing": "Small" })

        # Assinatura do sistema (substitui a propaganda do n8n)
        card_body.append({
            "type": "TextBlock", 
            "text": "🤖 *Enviado automaticamente pelo Hub de NPS*", 
            "wrap": True, 
            "spacing": "Large", 
            "size": "Small", 
            "isSubtle": True
        })

        # 6. Embrulha no formato JSON do Teams e adiciona as "Actions" (Botões)
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

        # 7. Disparo!
        resposta = requests.post(webhook_url, json=payload_teams, headers={"Content-Type": "application/json"})
        
        if resposta.status_code in (200, 201, 202):
            print(f"📣 Alerta Teams enviado com sucesso para {nome}!")
        else:
            print(f"❌ Falha ao enviar para o Teams. HTTP {resposta.status_code}: {resposta.text}")

    except Exception as e:
        print(f"❌ Erro interno ao enviar alerta do Teams: {e}")