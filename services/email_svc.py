import requests
from sqlalchemy import text


def obter_configuracoes_email():
    """Procura as credenciais ativas na base de dados."""
    from main import get_engine # Import local para evitar import circular
    engine = get_engine()
    with engine.connect() as conn:
        query = text("""
            SELECT TOP 1 tenant_id, client_id, client_secret, refresh_token, email_remetente 
            FROM dbo.nps_configuracoes_email
        """)
        return conn.execute(query).fetchone()

def gerar_access_token(config):
    """Troca o refresh_token por um access_token válido."""
    url = f"https://login.microsoftonline.com/{config.tenant_id}/oauth2/v2.0/token"
    payload = {
        'client_id': config.client_id,
        'client_secret': config.client_secret,
        'refresh_token': config.refresh_token,
        'grant_type': 'refresh_token',
        'scope': 'mail.send'
    }
    res = requests.post(url, data=payload).json()
    return res.get('access_token')

def enviar_email_recuperacao(email_destino, link_recuperacao):
    """Fluxo principal de disparo usando as configs do banco."""
    config = obter_configuracoes_email()
    
    if not config or not config.refresh_token:
        print("Erro: E-mail não configurado ou não autorizado na aba Configurações.")
        return False

    # 1. Obtém token novo (válido por 1 hora)
    access_token = gerar_access_token(config)
    
    if not access_token:
        print("Erro: Não foi possível renovar o token da Microsoft.")
        return False

    # 2. Envia via Microsoft Graph
    url_send = f"https://graph.microsoft.com/v1.0/users/{config.email_remetente}/sendMail"
    
    payload = {
        "message": {
            "subject": "Recuperação de Senha - NPS Intelligence",
            "body": {
                "contentType": "HTML",
                "content": f"""
                <div style="font-family: sans-serif; color: #334155; max-width: 500px;">
                    <h2 style="color: #f97316;">Olá!</h2>
                    <p>Recebemos uma solicitação para redefinir a sua senha no <b>NPS Intelligence</b>.</p>
                    <p>Clique no botão abaixo para prosseguir. Este link é válido por 30 minutos.</p>
                    <div style="margin: 30px 0;">
                        <a href="{link_recuperacao}" 
                           style="background-color: #f97316; color: white; padding: 12px 25px; text-decoration: none; border-radius: 10px; font-weight: bold; display: inline-block;">
                           Redefinir Minha Senha
                        </a>
                    </div>
                    <p style="font-size: 12px; color: #64748b;">
                        Se não solicitou esta alteração, ignore este e-mail.
                    </p>
                </div>
                """
            },
            "toRecipients": [{"emailAddress": {"address": email_destino}}]
        }
    }

    headers = {'Authorization': f'Bearer {access_token}', 'Content-Type': 'application/json'}
    response = requests.post(url_send, json=payload, headers=headers)
    
    return response.status_code == 202