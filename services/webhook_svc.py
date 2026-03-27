import traceback
from services.respostas_svc import processar_webhook_fillout
from services.teams_svc import enviar_alerta_tecnico_teams

def processar_webhook_background(payload: dict):
    """
    Processa os dados do Fillout em segundo plano.
    """
    try:
        print("⏳ [Webhook] A processar dados do Fillout...")
        
        # Chama a função que insere no banco
        processar_webhook_fillout(payload)
        
        print("✅ [Webhook] Gravado no banco com sucesso.")
        
    except Exception as e:
        err_msg = str(e)
        print(f"❌ [Webhook] ERRO GRAVE: {err_msg}\n{traceback.format_exc()}")
        
        # Alerta a equipa técnica
        enviar_alerta_tecnico_teams(f"**Falha no Webhook (Fillout)**\n\nErro: {err_msg}")