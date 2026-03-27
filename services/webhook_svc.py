import traceback

# Importa a lógica pesada de processamento
from services.respostas_svc import processar_webhook_fillout

# Importa a nossa nova função de alerta crítico
from services.teams_svc import enviar_alerta_tecnico_teams

def processar_webhook_background(payload: dict):
    """
    Processa o payload do webhook em segundo plano.
    """
    try:
        print("⏳ [Webhook SVC] A iniciar processamento em background do Fillout...")
        
        # Executa a regra de negócio pesada
        processar_webhook_fillout(payload)
        
        print("✅ [Webhook SVC] Sucesso: Webhook processado e salvo no banco.")
        
    except Exception as e:
        err_msg = str(e)
        err_trace = traceback.format_exc()
        
        print(f"❌ [Webhook SVC] ERRO GRAVE NO BACKGROUND DO WEBHOOK: {err_msg}")
        print(err_trace)
        
        # Formata a mensagem de erro para o Teams
        alerta = f"**Falha no Processamento do Webhook (Fillout)**\n\n**Erro:** {err_msg}\n\nVerifique os logs da Azure (Application Insights) para ver o Traceback completo."
        
        # Dispara o alerta para a equipa técnica
        enviar_alerta_tecnico_teams(alerta)