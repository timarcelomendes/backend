import traceback
from services.respostas_svc import processar_webhook_fillout
from services.teams_svc import enviar_alerta_tecnico_teams

def processar_webhook_background(evento: dict):
    """
    Processa o evento completo do webhook em segundo plano.
    """
    try:
        print("⏳ [Webhook SVC] A iniciar processamento em background...")
        
        # 1. Extrai o payload real de dentro do "evento" que o main.py montou
        payload_real = evento.get("payload")
        
        if not payload_real:
            print("⚠️ [Webhook SVC] O webhook chegou sem payload (corpo vazio). Ignorando.")
            return
            
        # 2. Executa a regra de negócio pesada (Inserção no banco, etc.)
        # Passamos apenas o payload_real para a função que já estava feita
        processar_webhook_fillout(payload_real)
        
        print("✅ [Webhook SVC] Sucesso: Webhook processado e salvo no banco.")
        
    except Exception as e:
        err_msg = str(e)
        err_trace = traceback.format_exc()
        
        print(f"❌ [Webhook SVC] ERRO GRAVE: {err_msg}")
        print(err_trace)
        
        alerta = f"**Falha no Processamento do Webhook**\n\n**Erro:** {err_msg}\n\nVerifique os logs da Azure para ver o Traceback."
        enviar_alerta_tecnico_teams(alerta)