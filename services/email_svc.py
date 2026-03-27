import requests
from sqlalchemy import text
import urllib.parse
from database import get_engine
from datetime import datetime

def obter_regras_dinamicas():
    """Lê as parametrizações de negócio da base de dados"""
    from database import get_engine
    from sqlalchemy import text
    
    # Valores de segurança (Fallback)
    regras = {
        "sla_detrator_dias": 2,
        "sla_neutro_dias": 5,
        "sla_promotor_dias": 7,
        "fillout_campos": "clienteId,email,nome,empresa,empresa_id",
        "email_template_html": "",
        "email_agradecimento_promotor": "",
        "email_agradecimento_neutro": "",
        "email_agradecimento_detrator": ""
    }
    
    try:
        engine = get_engine()
        with engine.connect() as conn:
            query = text("SELECT chave, valor FROM dbo.nps_configuracoes WHERE chave IN ('sla_detrator_dias', 'sla_neutro_dias', 'sla_promotor_dias', 'fillout_campos', 'email_template_html', 'email_agradecimento_html')")
            for linha in conn.execute(query).fetchall():
                if linha.chave in ['sla_detrator_dias', 'sla_neutro_dias', 'sla_promotor_dias']:
                    regras[linha.chave] = int(linha.valor) if linha.valor else regras[linha.chave]
                else:
                    regras[linha.chave] = linha.valor
    except Exception as e:
        print(f"⚠️ Usando regras padrão. Erro ao ler banco: {e}")
        
    return regras

def obter_configuracoes_email():
    """Procura as credenciais ativas na base de dados."""
    # Import local para evitar import circular
    from database import get_engine 
    engine = get_engine()
    try:
        with engine.connect() as conn:
            query = text("""
                SELECT TOP 1 tenant_id, client_id, client_secret, refresh_token, email_remetente 
                FROM dbo.nps_configuracoes_email
            """)
            # Usamos mappings() para poder aceder como config['tenant_id']
            return conn.execute(query).mappings().first()
    except Exception as e:
        print(f"❌ Erro ao ler banco: {e}")
        return None

def gerar_access_token(config):
    """Troca o refresh_token por um access_token válido."""
    try:
        url = f"https://login.microsoftonline.com/{config['tenant_id']}/oauth2/v2.0/token"
        payload = {
            'client_id': config['client_id'],
            'client_secret': config['client_secret'],
            'refresh_token': config['refresh_token'],
            'grant_type': 'refresh_token',
            'scope': 'mail.send'
        }
        res = requests.post(url, data=payload).json()
        
        if 'error' in res:
            print(f"❌ Erro Microsoft Token: {res.get('error_description')}")
            return None
            
        return res.get('access_token')
    except Exception as e:
        print(f"❌ Erro na renovação do token: {e}")
        return None
    
def get_valid_access_token():
    """Função mestre para obter um token pronto para uso"""
    config = obter_configuracoes_email()
    if not config or not config['refresh_token']:
        print("⚠️ E-mail não configurado ou não autorizado.")
        return None
    return gerar_access_token(config)

import os  # <-- Certifique-se de que tem este import no topo do seu ficheiro email_svc.py

def enviar_email_recuperacao(email_destino, token):
    """Envia o e-mail com o link de recuperação de palavra-passe com design premium."""
    
    # 1. Obtém o token válido da Graph API
    access_token = get_valid_access_token()
    
    if not access_token:
        print("❌ Falha crítica: Não foi possível obter Access Token para recuperação de senha.")
        return False

    url_send = "https://graph.microsoft.com/v1.0/me/sendMail"
    
    # 🔗 LINK INTELIGENTE (Localhost vs Nuvem)
    frontend_url = os.getenv("FRONTEND_URL", "http://localhost:5173")
    
    # Limpa uma eventual barra no final do link para evitar erros como "com//redefinir"
    frontend_url = frontend_url.rstrip('/') 
    
    link_recuperacao = f"{frontend_url}/redefinir-senha?token={token}"
    
    # 2. Monta o corpo do e-mail de recuperação (Design Premium)
    payload = {
        "message": {
            "subject": "Recuperação de Palavra-passe - NPS Intelligence",
            "body": {
                "contentType": "HTML",
                "content": f"""
                <!DOCTYPE html>
                <html>
                <body style="margin: 0; padding: 0; background-color: #f8fafc; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;">
                    <table width="100%" cellpadding="0" cellspacing="0" style="background-color: #f8fafc; padding: 40px 20px;">
                        <tr>
                            <td align="center">
                                <table width="100%" max-width="500" cellpadding="0" cellspacing="0" style="max-width: 500px; background-color: #ffffff; border-radius: 20px; box-shadow: 0 10px 25px -5px rgba(0,0,0,0.05); border: 1px solid #e2e8f0; overflow: hidden;">
                                    
                                    <tr>
                                        <td align="center" style="padding: 40px 20px 20px 20px;">
                                            <span style="font-size: 28px; font-weight: 900; color: #0f172a; font-style: italic; letter-spacing: -1px;">
                                                NPS <span style="color: #f97316;">Intelligence</span>
                                            </span>
                                        </td>
                                    </tr>
                                    
                                    <tr>
                                        <td style="padding: 0 40px 30px 40px; text-align: left;">
                                            <h2 style="color: #0f172a; font-size: 20px; margin-bottom: 15px; font-weight: 800; letter-spacing: -0.5px;">Recuperação de Acesso</h2>
                                            
                                            <p style="color: #475569; font-size: 15px; line-height: 1.6; margin-bottom: 25px;">
                                                Recebemos um pedido para repor a palavra-passe associada à sua conta corporativa. Clique no botão abaixo para criar uma nova palavra-passe de acesso à plataforma.
                                            </p>
                                            
                                            <table width="100%" cellpadding="0" cellspacing="0">
                                                <tr>
                                                    <td align="center" style="padding: 10px 0 30px 0;">
                                                        <a href="{link_recuperacao}" target="_blank" style="display: inline-block; background-color: #f97316; background-image: linear-gradient(to right, #f97316, #e11d48); color: #ffffff; font-size: 14px; font-weight: bold; text-decoration: none; padding: 16px 32px; border-radius: 12px; text-transform: uppercase; letter-spacing: 2px;">
                                                            Criar Nova Palavra-passe
                                                        </a>
                                                    </td>
                                                </tr>
                                            </table>
                                            
                                            <p style="color: #64748b; font-size: 14px; line-height: 1.6; margin-bottom: 0;">
                                                <strong>Atenção:</strong> Este link é válido apenas por <strong>1 hora</strong>. Se o prazo expirar, terá de solicitar um novo link de recuperação.
                                            </p>
                                        </td>
                                    </tr>
                                    
                                    <tr>
                                        <td style="background-color: #f1f5f9; padding: 25px 40px; border-top: 1px solid #e2e8f0;">
                                            <p style="margin: 0; color: #64748b; font-size: 12px; line-height: 1.5; text-align: center;">
                                                Se não pediu a reposição da palavra-passe, pode ignorar este e-mail com segurança. A sua conta continuará protegida.
                                            </p>
                                        </td>
                                    </tr>
                                </table>
                                
                                <table width="100%" max-width="500" cellpadding="0" cellspacing="0" style="max-width: 500px;">
                                    <tr>
                                        <td align="center" style="padding: 20px 0;">
                                            <p style="margin: 0; color: #94a3b8; font-size: 10px; text-transform: uppercase; letter-spacing: 3px; font-weight: 800;">
                                                NPS Intelligence © 2026
                                            </p>
                                        </td>
                                    </tr>
                                </table>
                                
                            </td>
                        </tr>
                    </table>
                </body>
                </html>
                """
            },
            "toRecipients": [{"emailAddress": {"address": email_destino}}]
        },
        "saveToSentItems": "true"
    }

    headers = {
        'Authorization': f'Bearer {access_token}', 
        'Content-Type': 'application/json'
    }

    try:
        import requests
        response = requests.post(url_send, json=payload, headers=headers)
        if response.status_code == 202:
            print(f"✅ E-mail de recuperação enviado para {email_destino}")
            return True
        else:
            print(f"❌ Erro Graph API ({response.status_code}): {response.text}")
            return False
    except Exception as e:
        print(f"❌ Falha no disparo de recuperação: {e}")
        return False
    
def enviar_email_senha_alterada(email_destino):
    """Envia um e-mail confirmando que a senha foi alterada com sucesso."""
    # 1. Obtém o token válido
    access_token = get_valid_access_token()
    
    if not access_token:
        print("❌ Falha crítica: Não foi possível obter Access Token para confirmação de senha.")
        return False

    url_send = "https://graph.microsoft.com/v1.0/me/sendMail"
    
    # 2. Monta o corpo do e-mail de segurança
    payload = {
        "message": {
            "subject": "Aviso de Segurança: A sua senha foi alterada - NPS Intelligence",
            "body": {
                "contentType": "HTML",
                "content": f"""
                <div style="font-family: sans-serif; color: #334155; max-width: 500px; padding: 25px; border: 1px solid #e2e8f0; border-radius: 16px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.05);">
                    <div style="text-align: center; margin-bottom: 20px;">
                        <span style="font-size: 24px; font-weight: 900; color: #0f172a; font-style: italic;">NPS <span style="color: #f97316;">Intelligence</span></span>
                    </div>
                    <h2 style="color: #10b981; margin-top: 0;">Senha Alterada com Sucesso</h2>
                    <p>Olá,</p>
                    <p>Confirmamos que a senha da sua conta foi alterada recentemente.</p>
                    <p>Se foi você quem fez esta alteração, não é necessária nenhuma ação adicional. Pode aceder à plataforma normalmente.</p>
                    <div style="margin: 30px 0; padding: 15px; background-color: #fef2f2; border-left: 4px solid #ef4444; border-radius: 4px;">
                        <p style="margin: 0; color: #991b1b; font-size: 14px;">
                            <strong>Não foi você?</strong><br>
                            Se não solicitou esta alteração, contacte imediatamente o administrador do sistema para proteger o seu acesso.
                        </p>
                    </div>
                    <hr style="border: none; border-top: 1px solid #e2e8f0; margin: 20px 0;">
                    <p style="font-size: 11px; color: #94a3b8; line-height: 1.5; text-align: center;">
                        Este é um e-mail automático de segurança, por favor não responda. <br>
                        NPS Intelligence Hub © 2026
                    </p>
                </div>
                """
            },
            "toRecipients": [{"emailAddress": {"address": email_destino}}]
        },
        "saveToSentItems": "true"
    }

    headers = {
        'Authorization': f'Bearer {access_token}', 
        'Content-Type': 'application/json'
    }

    try:
        import requests
        response = requests.post(url_send, json=payload, headers=headers)
        if response.status_code == 202:
            print(f"✅ E-mail de confirmação de alteração de senha enviado para {email_destino}")
            return True
        else:
            print(f"❌ Erro Graph API ({response.status_code}): {response.text}")
            return False
    except Exception as e:
        print(f"❌ Falha no disparo de confirmação de senha: {e}")
        return False
    
def enviar_email_teste(email_destino):
    """Envia um e-mail simples para validar a configuração da API."""
    config = obter_configuracoes_email()
    
    if not config or not config['refresh_token']:
        print("⚠️ E-mail não configurado ou não autorizado.")
        return False

    # 1. Obtém um token novo
    access_token = gerar_access_token(config)
    if not access_token:
        return False

    # 2. Configura o endpoint da Microsoft Graph
    url_send = "https://graph.microsoft.com/v1.0/me/sendMail"
    
    # 3. Monta o conteúdo do e-mail de teste
    payload = {
        "message": {
            "subject": "Teste de Conexão - NPS Intelligence ✅",
            "body": {
                "contentType": "HTML",
                "content": f"""
                <div style="font-family: sans-serif; border: 2px solid #10b981; padding: 20px; border-radius: 15px;">
                    <h2 style="color: #10b981;">Conexão Bem-sucedida!</h2>
                    <p>Este é um e-mail de teste disparado pelo painel de configurações.</p>
                    <p>Se você recebeu esta mensagem, a integração com o Outlook está <b>ativa e funcional</b>.</p>
                </div>
                """
            },
            "toRecipients": [{"emailAddress": {"address": email_destino}}]
        },
        "saveToSentItems": True
    }

    headers = {
        'Authorization': f'Bearer {access_token}', 
        'Content-Type': 'application/json'
    }

    try:
        response = requests.post(url_send, json=payload, headers=headers)
        return response.status_code == 202 
    except Exception as e:
        print(f"❌ Falha no disparo de teste: {e}")
        return False

def processar_disparos_nps():
    """Busca clientes elegíveis e envia a pesquisa via Microsoft Graph"""
    print("⏳ Iniciando rotina de disparo de NPS...")
    engine = get_engine()
    
    # 1. Buscar quem deve receber a pesquisa hoje
    sql_busca = text("""
        SELECT TOP (100)
            c.cliente_id, c.email, c.nome, c.empresa, 
            e.id AS empresa_id
        FROM dbo.nps_clientes c
        LEFT JOIN dbo.nps_empresas e ON c.empresa = e.nome
        WHERE c.ativo = 1
          AND c.status_envio IN ('Pendente', 'Erro')
          AND (c.proximo_envio IS NULL OR c.proximo_envio <= CAST(GETDATE() AS DATE))
        ORDER BY COALESCE(c.proximo_envio, '1900-01-01') ASC
    """)
    
    try:
        with engine.connect() as conn:
            elegiveis = conn.execute(sql_busca).mappings().all()
            
        if not elegiveis:
            print("✅ Nenhum cliente elegível para disparo de NPS no momento.")
            return

        # 2. Obter o Token do Microsoft Graph (USE A SUA FUNÇÃO EXISTENTE AQUI)
        access_token = get_valid_access_token() 
        
        if not access_token:
            print("❌ Falha crítica: Não foi possível obter Access Token.")
            return
        
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        }

        enviados = 0
        with engine.begin() as conn: # Usamos begin() para garantir os updates
            for cliente in elegiveis:
                try:
                    # 3. Montar a URL do Fillout
                    params = {
                        "clienteId": cliente["cliente_id"],
                        "email": cliente["email"],
                        "nome": cliente["nome"],
                        "empresa": cliente["empresa"] or "",
                        "empresa_id": cliente["empresa_id"] or ""
                    }
                    query_string = urllib.parse.urlencode({k: v for k, v in params.items() if v})
                    survey_url = f"https://forms.fillout.com/t/dPJSvuBRcDus?{query_string}"
                    
                    # 4. Montar o HTML do E-mail
                    nome_exibicao = cliente["nome"] or "Parceiro"
                    empresa_exibicao = cliente["empresa"] or "sua empresa"
                    
                    mail_html = f"""
                    <!DOCTYPE html>
                    <html>
                    <body style="margin:0;padding:40px 15px;background-color:#F0F2F5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
                        <table width="600" align="center" cellpadding="0" cellspacing="0" style="background-color:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 4px 20px rgba(0,0,0,0.05);">
                            <tr>
                                <td><img src="https://images.fillout.com/orgid-605566/flowpublicid-dPJSvuBRcDus/widgetid-undefined/4XSnUZoTXsHHQrgj2vxtL4/1763399620234.jpg?a=8rcQiWHivWYgnLfV5ojCyf" width="600" style="display:block;width:100%;max-width:600px;height:auto;"></td>
                            </tr>
                            <tr>
                                <td style="padding:40px;color:#333333;line-height:1.6;">
                                    <h1 style="margin:0 0 20px 0;font-size:22px;color:#1A1A1A;text-align:center;font-weight:700;">Pesquisa de Satisfação</h1>
                                    <p style="font-size:16px;margin-bottom:20px;">Olá, <strong>{nome_exibicao}</strong>,</p>
                                    <p style="font-size:16px;margin-bottom:30px;color:#4A4A4A;">Para continuarmos elevando o nível da nossa parceria com a <strong>{empresa_exibicao}</strong>, precisamos ouvir você.</p>
                                    <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:30px;">
                                        <tr>
                                            <td align="center">
                                                <a href="{survey_url}" target="_blank" style="display:inline-block;padding:16px 36px;background-color:#F97316;color:#ffffff;font-size:16px;font-weight:bold;text-decoration:none;border-radius:8px;">Responder em 1 minuto</a>
                                            </td>
                                        </tr>
                                    </table>
                                    <div style="border-top:1px solid #EAEAEA;padding-top:25px;">
                                        <p style="margin:0;font-size:14px;color:#666666;">Um abraço,<br><strong style="color:#1A1A1A;">Equipe Gauge</strong> • Stefanini Group</p>
                                    </div>
                                </td>
                            </tr>
                        </table>
                    </body>
                    </html>
                    """

                    # 5. Disparar via Microsoft Graph API
                    payload = {
                        "message": {
                            "subject": f"[Pesquisa NPS] Sua opinião importa {'— ' + cliente['empresa'] if cliente['empresa'] else ''}",
                            "body": {"contentType": "HTML", "content": mail_html},
                            "toRecipients": [{"emailAddress": {"address": cliente["email"]}}]
                        },
                        "saveToSentItems": True
                    }

                    resposta_ms = requests.post(
                        "https://graph.microsoft.com/v1.0/me/sendMail",
                        headers=headers,
                        json=payload
                    )
                    
                    if resposta_ms.status_code in (200, 202):
                        # 6. Sucesso! Atualiza o banco (Soma 90 dias para o próximo envio)
                        sql_update = text("""
                            UPDATE dbo.nps_clientes
                            SET status_envio = 'Enviado',
                                ultimo_envio = CAST(GETDATE() AS DATE),
                                proximo_envio = DATEADD(DAY, 90, CAST(GETDATE() AS DATE)),
                                ultimo_erro = NULL,
                                updated_at = SYSUTCDATETIME()
                            WHERE cliente_id = :id
                        """)
                        conn.execute(sql_update, {"id": cliente["cliente_id"]})
                        enviados += 1
                    else:
                        # Falha ao enviar pela MS
                        raise Exception(f"Erro MS Graph: {resposta_ms.text}")

                except Exception as erro_cliente:
                    # 7. Regista o erro neste cliente específico, mas não para o loop!
                    sql_erro = text("""
                        UPDATE dbo.nps_clientes
                        SET status_envio = 'Erro', ultimo_erro = :erro, updated_at = SYSUTCDATETIME()
                        WHERE cliente_id = :id
                    """)
                    conn.execute(sql_erro, {"erro": str(erro_cliente)[:250], "id": cliente["cliente_id"]})
                    print(f"❌ Erro ao enviar para {cliente['email']}: {erro_cliente}")

        print(f"🏁 Rotina finalizada! {enviados} convites de NPS enviados com sucesso.")
        
    except Exception as e:
        print(f"❌ Erro Fatal na rotina de NPS: {e}")

def disparar_convite_nps_especifico(cliente_ids: list):
    """Busca clientes específicos e força o envio nativo do NPS pelo MS Graph"""
    if not cliente_ids:
        return
        
    print(f"🚀 Iniciando disparo forçado nativo para {len(cliente_ids)} cliente(s)...")
    from database import get_engine
    engine = get_engine()
    
    # Prepara a query de forma segura
    ids_formatados = ",".join([f"'{cid}'" for cid in cliente_ids])
    
    sql_busca = text(f"""
        SELECT c.cliente_id, c.email, c.nome, c.empresa, e.id AS empresa_id
        FROM dbo.nps_clientes c
        LEFT JOIN dbo.nps_empresas e ON c.empresa = e.nome
        WHERE c.cliente_id IN ({ids_formatados})
    """)
    
    try:
        with engine.connect() as conn:
            clientes = conn.execute(sql_busca).mappings().all()
            
        if not clientes:
            print("❌ Nenhum cliente encontrado.")
            return

        access_token = get_valid_access_token() 
        
        if not access_token:
            print("❌ Falha crítica: Não foi possível obter Access Token.")
            return
        
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        }

        with engine.begin() as conn:
            for cliente in clientes:
                try:
                    # 1. Carregar regras
                    regras = obter_regras_dinamicas()
                    
                    # 2. Construir URL do Fillout apenas com os campos permitidos
                    campos_permitidos = [c.strip().lower() for c in regras["fillout_campos"].split(",")]
                    
                    params_completos = {
                        "clienteId": cliente["cliente_id"],
                        "email": cliente["email"],
                        "nome": cliente["nome"],
                        "empresa": cliente["empresa"] or "",
                        "empresa_id": str(cliente["empresa_id"]) if cliente["empresa_id"] else ""
                        # Se adicionar gestor/segmento na query da tabela nps_clientes, pode mapear aqui também
                    }
                    
                    # Filtra o dicionário para conter apenas as chaves que o admin selecionou no frontend
                    params_finais = {k: v for k, v in params_completos.items() if k.lower() in campos_permitidos and v}
                    
                    query_string = urllib.parse.urlencode(params_finais)
                    survey_url = f"https://forms.fillout.com/t/dPJSvuBRcDus?{query_string}"
                    
                    # 3. Textos de exibição seguros
                    nome_exibicao = cliente["nome"].split(" ")[0] if cliente["nome"] else "Parceiro"
                    empresa_exibicao = cliente["empresa"] or "sua empresa"
                    
                    # 4. Template HTML Dinâmico (Se o admin tiver criado um, usa-o. Senão, usa o código padrão Gauge)
                    template_customizado = regras["email_template_html"]
                    
                    if template_customizado and "{survey_url}" in template_customizado:
                        # Substitui as variáveis dinâmicas no HTML do administrador
                        mail_html = template_customizado.replace("{nome}", nome_exibicao) \
                                                        .replace("{empresa}", empresa_exibicao) \
                                                        .replace("{survey_url}", survey_url)
                    else:
                        # O template fixo da Gauge que você já tinha caso o admin não insira nada
                        mail_html = f"""
                        <!DOCTYPE html>
                        <html>
                        <body style="margin:0;padding:40px 15px;background-color:#F0F2F5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
                            <table width="600" align="center" cellpadding="0" cellspacing="0" style="background-color:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 4px 20px rgba(0,0,0,0.05);">
                                <tr>
                                    <td style="padding:40px;color:#333333;line-height:1.6;">
                                        <h1 style="margin:0 0 20px 0;font-size:22px;color:#1A1A1A;text-align:center;font-weight:700;">Pesquisa de Satisfação</h1>
                                        <p style="font-size:16px;margin-bottom:20px;">Olá, <strong>{nome_exibicao}</strong>,</p>
                                        <p style="font-size:16px;margin-bottom:30px;color:#4A4A4A;">Para continuarmos elevando o nível da nossa parceria com a <strong>{empresa_exibicao}</strong>, precisamos ouvir você.</p>
                                        <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:30px;">
                                            <tr>
                                                <td align="center">
                                                    <a href="{survey_url}" target="_blank" style="display:inline-block;padding:16px 36px;background-color:#F97316;color:#ffffff;font-size:16px;font-weight:bold;text-decoration:none;border-radius:8px;">Responder em 1 minuto</a>
                                                </td>
                                            </tr>
                                        </table>
                                    </td>
                                </tr>
                            </table>
                        </body>
                        </html>
                        """

                    # 3. Disparar via MS Graph API
                    payload = {
                        "message": {
                            "subject": f"[Pesquisa NPS] Sua opinião importa {'— ' + cliente['empresa'] if cliente['empresa'] else ''}",
                            "body": {"contentType": "HTML", "content": mail_html},
                            "toRecipients": [{"emailAddress": {"address": cliente["email"]}}]
                        },
                        "saveToSentItems": True
                    }

                    resposta_ms = requests.post(
                        "https://graph.microsoft.com/v1.0/me/sendMail",
                        headers=headers,
                        json=payload
                    )
                    
                    if resposta_ms.status_code in (200, 202):
                        # 4. Atualizar o banco de dados
                        sql_update = text("""
                            UPDATE dbo.nps_clientes
                            SET status_envio = 'Enviado',
                                ultimo_envio = CAST(GETDATE() AS DATE),
                                proximo_envio = DATEADD(DAY, 90, CAST(GETDATE() AS DATE)),
                                ultimo_erro = NULL,
                                updated_at = SYSUTCDATETIME()
                            WHERE cliente_id = :id
                        """)
                        conn.execute(sql_update, {"id": cliente["cliente_id"]})
                        print(f"✅ Convite enviado com sucesso para {cliente['email']}")
                    else:
                        raise Exception(f"Erro na API da Microsoft: {resposta_ms.text}")

                except Exception as erro_cliente:
                    # Se falhar um cliente, guarda o erro no banco mas continua para o próximo
                    sql_erro = text("UPDATE dbo.nps_clientes SET status_envio = 'Erro', ultimo_erro = :erro, updated_at = SYSUTCDATETIME() WHERE cliente_id = :id")
                    conn.execute(sql_erro, {"erro": str(erro_cliente)[:250], "id": cliente["cliente_id"]})
                    print(f"❌ Erro ao enviar para {cliente['email']}: {erro_cliente}")

    except Exception as e:
        print(f"❌ Erro fatal no disparo manual: {e}")

def enviar_email_resposta(email_destino: str, nome: str, empresa: str, nota: int, categoria: str, motivo: str = "", expectativas: str = "", o_que_faltava: str = ""):
    if not email_destino or email_destino == "-":
        print("⚠️ E-mail de destino não fornecido. Agradecimento ignorado.")
        return

    print(f"📧 Preparando e-mail de agradecimento para {nome} ({categoria})...")
    
    # 1. Obter token de autorização da Microsoft
    access_token = get_valid_access_token()
    if not access_token:
        print("❌ Não foi possível obter o token para enviar o agradecimento.")
        return

    # Textos de exibição seguros
    primeiro_nome = nome.split(" ")[0] if nome else "Parceiro"
    empresa_exibicao = empresa if empresa else "sua empresa" 
    motivo_exibicao = motivo if motivo else "Nenhum comentário adicional deixado no formulário."
    expectativas_exibicao = expectativas if expectativas else "Não respondido."
    falta_exibicao = o_que_faltava if o_que_faltava else "Não respondido."

    # 1. Carregar os Templates do Banco
    regras = obter_regras_dinamicas()
    
    # 2. Escolher o template e assunto com base na Categoria
    if categoria == 'Promotor':
        assunto = f"Obrigado pela sua nota {nota}! 🌟"
        template_customizado = regras.get("email_agradecimento_promotor", "")
    elif categoria == 'Neutro':
        assunto = "Recebemos a sua avaliação. Vamos melhorar! 🚀"
        template_customizado = regras.get("email_agradecimento_neutro", "")
    else:
        assunto = "O seu feedback é muito importante para nós 💡"
        template_customizado = regras.get("email_agradecimento_detrator", "")

    # 3. Injetar Variáveis no Template
    if template_customizado:
        mail_html = template_customizado.replace("{nome}", primeiro_nome) \
                                        .replace("{empresa}", empresa_exibicao) \
                                        .replace("{nota}", str(nota)
                                        .replace("{motivo}", motivo_exibicao)
                                        .replace("{expectativas}", expectativas_exibicao)
                                        .replace("{o_que_faltava}", falta_exibicao))
    else:
        # Fallback de emergência caso o Admin deixe as caixas em branco
        mail_html = f"<h2>Obrigado, {primeiro_nome}!</h2><p>A sua nota {nota} foi registada para a empresa {empresa_exibicao}.</p>"

    # 4. Disparo via MS Graph API
    payload = {
        "message": {
            "subject": assunto,
            "body": {"contentType": "HTML", "content": mail_html},
            "toRecipients": [{"emailAddress": {"address": email_destino}}]
        },
        "saveToSentItems": True
    }

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }

    try:
        import requests
        resposta_ms = requests.post("https://graph.microsoft.com/v1.0/me/sendMail", headers=headers, json=payload)
        
        if resposta_ms.status_code in (200, 202):
            print(f"✅ E-mail de agradecimento enviado com sucesso para {email_destino}")
        else:
            print(f"❌ Falha ao enviar agradecimento: {resposta_ms.text}")
    except Exception as e:
        print(f"❌ Erro crítico ao enviar agradecimento: {e}")