import requests
from sqlalchemy import text
import urllib.parse
from database import get_engine
from datetime import datetime

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

def enviar_email_recuperacao(email_destino, link_recuperacao):
    """Fluxo principal de disparo usando as configs do banco."""
    config = obter_configuracoes_email()
    
    if not config or not config['refresh_token']:
        print("⚠️ E-mail não configurado ou não autorizado na aba Configurações.")
        return False

    # 1. Obtém token novo
    access_token = gerar_access_token(config)
    
    if not access_token:
        print("❌ Falha crítica: Não foi possível obter Access Token.")
        return False

    # 2. Envia via Microsoft Graph 
    # Usar /me/sendMail é mais seguro do que passar o e-mail na URL
    url_send = "https://graph.microsoft.com/v1.0/me/sendMail"
    
    payload = {
        "message": {
            "subject": "Recuperação de Senha - NPS Intelligence",
            "body": {
                "contentType": "HTML",
                "content": f"""
                <div style="font-family: sans-serif; color: #334155; max-width: 500px; padding: 20px; border: 1px solid #e2e8f0; border-radius: 20px;">
                    <h2 style="color: #f97316;">Olá!</h2>
                    <p>Recebemos uma solicitação para redefinir a sua senha no <b>NPS Intelligence</b>.</p>
                    <p>Clique no botão abaixo para prosseguir. Este link é válido por 30 minutos.</p>
                    <div style="margin: 30px 0; text-align: center;">
                        <a href="{link_recuperacao}" 
                           style="background-color: #1e293b; color: white; padding: 14px 30px; text-decoration: none; border-radius: 12px; font-weight: bold; display: inline-block; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1);">
                           Redefinir Minha Senha
                        </a>
                    </div>
                    <p style="font-size: 11px; color: #94a3b8; line-height: 1.5;">
                        Se não solicitou esta alteração, pode ignorar este e-mail em segurança. <br>
                        Este é um e-mail automático, por favor não responda.
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
        response = requests.post(url_send, json=payload, headers=headers)
        if response.status_code == 202:
            print(f"✅ E-mail enviado para {email_destino}")
            return True
        else:
            # 🟢 ISTO É O QUE VAI SALVAR O SEU DIAGNÓSTICO:
            erro_json = response.json() if response.text else "Sem corpo de resposta"
            print(f"❌ Erro Graph API ({response.status_code}): {erro_json}")
            return False
    except Exception as e:
        print(f"❌ Falha no disparo: {e}")
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
        # access_token = obter_token_microsoft() # <- Substitua pela sua função real
        # Para este exemplo, assumo que você tem uma função que retorna o token válido:
        from services.email_svc import get_valid_access_token 
        access_token = get_valid_access_token()
        
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