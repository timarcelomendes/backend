import os
import io
import json
import traceback
import re
import secrets
import string
import random
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Any
from contextlib import asynccontextmanager
import shutil
from fastapi.staticfiles import StaticFiles

import pandas as pd
import bcrypt
import requests
import openai
from fastapi import FastAPI, HTTPException, File, UploadFile, Query, BackgroundTasks, Body, Depends, status, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel, EmailStr
from passlib.context import CryptContext
from jose import jwt, JWTError, ExpiredSignatureError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

# Importações do Agendador (Scheduler)
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

# Importações Locais
from database import get_engine, exec_sql
from services.auth_utils import hash_password
from services.email_svc import enviar_email_recuperacao, processar_disparos_nps
from services import clientes_svc, respostas_svc, dashboard_svc, importacao_svc
from services.teams_svc import enviar_resumo_matinal_gestores, enviar_alerta_tecnico_teams 
from services.webhook_svc import processar_webhook_background

# ==========================================
# ⚙️ 1. CONFIGURAÇÕES E SEGURANÇA
# ==========================================
SECRET_KEY = os.getenv("JWT_SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError("ERRO CRÍTICO: JWT_SECRET_KEY não configurada nas variáveis de ambiente.")

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 2
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/login")

# ==========================================
# ⏰ 2. LIFESPAN E SCHEDULERS
# ==========================================
# 👈 1. O scheduler passa a ser GLOBAL (coloque fora/antes da função lifespan)
scheduler = BackgroundScheduler()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 2. Ao iniciar o servidor, vai buscar o horário guardado no banco
    hora_teams, minuto_teams = 8, 0 # Padrão
    try:
        engine = get_engine()
        with engine.connect() as conn:
            res = conn.execute(text("SELECT valor FROM dbo.nps_configuracoes WHERE chave = 'teams_horario_resumo'")).scalar()
            if res and ":" in res:
                hora_teams, minuto_teams = map(int, res.split(":"))
    except Exception as e:
        print(f"⚠️ Aviso ao ler horário do Teams (usando padrão 08:00): {e}")

    # Job 1: Robô de Disparo de NPS a cada 6 horas
    scheduler.add_job(
        processar_disparos_nps, 
        IntervalTrigger(hours=6), 
        id="disparo_nps_job", 
        replace_existing=True
    )
    
    # Job 2: Robô de Alertas do Teams (Agora usa as variáveis dinâmicas)
    scheduler.add_job(
        enviar_resumo_matinal_gestores, 
        CronTrigger(day_of_week='mon-fri', hour=hora_teams, minute=minuto_teams), 
        id="alerta_matinal_teams_job", # 👈 ID correto que você já usava
        replace_existing=True
    )
    
    scheduler.start()
    print("⏰ Agendador de tarefas (CRON) iniciado com sucesso! (NPS e Teams)")
    yield
    scheduler.shutdown()

# ==========================================
# 🚀 3. INICIALIZAÇÃO DO APP E MIDDLEWARES
# ==========================================
app = FastAPI(
    title="NPS API - Gauge Stefanini",
    description="API centralizada para gestão de NPS, Clientes e Respostas",
    version="1.0.0",
    lifespan=lifespan
)

origins = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "https://blue-sand-0bbaa2010.6.azurestaticapps.net"
]

front_url_azure = os.getenv("FRONTEND_URL")
if front_url_azure:
    origins.append(front_url_azure.rstrip("/"))

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_origin_regex=".*",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==========================================
# 🥇 1. WEBHOOKS (PADRÃO OFICIAL)
# ==========================================

@app.post("/api/webhook/fillout")
async def receber_webhook_fillout(request: Request, background_tasks: BackgroundTasks):
    """Rota POST nativa e simples para receber o Fillout"""
    try:
        payload = await request.json()
        from services.webhook_svc import processar_webhook_background
        background_tasks.add_task(processar_webhook_background, payload)
        return {"status": "success", "message": "Recebido"}
        
    except Exception as e:
        enviar_alerta_tecnico_teams(f"Falha de Recepção no Webhook (Fillout): {str(e)}")
        print(f"❌ Erro ao receber webhook: {e}")
        return {"status": "error", "message": "Falha na leitura"}


@app.get("/api/webhook/fillout")
async def status_webhook_fillout():
    """Healthcheck simples para o botão do Frontend"""
    return {"status": "success", "message": "🟢 Ouvindo POSTs no novo endereço!"}

# ==========================================
# 🔐 4. DEPENDÊNCIAS DE AUTENTICAÇÃO
# ==========================================
async def get_current_user(token: str = Depends(oauth2_scheme)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Sessão expirada. Por favor, faça login novamente.",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM], options={"verify_exp": True})
        email: str = payload.get("sub")
        if email is None:
            raise credentials_exception
        return email
    except (ExpiredSignatureError, JWTError):
        raise credentials_exception

async def get_current_user_token_data(token: str = Depends(oauth2_scheme)):
    """Descodifica o token e devolve os dados (incluindo o tipo)"""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except JWTError:
        raise HTTPException(status_code=401, detail="Sessão inválida.")

def exigir_admin(token_data: dict = Depends(get_current_user_token_data)):
    if token_data.get("tipo") != "Admin":
        raise HTTPException(status_code=403, detail="Acesso negado. Apenas Administradores.")
    return token_data.get("sub")

def exigir_manager(token_data: dict = Depends(get_current_user_token_data)):
    if token_data.get("tipo") not in ["Admin", "Manager"]:
        raise HTTPException(status_code=403, detail="Acesso negado. Requer nível Manager ou superior.")
    return token_data.get("sub")

# ==========================================
# 📦 5. SCHEMAS (Pydantic Models)
# Validam os dados que chegam do Frontend
# ==========================================

class AcaoCriar(BaseModel):
    resposta_id: str
    empresa_id: Optional[int] = 0
    gestor_id: Optional[int] = None
    titulo: str
    descricao: Optional[str] = ""
    prioridade: Optional[str] = "Alta"
    prazo_limite: Optional[str] = None

class AcaoAtualizar(BaseModel):
    status: Optional[str] = None
    prioridade: Optional[str] = None
    descricao: Optional[str] = None
    prazo_limite: Optional[str] = None
    gestor_id: Optional[int] = None
    empresa_id: Optional[int] = None

class BasicoSchema(BaseModel):
    nome: str

class RespostaUpdate(BaseModel):
    nota: int
    categoria: str
    motivo: Optional[str] = ""
    canal: Optional[str] = ""
    expectativas: Optional[str] = ""
    o_que_faltava: Optional[str] = ""

class ConfigEmailRequest(BaseModel):
    tenant_id: str
    client_id: str
    client_secret: str
    email_remetente: EmailStr

class AutorizarEmailRequest(BaseModel):
    code: str

class ConfigEmailSchema(BaseModel):
    tenant_id: str
    client_id: str
    client_secret: str
    email_remetente: EmailStr
    base_url_frontend: Optional[str] = "http://localhost:5173"
    envios_ativos: Optional[bool] = True


class RegistroRequest(BaseModel):
    nome: str
    email: str
    password: str

class ResetPasswordRequest(BaseModel):
    token: str
    nova_senha: str

class LoginRequest(BaseModel):
    email: str
    password: str
    remember: bool = False

class EsqueciSenhaRequest(BaseModel):
    email: str

class AlterarSenhaRequest(BaseModel):
    senha_atual: str
    nova_senha: str

class UsuarioCreate(BaseModel):
    nome: str
    email: str
    password: str
    cargo: str = "Analista"

class LoteEnvio(BaseModel):
    cliente_ids: List[str]

class SettingUpdate(BaseModel):
    valor: bool

class EmpresaSchema(BaseModel):
    nome: str
    segmento: Optional[str] = None
    valor_contrato: Optional[float] = 0.0
    gestor: Optional[str] = None
    gestor_id: Optional[int] = None 
    companhia_id: Optional[int] = None 

class ClienteCreate(BaseModel):
    nome: str
    email: str
    telefone: Optional[str] = ""
    empresa: Optional[str] = ""
    perfil_decisor: Optional[str] = ""
    segmento: Optional[str] = ""
    cargo: str
    gestor: Optional[str] = ""

class ClienteUpdate(BaseModel):
    nome: str
    email: str
    telefone: Optional[str] = ""
    empresa: Optional[str] = ""
    perfil_decisor: Optional[str] = ""
    segmento: Optional[str] = ""
    cargo: str
    gestor: Optional[str] = ""
    ativo: Optional[bool] = True

class StatusUpdate(BaseModel):
    ativo: bool

class ConfigItem(BaseModel):
    chave: str
    valor: str

class GestorSchema(BaseModel):
    nome: str
    papel: Optional[str] = ""
    email: Optional[str] = ""
    teams_webhook: Optional[str] = ""
    avatar: Optional[str] = None

class AlertaGestorRequest(BaseModel):
    empresa: str
    gestor: str
    nps: int

class ReportEmailPayload(BaseModel):
    emails: List[str]
    periodo: str
    resumo_ia: str
    foco: str
    prioridade: str

class EmpresaPayload(BaseModel):
    nome: str
    segmento: Optional[str] = None
    valor_contrato: Optional[float] = 0.0
    gestor: Optional[str] = None
    gestor_id: Optional[int] = None

class IntegracoesUpdate(BaseModel):
    webhook_global: Optional[str] = None
    webhook_tecnico: Optional[str] = None

class RegrasNegocioConfig(BaseModel):
    scheduler_hora_inicio: str = "09:00"
    scheduler_horas: int = 6
    sla_detrator_dias: int = 2
    sla_neutro_dias: int = 5
    sla_promotor_dias: int = 7
    recorrencia_dias: int = 90
    fillout_campos: str = "clienteid,email,nome,empresa,empresa_id"
    email_template_html: Optional[str] = ""
    email_agradecimento_promotor: Optional[str] = ""
    email_agradecimento_neutro: Optional[str] = ""
    email_agradecimento_detrator: Optional[str] = ""
    email_template_lembrete_1: Optional[str] = ""
    email_template_lembrete_2: Optional[str] = ""
    email_template_lembrete_3: Optional[str] = ""
    teams_horario_resumo: str = "08:00"
    lembrete_qtd_maxima: int = 3
    lembrete_dias_1: int = 3
    lembrete_dias_2: int = 7
    lembrete_dias_3: int = 15

class TesteTemplatePayload(BaseModel):
    email_destino: str
    html_content: str
    categoria: str # 'promotor', 'neutro', 'detrator'

class TesteWebhookPayload(BaseModel):
    webhook_url: str

class PermissaoUpdate(BaseModel):
    perfil: str
    chaves: List[str]

class RespostaManual(BaseModel):
    cliente_id: str
    nota: int
    motivo: Optional[str] = ""
    canal: str = "Manual"
    
# ==========================================
# 🔗 ROTAS DE INTEGRAÇÕES (TEAMS / FILLOUT)
# ==========================================

@app.get("/api/configuracoes/integracoes")
def get_integracoes(usuario_email: str = Depends(get_current_user)):
    """Busca as configurações atuais de integração (Protegido)"""
    try:
        from database import get_engine
        from sqlalchemy import text
        
        engine = get_engine()
        with engine.connect() as conn:
            query = text("SELECT chave, valor FROM dbo.nps_configuracoes WHERE chave IN ('teams_webhook_url', 'teams_alerts_webhook')")
            rows = conn.execute(query).fetchall()
            
            config = {row.chave: row.valor for row in rows}
            
            return {
                "webhook_global": config.get("teams_webhook_url", ""),
                "webhook_tecnico": config.get("teams_alerts_webhook", "")
            }
    except Exception as e:
        import traceback
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail="Erro ao carregar integrações")

@app.put("/api/configuracoes/integracoes")
def update_integracoes(config: IntegracoesUpdate, usuario_email: str = Depends(get_current_user)):
    """Atualiza ou cria as chaves de integração no banco (Protegido)"""
    try:
        from database import get_engine
        from sqlalchemy import text
        
        engine = get_engine()
        with engine.begin() as conn:
            # Lógica de Upsert otimizada (incluindo o updated_at da sua rota antiga)
            sql_upsert = text("""
                IF EXISTS (SELECT 1 FROM dbo.nps_configuracoes WHERE chave = :chave)
                    UPDATE dbo.nps_configuracoes 
                    SET valor = :valor, updated_at = CURRENT_TIMESTAMP 
                    WHERE chave = :chave
                ELSE
                    INSERT INTO dbo.nps_configuracoes (chave, valor, updated_at) 
                    VALUES (:chave, :valor, CURRENT_TIMESTAMP)
            """)
            
            # Grava o Webhook Global
            if config.webhook_global is not None:
                conn.execute(sql_upsert, {"chave": "teams_webhook_url", "valor": config.webhook_global})
            
            # Grava o Webhook Técnico
            if config.webhook_tecnico is not None:
                conn.execute(sql_upsert, {"chave": "teams_alerts_webhook", "valor": config.webhook_tecnico})
                
        return {"status": "success", "message": "Integrações atualizadas com sucesso!"}
    except Exception as e:
        import traceback
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail="Erro ao gravar integrações")
    
@app.get("/api/config/regras")
def obter_regras(usuario_email: str = Depends(get_current_user)):
    try:
        engine = get_engine()
        with engine.connect() as conn:
            sql = text("SELECT chave, valor FROM dbo.nps_configuracoes")
            result = conn.execute(sql).fetchall()
            
            configuracoes = {linha[0]: linha[1] for linha in result}
            
            if not configuracoes:
                return {"recorrencia_dias": 90}
                
            return configuracoes
            
    except Exception as e:
        print(f"Erro ao carregar regras: {e}")
        raise HTTPException(status_code=500, detail="Erro ao carregar configurações.")

@app.post("/api/config/regras")
def salvar_regras(payload: RegrasNegocioConfig, usuario_email: str = Depends(get_current_user)):
    try:
        engine = get_engine()
        with engine.begin() as conn:
            
            configuracoes = payload.dict()
            
            sql = text("""
                UPDATE dbo.nps_configuracoes 
                SET valor = :valor 
                WHERE chave = :chave
            """)
            
            for chave, valor in configuracoes.items():

                valor_string = str(valor) if valor is not None else ""
                
                conn.execute(sql, {"chave": chave, "valor": valor_string})
            
        return {"message": "Regras de negócio guardadas com sucesso!"}
    except Exception as e:
        print(f"Erro ao salvar regras chave-valor: {e}")
        raise HTTPException(status_code=500, detail=f"Erro interno: {str(e)}")
    
@app.post("/api/config/testar-template")
def testar_template_html(payload: TesteTemplatePayload, usuario_email: str = Depends(get_current_user)):
    """Recebe um HTML do frontend e envia um e-mail de teste instantâneo"""
    try:
        from services.email_svc import get_valid_access_token
        import requests

        access_token = get_valid_access_token()
        if not access_token:
            raise HTTPException(status_code=400, detail="A conexão com o e-mail não está ativa. Autorize o Microsoft Graph primeiro.")

        if not payload.html_content:
            raise HTTPException(status_code=400, detail="A caixa de texto do HTML está vazia.")

        if payload.categoria == 'convite':
            assunto_teste = "[Gauge Teste] Preview do Convite NPS"
            html_pronto = payload.html_content.replace("{nome}", "Maria (Teste)") \
                                              .replace("{empresa}", "Empresa Fictícia S/A") \
                                              .replace("{survey_url}", "https://forms.fillout.com/t/preview123456789")
        else:
            assunto_teste = f"[Gauge Teste] Preview do Layout — {payload.categoria.capitalize()}"
            nota_teste = "10" if payload.categoria == 'promotor' else "7" if payload.categoria == 'neutro' else "3"
            motivo_teste = "A equipa foi fantástica, mas acho que o portal poderia ser mais intuitivo."
            exp_teste = "Sim, o atendimento atendeu às expectativas."
            falta_teste = "Faltou apenas um manual de utilizador mais detalhado."
            
            html_pronto = payload.html_content.replace("{nome}", "João (Teste)") \
                                              .replace("{empresa}", "Empresa Fictícia S/A") \
                                              .replace("{nota}", nota_teste) \
                                              .replace("{motivo}", motivo_teste) \
                                              .replace("{expectativas}", exp_teste) \
                                              .replace("{o_que_faltava}", falta_teste)

        msg_payload = {
            "message": {
                "subject": assunto_teste, 
                "body": {"contentType": "HTML", "content": html_pronto},
                "toRecipients": [{"emailAddress": {"address": payload.email_destino}}]
            },
            "saveToSentItems": False
        }

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        }

        res = requests.post("https://graph.microsoft.com/v1.0/me/sendMail", headers=headers, json=msg_payload)
        if res.status_code not in (200, 202):
            raise Exception(res.text)

        return {"status": "success", "message": "E-mail de teste despachado!"}

    except Exception as e:
        print(f"❌ Erro ao enviar e-mail de teste: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    
# ==========================================
# 🤖 AUTENTICACAO (Login, Registros)
# ==========================================
@app.post("/api/login")
async def login(requisicao: LoginRequest, request: Request):
    try:
        engine = get_engine()
        with engine.connect() as conn:
            query = text("""
                SELECT usuario_id, nome, email, senha_hash, cargo, tipo, ativo 
                FROM dbo.nps_usuarios 
                WHERE email = :email
            """)
            resultado = conn.execute(query, {"email": requisicao.email}).mappings().first()

            if not resultado:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED, 
                    detail="Este e-mail não está registado na plataforma."
                )

            ativo_val = str(resultado["ativo"]).strip().lower()
            if ativo_val not in ['1', 'true']:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN, 
                    detail="A sua conta está inativa ou aguarda aprovação do administrador."
                )

            try:
                senha_correta = bcrypt.checkpw(
                    requisicao.password.encode('utf-8'), 
                    resultado["senha_hash"].encode('utf-8')
                )
            except Exception as e:
                # 🚨 ALERTA TI: Falha no sistema de encriptação
                enviar_alerta_tecnico_teams(f"Erro Crítico no Bcrypt durante o Login: {str(e)}")
                print(f"Erro Bcrypt: {e}")
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, 
                    detail="Erro na encriptação. Contacte o suporte técnico."
                )

            if not senha_correta:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED, 
                    detail="A palavra-passe digitada está incorreta."
                )

            user_agent = request.headers.get("user-agent", "Dispositivo Desconhecido")
            ip_address = request.client.host if request.client else "IP Desconhecido"
            
            tipo_disp = "Desktop/Browser"
            if any(x in user_agent for x in ["Mobile", "iPhone", "Android"]):
                tipo_disp = "Mobile"
            elif "Mac OS" in user_agent:
                tipo_disp = "Mac/Apple"
            elif "Windows" in user_agent:
                tipo_disp = "Windows/PC"
                
            dispositivo_amigavel = f"{tipo_disp} • {user_agent[:30]}..."

            check_sessao = conn.execute(text("""
                SELECT id FROM dbo.nps_sessoes_ativas 
                WHERE usuario_id = :uid AND ip_address = :ip AND dispositivo = :disp AND revogado = 0
            """), {
                "uid": resultado["usuario_id"],
                "ip": ip_address,
                "disp": dispositivo_amigavel
            }).fetchone()

            agora_utc = datetime.now(timezone.utc)

            if check_sessao:
                conn.execute(text("""
                    UPDATE dbo.nps_sessoes_ativas 
                    SET criado_em = :agora 
                    WHERE id = :sid
                """), {"agora": agora_utc, "sid": check_sessao.id})
            else:
                conn.execute(text("""
                    INSERT INTO dbo.nps_sessoes_ativas (usuario_id, dispositivo, ip_address, localizacao, criado_em, revogado)
                    VALUES (:uid, :disp, :ip, 'Detetado Automaticamente', :agora, 0)
                """), {
                    "uid": resultado["usuario_id"],
                    "disp": dispositivo_amigavel,
                    "ip": ip_address,
                    "agora": agora_utc
                })
            
            # 6. Atualiza último acesso do utilizador
            conn.execute(text("""
                UPDATE dbo.nps_usuarios 
                SET ultimo_acesso = :agora
                WHERE usuario_id = :uid
            """), {
                "agora": agora_utc,
                "uid": resultado["usuario_id"]
            })
            
            resultado_tempo = conn.execute(text(
                "SELECT valor FROM dbo.nps_configuracoes WHERE chave = 'sessao_expiracao_minutos'"
            )).scalar()
            
            tempo_minutos = int(resultado_tempo) if resultado_tempo and str(resultado_tempo).isdigit() else 60

            conn.commit() 

            expires_delta = timedelta(days=30) if requisicao.remember else timedelta(minutes=tempo_minutos)

            expire = datetime.utcnow() + timedelta(hours=8)
            to_encode = {
                "sub": resultado["email"],
                "exp": expire,
                "tipo": resultado["tipo"]
            }
            access_token = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

            # 7. Buscar as permissões dinâmicas do banco de dados
            sql_perm = text("SELECT chave FROM dbo.nps_permissoes WHERE perfil = :perfil")
            res_perm = conn.execute(sql_perm, {"perfil": resultado["tipo"]}).fetchall()
            
            lista_permissoes = [row.chave for row in res_perm]

            # 8. Devolver os dados + permissões para o Frontend
            return {
                "access_token": access_token,
                "token_type": "bearer",
                "nome": resultado["nome"],
                "cargo": resultado["cargo"], # 👈 ADICIONE ESTA LINHA
                "tipo": resultado["tipo"],
                "permissoes": lista_permissoes
            }

    except HTTPException:
        # Repassa os erros de senha errada, conta inativa, etc, sem alertar o Teams (pois é culpa do utilizador)
        raise
    except Exception as e:
        # 🚨 ALERTA TI: O banco de dados caiu, a rede falhou, etc.
        enviar_alerta_tecnico_teams(f"Falha Crítica no Login (Banco Offline?): {str(e)}")
        print(traceback.format_exc())
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, 
            detail="Erro interno no servidor. A equipa técnica já foi notificada."
        )
    
@app.post("/api/register")
def registrar_usuario(requisicao: RegistroRequest):
    engine = get_engine()
    
    with engine.begin() as conn:
        query_check = text("SELECT usuario_id FROM dbo.nps_usuarios WHERE email = :email")
        if conn.execute(query_check, {"email": requisicao.email}).fetchone():
            raise HTTPException(status_code=400, detail="Este e-mail já possui uma conta associada.")
        
        senha_hash = bcrypt.hashpw(requisicao.password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
        
        query_insert = text("""
            INSERT INTO dbo.nps_usuarios (nome, email, senha_hash, cargo, ativo, tipo)
            VALUES (:nome, :email, :senha_hash, 'Analista', 0, 'Usuário')
        """)
        
        conn.execute(query_insert, {
            "nome": requisicao.nome,
            "email": requisicao.email,
            "senha_hash": senha_hash
        })
        
    return {"mensagem": "Conta criada com sucesso e aguarda aprovação do administrador!"}


@app.post("/api/reset-password")
async def resetar_senha(req: ResetPasswordRequest, background_tasks: BackgroundTasks):
    from database import get_engine 
    engine = get_engine()
    
    try:
        try:
            payload = jwt.decode(req.token, SECRET_KEY, algorithms=[ALGORITHM])
            email_usuario = payload.get("sub")
            tipo_token = payload.get("tipo")
            
            if email_usuario is None or tipo_token != "reset":
                raise HTTPException(status_code=400, detail="Token inválido.")
        except JWTError:
            raise HTTPException(status_code=400, detail="O link de recuperação expirou ou é inválido.")

        senha_encriptada = pwd_context.hash(req.nova_senha)
        
        with engine.begin() as conn:
            query_update = text("""
                UPDATE dbo.nps_usuarios 
                SET senha_hash = :senha_hash
                WHERE email = :email
            """)
            resultado = conn.execute(query_update, {
                "senha_hash": senha_encriptada, 
                "email": email_usuario
            })
            
            if resultado.rowcount == 0:
                raise HTTPException(status_code=404, detail="Utilizador não encontrado.")
            
        # 🚀 DISPARA O E-MAIL DE CONFIRMAÇÃO EM SEGUNDO PLANO
        from services.email_svc import enviar_email_senha_alterada
        background_tasks.add_task(enviar_email_senha_alterada, email_usuario)
            
        return {"status": "success", "message": "Palavra-passe alterada com sucesso!"}
            
    except HTTPException:
        raise
    except Exception as e:
        # 🚨 ALERTA TI: Falha ao escrever a nova senha na base de dados
        enviar_alerta_tecnico_teams(f"Falha ao atualizar a Hash de Palavra-passe no BD: {str(e)}")
        print(f"❌ Erro ao redefinir a palavra-passe no banco: {e}")
        raise HTTPException(status_code=500, detail="Erro interno ao guardar a nova palavra-passe.")

@app.post("/api/esqueci-senha")
async def solicitar_recuperacao(requisicao: EsqueciSenhaRequest, background_tasks: BackgroundTasks):
    engine = get_engine()
    
    # 1. BLINDAGEM PYTHON: Remove espaços no início/fim e força tudo para minúsculas
    email_limpo = requisicao.email.strip().lower()
    
    try:
        with engine.connect() as conn:
            # 2. BLINDAGEM SQL: LTRIM e RTRIM removem espaços no banco, LOWER iguala as letras
            query = text("""
                SELECT email 
                FROM dbo.nps_usuarios 
                WHERE LOWER(LTRIM(RTRIM(email))) = :email
            """)
            
            # Passamos o email_limpo para a query
            resultado = conn.execute(query, {"email": email_limpo}).mappings().first()
            
            if not resultado:
                print(f"ℹ️ Recuperação solicitada para e-mail inexistente: '{email_limpo}'")
                return {"mensagem": "Se o e-mail existir no nosso sistema, receberá um link de recuperação em breve."}

            # Usamos o e-mail exato devolvido pelo banco para garantir consistência
            email_banco = resultado['email']

            expira = datetime.utcnow() + timedelta(minutes=30)
            token = jwt.encode(
                {"sub": email_banco, "exp": expira, "tipo": "reset"}, 
                SECRET_KEY, 
                algorithm=ALGORITHM
            )
            
            print(f"📧 A disparar e-mail de recuperação para: {email_banco}")
            
            # 3. Integração com o novo email_svc.py premium (que agora recebe o token)
            background_tasks.add_task(enviar_email_recuperacao, email_banco, token)
                
        return {"mensagem": "Se o e-mail existir no nosso sistema, receberá um link de recuperação em breve."}
    
    except Exception as e:
        # 🚨 ALERTA TI: Falha ao gerar o token JWT ou conectar à Base de Dados
        enviar_alerta_tecnico_teams(f"Falha ao gerar E-mail de Recuperação de Senha: {str(e)}")
        print(f"❌ ERRO CRÍTICO NO FORGOT PASSWORD: {str(e)}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Erro interno ao processar recuperação.")

@app.post("/api/usuarios/alterar-senha")
async def alterar_minha_senha(requisicao: AlterarSenhaRequest, usuario_email: str = Depends(get_current_user)):
    engine = get_engine()
    with engine.connect() as conn:
        user = conn.execute(
            text("SELECT senha_hash FROM dbo.nps_usuarios WHERE email = :email"),
            {"email": usuario_email}
        ).fetchone()

        if not bcrypt.checkpw(requisicao.senha_atual.encode('utf-8'), user.senha_hash.encode('utf-8')):
            raise HTTPException(status_code=400, detail="A senha atual está incorreta.")

        novo_hash = bcrypt.hashpw(requisicao.nova_senha.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
        conn.execute(
            text("UPDATE dbo.nps_usuarios SET senha_hash = :hash WHERE email = :email"),
            {"hash": novo_hash, "email": usuario_email}
        )
        conn.commit()
        
    return {"message": "Senha alterada com sucesso!"}

@app.post("/api/usuarios/{usuario_id}/reset-manual")
async def reset_manual_senha(usuario_id: str):
    engine = get_engine()
    caracteres = string.ascii_letters + string.digits
    senha_provisoria = ''.join(secrets.choice(caracteres) for i in range(10))
    senha_hash = bcrypt.hashpw(senha_provisoria.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
    
    try:
        with engine.connect() as conn:
            conn.execute(
                text("UPDATE dbo.nps_usuarios SET senha_hash = :hash WHERE usuario_id = :id"),
                {"hash": senha_hash, "id": usuario_id}
            )
            conn.commit()
        return {"senha_provisoria": senha_provisoria}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
# ==========================================
# 🔐 GESTÃO DE PERMISSÕES (RBAC/PBAC)
# ==========================================

@app.get("/api/permissoes")
def listar_permissoes(usuario = Depends(exigir_admin)):
    try:
        engine = get_engine()
        with engine.begin() as conn:
            res = conn.execute(text("SELECT perfil, chave FROM dbo.nps_permissoes")).fetchall()
            
            # Inicializa a estrutura
            permissoes = {"Viewer": [], "Manager": []}
            
            for row in res:
                # O Admin não vem do banco porque tem acesso total '*' por defeito
                if row.perfil in permissoes and row.chave != '*':
                    permissoes[row.perfil].append(row.chave)
                    
        return {"status": "success", "permissoes": permissoes}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/permissoes")
def atualizar_permissoes(payload: List[PermissaoUpdate], usuario = Depends(exigir_admin)):
    try:
        engine = get_engine()
        with engine.begin() as conn:
            for item in payload:
                # Ignoramos o Admin, pois o Admin tem sempre acesso '*' nativamente no código
                if item.perfil == 'Admin':
                    continue
                    
                # 1. Apaga as permissões antigas do perfil
                conn.execute(text("DELETE FROM dbo.nps_permissoes WHERE perfil = :p"), {"p": item.perfil})
                
                # 2. Insere as novas opções selecionadas
                if item.chaves and len(item.chaves) > 0:
                    sql_insert = text("INSERT INTO dbo.nps_permissoes (perfil, chave) VALUES (:p, :c)")
                    for chave in item.chaves:
                        conn.execute(sql_insert, {"p": item.perfil, "c": chave})
                        
        return {"status": "success", "message": "Matriz de permissões atualizada com sucesso!"}
    except Exception as e:
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))

# ==========================================
# ⚙️ ROTAS DE CONFIGURAÇÃO
# ==========================================
@app.get("/api/configuracoes")
async def get_configuracoes():
    try:
        engine = get_engine()
        with engine.connect() as conn:
            resultado = conn.execute(text("SELECT chave, valor FROM dbo.nps_configuracoes")).fetchall()
            configs = {row.chave: row.valor for row in resultado}
            return {"status": "success", "data": configs}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/configuracoes")
async def save_configuracoes(configs: List[ConfigItem]):
    try:
        engine = get_engine()
        with engine.begin() as conn:
            for item in configs:
                conn.execute(text("""
                    UPDATE dbo.nps_configuracoes 
                    SET valor = :valor, updated_at = GETDATE() 
                    WHERE chave = :chave
                """), {"valor": item.valor, "chave": item.chave})
        return {"status": "success", "detail": "Configurações salvas!"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ==========================================
# 🤖 MAGIC AI (LENDO CHAVE DO BANCO)
# ==========================================

@app.get("/api/dashboard/magic-ai")
async def get_magic_ai_insights():
    try:
        engine = get_engine()
        
        # 1. Puxa as configurações diretamente do Banco de Dados
        with engine.connect() as conn:
            api_key = conn.execute(text("SELECT valor FROM dbo.nps_configuracoes WHERE chave = 'openai_api_key'")).scalar()
            ai_model = conn.execute(text("SELECT valor FROM dbo.nps_configuracoes WHERE chave = 'openai_model'")).scalar() or "gpt-4o-mini"
            ai_temp = conn.execute(text("SELECT valor FROM dbo.nps_configuracoes WHERE chave = 'ai_temperature'")).scalar() or "0.4"
            
            if not api_key or api_key.strip() == "":
                return {
                    "status": "success", 
                    "insights": {
                        "arder": "Atenção necessária:",
                        "amar": "A funcionalidade de Inteligência Artificial está adormecida.",
                        "recomendacao": "Vá ao menu Definições > Inteligência Artificial e insira a sua chave da OpenAI."
                    }
                }

            # 2. Busca os comentários reais
            sql = """
                SELECT TOP 100 nota, motivo, categoria 
                FROM dbo.nps_respostas 
                WHERE motivo IS NOT NULL AND motivo != '' AND excluido = 0
                ORDER BY created_at DESC
            """
            df = pd.read_sql(text(sql), conn)

        if df.empty:
            return {"status": "success", "insights": {"arder": "Sem dados suficientes.", "amar": "Aguardando submissões.", "recomendacao": "Dispare uma nova pesquisa."}}

        lista_comentarios = [f"Nota: {row['nota']} - Categoria: {row['categoria']} - Comentário: {row['motivo']}" for _, row in df.iterrows()]
        texto_para_ia = "\n".join(lista_comentarios)

        # 3. Executa a IA com os parâmetros dinâmicos do Banco
        client = openai.OpenAI(api_key=api_key.strip())
        prompt_sistema = f"""
        Atue como um Consultor Executivo de CX. Analise estes feedbacks:
        {texto_para_ia}
        
        Forneça um resumo executivo com exatamente 3 pontos em formato JSON estrito:
        {{
            "arder": "1 frase resumindo o principal problema.",
            "amar": "1 frase resumindo os elogios.",
            "recomendacao": "1 frase com um plano de ação direto."
        }}
        """

        resposta_ia = client.chat.completions.create(
            model=ai_model,
            messages=[{"role": "user", "content": prompt_sistema}],
            response_format={ "type": "json_object" },
            temperature=float(ai_temp)
        )

        return {"status": "success", "insights": json.loads(resposta_ia.choices[0].message.content)}

    except Exception as e:
        print(f"❌ ERRO IA: {str(e)}")
        raise HTTPException(status_code=500, detail="Falha ao gerar insights. Verifique a API Key.")

def enviar_email_alerta_gestor(empresa: str, gestor_nome: str, gestor_email: str, nps: int):
    engine = get_engine()
    with engine.connect() as conn:
        cfg = conn.execute(text("SELECT TOP 1 tenant_id, client_id, client_secret, email_remetente, refresh_token FROM dbo.nps_configuracoes_email")).fetchone()
        if not cfg or not cfg.refresh_token:
            raise Exception("O sistema de e-mail não está autenticado. Vá às Configurações e conecte a conta Microsoft.")
        cfg = dict(cfg._mapping)

    # 1. Obter um Access Token NOVO usando o seu Refresh Token (o método que funciona)
    token_url = f"https://login.microsoftonline.com/{cfg['tenant_id']}/oauth2/v2.0/token"
    token_data = {
        'client_id': cfg['client_id'],
        'client_secret': cfg['client_secret'],
        'refresh_token': cfg['refresh_token'],
        'grant_type': 'refresh_token',
        'scope': 'offline_access mail.send'
    }
    
    r_token = requests.post(token_url, data=token_data)
    if r_token.status_code != 200:
        raise Exception(f"Falha ao renovar sessão Microsoft: {r_token.text}")
    
    token_json = r_token.json()
    access_token = token_json.get("access_token")
    
    # Opcional: Atualizar o refresh_token se a Microsoft enviou um novo
    if "refresh_token" in token_json:
        with engine.begin() as conn:
            conn.execute(text("UPDATE dbo.nps_configuracoes_email SET refresh_token = :rt, atualizado_em = GETDATE()"), {"rt": token_json["refresh_token"]})

    # 2. Template do E-mail
    corpo_html = f"""
    <div style="font-family: sans-serif; max-width: 600px; border: 1px solid #eee; border-radius: 10px; overflow: hidden;">
        <div style="background: #f43f5e; color: white; padding: 20px; text-align: center;">
            <h2 style="margin: 0;">🚨 ALERTA DE RISCO</h2>
        </div>
        <div style="padding: 20px; color: #333;">
            <p>Olá <strong>{gestor_nome}</strong>,</p>
            <p>O cliente <strong>{empresa}</strong> registou um NPS crítico de <strong>{nps} pts</strong>.</p>
            <p style="color: #be123c; font-weight: bold;">Ação de retenção aconselhada nas próximas 24h.</p>
        </div>
    </div>
    """

    # 3. Enviar o e-mail usando o token renovado
    send_url = f"https://graph.microsoft.com/v1.0/users/{cfg['email_remetente']}/sendMail"
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    mail_payload = {
        "message": {
            "subject": f"Ação Necessária: {empresa} (NPS {nps})",
            "body": {"contentType": "HTML", "content": corpo_html},
            "toRecipients": [{"emailAddress": {"address": gestor_email}}]
        }
    }
    
    r_send = requests.post(send_url, headers=headers, json=mail_payload)
    if r_send.status_code not in [200, 202]:
        raise Exception(f"A Microsoft recusou o envio: {r_send.text}")

    return True

@app.post("/api/dashboard/acionar-gestor")
def acionar_gestor_endpoint(req: AlertaGestorRequest):
    engine = get_engine()
    with engine.connect() as conn:
        gestor_db = conn.execute(text("SELECT email FROM dbo.nps_gestores WHERE nome = :nome"), {"nome": req.gestor}).fetchone()
        if not gestor_db or not gestor_db.email:
            raise HTTPException(status_code=400, detail="Gestor sem e-mail configurado.")
            
    try:
        enviar_email_alerta_gestor(req.empresa, req.gestor, gestor_db.email, req.nps)
        return {"status": "success", "message": f"Alerta enviado com sucesso para {gestor_db.email}!"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

# ==========================================
# 👥 ROTAS: GESTÃO DE OPERADORES (USUÁRIOS)
# ==========================================

@app.post("/api/usuarios")
def criar_usuario(usuario: UsuarioCreate):
    engine = get_engine()
    with engine.connect() as conn:
        
        check_query = text("SELECT usuario_id FROM dbo.nps_usuarios WHERE email = :email")
        existe = conn.execute(check_query, {"email": usuario.email}).fetchone()
        
        if existe:
            raise HTTPException(status_code=400, detail="Este e-mail já está registado no sistema.")
        
        bytes_senha = usuario.password.encode('utf-8')
        salt = bcrypt.gensalt()
        senha_hash = bcrypt.hashpw(bytes_senha, salt).decode('utf-8')
        
        insert_query = text("""
            INSERT INTO dbo.nps_usuarios (nome, email, senha_hash, cargo, ativo)
            VALUES (:nome, :email, :senha_hash, :cargo, 1)
        """)
        
        conn.execute(insert_query, {
            "nome": usuario.nome,
            "email": usuario.email,
            "senha_hash": senha_hash,
            "cargo": usuario.cargo,
        })
        conn.commit() 
        
        return {
            "status": "success", 
            "mensagem": f"Operador {usuario.nome} criado com sucesso!"
        }
    
@app.get("/api/usuarios")
async def listar_operadores():
    try:
        engine = get_engine()
        with engine.connect() as conn:
            query = text("""
                SELECT usuario_id, nome, email, cargo, tipo, ativo, ultimo_acesso 
                FROM dbo.nps_usuarios 
                ORDER BY nome ASC
            """)
            result = conn.execute(query).mappings().all()
            
            lista_usuarios = []
            for r in result:
                usuario = dict(r)
                
                if usuario.get("ultimo_acesso"):
                    data_utc = usuario["ultimo_acesso"].replace(tzinfo=timezone.utc)
                    usuario["ultimo_acesso"] = data_utc.isoformat()
                else:
                    usuario["ultimo_acesso"] = None
                    
                lista_usuarios.append(usuario)
                
            return lista_usuarios
            
    except Exception as e:
        print(f"Erro ao listar usuários: {e}")
        raise HTTPException(status_code=500, detail="Erro ao carregar lista de usuários.")

# ==========================================
# 🏠 ROTAS: DASHBOARD (Home)
# ==========================================
import re
import io
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional
from fastapi import Query, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import text
import pandas as pd

@app.get("/api/dashboard/kpis")
def get_dashboard_kpis(
    empresa: Optional[str] = Query(None),
    companhia: Optional[str] = Query(None),
    data_inicio: Optional[str] = Query(None),
    data_fim: Optional[str] = Query(None),
    apenas_ativos: bool = Query(True) # 👈 1. Parâmetro Adicionado
):
    try:
        engine = get_engine()
        with engine.connect() as conn:
            sql_set = text("SELECT valor FROM dbo.nps_configuracoes WHERE chave = 'mostrar_sem_cliente'")
            config_valor = conn.execute(sql_set).scalar()
            tipo_join = "LEFT JOIN"
            
            filtros_sql = []
            parametros = {}
            
            # 👇 2. Injeção do Parâmetro na Base
            parametros["apenas_ativos"] = 1 if apenas_ativos else 0
            # 🛡️ Adiciona a regra de Inativos logo de base
            filtros_sql.append("(:apenas_ativos = 0 OR e.ativo = 1)")
            
            if companhia and companhia != "Todas as Companhias":
                filtros_sql.append("""
                    COALESCE(r.empresa, c.empresa) IN (
                        SELECT emp.nome 
                        FROM dbo.nps_empresas emp 
                        INNER JOIN dbo.nps_companhias comp ON emp.companhia_id = comp.id 
                        WHERE comp.nome = :companhia
                    )
                """)
                parametros["companhia"] = companhia
            
            if empresa:
                if empresa == "Não Identificado":
                    filtros_sql.append("c.empresa IS NULL")
                else:
                    filtros_sql.append("c.empresa = :empresa")
                    parametros["empresa"] = empresa
                    
            if data_inicio and data_fim:
                filtros_sql.append("COALESCE(r.data_resposta, r.created_at) >= :data_inicio")
                filtros_sql.append("COALESCE(r.data_resposta, r.created_at) <= :data_fim")
                parametros["data_inicio"] = f"{data_inicio} 00:00:00"
                parametros["data_fim"] = f"{data_fim} 23:59:59"

            condicao_filtro = ""
            if len(filtros_sql) > 0:
                condicao_filtro = " WHERE " + " AND ".join(filtros_sql)

            # --- 3. PROCESSAMENTO DE PALAVRAS MAIS USADAS ---
            sql_termos = text(f"""
                SELECT CAST(r.motivo AS NVARCHAR(MAX)) as comentario
                FROM dbo.nps_respostas r
                {tipo_join} dbo.nps_clientes c ON r.cliente_id = c.cliente_id
                LEFT JOIN dbo.nps_empresas e ON COALESCE(r.empresa, c.empresa) = e.nome -- 👈 O JOIN OBRIGATÓRIO
                {condicao_filtro} 
                { "AND" if condicao_filtro else "WHERE" } r.motivo IS NOT NULL AND LEN(CAST(r.motivo AS NVARCHAR(MAX))) > 3
            """)
            
            comentarios_raw = conn.execute(sql_termos, parametros).scalars().all()
            
            stop_words = {
                'para', 'com', 'mais', 'esta', 'está', 'pela', 'pelo', 'como', 'muito', 'tudo', 
                'fazer', 'quando', 'você', 'pode', 'seria', 'estão', 'neste', 'esse', 'isso',
                'pela', 'pelo', 'uma', 'umas', 'uns', 'tem', 'têm', 'fui', 'foi', 'ser', 'bom', 'bem'
            }
            
            texto_unificado = " ".join([str(c).lower() for c in comentarios_raw if c])
            palavras = re.findall(r'\b[a-zà-ÿ]{4,}\b', texto_unificado)
            contagem = Counter([p for p in palavras if p not in stop_words])
            termos_frequentes = [{"palavra": p, "quantidade": q} for p, q in contagem.most_common(12)]

            # --- 4. QUERY DE KPIS PRINCIPAIS ---
            sql_kpis = text(f"""
                SELECT 
                    COUNT(r.resposta_id) as total_respostas,
                    SUM(CASE WHEN r.nota >= 9 THEN 1 ELSE 0 END) as promotores,
                    SUM(CASE WHEN r.nota BETWEEN 7 AND 8 THEN 1 ELSE 0 END) as neutros,
                    SUM(CASE WHEN r.nota <= 6 THEN 1 ELSE 0 END) as detratores,
                    
                    SUM(CASE WHEN LOWER(c.perfil_decisor) LIKE '%decisor%' AND r.nota >= 9 THEN 1 ELSE 0 END) as decisor_promotores,
                    SUM(CASE WHEN LOWER(c.perfil_decisor) LIKE '%decisor%' AND r.nota <= 6 THEN 1 ELSE 0 END) as decisor_detratores,
                    SUM(CASE WHEN LOWER(c.perfil_decisor) LIKE '%decisor%' THEN 1 ELSE 0 END) as decisor_total
                FROM dbo.nps_respostas r
                {tipo_join} dbo.nps_clientes c ON r.cliente_id = c.cliente_id
                LEFT JOIN dbo.nps_empresas e ON COALESCE(r.empresa, c.empresa) = e.nome -- 👈 JOIN
                {condicao_filtro};
            """)
                    
            resumo = conn.execute(sql_kpis, parametros).mappings().first()
            
            total = resumo['total_respostas'] or 0
            promotores = resumo['promotores'] or 0
            neutros = resumo['neutros'] or 0
            detratores = resumo['detratores'] or 0
            
            nps_score = 0
            if total > 0:
                nps_score = round(((promotores - detratores) / total) * 100)
                
            dec_total = resumo['decisor_total'] or 0
            nps_decisor = 0
            if dec_total > 0:
                nps_decisor = round(((resumo['decisor_promotores'] - resumo['decisor_detratores']) / dec_total) * 100)

            # --- 5. CÁLCULO REVENUE AT RISK ---
            filtro_sub = condicao_filtro.replace("WHERE", "AND") if condicao_filtro else ""
            sql_rev = text(f"""
                SELECT SUM(emp_out.valor_contrato) as risco
                FROM dbo.nps_empresas emp_out
                WHERE emp_out.nome IN (
                    SELECT DISTINCT COALESCE(r.empresa, c.empresa)
                    FROM dbo.nps_respostas r
                    INNER JOIN dbo.nps_clientes c ON r.cliente_id = c.cliente_id
                    LEFT JOIN dbo.nps_empresas e ON COALESCE(r.empresa, c.empresa) = e.nome -- 👈 JOIN
                    WHERE r.nota <= 6 {filtro_sub}
                )
            """)
            risco_real = conn.execute(sql_rev, parametros).scalar() or 0
                
            # --- 6. FEEDBACKS RECENTES ---
            sql_feedbacks = text(f"""
                SELECT TOP 10 
                    r.nota, CAST(r.motivo AS NVARCHAR(MAX)) as comentario, 
                    r.created_at, r.jira_issue_url,
                    c.nome as cliente, COALESCE(r.empresa, c.empresa) as empresa, c.perfil_decisor
                FROM dbo.nps_respostas r
                INNER JOIN dbo.nps_clientes c ON r.cliente_id = c.cliente_id
                LEFT JOIN dbo.nps_empresas e ON COALESCE(r.empresa, c.empresa) = e.nome -- 👈 JOIN
                WHERE r.motivo IS NOT NULL AND LEN(CAST(r.motivo AS NVARCHAR(MAX))) > 0
                {condicao_filtro.replace("WHERE", "AND") if condicao_filtro else ""} 
                ORDER BY r.created_at DESC;
            """)
            
            feedbacks_raw = conn.execute(sql_feedbacks, parametros).mappings().all()
            
            regras_tags = {
                "Performance": ["lento", "lentidão", "trava", "demora", "carregar", "devagar"],
                "UX/UI": ["difícil", "layout", "design", "confuso", "interface", "navegação"],
                "Atendimento": ["suporte", "atendimento", "ajuda", "cs", "resposta"],
                "Integração": ["integração", "jira", "api", "conectar", "sincronizar"],
                "Bugs": ["erro", "bug", "falha", "quebrou", "problema"]
            }
            
            feedbacks_processados = []
            for f_raw in feedbacks_raw:
                f = dict(f_raw)
                texto = str(f.get("comentario") or "").lower()
                f["tags"] = [tag for tag, keys in regras_tags.items() if any(k in texto for k in keys)]
                feedbacks_processados.append(f)

            condicao_resgate = condicao_filtro.replace("r.", "atual.")
            if condicao_resgate:
                condicao_resgate = condicao_resgate.replace("WHERE", "AND") 
                
            query_resgates = text(f"""
                WITH Historico AS (
                    SELECT cliente_id, nota, data_resposta, created_at, empresa,
                           ROW_NUMBER() OVER(PARTITION BY cliente_id ORDER BY COALESCE(data_resposta, created_at) DESC, resposta_id DESC) as rn
                    FROM dbo.nps_respostas
                    WHERE excluido = 0 AND cliente_id IS NOT NULL AND cliente_id <> ''
                )
                SELECT COUNT(*) 
                FROM Historico atual
                JOIN Historico anterior ON atual.cliente_id = anterior.cliente_id AND anterior.rn = 2
                {tipo_join} dbo.nps_clientes c ON atual.cliente_id = c.cliente_id
                LEFT JOIN dbo.nps_empresas e ON COALESCE(atual.empresa, c.empresa) = e.nome -- 👈 JOIN
                WHERE atual.rn = 1 
                  AND anterior.nota <= 8  
                  AND atual.nota >= 9     
                  {condicao_resgate}      
            """)
            
            total_resgatados = conn.execute(query_resgates, parametros).scalar() or 0

            # --- VARIÁVEIS ANTIGAS ---
            filtros_sql_ant = []
            params_ant = {}
            
            params_ant["apenas_ativos"] = 1 if apenas_ativos else 0
            filtros_sql_ant.append("(:apenas_ativos = 0 OR e.ativo = 1)")
            
            if companhia and companhia != "Todas as Companhias":
                filtros_sql_ant.append("""
                    COALESCE(r.empresa, c.empresa) IN (
                        SELECT emp.nome 
                        FROM dbo.nps_empresas emp 
                        INNER JOIN dbo.nps_companhias comp ON emp.companhia_id = comp.id 
                        WHERE comp.nome = :companhia
                    )
                """)
                params_ant["companhia"] = companhia
            
            if empresa:
                if empresa == "Não Identificado":
                    filtros_sql_ant.append("c.empresa IS NULL")
                else:
                    filtros_sql_ant.append("c.empresa = :empresa")
                    params_ant["empresa"] = empresa
                    
            if data_inicio and data_fim:
                dt_ini = datetime.strptime(data_inicio, "%Y-%m-%d")
                dt_fim = datetime.strptime(data_fim, "%Y-%m-%d")
                dias = (dt_fim - dt_ini).days + 1
                ant_ini = dt_ini - timedelta(days=dias)
                ant_fim = dt_ini - timedelta(seconds=1)
                
                filtros_sql_ant.append("COALESCE(r.data_resposta, r.created_at) >= :ant_ini")
                filtros_sql_ant.append("COALESCE(r.data_resposta, r.created_at) <= :ant_fim")
                params_ant["ant_ini"] = ant_ini.strftime("%Y-%m-%d 00:00:00")
                params_ant["ant_fim"] = ant_fim.strftime("%Y-%m-%d 23:59:59")
            else:
                ant_fim = datetime.now(timezone.utc) - timedelta(days=30)
                filtros_sql_ant.append("COALESCE(r.data_resposta, r.created_at) <= :ant_fim")
                params_ant["ant_fim"] = ant_fim.strftime("%Y-%m-%d 23:59:59")

            condicao_ant = " WHERE " + " AND ".join(filtros_sql_ant) if filtros_sql_ant else ""
            
            sql_nps_ant = text(f"""
                SELECT 
                    COUNT(r.resposta_id) as total,
                    SUM(CASE WHEN r.nota >= 9 THEN 1 ELSE 0 END) as prom,
                    SUM(CASE WHEN r.nota <= 6 THEN 1 ELSE 0 END) as detr
                FROM dbo.nps_respostas r
                {tipo_join} dbo.nps_clientes c ON r.cliente_id = c.cliente_id
                LEFT JOIN dbo.nps_empresas e ON COALESCE(r.empresa, c.empresa) = e.nome -- 👈 JOIN
                {condicao_ant};
            """)
            
            res_ant = conn.execute(sql_nps_ant, params_ant).mappings().first()
            nps_anterior = 0
            if res_ant and res_ant['total'] > 0:
                nps_anterior = round(((res_ant['prom'] - res_ant['detr']) / res_ant['total']) * 100)
                
            variacao_nps = nps_score - nps_anterior

            # --- 7. TÓPICOS CRÍTICOS ---
            sql_topicos = text(f"""
                SELECT TOP 5
                    COALESCE(r.motivo, 'Sem Classificação') as tema,
                    COUNT(r.resposta_id) as mencoes,
                    AVG(CAST(r.nota AS FLOAT)) as notaMedia
                FROM dbo.nps_respostas r
                {tipo_join} dbo.nps_clientes c ON r.cliente_id = c.cliente_id
                LEFT JOIN dbo.nps_empresas e ON COALESCE(r.empresa, c.empresa) = e.nome -- 👈 JOIN
                {condicao_filtro}
                { "AND" if condicao_filtro else "WHERE" } 
                    r.motivo IS NOT NULL 
                    AND r.motivo != '' 
                    AND r.motivo NOT IN ('Promotor', 'Detrator', 'Neutro', 'Passivo')
                    AND r.excluido = 0
                GROUP BY COALESCE(r.motivo, 'Sem Classificação')
                ORDER BY mencoes DESC, notaMedia ASC
            """)
            
            topicos_raw = conn.execute(sql_topicos, parametros).mappings().all()
            
            topicos_criticos = [
                {"tema": r['tema'], "mencoes": r['mencoes'], "notaMedia": float(r['notaMedia'])} 
                for r in topicos_raw if r['tema'] != 'Sem Classificação'
            ]

            # --- CÁLCULO DE PROMOTORES PERDIDOS / EM RISCO ---
            query_perdidos = text(f"""
                WITH Historico AS (
                    SELECT cliente_id, nota, data_resposta, created_at, empresa,
                           ROW_NUMBER() OVER(PARTITION BY cliente_id ORDER BY COALESCE(data_resposta, created_at) DESC, resposta_id DESC) as rn
                    FROM dbo.nps_respostas
                    WHERE excluido = 0 AND cliente_id IS NOT NULL AND cliente_id <> ''
                )
                SELECT 
                    SUM(CASE WHEN anterior.nota >= 9 AND atual.nota <= 8 THEN 1 ELSE 0 END) as total_em_risco,
                    SUM(CASE WHEN anterior.nota >= 9 AND atual.nota <= 6 THEN 1 ELSE 0 END) as queda_drastica
                FROM Historico atual
                JOIN Historico anterior ON atual.cliente_id = anterior.cliente_id AND anterior.rn = 2
                {tipo_join} dbo.nps_clientes c ON atual.cliente_id = c.cliente_id
                LEFT JOIN dbo.nps_empresas e ON COALESCE(atual.empresa, c.empresa) = e.nome -- 👈 JOIN
                WHERE atual.rn = 1      
                  {condicao_resgate}      
            """)
            
            res_perdidos = conn.execute(query_perdidos, parametros).mappings().first()
            clientes_em_risco = res_perdidos['total_em_risco'] or 0
            queda_drastica = res_perdidos['queda_drastica'] or 0
            
        return {
            "status": "success",
            "kpis": {
                "score": nps_score,
                "total_respostas": total,
                "promotores": promotores,
                "neutros": neutros,
                "detratores": detratores,
                "nps_decisor": nps_decisor, 
                "clientes_resgatados": total_resgatados,
                "clientes_em_risco": clientes_em_risco, 
                "queda_drastica": queda_drastica,       
                "variacao_nps": variacao_nps, 
                "revenue_at_risk": float(risco_real),
                "termos_frequentes": termos_frequentes,
                "total_decisores": dec_total,
                "topicos_criticos": topicos_criticos 
            },
            "feedbacks": feedbacks_processados
        }
    
    except Exception as e:
        import traceback
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))
    

@app.get("/api/dashboard/detalhes")
def get_dashboard_detalhes(
    empresa: Optional[str] = Query(None),
    companhia: Optional[str] = Query(None), 
    data_inicio: Optional[str] = Query(None), 
    data_fim: Optional[str] = Query(None),
    apenas_ativos: bool = Query(True) 
):
    try:
        from sqlalchemy import text
        engine = get_engine()
        with engine.connect() as conn:
            filtros_sql_c = []
            filtros_sql_puro = []
            params = {}

            params["apenas_ativos"] = 1 if apenas_ativos else 0

            if companhia and companhia != "Todas as Companhias":
                filtros_sql_c.append("""
                    COALESCE(r.empresa, c.empresa) IN (
                        SELECT e.nome 
                        FROM dbo.nps_empresas e 
                        INNER JOIN dbo.nps_companhias comp ON e.companhia_id = comp.id 
                        WHERE comp.nome = :companhia
                    )
                """)
                filtros_sql_puro.append("""
                    empresa IN (
                        SELECT e.nome 
                        FROM dbo.nps_empresas e 
                        INNER JOIN dbo.nps_companhias comp ON e.companhia_id = comp.id 
                        WHERE comp.nome = :companhia
                    )
                """)
                params["companhia"] = companhia

            if empresa:
                params["empresa"] = empresa
                if empresa == "Não Identificado":
                    filtros_sql_c.append("c.empresa IS NULL")
                    filtros_sql_puro.append("empresa IS NULL")
                else:
                    filtros_sql_c.append("c.empresa = :empresa")
                    filtros_sql_puro.append("empresa = :empresa")

            if data_inicio and data_fim:
                filtros_sql_c.append("COALESCE(r.data_resposta, r.created_at) >= :data_inicio")
                filtros_sql_c.append("COALESCE(r.data_resposta, r.created_at) <= :data_fim")
                params["data_inicio"] = f"{data_inicio} 00:00:00"
                params["data_fim"] = f"{data_fim} 23:59:59"

            str_filtro_c = "WHERE 1=1"
            if len(filtros_sql_c) > 0:
                str_filtro_c += " AND " + " AND ".join(filtros_sql_c)
                
            str_filtro_puro = "WHERE 1=1"
            if len(filtros_sql_puro) > 0:
                str_filtro_puro += " AND " + " AND ".join(filtros_sql_puro)

            coluna_nome = "COALESCE(r.empresa, c.empresa, 'Não Identificado')" if not empresa else "COALESCE(c.segmento, 'Sem Segmento')"
            
            sql_ranking = text(f"""
                SELECT 
                    {coluna_nome} as nome,
                    MAX(e.gestor) as gestor, 
                    MAX(g.avatar) as gestor_avatar,
                    
                    MAX(CAST(COALESCE(e.ativo, 1) AS INT)) as ativo, 
                    
                    COUNT(r.resposta_id) as total,
                    MAX(COALESCE(r.data_resposta, r.created_at)) as data_ultima_resposta,
                    
                    -- 👇 CORREÇÃO: Agora busca ações vinculadas à resposta OU vinculadas diretamente à empresa
                    (SELECT TOP 1 a.id 
                     FROM dbo.nps_acoes a 
                     WHERE a.resposta_id = MAX(r.resposta_id) OR a.empresa_id = MAX(e.id)
                     ORDER BY a.created_at DESC) as acao_id,

                    (SELECT TOP 1 a.status 
                     FROM dbo.nps_acoes a 
                     WHERE a.resposta_id = MAX(r.resposta_id) OR a.empresa_id = MAX(e.id)
                     ORDER BY a.created_at DESC) as acao_status,

                    ROUND(
                        (SUM(CASE WHEN r.nota >= 9 THEN 1.0 ELSE 0 END) / NULLIF(COUNT(r.resposta_id), 0) * 100) - 
                        (SUM(CASE WHEN r.nota <= 6 THEN 1.0 ELSE 0 END) / NULLIF(COUNT(r.resposta_id), 0) * 100), 0
                    ) as nps
                FROM dbo.nps_respostas r
                LEFT JOIN dbo.nps_clientes c ON r.cliente_id = c.cliente_id
                LEFT JOIN dbo.nps_empresas e ON COALESCE(r.empresa, c.empresa) = e.nome 
                LEFT JOIN dbo.nps_gestores g ON e.gestor_id = g.id
                
                {str_filtro_c}
                
                AND (:apenas_ativos = 0 OR e.ativo = 1)
                
                GROUP BY {coluna_nome}
                ORDER BY nps ASC, data_ultima_resposta ASC;
            """)
            
            ranking_raw = conn.execute(sql_ranking, params).mappings().all()
            ranking = [dict(r) for r in ranking_raw]

            sql_taxa = text(f"""
                SELECT 
                    (SELECT COUNT(*) FROM dbo.nps_clientes {str_filtro_puro}) as total_convidados,
                    (SELECT COUNT(DISTINCT r.cliente_id) 
                     FROM dbo.nps_respostas r
                     LEFT JOIN dbo.nps_clientes c ON r.cliente_id = c.cliente_id
                     {str_filtro_c}) as total_responderam
            """)
            
            res_taxa = conn.execute(sql_taxa, params).mappings().first()
            
            taxa_pct = 0
            if res_taxa and res_taxa['total_convidados'] > 0:
                taxa_pct = round((res_taxa['total_responderam'] / res_taxa['total_convidados']) * 100)

        return {
            "ranking": ranking,
            "taxa_resposta": taxa_pct
        }
    except Exception as e:
        import traceback
        print(traceback.format_exc())
        from fastapi import HTTPException
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/dashboard/trend")
def get_dashboard_trend(
    empresa: Optional[str] = Query(None),
    companhia: Optional[str] = Query(None),
    data_inicio: Optional[str] = Query(None),
    data_fim: Optional[str] = Query(None),
    apenas_ativos: bool = Query(True) # 👈 Adicionado
):
    try:
        engine = get_engine()
        with engine.connect() as conn:
            sql_set = text("SELECT valor FROM dbo.nps_configuracoes WHERE chave = 'mostrar_sem_cliente'")
            config_valor = conn.execute(sql_set).scalar()
            tipo_join = "LEFT JOIN"

            filtros_sql = ["COALESCE(r.data_resposta, r.created_at) IS NOT NULL", "(r.excluido = 0 OR r.excluido IS NULL)"]
            params = {}
            
            # 👇 Adicionado Parâmetro e Filtro
            params["apenas_ativos"] = 1 if apenas_ativos else 0
            filtros_sql.append("(:apenas_ativos = 0 OR e.ativo = 1)")
            
            if companhia and companhia != "Todas as Companhias":
                filtros_sql.append("""
                    COALESCE(r.empresa, c.empresa) IN (
                        SELECT e_sub.nome 
                        FROM dbo.nps_empresas e_sub 
                        INNER JOIN dbo.nps_companhias comp ON e_sub.companhia_id = comp.id 
                        WHERE comp.nome = :companhia
                    )
                """)
                params["companhia"] = companhia
                
            if empresa:
                if empresa == "Não Identificado":
                    filtros_sql.append("c.empresa IS NULL")
                else:
                    filtros_sql.append("c.empresa = :empresa")
                    params["empresa"] = empresa
                    
            if data_inicio and data_fim:
                filtros_sql.append("COALESCE(r.data_resposta, r.created_at) >= :data_inicio")
                filtros_sql.append("COALESCE(r.data_resposta, r.created_at) <= :data_fim")
                params["data_inicio"] = f"{data_inicio} 00:00:00"
                params["data_fim"] = f"{data_fim} 23:59:59"

            condicao = " WHERE " + " AND ".join(filtros_sql)

            sql_trend = text(f"""
                WITH UltimosMeses AS (
                    SELECT TOP 6
                        LEFT(CAST(COALESCE(r.data_resposta, r.created_at) AS VARCHAR(10)), 7) as mes,
                        COUNT(r.resposta_id) as total,
                        SUM(CASE WHEN r.nota >= 9 THEN 1 ELSE 0 END) as promotores,
                        SUM(CASE WHEN r.nota <= 6 THEN 1 ELSE 0 END) as detratores
                    FROM dbo.nps_respostas r
                    {tipo_join} dbo.nps_clientes c ON r.cliente_id = c.cliente_id
                    LEFT JOIN dbo.nps_empresas e ON COALESCE(r.empresa, c.empresa) = e.nome -- 👈 JOIN Adicionado
                    {condicao}
                    GROUP BY LEFT(CAST(COALESCE(r.data_resposta, r.created_at) AS VARCHAR(10)), 7)
                    ORDER BY LEFT(CAST(COALESCE(r.data_resposta, r.created_at) AS VARCHAR(10)), 7) DESC
                )
                SELECT * FROM UltimosMeses ORDER BY mes ASC;
            """)

            result = conn.execute(sql_trend, params).mappings().all()
            
            labels = []
            scores = []
            
            meses_pt = {'01':'Jan', '02':'Fev', '03':'Mar', '04':'Abr', '05':'Mai', '06':'Jun', 
                        '07':'Jul', '08':'Ago', '09':'Set', '10':'Out', '11':'Nov', '12':'Dez'}

            for row in result:
                if not row['mes'] or '-' not in row['mes']: 
                    continue
                
                ano, mes_num = row['mes'].split('-')
                nome_mes = meses_pt.get(mes_num, mes_num) 
                mes_nome = f"{nome_mes}/{ano[2:]}" 
                
                total = row['total'] or 0
                prom = row['promotores'] or 0
                detr = row['detratores'] or 0
                
                nps = round(((prom / total) * 100) - ((detr / total) * 100)) if total > 0 else 0
                    
                labels.append(mes_nome)
                scores.append(nps)

        return {"status": "success", "labels": labels, "scores": scores}
    
    except Exception as e:
        import traceback
        print("\n🔥 ERRO NO GRÁFICO:")
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))
    
@app.get("/api/dashboard/nuvem-palavras")
def get_nuvem_palavras(
    empresa: Optional[str] = Query(None),
    companhia: Optional[str] = Query(None), 
    data_inicio: Optional[str] = Query(None),
    data_fim: Optional[str] = Query(None),
    apenas_ativos: bool = Query(True) # 👈 Adicionado
):
    try:
        engine = get_engine()
        with engine.connect() as conn:
            filtros_sql = ["r.nota <= 6", "r.motivo IS NOT NULL", "LEN(CAST(r.motivo AS NVARCHAR(MAX))) > 0", "(r.excluido = 0 OR r.excluido IS NULL)"]
            params = {}
            
            # 👇 Adicionado Parâmetro e Filtro
            params["apenas_ativos"] = 1 if apenas_ativos else 0
            filtros_sql.append("(:apenas_ativos = 0 OR e.ativo = 1)")
            
            if companhia and companhia != "Todas as Companhias":
                filtros_sql.append("""
                    COALESCE(r.empresa, c.empresa) IN (
                        SELECT e_sub.nome 
                        FROM dbo.nps_empresas e_sub 
                        INNER JOIN dbo.nps_companhias comp ON e_sub.companhia_id = comp.id 
                        WHERE comp.nome = :companhia
                    )
                """)
                params["companhia"] = companhia
                
            if empresa:
                if empresa == "Não Identificado":
                    filtros_sql.append("c.empresa IS NULL")
                else:
                    filtros_sql.append("c.empresa = :empresa")
                    params["empresa"] = empresa
                    
            if data_inicio and data_fim:
                filtros_sql.append("COALESCE(r.data_resposta, r.created_at) >= :data_inicio")
                filtros_sql.append("COALESCE(r.data_resposta, r.created_at) <= :data_fim")
                params["data_inicio"] = f"{data_inicio} 00:00:00"
                params["data_fim"] = f"{data_fim} 23:59:59"

            condicao = " WHERE " + " AND ".join(filtros_sql)

            sql = text(f"""
                SELECT CAST(r.motivo AS NVARCHAR(MAX)) as motivo
                FROM dbo.nps_respostas r
                LEFT JOIN dbo.nps_clientes c ON r.cliente_id = c.cliente_id
                LEFT JOIN dbo.nps_empresas e ON COALESCE(r.empresa, c.empresa) = e.nome -- 👈 JOIN Adicionado
                {condicao}
            """)
            
            result = conn.execute(sql, params).mappings().all()
            texto_completo = " ".join([r['motivo'].lower() for r in result if r['motivo']])
            palavras = re.findall(r'\b[a-zà-ú]{3,}\b', texto_completo)
            
            stop_words = {
                'que', 'não', 'para', 'com', 'uma', 'dos', 'das', 'aos', 'nas', 'nos', 
                'como', 'mais', 'mas', 'foi', 'por', 'sua', 'seu', 'tem', 'muito', 'isso', 
                'está', 'também', 'pelo', 'pela', 'até', 'quando', 'ou', 'só', 'ter', 'ser', 
                'fazer', 'estou', 'sobre', 'ainda', 'sem', 'porque', 'neste', 'nesta'
            }
            
            palavras_uteis = [p for p in palavras if p not in stop_words and len(p) > 3]
            contagem = Counter(palavras_uteis).most_common(15) 
            nuvem = [{"texto": p[0], "peso": p[1]} for p in contagem]
            
            return {"status": "success", "nuvem": nuvem}
    except Exception as e:
        import traceback
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/dashboard/exportar")
def exportar_dashboard(
    empresa: Optional[str] = Query(None),
    companhia: Optional[str] = Query(None), 
    data_inicio: Optional[str] = Query(None),
    data_fim: Optional[str] = Query(None),
    apenas_ativos: bool = Query(True) # 👈 Adicionado
):
    try:
        engine = get_engine()
        with engine.connect() as conn:
            filtros_sql = ["(r.excluido = 0 OR r.excluido IS NULL)"]
            parametros = {}
            
            # 👇 Adicionado Parâmetro e Filtro
            parametros["apenas_ativos"] = 1 if apenas_ativos else 0
            filtros_sql.append("(:apenas_ativos = 0 OR e.ativo = 1)")
            
            if companhia and companhia != "Todas as Companhias":
                filtros_sql.append("""
                    COALESCE(r.empresa, c.empresa) IN (
                        SELECT e_sub.nome 
                        FROM dbo.nps_empresas e_sub 
                        INNER JOIN dbo.nps_companhias comp ON e_sub.companhia_id = comp.id 
                        WHERE comp.nome = :companhia
                    )
                """)
                parametros["companhia"] = companhia
                
            if empresa:
                if empresa == "Não Identificado":
                    filtros_sql.append("c.empresa IS NULL")
                else:
                    filtros_sql.append("c.empresa = :empresa")
                    parametros["empresa"] = empresa
                    
            if data_inicio and data_fim:
                filtros_sql.append("COALESCE(r.data_resposta, r.created_at) >= :data_inicio")
                filtros_sql.append("COALESCE(r.data_resposta, r.created_at) <= :data_fim")
                parametros["data_inicio"] = f"{data_inicio} 00:00:00"
                parametros["data_fim"] = f"{data_fim} 23:59:59"

            condicao_filtro = " WHERE " + " AND ".join(filtros_sql)

            sql_relatorio = text(f"""
                SELECT 
                    c.nome as Cliente,
                    c.email as Email,
                    COALESCE(r.empresa, c.empresa) as Empresa, 
                    c.segmento as Segmento,
                    c.perfil_decisor as Perfil,
                    c.valor_contrato as Receita_ARR,
                    r.nota as Nota_NPS,
                    CASE 
                        WHEN r.nota >= 9 THEN 'Promotor'
                        WHEN r.nota >= 7 THEN 'Neutro'
                        ELSE 'Detrator'
                    END as Classificacao,
                    r.motivo as Comentario,
                    r.categoria as Categoria,
                    COALESCE(r.data_resposta, r.created_at) as Data_Resposta
                FROM dbo.nps_respostas r
                LEFT JOIN dbo.nps_clientes c ON r.cliente_id = c.cliente_id
                LEFT JOIN dbo.nps_empresas e ON COALESCE(r.empresa, c.empresa) = e.nome -- 👈 JOIN Adicionado
                {condicao_filtro}
                ORDER BY Data_Resposta DESC
            """)

            df = pd.read_sql(sql_relatorio, conn, params=parametros)

        stream = io.StringIO()
        df.to_csv(stream, index=False, sep=';', encoding='utf-8-sig') 
        
        response = StreamingResponse(iter([stream.getvalue()]), media_type="text/csv")
        response.headers["Content-Disposition"] = "attachment; filename=NPS_CommandCenter_Export.csv"
        return response

    except Exception as e:
        print(f"Erro Exportação: {str(e)}")
        raise HTTPException(status_code=500, detail="Falha ao gerar o ficheiro.")
        
@app.get("/api/dashboard/companhias")
def get_lista_companhias():
    try:
        engine = get_engine()
        with engine.connect() as conn:
            sql = text("SELECT nome FROM dbo.nps_companhias ORDER BY nome")
            resultados = conn.execute(sql).scalars().all()
            
            return ["Todas as Companhias"] + list(resultados)
    except Exception as e:
        print(f"Erro ao buscar companhias: {e}")
        return ["Todas as Companhias"]
    
# ==========================================
# 🏢 ROTAS: EMPRESAS, SEGMENTOS E PERFIS
# ==========================================

@app.get("/api/empresas")
def listar_empresas():
    try:
        engine = get_engine()
        with engine.connect() as conn:
            # Magia: Agrupa os clientes pela empresa e soma a receita (ARR) automaticamente!
            sql = text("""
                SELECT 
                    empresa as nome, 
                    COUNT(cliente_id) as total_contatos,
                    SUM(COALESCE(valor_contrato, 0)) as arr_total
                FROM dbo.nps_clientes
                WHERE empresa IS NOT NULL AND empresa <> ''
                GROUP BY empresa
                ORDER BY arr_total DESC
            """)
            res = conn.execute(sql).mappings().all()
            return [dict(r) for r in res]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

def crud_factory(route_path, table_name, schema=BasicoSchema):
    @app.get(route_path)
    def listar():
        with get_engine().connect() as conn: 
            return [dict(r) for r in conn.execute(text(f"SELECT * FROM {table_name} ORDER BY nome")).mappings().all()]
            
    @app.post(route_path)
    def salvar(item: schema): # type: ignore  
        with get_engine().begin() as conn:
            if table_name == 'dbo.nps_gestores': 
                conn.execute(text(f"INSERT INTO {table_name} (nome, papel, email, teams_webhook, avatar) VALUES (:n, :p, :e, :t, :a)"), {
                    "n": item.nome, 
                    "p": getattr(item, 'papel', ''), 
                    "e": getattr(item, 'email', ''),
                    "t": getattr(item, 'teams_webhook', ''),
                    "a": getattr(item, 'avatar', None)
                })
            else: 
                conn.execute(text(f"INSERT INTO {table_name} (nome) VALUES (:n)"), {"n": item.nome})
        return {"status": "success"}
        
    @app.put(route_path + "/{item_id}")
    def atualizar(item_id: int, item: schema): # type: ignore  
        with get_engine().begin() as conn:
            nome_antigo = conn.execute(text(f"SELECT nome FROM {table_name} WHERE id=:id"), {"id": item_id}).scalar()
            
            if table_name == 'dbo.nps_gestores': 
                conn.execute(text(f"UPDATE {table_name} SET nome=:n, papel=:p, email=:e, teams_webhook=:t, avatar=:a WHERE id=:id"), {
                    "n": item.nome, 
                    "p": getattr(item, 'papel', ''), 
                    "e": getattr(item, 'email', ''), 
                    "t": getattr(item, 'teams_webhook', ''),
                    "a": getattr(item, 'avatar', None),
                    "id": item_id
                })
            else: 
                conn.execute(text(f"UPDATE {table_name} SET nome=:n WHERE id=:id"), {"n": item.nome, "id": item_id})
            
            if nome_antigo and str(nome_antigo) != str(item.nome):
                if table_name == 'dbo.nps_segmentos':
                    conn.execute(text("UPDATE dbo.nps_empresas SET segmento=:novo WHERE segmento=:antigo"), {"novo": item.nome, "antigo": nome_antigo})
                elif table_name == 'dbo.nps_perfis':
                    conn.execute(text("UPDATE dbo.nps_clientes SET perfil_decisor=:novo WHERE perfil_decisor=:antigo"), {"novo": item.nome, "antigo": nome_antigo})
                elif table_name == 'dbo.nps_cargos':
                    conn.execute(text("UPDATE dbo.nps_clientes SET cargo=:novo WHERE cargo=:antigo"), {"novo": item.nome, "antigo": nome_antigo})
                elif table_name == 'dbo.nps_gestores':
                    conn.execute(text("UPDATE dbo.nps_empresas SET gestor=:novo WHERE gestor=:antigo"), {"novo": item.nome, "antigo": nome_antigo})

        return {"status": "success"}
        
    @app.delete(route_path + "/{item_id}")
    def deletar(item_id: int):
        with get_engine().begin() as conn: conn.execute(text(f"DELETE FROM {table_name} WHERE id = :id"), {"id": item_id})
        return {"message": "Removido"}

# Estas 4 linhas substituem dezenas de rotas antigas e ativam todos os menus!
crud_factory("/api/cadastros/segmentos", "dbo.nps_segmentos")
crud_factory("/api/cadastros/perfis", "dbo.nps_perfis")
crud_factory("/api/cadastros/cargos", "dbo.nps_cargos")
crud_factory("/api/cadastros/gestores", "dbo.nps_gestores", GestorSchema)
crud_factory("/api/cadastros/companhias", "dbo.nps_companhias")
    
# --- ROTAS DE GESTORES DE CONTA ---
@app.get("/api/gestores")
async def get_lista_gestores():
    try:
        engine = get_engine()
        with engine.connect() as conn:
            sql = text("SELECT id, nome, email FROM dbo.usuarios WHERE ativo = 1")
            resultados = conn.execute(sql).mappings().all()
            
            gestores = [{"id": r['id'], "nome": r['nome'], "email": r['email']} for r in resultados if r['email']]
            return gestores
    except Exception as e:
        print(f"❌ Erro ao buscar gestores: {e}")
        return []
    
@app.post("/api/gestores/testar-webhook")
def testar_webhook_teams(payload: TesteWebhookPayload):
    if not payload.webhook_url:
        raise HTTPException(status_code=400, detail="URL do Webhook não fornecida.")

    # Um Cartão Adaptativo bonito só para confirmar que a ligação funciona
    adaptive_card = {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.4",
                "body": [
                    {
                        "type": "TextBlock",
                        "text": "🚀 Conexão Estabelecida!",
                        "size": "Large",
                        "weight": "Bolder",
                        "color": "Good"
                    },
                    {
                        "type": "TextBlock",
                        "text": "O Hub de NPS da Gauge está agora conectado a este canal. Você receberá os resumos matinais de ações pendentes aqui.",
                        "wrap": True
                    }
                ]
            }
        }]
    }

    try:
        import requests
        resp = requests.post(payload.webhook_url, json=adaptive_card, headers={"Content-Type": "application/json"})
        resp.raise_for_status()
        return {"status": "success", "message": "Mensagem de teste enviada com sucesso!"}
    except Exception as e:
        print(f"Erro ao testar webhook: {e}")
        raise HTTPException(status_code=500, detail="Falha ao enviar mensagem. Verifique se a URL é válida.")

# ==========================================
# 🚀 SALVAR NOVOS CADASTROS
# ==========================================

@app.get("/api/cadastros/empresas")
async def listar_empresas():
    engine = get_engine()
    with engine.connect() as conn:
        sql = text("""
            SELECT 
                e.id, e.nome, e.segmento, e.valor_contrato as arr_total, 
                g.nome as gestor, e.gestor_id,
                comp.nome as companhia, e.companhia_id,
                e.ativo -- 👈 ADICIONADO AQUI!
            FROM dbo.nps_empresas e
            LEFT JOIN dbo.nps_gestores g ON e.gestor_id = g.id
            LEFT JOIN dbo.nps_companhias comp ON e.companhia_id = comp.id
            ORDER BY e.nome ASC
        """)
        return conn.execute(sql).mappings().all()

@app.post("/api/cadastros/empresas")
def save_empresa(emp: EmpresaSchema):
    try:
        engine = get_engine()
        with engine.begin() as conn:
            sql_insert = text("""
                INSERT INTO dbo.nps_empresas 
                (nome, segmento, valor_contrato, gestor, gestor_id, companhia_id) 
                VALUES (:n, :s, :v, :g, :gid, :cid)
            """)
            conn.execute(sql_insert, {
                "n": emp.nome, 
                "s": emp.segmento, 
                "v": emp.valor_contrato, 
                "g": emp.gestor,
                "gid": emp.gestor_id, # 👈 O ID agora é guardado!
                "cid": emp.companhia_id
            })
        return {"status": "success", "message": "Empresa cadastrada"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/api/cadastros/empresas/{empresa_id}")
def update_empresa(empresa_id: int, emp: EmpresaSchema):
    engine = get_engine()
    with engine.begin() as conn: 
        nome_antigo = conn.execute(text("SELECT nome FROM dbo.nps_empresas WHERE id=:id"), {"id": empresa_id}).scalar()
        
        # 👇 CORREÇÃO: Adicionado gestor_id=:gid na query SQL
        sql_update = text("""
            UPDATE dbo.nps_empresas 
            SET nome=:n, segmento=:s, valor_contrato=:v, gestor=:g, gestor_id=:gid, companhia_id=:cid 
            WHERE id=:id
        """)
        
        conn.execute(sql_update, {
            "n": emp.nome, 
            "s": emp.segmento, 
            "v": emp.valor_contrato, 
            "g": emp.gestor, 
            "gid": emp.gestor_id, # 👈 O ID agora é guardado!
            "cid": emp.companhia_id, 
            "id": empresa_id
        })
        
        if nome_antigo and str(nome_antigo) != str(emp.nome):
            conn.execute(text("UPDATE dbo.nps_clientes SET empresa=:novo WHERE empresa=:antigo"), 
                         {"novo": emp.nome, "antigo": nome_antigo})
            
    return {"status": "success"}
        
# ==========================================
# 🗑️ EXCLUIR CADASTROS (DELETE)
# ==========================================

@app.delete("/api/cadastros/empresas/{empresa_id}")
def delete_empresa(empresa_id: int):
    try:
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("DELETE FROM dbo.nps_empresas WHERE id = :id"), {"id": empresa_id})
            conn.commit()
            return {"message": "Empresa removida com sucesso"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ==========================================
# 👤 ROTAS: CLIENTES
# ==========================================

def auto_cadastrar_referencias(cargo: str, empresa: str, perfil_decisor: str, gestor: str = None):
    engine = get_engine()
    with engine.begin() as conn:
        if cargo and cargo.strip():
            conn.execute(text("IF NOT EXISTS (SELECT 1 FROM dbo.nps_cargos WHERE nome = :nome) BEGIN INSERT INTO dbo.nps_cargos (nome) VALUES (:nome) END"), {"nome": cargo.strip()})
        if empresa and empresa.strip():
            conn.execute(text("IF NOT EXISTS (SELECT 1 FROM dbo.nps_empresas WHERE nome = :nome) BEGIN INSERT INTO dbo.nps_empresas (nome, segmento, valor_contrato) VALUES (:nome, '', 0) END"), {"nome": empresa.strip()})
        if perfil_decisor and perfil_decisor.strip():
            conn.execute(text("IF NOT EXISTS (SELECT 1 FROM dbo.nps_perfis WHERE nome = :nome) BEGIN INSERT INTO dbo.nps_perfis (nome) VALUES (:nome) END"), {"nome": perfil_decisor.strip()})
        if gestor and gestor.strip():
            conn.execute(text("IF NOT EXISTS (SELECT 1 FROM dbo.nps_gestores WHERE nome = :nome) BEGIN INSERT INTO dbo.nps_gestores (nome, papel, email) VALUES (:nome, '', '') END"), {"nome": gestor.strip()})

@app.get("/api/clientes")
def list_clientes(
    q: str = "", 
    ativo: str = "Ativos", 
    perfil: str = "Todos", 
    topn: int = 100000,
    _t: str = None,
    usuario_email: str = Depends(get_current_user)
):
    try:
        engine = get_engine()
        
        df = clientes_svc.load_clientes(q, ativo, perfil, topn)
        return df.fillna("").to_dict(orient="records")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/clientes/{cliente_id}/status")
def change_cliente_status(cliente_id: str, payload: StatusUpdate):
    try:
        clientes_svc.set_ativo(cliente_id, payload.ativo)
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/clientes/{cliente_id}/forcar-envio")
def forcar_envio_nps(cliente_id: str, background_tasks: BackgroundTasks, usuario: str = Depends(get_current_user)):
    try:
        from services.email_svc import disparar_convite_nps_especifico
        background_tasks.add_task(disparar_convite_nps_especifico, [cliente_id])
        
        return {
            "status": "success", 
            "message": "Solicitação recebida! O e-mail está a ser despachado agora mesmo."
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail="Não conseguimos processar o envio manual. Tente novamente em instantes.")

@app.post("/api/clientes/forcar-envio-lote")
def forcar_envio_lote(payload: LoteEnvio, background_tasks: BackgroundTasks, usuario: str = Depends(get_current_user)):
    try:
        from services.email_svc import disparar_convite_nps_especifico
        background_tasks.add_task(disparar_convite_nps_especifico, payload.cliente_ids)
        
        return {
            "status": "success", 
            "message": f"O motor de disparos iniciou o processamento de {len(payload.cliente_ids)} e-mails com sucesso."
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail="Ocorreu um erro ao tentar processar o lote de envios.")

@app.delete("/api/clientes/{cliente_id}")
def delete_cliente_route(cliente_id: str, delete_respostas: bool = True): 
    try:
        ok, msg = clientes_svc.delete_cliente(cliente_id, delete_respostas)
        return {"status": "success", "message": msg}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/clientes")
def create_cliente_route(payload: ClienteCreate):
    try:
        auto_cadastrar_referencias(payload.cargo, payload.empresa, payload.perfil_decisor, payload.gestor)
        novo_id = clientes_svc.insert_cliente(
            payload.nome, payload.email, payload.telefone, 
            payload.empresa, payload.perfil_decisor, payload.segmento, payload.cargo, payload.gestor
        )
        return {"status": "success", "cliente_id": novo_id, "message": "Cliente cadastrado!"}
    except Exception as e:
        if "2627" in str(e) or "2601" in str(e):
            raise HTTPException(status_code=400, detail="Já existe cliente com este e-mail.")
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/api/clientes/{cliente_id}")
def update_cliente_route(cliente_id: str, payload: ClienteUpdate):
    try:
        clientes_svc.update_cliente(
            cliente_id, 
            payload.nome, 
            payload.email, 
            payload.telefone, 
            payload.empresa, 
            payload.perfil_decisor, 
            payload.segmento, 
            payload.cargo,
            payload.ativo 
        )
        return {"status": "success", "message": "Cliente atualizado."}
        
    except IntegrityError as e:
        error_msg = str(e)
        if "UQ_nps_clientes_email" in error_msg or "duplicate key" in error_msg.lower():
            raise HTTPException(
                status_code=400, 
                detail="Este e-mail já está registado para outro cliente. Utilize um e-mail diferente."
            )
        raise HTTPException(status_code=400, detail="Erro de restrição no banco de dados.")
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
    
# ==========================================
# 🛑 ROTAS PARA ATIVAR / INATIVAR PESSOAS E EMPRESAS
# ==========================================

@app.put("/api/clientes/{cliente_id}/status")
def alterar_status_cliente(cliente_id: str, payload: dict):
    try:
        # Pega o valor (True/False ou 1/0) e converte para Inteiro do SQL (1 ou 0)
        ativo = 1 if payload.get("ativo") else 0
        engine = get_engine()
        with engine.begin() as conn:
            conn.execute(text("""
                UPDATE dbo.nps_clientes 
                SET ativo = :a, updated_at = CURRENT_TIMESTAMP 
                WHERE cliente_id = :id
            """), {"a": ativo, "id": cliente_id})
        return {"status": "success", "message": "Status atualizado."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/api/empresas/{empresa_id}/status")
def alterar_status_empresa(empresa_id: int, payload: dict):
    try:
        ativo = 1 if payload.get("ativo") else 0
        engine = get_engine()
        with engine.begin() as conn:
            conn.execute(text("""
                UPDATE dbo.nps_empresas 
                SET ativo = :a 
                WHERE id = :id
            """), {"a": ativo, "id": empresa_id})
        return {"status": "success", "message": "Status atualizado."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ==========================================
# 📋 LISTAR E ATUALIZAR FEEDBACKS (Respostas)
# ==========================================
@app.get("/api/respostas")
async def listar_respostas(
    q: str = "",
    companhia: str = "Todas",
    empresa: str = "",
    categoria: str = "Todas",
    perfil: str = "Todos",
    incluir_excluidas: bool = False,
    topn: int = 100000
):
    try:
        from services import respostas_svc
        df = respostas_svc.load_respostas(q, companhia, empresa, categoria, perfil, incluir_excluidas, topn)
        return df.fillna("").to_dict(orient="records")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/api/respostas/{resposta_id}")
def update_resposta_route(resposta_id: str, payload: RespostaUpdate):
    try:
        from services import respostas_svc
        respostas_svc.update_resposta(
            resposta_id, payload.nota, payload.categoria, payload.motivo, 
            payload.canal, payload.expectativas, payload.o_que_faltava
        )
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ==========================================
# 🗂️ ARQUIVAR / DESARQUIVAR FEEDBACKS
# ==========================================
@app.post("/api/respostas/{resposta_id}/soft-delete")
async def soft_delete_resposta_route(resposta_id: str):
    try:
        engine = get_engine()
        with engine.begin() as conn:
            conn.execute(text("UPDATE dbo.nps_respostas SET excluido = 1 WHERE resposta_id = :id"), {"id": resposta_id})
        return {"status": "success", "detail": "Arquivado com sucesso"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/respostas/{resposta_id}/restore")
async def restore_resposta_route(resposta_id: str):
    try:
        engine = get_engine()
        with engine.begin() as conn:
            conn.execute(text("UPDATE dbo.nps_respostas SET excluido = 0 WHERE resposta_id = :id"), {"id": resposta_id})
        return {"status": "success", "detail": "Restaurado com sucesso"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
@app.post("/api/respostas/manual")
def inserir_resposta_manual(resp: RespostaManual, usuario_email: str = Depends(get_current_user)):
    try:
        engine = get_engine()
        with engine.begin() as conn:
            
            # 1. Obter o nome da empresa associada a este cliente
            sql_cliente = text("SELECT empresa FROM dbo.nps_clientes WHERE cliente_id = :cliente_id")
            resultado_cliente = conn.execute(sql_cliente, {"cliente_id": resp.cliente_id}).fetchone()
            
            if not resultado_cliente:
                raise HTTPException(status_code=404, detail="Cliente não encontrado.")
            
            empresa_nome = resultado_cliente.empresa

            # 2. Inserir a resposta na tabela principal
            sql_insert = text("""
                INSERT INTO dbo.nps_respostas 
                (cliente_id, empresa, nota, motivo, canal, data_resposta) 
                VALUES (:cliente_id, :empresa, :nota, :motivo, :canal, GETDATE())
            """)
            conn.execute(sql_insert, {
                "cliente_id": resp.cliente_id,
                "empresa": empresa_nome,
                "nota": resp.nota,
                "motivo": resp.motivo,
                "canal": resp.canal
            })

            # 3. INTERROMPER A RÉGUA DE LEMBRETES (Mudar status para Respondido)
            sql_update_disparo = text("""
                UPDATE dbo.nps_disparos 
                SET status = 'Respondido', data_resposta = GETDATE() 
                WHERE cliente_id = :cliente_id
            """)
            conn.execute(sql_update_disparo, {"cliente_id": resp.cliente_id})
            
            # Atualizar também na tabela de clientes por segurança
            sql_update_cliente = text("""
                UPDATE dbo.nps_clientes 
                SET status_envio = 'Respondido' 
                WHERE cliente_id = :cliente_id
            """)
            conn.execute(sql_update_cliente, {"cliente_id": resp.cliente_id})

            # 4. CRIAR AÇÃO AUTOMÁTICA SE FOR DETRATOR (Notas 0 a 6)
            if resp.nota <= 6:
                # Obter o ID da empresa para associar a ação
                sql_empresa_id = text("SELECT id FROM dbo.nps_empresas WHERE nome = :nome")
                res_emp = conn.execute(sql_empresa_id, {"nome": empresa_nome}).fetchone()
                
                if res_emp:
                    sql_acao = text("""
                        INSERT INTO dbo.nps_acoes (empresa_id, descricao, prioridade, status, data_criacao)
                        VALUES (:empresa_id, :descricao, 'Alta', 'Pendente', GETDATE())
                    """)
                    desc = f"Tratar Detrator (Nota {resp.nota}). Feedback inserido manualmente via {resp.canal}."
                    conn.execute(sql_acao, {"empresa_id": res_emp.id, "descricao": desc})

        return {"status": "success", "message": "Resposta inserida com sucesso!"}
    
    except Exception as e:
        print(f"Erro ao inserir resposta manual: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    
# ==========================================
# 🗑️ EXCLUSÃO DEFINITIVA DE FEEDBACKS (ADMIN)
# ==========================================
@app.delete("/api/respostas/{resposta_id}")
def excluir_resposta_definitiva(resposta_id: str, usuario = Depends(exigir_admin)):
    """Exclui permanentemente uma resposta do banco de dados (Apenas Admins)"""
    try:
        engine = get_engine()
        with engine.begin() as conn:
            check = conn.execute(text("SELECT resposta_id FROM dbo.nps_respostas WHERE resposta_id = :id"), {"id": resposta_id}).fetchone()
            if not check:
                raise HTTPException(status_code=404, detail="Resposta não encontrada.")
            
            conn.execute(text("DELETE FROM dbo.nps_acoes WHERE resposta_id = :id"), {"id": resposta_id})
                
            conn.execute(text("DELETE FROM dbo.nps_respostas WHERE resposta_id = :id"), {"id": resposta_id})
            
        return {"status": "success", "message": "Feedback e ações vinculadas foram excluídos permanentemente."}
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        print(f"Erro ao excluir resposta: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail="Erro interno ao excluir a resposta.")

# ==========================================
# 📥 ROTAS: IMPORTAÇÃO
# ==========================================

@app.post("/api/importar/preview")
async def preview_importacao(file: UploadFile = File(...)):
    try:
        contents = await file.read()
        
        if file.filename.lower().endswith(('.xlsx', '.xls')):
            df = pd.read_excel(io.BytesIO(contents))
        else:
            try:
                conteudo_texto = contents.decode('utf-8-sig').strip()
            except UnicodeDecodeError:
                conteudo_texto = contents.decode('latin1').strip()
                
            if not conteudo_texto:
                raise ValueError("O ficheiro está vazio ou só contém linhas em branco.")

            df = pd.read_csv(io.StringIO(conteudo_texto), sep=None, engine='python')
        
        df.columns = df.columns.str.strip().str.lower()
        
        df = df.fillna("")
        for col in df.select_dtypes(include=['datetime64', 'datetimetz']).columns:
            df[col] = df[col].astype(str)

        dados = df.to_dict(orient='records')
        return dados
        
    except Exception as e:
        print(f"🚨 ERRO REAL NO PYTHON: {str(e)}")
        raise HTTPException(status_code=400, detail=f"Erro ao ler arquivo: {str(e)}")


# A NOVA ROTA UNIFICADA E ROBUSTA QUE O FRONTEND ESTÁ CHAMANDO
@app.post("/api/importar/processar")
async def processar_importacao(payload: dict):
    tipo = payload.get("tipo")
    dados = payload.get("dados", [])
    chaves_cliente = payload.get("chaves_cliente", [])
    chaves_resposta = payload.get("chaves_resposta", [])
    
    # 👇 1. CAPTURAMOS A COMPANHIA SELECIONADA NO FRONTEND
    configuracao = payload.get("configuracao", {})
    overwrite = configuracao.get("overwrite", True)
    companhia_id_selecionada = configuracao.get("companhia_id") # Pode ser None

    if not dados:
        raise HTTPException(status_code=400, detail="Nenhum dado válido recebido.")
    if not chaves_cliente:
        raise HTTPException(status_code=400, detail="Defina pelo menos uma chave para identificar o cliente.")

    engine = get_engine()
    inserted_count = 0
    updated_count = 0
    ignored_count = 0

    detalhes_erros = [] 

    try:
        with engine.begin() as conn:
            
            # ==========================================
            # 🏢 1. GARANTIR EMPRESAS COM A COMPANHIA DO DROPDOWN
            # ==========================================
            empresas_unicas = set()
            cargos_unicos = set()
            segmentos_unicos = set()

            for row in dados:
                emp_nome = str(row.get("empresa", "")).strip()
                if emp_nome: 
                    empresas_unicas.add(emp_nome)
                
                if tipo == 'clientes':
                    cargo_nome = str(row.get("cargo", "")).strip()
                    if cargo_nome: cargos_unicos.add(cargo_nome)
                    
                    seg_nome = str(row.get("segmento", "")).strip()
                    if seg_nome: segmentos_unicos.add(seg_nome)
            
            # 1.1 Insere ou Atualiza as EMPRESAS
            for emp_nome in empresas_unicas:
                check_emp_sql = text("SELECT id FROM dbo.nps_empresas WHERE nome = :nome")
                emp_existente = conn.execute(check_emp_sql, {"nome": emp_nome}).fetchone()

                if not emp_existente:
                    # Cria a empresa nova já com a Companhia selecionada no UI (mesmo que seja None)
                    conn.execute(text("""
                        INSERT INTO dbo.nps_empresas (nome, companhia_id, created_at) 
                        VALUES (:nome, :comp_id, CURRENT_TIMESTAMP)
                    """), {"nome": emp_nome, "comp_id": companhia_id_selecionada})
                else:
                    # Se a empresa já existe e o utilizador escolheu uma Companhia no UI, atualiza o vínculo
                    if overwrite and companhia_id_selecionada:
                        conn.execute(text("""
                            UPDATE dbo.nps_empresas 
                            SET companhia_id = :comp_id 
                            WHERE id = :id
                        """), {"comp_id": companhia_id_selecionada, "id": emp_existente.id})

            # 1.2 Verifica e insere os CARGOS
            for cargo_nome in cargos_unicos:
                check_cargo_sql = text("SELECT id FROM dbo.nps_cargos WHERE nome = :nome")
                if not conn.execute(check_cargo_sql, {"nome": cargo_nome}).fetchone():
                    conn.execute(text("INSERT INTO dbo.nps_cargos (nome) VALUES (:nome)"), {"nome": cargo_nome})

            # 1.3 Verifica e insere os SEGMENTOS
            for seg_nome in segmentos_unicos:
                check_seg_sql = text("SELECT id FROM dbo.nps_segmentos WHERE nome = :nome")
                if not conn.execute(check_seg_sql, {"nome": seg_nome}).fetchone():
                    conn.execute(text("INSERT INTO dbo.nps_segmentos (nome) VALUES (:nome)"), {"nome": seg_nome})
            
            # ==========================================
            # 🧑‍💼 2. IMPORTAÇÃO DE BASE DE CLIENTES
            # ==========================================
            if tipo == 'clientes':
                mapa_db = {
                    "e-mail": "email", "email_cliente": "email",
                    "cliente_id": "cliente_id", "id_cliente": "cliente_id",
                    "perfil": "perfil_decisor"
                }

                for c in dados:
                    where_clauses = []
                    params_busca = {}
                    has_null = False

                    for idx, col_arq in enumerate(chaves_cliente):
                        val = str(c.get(col_arq, "")).strip()
                        if not val:
                            has_null = True
                            break
                        col_db = mapa_db.get(col_arq.lower(), col_arq.lower())
                        param_name = f"c_param_{idx}"
                        where_clauses.append(f"{col_db} = :{param_name}")
                        params_busca[param_name] = val

                    if has_null or not where_clauses:
                        ignored_count += 1
                        continue

                    where_sql = " AND ".join(where_clauses)
                    check_query = text(f"SELECT cliente_id FROM dbo.nps_clientes WHERE {where_sql}")
                    existente = conn.execute(check_query, params_busca).fetchone()

                    email = str(c.get("email", c.get("e-mail", c.get("email_cliente", "")))).strip().lower()
                    
                    raw_dt_envio = str(c.get("ultimo_envio", c.get("data_ultimo_envio", ""))).strip()
                    dt_envio = None
                    if raw_dt_envio and raw_dt_envio.lower() not in ['nan', 'nat', 'none', 'null', '']:
                        dt_envio = raw_dt_envio
                    
                    raw_ativo = str(c.get("ativo", "True")).strip().lower()
                    status_ativo = 0 if raw_ativo in ['false', '0', 'falso', 'nao', 'não', 'f'] else 1

                    params_save = {
                        "nome": str(c.get("nome", "")).strip(),
                        "email": email,
                        "empresa": str(c.get("empresa", "")).strip(),
                        "cargo": str(c.get("cargo", "")).strip(),
                        "perfil": str(c.get("perfil_decisor", c.get("perfil", "Decisor"))).strip(),
                        "segmento": str(c.get("segmento", "")).strip(),
                        "ultimo_envio": dt_envio,
                        "ativo": status_ativo 
                    }

                    if existente:
                        if overwrite:
                            params_save["cid"] = existente.cliente_id
                            
                            update_sql = text("""
                                UPDATE dbo.nps_clientes 
                                SET nome = COALESCE(NULLIF(:nome, ''), nome), 
                                    email = COALESCE(NULLIF(:email, ''), email),
                                    empresa = COALESCE(NULLIF(:empresa, ''), empresa), 
                                    cargo = COALESCE(NULLIF(:cargo, ''), cargo),
                                    perfil_decisor = COALESCE(NULLIF(:perfil, ''), perfil_decisor), 
                                    segmento = COALESCE(NULLIF(:segmento, ''), segmento),
                                    ativo = :ativo,
                                    ultimo_envio = COALESCE(:ultimo_envio, ultimo_envio),
                                    updated_at = CURRENT_TIMESTAMP
                                WHERE cliente_id = :cid
                            """)
                            conn.execute(update_sql, params_save)
                            updated_count += 1
                        else:
                            ignored_count += 1
                    else:
                        params_save["cliente_id"] = str(random.randint(100000000, 999999999))
                        
                        insert_sql = text("""
                            INSERT INTO dbo.nps_clientes (
                                cliente_id, nome, email, empresa, cargo, 
                                perfil_decisor, segmento, ativo, ultimo_envio
                            )
                            VALUES (
                                :cliente_id, :nome, :email, :empresa, :cargo, 
                                :perfil, :segmento, :ativo, :ultimo_envio
                            )
                        """)
                        conn.execute(insert_sql, params_save)
                        inserted_count += 1

            # ==========================================
            # 📊 3. IMPORTAÇÃO DE HISTÓRICO DE RESPOSTAS
            # ==========================================
            elif tipo == 'respostas':
                
                mapa_clientes = {
                    "email_cliente": "email", "email": "email", "e-mail": "email",
                    "cliente_id": "cliente_id", "id_cliente": "cliente_id",
                    "empresa": "empresa", "nome": "nome"
                }
                mapa_respostas = {
                    "data_resposta": "CAST(data_resposta AS DATE)",
                    "data": "CAST(data_resposta AS DATE)",
                    "resposta_id": "resposta_id", "id_resposta": "resposta_id"
                }

                for r in dados:
                    email_atual = str(r.get("email", r.get("e-mail", "Desconhecido")))
                    
                    # 👇 1. CAPTURAR A EMPRESA E O SEU ID (Para o Dashboard funcionar)
                    emp_nome = str(r.get("empresa", "")).strip()
                    empresa_id_banco = None
                    if emp_nome:
                        # Busca o ID da empresa que foi inserida/atualizada com a Companhia no passo 1
                        emp_db = conn.execute(text("SELECT id FROM dbo.nps_empresas WHERE nome = :n"), {"n": emp_nome}).fetchone()
                        if emp_db:
                            empresa_id_banco = emp_db.id
                    
                    nota_str = str(r.get("nota", "")).strip()
                    try:
                        nota = int(nota_str)
                    except ValueError:
                        ignored_count += 1
                        detalhes_erros.append({"email": email_atual, "motivo": f"Nota inválida ou vazia: '{nota_str}'"})
                        continue

                    if nota >= 9: categoria_nps = "Promotor"
                    elif nota >= 7: categoria_nps = "Neutro"
                    else: categoria_nps = "Detrator"

                    where_clauses = []
                    params_cliente = {}
                    has_null = False

                    for idx, col_arq in enumerate(chaves_cliente):
                        val = str(r.get(col_arq, "")).strip()
                        if not val:
                            has_null = True
                            break
                        col_db = mapa_clientes.get(col_arq.lower(), col_arq.lower())
                        param_name = f"c_param_{idx}"
                        where_clauses.append(f"{col_db} = :{param_name}")
                        params_cliente[param_name] = val
                        
                    if has_null or not where_clauses:
                        ignored_count += 1
                        detalhes_erros.append({"email": email_atual, "motivo": "Coluna de identificação do cliente está vazia."})
                        continue
                        
                    where_sql = " AND ".join(where_clauses)
                    check_query = text(f"SELECT cliente_id FROM dbo.nps_clientes WHERE {where_sql}")
                    cliente_existente = conn.execute(check_query, params_cliente).fetchone()

                    if not cliente_existente:
                        ignored_count += 1 
                        detalhes_erros.append({"email": email_atual, "motivo": "Pessoa não encontrada na base de clientes."})
                        continue
                    
                    cliente_id = cliente_existente.cliente_id
                    
                    resposta_existente_id = None
                    if chaves_resposta:
                        where_resp = ["cliente_id = :cid"]
                        params_resp = {"cid": cliente_id}
                        has_null_resp = False
                        
                        for idx, col_arq in enumerate(chaves_resposta):
                            val = str(r.get(col_arq, "")).strip()
                            if not val:
                                has_null_resp = True
                                break
                            col_db = mapa_respostas.get(col_arq.lower(), col_arq.lower())
                            param_name = f"r_param_{idx}"
                            
                            if "DATE" in col_db:
                                where_resp.append(f"{col_db} = CAST(:{param_name} AS DATE)")
                                params_resp[param_name] = val[:10] 
                            else:
                                where_resp.append(f"{col_db} = :{param_name}")
                                params_resp[param_name] = val
                                
                        if not has_null_resp:
                            resp_sql = " AND ".join(where_resp)
                            check_resp_query = text(f"SELECT resposta_id FROM dbo.nps_respostas WHERE {resp_sql}")
                            resp_existente = conn.execute(check_resp_query, params_resp).fetchone()
                            if resp_existente:
                                resposta_existente_id = resp_existente.resposta_id

                    dt_resposta = r.get("data_resposta")
                    if not dt_resposta or str(dt_resposta).strip() == "":
                        dt_resposta = None
                    motivo = str(r.get("comentario", r.get("motivo", ""))).strip()

                    # 👇 2. ATUALIZAR SQL PARA GRAVAR 'empresa' E 'empresa_id'
                    if resposta_existente_id:
                        if overwrite:
                            update_sql = text("""
                                UPDATE dbo.nps_respostas
                                SET nota = :nota, motivo = :motivo, categoria = :categoria,
                                    data_resposta = COALESCE(:dt_resp, data_resposta),
                                    empresa = COALESCE(NULLIF(:empresa, ''), empresa),
                                    empresa_id = COALESCE(:empresa_id, empresa_id),
                                    excluido = 0
                                WHERE resposta_id = :rid
                            """)
                            conn.execute(update_sql, {
                                "nota": nota, "motivo": motivo, "categoria": categoria_nps,
                                "dt_resp": dt_resposta, 
                                "empresa": emp_nome, "empresa_id": empresa_id_banco, # 👈 Vínculos
                                "rid": resposta_existente_id
                            })
                            updated_count += 1
                        else:
                            ignored_count += 1
                            detalhes_erros.append({"email": email_atual, "motivo": "Resposta já existe e overwrite=False."})
                    else:
                        resposta_id = str(random.randint(100000000, 999999999))
                        insert_sql = text("""
                            INSERT INTO dbo.nps_respostas (
                                resposta_id, cliente_id, nota, motivo, categoria, 
                                canal, excluido, data_resposta, created_at,
                                empresa, empresa_id
                            )
                            VALUES (
                                :rid, :cid, :nota, :motivo, :categoria, 
                                'Importacao_Manual', 0, :dt_resp, SYSUTCDATETIME(),
                                :empresa, :empresa_id
                            )
                        """)
                        conn.execute(insert_sql, {
                            "rid": resposta_id, "cid": cliente_id, "nota": nota,
                            "motivo": motivo, "categoria": categoria_nps, "dt_resp": dt_resposta,
                            "empresa": emp_nome, "empresa_id": empresa_id_banco # 👈 Vínculos
                        })
                        inserted_count += 1

        return {
            "status": "success", 
            "inseridos": inserted_count + updated_count,
            "erros": ignored_count,
            "detalhes": detalhes_erros
        }

    except Exception as e:
        import traceback
        print(f"🔥 Erro na importação: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))
    
@app.delete("/api/admin/limpar-dados")
def limpar_dados_em_massa(tipo: str, usuario = Depends(get_current_user)):
    try:
        engine = get_engine()
        with engine.begin() as conn:
            if tipo == 'respostas':
                # Apaga apenas as respostas (mantém os clientes e empresas intactos)
                conn.execute(text("DELETE FROM dbo.nps_respostas"))
                msg = "Todas as respostas (NPS) foram apagadas com sucesso."
                
            elif tipo == 'clientes':
                # Para apagar clientes, OBRIGATORIAMENTE temos de apagar as respostas deles primeiro
                conn.execute(text("DELETE FROM dbo.nps_respostas"))
                conn.execute(text("DELETE FROM dbo.nps_clientes"))
                msg = "Todos os clientes e respostas foram apagados com sucesso."
                
            elif tipo == 'empresas':
                # 👇 NOVA OPÇÃO: Para apagar empresas, apagamos a cadeia inteira
                conn.execute(text("DELETE FROM dbo.nps_respostas"))
                conn.execute(text("DELETE FROM dbo.nps_clientes"))
                conn.execute(text("DELETE FROM dbo.nps_empresas"))
                msg = "Toda a base (Empresas, Clientes e Respostas) foi limpa com sucesso."
                
            else:
                raise HTTPException(status_code=400, detail="Comando de limpeza inválido.")
                
        return {"status": "success", "message": msg}
        
    except Exception as e:
        import traceback
        error_msg = str(e)
        print(f"Erro ao limpar banco: {traceback.format_exc()}")
        
        if "REFERENCE constraint" in error_msg or "FOREIGN KEY" in error_msg:
            raise HTTPException(
                status_code=400, 
                detail="Bloqueio de segurança: Ainda existem dados vinculados a estas empresas."
            )
            
        raise HTTPException(status_code=500, detail=f"Erro interno ao limpar dados: {error_msg}")

# ==========================================
# 🔂 ROTAS: STATUS DE CONEXÃO
# ==========================================

@app.get("/api/status")
def check_status():
    try:
        engine = get_engine()
        with engine.connect() as conn:
            from sqlalchemy import text
            conn.execute(text("SELECT 1"))
        return {"banco_online": True, "api_status": "OK"}
    except Exception as e:
        return {"banco_online": False, "api_status": "ERROR", "detalhe": str(e)}
    
# ==========================================
# 🔂 ROTAS: CONFIGURAÇÃO
# ==========================================

@app.get("/api/settings/mostrar-sem-cliente")
def get_setting_mostrar():
    engine = get_engine()
    with engine.connect() as conn:
        res = conn.execute(text("SELECT valor FROM dbo.nps_configuracoes WHERE chave = 'mostrar_sem_cliente'")).scalar()
        return {"valor": res == 'true'}

@app.post("/api/settings/mostrar-sem-cliente")
def update_setting_mostrar(payload: SettingUpdate):
    engine = get_engine()
    with engine.connect() as conn:
        val_str = 'true' if payload.valor else 'false'
        conn.execute(text("UPDATE dbo.nps_configuracoes SET valor = :v WHERE chave = 'mostrar_sem_cliente'"), {"v": val_str})
        conn.commit()
        return {"status": "success"}

def obter_tipo_join():
    engine = get_engine()
    with engine.connect() as conn:
        res = conn.execute(text("SELECT valor FROM dbo.nps_configuracoes WHERE chave = 'mostrar_sem_cliente'")).scalar()
        return "LEFT JOIN" if res == 'true' else "INNER JOIN"
    
# ==========================================
# 🚀 ROTAS DE USUÁRIOS
# ==========================================
    
@app.put("/api/usuarios/{usuario_id}")
async def atualizar_usuario(usuario_id: str, data: dict):
    try:
        engine = get_engine()
        ativo_status = data.get("ativo") 

        with engine.begin() as conn:
            query = text("""
                UPDATE dbo.nps_usuarios 
                SET nome = :nome, 
                    email = :email, 
                    cargo = :cargo, 
                    ativo = :ativo,
                    tipo = :tipo 
                WHERE usuario_id = :id
            """)
            conn.execute(query, {
                "nome": data.get("nome"),
                "email": data.get("email"),
                "cargo": data.get("cargo"),
                "ativo": str(data.get("ativo")),
                "tipo": data.get("tipo", "Usuário"), # 💡 NOVA LINHA
                "id": usuario_id
            })

            password = data.get("password")
            if password and password.strip():
                senha_hash = hash_password(password)
                conn.execute(
                    text("UPDATE dbo.nps_usuarios SET senha_hash = :h WHERE usuario_id = :id"),
                    {"h": senha_hash, "id": usuario_id}
                )

        return {"mensagem": "Utilizador atualizado com sucesso"}
    except Exception as e:
        print(f"Erro ao desativar: {e}")
        raise HTTPException(status_code=500, detail="Erro ao atualizar status no banco")


# ==========================================
# 🚀 ROTAS DE CONFIGURAÇÕES DE E-MAIL
# ==========================================

@app.get("/api/config/email")
async def buscar_config_email():
    try:
        engine = get_engine()
        with engine.connect() as conn:
            # 1. Busca as credenciais de e-mail
            query = text("SELECT TOP 1 * FROM dbo.nps_configuracoes_email")
            res = conn.execute(query).fetchone()
            
            dados = dict(res._mapping) if res else {}
            
            # 2. Busca o estado da Chave Mestra (Kill Switch)
            query_ks = text("SELECT valor FROM dbo.nps_configuracoes WHERE chave = 'envios_ativos'")
            res_ks = conn.execute(query_ks).scalar()
            
            # Se a chave existir, converte para booleano. Se não existir, assume True (Ligado).
            if res_ks is not None:
                dados["envios_ativos"] = str(res_ks).lower() == 'true'
            else:
                dados["envios_ativos"] = True
                
            return dados
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/config/email")
async def salvar_config_email(config: ConfigEmailSchema):
    engine = get_engine()
    # Usamos begin() para garantir que tudo salva junto (Transação)
    with engine.begin() as conn: 
        # 1. Limpamos e inserimos as credenciais de e-mail (Microsoft)
        conn.execute(text("DELETE FROM dbo.nps_configuracoes_email"))
        query_email = text("""
            INSERT INTO dbo.nps_configuracoes_email 
            (tenant_id, client_id, client_secret, email_remetente, base_url_frontend)
            VALUES (:t, :c, :s, :e, :b)
        """)
        conn.execute(query_email, {
            "t": config.tenant_id, 
            "c": config.client_id, 
            "s": config.client_secret, 
            "e": config.email_remetente,
            "b": config.base_url_frontend 
        })
        
        # 2. Salva o status do Botão de Pânico na tabela global (como texto 'true' ou 'false')
        valor_kill_switch = 'true' if config.envios_ativos else 'false'
        query_ks = text("""
            IF EXISTS (SELECT 1 FROM dbo.nps_configuracoes WHERE chave = 'envios_ativos')
                UPDATE dbo.nps_configuracoes SET valor = :v WHERE chave = 'envios_ativos'
            ELSE
                INSERT INTO dbo.nps_configuracoes (chave, valor) VALUES ('envios_ativos', :v)
        """)
        conn.execute(query_ks, {"v": valor_kill_switch})

    return {"status": "sucesso"}

@app.post("/api/config/email/autorizar")
async def autorizar_microsoft(requisicao: AutorizarEmailRequest):
    engine = get_engine()
    with engine.connect() as conn:
        config = conn.execute(text("""
            SELECT TOP 1 tenant_id, client_id, client_secret, base_url_frontend 
            FROM dbo.nps_configuracoes_email
        """)).fetchone()
        
        if not config:
            raise HTTPException(status_code=400, detail="Configurações não encontradas no banco.")

        # 🟢 SINCRONIZAÇÃO: O Backend deve gerar a MESMA URI que o Frontend gerou
        base_url = config.base_url_frontend.strip().rstrip('/')
        redirect_uri = f"{base_url}/configuracoes"

        url = f"https://login.microsoftonline.com/{config.tenant_id}/oauth2/v2.0/token"
        
        payload = {
            'client_id': config.client_id,
            'client_secret': config.client_secret, # Certifique-se que este é o VALOR e não o ID
            'code': requisicao.code,
            'grant_type': 'authorization_code',
            'redirect_uri': redirect_uri, 
            'scope': 'offline_access mail.send'
        }
                
        # Chamada para a Microsoft
        res_raw = requests.post(url, data=payload)
        res = res_raw.json()

        # Se a Microsoft devolver erro, o log dirá exatamente porquê (Ex: invalid_client)
        if "refresh_token" not in res:
            print(f"❌ Erro Microsoft: {res}") 
            raise HTTPException(status_code=400, detail=res.get("error_description", "Falha no token"))

        conn.execute(text("UPDATE dbo.nps_configuracoes_email SET refresh_token = :rt, atualizado_em = GETDATE()"), 
                     {"rt": res["refresh_token"]})
        conn.commit()
        
    return {"status": "conectado"}

@app.post("/api/config/email/teste")
async def testar_envio_email(usuario_email: str = Depends(get_current_user)):
    try:
        from services.email_svc import enviar_email_teste
        
        FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173")
        link_teste = f"{FRONTEND_URL}/configuracoes"
        
        ok = enviar_email_teste(usuario_email)
        
        if ok:
            return {"status": "success", "message": "E-mail de teste enviado!"}
        else:
            raise HTTPException(status_code=500, detail="O motor de envio devolveu falha. Verifique o terminal do Python.")
            
    except Exception as e:
        print(f"❌ ERRO NO TESTE DE ENVIO: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
    
@app.get("/api/config/nps/elegiveis")
def contar_elegiveis_nps():
    """Conta quantos clientes estão prontos para receber o NPS hoje"""
    try:
        engine = get_engine()
        with engine.connect() as conn:
            sql = text("""
                SELECT c.cliente_id, c.nome, c.email
                FROM dbo.nps_clientes c
                LEFT JOIN dbo.nps_disparos d ON c.cliente_id = d.cliente_id
                WHERE c.ativo = 1 
                AND (
                    -- 1. Clientes que NUNCA receberam a pesquisa
                    COALESCE(d.data_ultimo_lembrete, d.data_envio_inicial, c.ultimo_envio) IS NULL 
                    
                    OR 
                    
                    -- 2. Clientes cuja data de carência (recorrencia_dias) já foi ultrapassada!
                    GETDATE() >= DATEADD(day, 
                        ISNULL((SELECT TOP 1 TRY_CAST(valor AS INT) FROM dbo.nps_configuracoes WHERE chave = 'recorrencia_dias'), 90), 
                        COALESCE(d.data_ultimo_lembrete, d.data_envio_inicial, c.ultimo_envio)
                    )
                )
            """)
            total = conn.execute(sql).scalar()
        return {"total": total}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/config/nps/forcar-disparo")
def forcar_disparo_nps(background_tasks: BackgroundTasks):
    """Inicia a rotina de disparo imediatamente em segundo plano"""
    try:
        from services.email_svc import processar_disparos_nps
        # Adiciona a tarefa ao background para responder ao Frontend imediatamente
        background_tasks.add_task(processar_disparos_nps)
        return {"status": "success", "message": "Disparo iniciado com sucesso! A enviar em segundo plano."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ==========================================
# 🖼️ GESTOR DE IMAGENS (E-MAIL TEMPLATES)
# ==========================================

# 1. Garante que a pasta "uploads" existe fisicamente no servidor
os.makedirs("uploads", exist_ok=True)

# 2. Transforma a pasta "uploads" numa pasta pública, para que os e-mails consigam aceder
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

@app.post("/api/upload-imagem")
async def upload_imagem_email(file: UploadFile = File(...), request: Request = None):
    try:
        # Salva o ficheiro na pasta local
        file_location = f"uploads/{file.filename}"
        with open(file_location, "wb+") as file_object:
            shutil.copyfileobj(file.file, file_object)
        
        # Gera a URL completa pública baseada no domínio do seu backend
        base_url = str(request.base_url).rstrip("/")
        file_url = f"{base_url}/uploads/{file.filename}"
        
        return {"nome": file.filename, "url": file_url}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
        
@app.get("/api/config/imagens")
def listar_imagens(request: Request):
    """Devolve a lista de todas as imagens já hospedadas no servidor"""
    try:
        base_url = str(request.base_url).rstrip("/")
        imagens = []
        if os.path.exists("uploads"):
            for filename in os.listdir("uploads"):
                if filename.lower().endswith(('.png', '.jpg', '.jpeg', '.gif')):
                    imagens.append({
                        "nome": filename,
                        "url": f"{base_url}/uploads/{filename}"
                    })
        # Ordena para as mais recentes aparecerem primeiro
        imagens.sort(key=lambda x: os.path.getmtime(f"uploads/{x['nome']}"), reverse=True)
        return imagens
    except Exception as e:
        return []

@app.delete("/api/config/imagens/{nome_arquivo}")
def remover_imagem(nome_arquivo: str):
    try:
        file_path = f"uploads/{nome_arquivo}"
        if os.path.exists(file_path):
            os.remove(file_path)
            return {"status": "success"}
        raise HTTPException(status_code=404, detail="Imagem não encontrada.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ==========================================
# 🚀 ROTAS DE SESSÕES DE USUÁRIOS
# ==========================================

# --- ROTA PARA LISTAR SESSÕES REAIS ---
@app.get("/api/usuarios/sessoes")
async def listar_sessoes(usuario_id: int): # Em produção, pegamos o ID do Token JWT
    try:
        engine = get_engine()
        with engine.connect() as conn:
            query = text("""
                SELECT id, dispositivo, ip_address as ip, localizacao as local, 
                       criado_em as data, revogado
                FROM dbo.nps_sessoes_ativas 
                WHERE usuario_id = :uid AND revogado = 0
                ORDER BY criado_em DESC
            """)
            res = conn.execute(query, {"uid": usuario_id}).fetchall()
            return [dict(r._mapping) for r in res]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# --- ROTA PARA REVOGAR (ENCERRAR) SESSÃO ---
@app.delete("/api/usuarios/sessoes/{sessao_id}")
async def encerrar_sessao(sessao_id: int):
    try:
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("UPDATE dbo.nps_sessoes_ativas SET revogado = 1 WHERE id = :sid"), {"sid": sessao_id})
            conn.commit()
            return {"detail": "Sessão encerrada"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
# ==========================================
# 🔒 ROTAS DE CONFIGURAÇÃO DE SEGURANÇA
# ==========================================

class SegurancaConfig(BaseModel):
    tempo_minutos: int

@app.get("/api/config/seguranca")
def obter_configuracoes_seguranca(usuario_email: str = Depends(get_current_user)):
    try:
        engine = get_engine()
        with engine.connect() as conn:
            # Vai buscar o tempo atual ao banco de dados
            query = text("SELECT valor FROM dbo.nps_configuracoes WHERE chave = 'sessao_expiracao_minutos'")
            resultado = conn.execute(query).scalar()
            
            # Se não encontrar ou houver erro, assume 60 minutos por segurança
            tempo = int(resultado) if resultado and str(resultado).isdigit() else 60
            
            return {"tempo_minutos": tempo}
            
    except Exception as e:
        print(f"Erro ao obter configuração de segurança: {e}")
        raise HTTPException(status_code=500, detail="Erro ao carregar configurações de segurança.")

@app.put("/api/config/seguranca")
def salvar_configuracoes_seguranca(payload: SegurancaConfig, usuario_email: str = Depends(get_current_user)):
    try:
        engine = get_engine()
        with engine.begin() as conn:
            conn.execute(
                text("""
                    UPDATE dbo.nps_configuracoes 
                    SET valor = :valor, updated_at = SYSUTCDATETIME() 
                    WHERE chave = 'sessao_expiracao_minutos'
                """),
                {"valor": str(payload.tempo_minutos)}
            )
            
        return {"status": "success", "message": "Tempo de sessão atualizado com sucesso!"}
        
    except Exception as e:
        print(f"Erro ao salvar configuração de segurança: {e}")
        raise HTTPException(status_code=500, detail="Erro ao guardar configurações de segurança.")

# ==========================================
# 🛠️ FUNÇÃO AUXILIAR: MONTADOR DE FILTROS SQL
# ==========================================
def build_bi_filters(periodo: str, segmento: str, arr: str, safra: str):
    where_clauses = ["r.excluido = 0"]
    params = {}

    # 1. PERÍODO (Baseado na data da resposta)
    if periodo == "Últimos 3 Meses":
        where_clauses.append("r.data_resposta >= DATEADD(month, -3, GETDATE())")
    elif periodo == "Últimos 6 Meses":
        where_clauses.append("r.data_resposta >= DATEADD(month, -6, GETDATE())")
    elif periodo == "Este Ano":
        where_clauses.append("YEAR(r.data_resposta) = YEAR(GETDATE())")

    # 2. SEGMENTO
    if segmento != "Todos":
        where_clauses.append("e.segmento = :segmento")
        params["segmento"] = segmento

    # 3. ARR (Receita)
    if arr == "> € 100k":
        where_clauses.append("e.valor_contrato > 100000")
    elif arr == "€ 50k - € 100k":
        where_clauses.append("e.valor_contrato BETWEEN 50000 AND 100000")
    elif arr == "< € 50k":
        where_clauses.append("e.valor_contrato < 50000")

    # 4. SAFRA / TEMPO DE CASA (Assumindo que a empresa tem coluna 'created_at')
    # Se a sua coluna se chamar 'data_criacao', altere abaixo:
    if safra == "0-3 Meses (Onboarding)":
        where_clauses.append("DATEDIFF(month, COALESCE(e.created_at, GETDATE()), GETDATE()) <= 3")
    elif safra == "3-12 Meses":
        where_clauses.append("DATEDIFF(month, COALESCE(e.created_at, GETDATE()), GETDATE()) > 3 AND DATEDIFF(month, COALESCE(e.created_at, GETDATE()), GETDATE()) <= 12")
    elif safra == "+1 Ano":
        where_clauses.append("DATEDIFF(month, COALESCE(e.created_at, GETDATE()), GETDATE()) > 12")

    where_sql = " AND ".join(where_clauses)
    return where_sql, params


# ==========================================
# 📊 LABORATÓRIO ANALÍTICO (BI ENGINE)
# ==========================================

# --- 2. ROTA DE PERFORMANCE DO GESTOR ---
@app.get("/api/reports/gestor")
def obter_performance_gestor(gestor_id: int, usuario_email: str = Depends(get_current_user)):
    try:
        engine = get_engine()
        with engine.connect() as conn:
            # 1. Indicadores Gerais com COALESCE para evitar NoneType
            sql_nps = text("""
                SELECT 
                    COUNT(r.resposta_id) as total,
                    COALESCE(SUM(CASE WHEN r.nota >= 9 THEN 1 ELSE 0 END), 0) as promotores,
                    COALESCE(SUM(CASE WHEN r.nota <= 6 THEN 1 ELSE 0 END), 0) as detratores
                FROM dbo.nps_respostas r
                INNER JOIN dbo.nps_clientes c ON r.cliente_id = c.cliente_id
                INNER JOIN dbo.nps_empresas e ON c.empresa = e.nome
                WHERE e.gestor_id = :gestor_id
            """)
            res = conn.execute(sql_nps, {"gestor_id": gestor_id}).mappings().first()
            
            total = res['total'] or 0
            promotores = res['promotores']
            detratores = res['detratores']
            neutros = total - (promotores + detratores)

            # 2. Cálculo do NPS seguro
            nps = 0
            if total > 0:
                nps = ((promotores - detratores) / total) * 100

            # 3. Ranking de empresas da carteira
            sql_empresas = text("""
                SELECT 
                    e.nome,
                    COALESCE(AVG(CAST(r.nota AS FLOAT)), 0) as media_nota,
                    COUNT(r.resposta_id) as qtd_respostas
                FROM dbo.nps_empresas e
                LEFT JOIN dbo.nps_respostas r ON e.nome = r.empresa
                WHERE e.gestor_id = :gestor_id
                GROUP BY e.nome
                ORDER BY media_nota DESC
            """)
            empresas_perf = conn.execute(sql_empresas, {"gestor_id": gestor_id}).mappings().all()

            return {
                "nps": round(nps, 1),
                "total_respostas": total,
                "distribuicao": {
                    "promotores": promotores,
                    "detratores": detratores,
                    "neutros": neutros
                },
                "ranking_empresas": [dict(row) for row in empresas_perf]
            }
    except Exception as e:
        print(f"Erro na performance do gestor: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# 1. MATRIZ DE PRIORIZAÇÃO (SCATTER CHART)
@app.get("/api/reports/bi-scatter")
async def get_bi_scatter(periodo: str = Query("Últimos 6 Meses"), segmento: str = Query("Todos"), arr: str = Query("Todos"), safra: str = Query("Todos")):
    try:
        where_sql, params = build_bi_filters(periodo, segmento, arr, safra)
        
        engine = get_engine()
        with engine.connect() as conn:
            # Fazemos o JOIN com a empresa para que os filtros de segmento e ARR funcionem
            sql = text(f"""
                SELECT 
                    COALESCE(r.categoria, 'Sem Classificação') as tema,
                    COUNT(r.resposta_id) as frequencia,
                    AVG(CAST(r.nota AS FLOAT)) as nota_media
                FROM dbo.nps_respostas r
                LEFT JOIN dbo.nps_empresas e ON r.empresa_id = e.id
                WHERE {where_sql} AND r.categoria IS NOT NULL
                GROUP BY r.categoria
                HAVING COUNT(r.resposta_id) > 1
            """)
            
            resultados = conn.execute(sql, params).mappings().all()
            
            scatter_data = [{"x": r['frequencia'], "y": round(r['nota_media'], 1), "r": 8, "tema": r['tema']} for r in resultados]
            return scatter_data
    except Exception as e:
        print(f"❌ Erro BI Scatter: {e}")
        return []

# 2. ANÁLISE DE SAFRA (STACKED BAR) - AGORA COM DADOS REAIS
@app.get("/api/reports/bi-safra")
async def get_bi_safra(periodo: str = Query("Últimos 6 Meses"), segmento: str = Query("Todos"), arr: str = Query("Todos"), safra: str = Query("Todos")):
    try:
        where_sql, params = build_bi_filters(periodo, segmento, arr, safra)
        
        engine = get_engine()
        with engine.connect() as conn:
            sql = text(f"""
                SELECT 
                    CASE 
                        WHEN DATEDIFF(month, e.created_at, GETDATE()) <= 3 THEN '0-3 Meses'
                        WHEN DATEDIFF(month, e.created_at, GETDATE()) <= 6 THEN '3-6 Meses'
                        WHEN DATEDIFF(month, e.created_at, GETDATE()) <= 12 THEN '6-12 Meses'
                        ELSE '+1 Ano'
                    END as safra_grupo,
                    SUM(CASE WHEN r.nota >= 9 THEN 1 ELSE 0 END) as promotores,
                    SUM(CASE WHEN r.nota BETWEEN 7 AND 8 THEN 1 ELSE 0 END) as neutros,
                    SUM(CASE WHEN r.nota <= 6 THEN 1 ELSE 0 END) as detratores
                FROM dbo.nps_respostas r
                INNER JOIN dbo.nps_empresas e ON r.empresa_id = e.id
                WHERE {where_sql}
                GROUP BY 
                    CASE 
                        WHEN DATEDIFF(month, e.created_at, GETDATE()) <= 3 THEN '0-3 Meses'
                        WHEN DATEDIFF(month, e.created_at, GETDATE()) <= 6 THEN '3-6 Meses'
                        WHEN DATEDIFF(month, e.created_at, GETDATE()) <= 12 THEN '6-12 Meses'
                        ELSE '+1 Ano'
                    END
            """)
            
            resultados = conn.execute(sql, params).mappings().all()
            
            # Estrutura base de retorno
            data = {
                "labels": ['0-3 Meses', '3-6 Meses', '6-12 Meses', '+1 Ano'],
                "promotores": [0, 0, 0, 0],
                "neutros": [0, 0, 0, 0],
                "detratores": [0, 0, 0, 0]
            }
            
            # Preenche o json com os totais reais do banco
            for r in resultados:
                if r['safra_grupo'] in data['labels']:
                    idx = data['labels'].index(r['safra_grupo'])
                    data['promotores'][idx] = r['promotores']
                    data['neutros'][idx] = r['neutros']
                    data['detratores'][idx] = r['detratores']
                    
            return data
    except Exception as e:
        print(f"❌ Erro BI Safra: {e}")
        return {"labels": [], "promotores": [], "neutros": [], "detratores": []}

# 3. RISCO FINANCEIRO (BUBBLE CHART)
@app.get("/api/reports/bi-risco")
async def get_bi_risco(periodo: str = Query("Últimos 6 Meses"), segmento: str = Query("Todos"), arr: str = Query("Todos"), safra: str = Query("Todos")):
    try:
        where_sql, params = build_bi_filters(periodo, segmento, arr, safra)
        
        engine = get_engine()
        with engine.connect() as conn:
            sql = text(f"""
                SELECT 
                    e.id as empresa_id,
                    e.nome as nome_empresa,
                    COUNT(r.resposta_id) as total_respostas,
                    SUM(CASE WHEN r.nota >= 9 THEN 1 ELSE 0 END) as promotores,
                    SUM(CASE WHEN r.nota <= 6 THEN 1 ELSE 0 END) as detratores,
                    MAX(COALESCE(e.valor_contrato, 0)) as arr
                FROM dbo.nps_respostas r
                INNER JOIN dbo.nps_empresas e ON r.empresa_id = e.id
                WHERE {where_sql}
                GROUP BY e.id, e.nome
            """)
            
            resultados = conn.execute(sql, params).mappings().all()
            
            bolhas = []
            for r in resultados:
                if r['total_respostas'] > 0:
                    nps = round(((r['promotores'] - r['detratores']) / r['total_respostas']) * 100)
                    bolhas.append({
                        "id": r['empresa_id'], 
                        "x": nps, 
                        "y": float(r['arr']), 
                        "r": min(max(r['total_respostas'] * 2, 5), 30), 
                        "empresa": r['nome_empresa']
                    })
                    
            return bolhas
    except Exception as e:
        print(f"❌ Erro BI Risco: {e}")
        return []

# 4. GAUGE AI - CONSULTORIA PARETO
@app.get("/api/reports/bi-ia")
async def get_bi_ia_reports(periodo: str = Query("Últimos 6 Meses"), segmento: str = Query("Todos"), arr: str = Query("Todos"), safra: str = Query("Todos")):
    try:
        where_sql, params = build_bi_filters(periodo, segmento, arr, safra)
        
        engine = get_engine()
        with engine.connect() as conn:
            api_key = conn.execute(text("SELECT valor FROM dbo.nps_configuracoes WHERE chave = 'openai_api_key'")).scalar()
            
            if not api_key:
                return {
                    "resumoParetoIA": "A Gauge AI requer uma API Key configurada para gerar o Pareto Analítico.", 
                    "recomendacaoIA": "Configure a chave da OpenAI no painel administrativo."
                }

            # 👉 BUSCAMOS O CONTEXTO REAL PARA ALIMENTAR A IA
            sql_contexto = text(f"""
                SELECT 
                    COUNT(r.resposta_id) as total_respostas,
                    SUM(CASE WHEN r.nota <= 6 THEN 1 ELSE 0 END) as total_detratores,
                    SUM(CASE WHEN r.nota >= 9 THEN 1 ELSE 0 END) as total_promotores
                FROM dbo.nps_respostas r
                LEFT JOIN dbo.nps_empresas e ON r.empresa_id = e.id
                WHERE {where_sql}
            """)
            dados = conn.execute(sql_contexto, params).mappings().first()
            
        # Proteção contra bases vazias
        if not dados or dados['total_respostas'] == 0:
            return {
                "resumoParetoIA": f"Não foram encontradas respostas no período de <strong>{periodo}</strong> para os filtros selecionados.",
                "recomendacaoIA": "Experimente alargar o seu intervalo de pesquisa ou remover alguns filtros."
            }

        client = openai.OpenAI(api_key=str(api_key).strip())
        
        prompt = f"""
        Atue como a 'Gauge AI', um Consultor Sênior de Business Intelligence em Customer Success.
        
        CONTEXTO ATUAL (Filtros aplicados pelo utilizador):
        - Período: {periodo}
        - Segmento: {segmento}
        - Tamanho/ARR: {arr}
        - Tempo de Casa (Safra): {safra}
        
        DADOS DESTE CORTE:
        - Total de Respostas: {dados['total_respostas']}
        - Detratores: {dados['total_detratores']}
        - Promotores: {dados['total_promotores']}
        
        Crie um parecer executivo divido em duas partes:
        1. "resumoParetoIA": Um parágrafo detalhado (usando tags HTML como <strong> para negrito) explicando a situação deste grupo de clientes. Foque-se no risco de churn.
        2. "recomendacaoIA": Uma recomendação tática, clara e direta do que o time de CS deve fazer nesta semana para este grupo de segmentação.
        
        Responda estritamente em JSON com as chaves "resumoParetoIA" e "recomendacaoIA".
        """

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": prompt}],
            temperature=0.7, 
            response_format={ "type": "json_object" }
        )
        
        return json.loads(response.choices[0].message.content)

    except Exception as e:
        print(f"❌ Erro na BI IA: {e}")
        return {
            "resumoParetoIA": "Analisando os filtros aplicados, identificamos uma falha de conexão com o motor cognitivo.",
            "recomendacaoIA": "Por favor, tente gerar a análise novamente."
        }

@app.get("/api/reports/jornada")
def obter_jornada_cliente(
    empresa: str, 
    data_inicio: Optional[str] = Query(None), 
    data_fim: Optional[str] = Query(None),
    usuario_email: str = Depends(get_current_user)
):
    try:
        engine = get_engine()
        with engine.connect() as conn:
            params = {
                "empresa": empresa,
                "inicio": data_inicio,
                "fim": data_fim
            }

            # 1. Cálculo do NPS com conversão para FLOAT para evitar arredondamento zero
            sql_nps = text("""
                SELECT 
                    COUNT(r.resposta_id) as total,
                    SUM(CASE WHEN r.nota >= 9 THEN 1.0 ELSE 0.0 END) as promotores,
                    SUM(CASE WHEN r.nota <= 6 THEN 1.0 ELSE 0.0 END) as detratores
                FROM dbo.nps_respostas r
                INNER JOIN dbo.nps_clientes c ON r.cliente_id = c.cliente_id
                WHERE c.empresa = :empresa 
                AND (r.excluido = 0 OR r.excluido IS NULL)
                AND (:inicio IS NULL OR r.data_resposta >= :inicio)
                AND (:fim IS NULL OR r.data_resposta <= :fim)
            """)
            
            res_nps = conn.execute(sql_nps, params).mappings().first()
            
            total = res_nps['total'] or 0
            nps_calculado = 0
            
            if total > 0:
                # Cálculo: ((Promotores - Detratores) / Total) * 100
                promotores = res_nps['promotores'] or 0
                detratores = res_nps['detratores'] or 0
                nps_calculado = round(((promotores - detratores) / total) * 100)

            # 3. SQL do Histórico (Timeline)
            sql_hist = text("""
                SELECT 
                    r.nota, r.motivo, r.canal, 
                    COALESCE(r.data_resposta, r.created_at) as data,
                    c.nome as cliente_nome, c.cargo
                FROM dbo.nps_respostas r
                INNER JOIN dbo.nps_clientes c ON r.cliente_id = c.cliente_id
                WHERE c.empresa = :empresa 
                AND (r.excluido = 0 OR r.excluido IS NULL)
                AND (:inicio IS NULL OR r.data_resposta >= :inicio)
                AND (:fim IS NULL OR r.data_resposta <= :fim)
                ORDER BY data DESC
            """)
            result = conn.execute(sql_hist, params).mappings().all()
            
            historico = []
            for r in result:
                item = dict(r)
                item["data_formatada"] = r["data"].strftime("%d/%m/%Y %H:%M") if r["data"] else "S/D"
                historico.append(item)
                
            return {
                "nps_atual": nps_calculado,
                "total_respostas": res_nps['total'] or 0,
                "historico": historico
            }
    except Exception as e:
        print(f"Erro na Rota Jornada: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

# --- ROTA PARA A ABA OPERACIONAL (KPIs DE EXECUÇÃO) ---
@app.get("/api/reports/operacional")
def obter_dados_operacionais(
    data_inicio: Optional[str] = Query(None), 
    data_fim: Optional[str] = Query(None),
    usuario_email: str = Depends(get_current_user)
):
    # Preparação de parâmetros para evitar SQL Injection e tratar valores nulos
    params = {
        "inicio": f"{data_inicio} 00:00:00" if data_inicio else None,
        "fim": f"{data_fim} 23:59:59" if data_fim else None
    }
    
    try:
        engine = get_engine()
        with engine.connect() as conn:
            # 1. Taxa de Resposta (Injetando o filtro de data no numerador)
            sql_taxa = text("""
                SELECT 
                    -- Denominador: Todos os clientes ativos (Base Real)
                    (SELECT COUNT(*) FROM dbo.nps_clientes WHERE ativo = 1) as total_base,
                    
                    -- Numerador: Respondentes únicos que estão ativos e dentro do período
                    (SELECT COUNT(DISTINCT r.cliente_id) 
                     FROM dbo.nps_respostas r
                     INNER JOIN dbo.nps_clientes c ON r.cliente_id = c.cliente_id
                     WHERE c.ativo = 1 
                       AND r.excluido = 0
                       AND (:inicio IS NULL OR r.data_resposta >= :inicio)
                       AND (:fim IS NULL OR r.data_resposta <= :fim)
                    ) as total_respostas
            """)
            res_taxa = conn.execute(sql_taxa, params).mappings().first()
            
            # 2. SLA Médio de Fechamento (Ajustado para usar os params corretamente)
            sql_sla = text("""
                SELECT 
                    AVG(CAST(DATEDIFF(minute, created_at, updated_at) AS FLOAT) / 60.0 / 24.0) as sla_real_dias
                FROM dbo.nps_acoes 
                WHERE status = 'Concluído' 
                  AND updated_at IS NOT NULL 
                  AND updated_at >= created_at
                  AND (:inicio IS NULL OR updated_at >= :inicio)
                  AND (:fim IS NULL OR updated_at <= :fim)
            """)
            res_sla = conn.execute(sql_sla, params).scalar() or 0

            return {
                "taxa_resposta": round((res_taxa['total_respostas'] / res_taxa['total_base'] * 100), 1) if res_taxa['total_base'] > 0 else 0,
                "sla_medio_dias": round(res_sla, 1)
            }
    except Exception as e:
        print(f"Erro Operacional: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
    
@app.get("/api/reports/lista-gestores")
def obter_lista_gestores_com_empresas(usuario_email: str = Depends(get_current_user)):
    try:
        engine = get_engine()
        with engine.connect() as conn:
            # Removemos o WHERE EXISTS para listar todos os cadastrados
            sql = text("""
                SELECT id, nome 
                FROM dbo.nps_gestores 
                ORDER BY nome
            """)
            result = conn.execute(sql).fetchall()
            return [{"id": linha[0], "nome": linha[1]} for linha in result]
    except Exception as e:
        raise HTTPException(status_code=500, detail="Erro ao processar lista de gestores")
    
@app.get("/api/reports/operacional/inativos")
def relatorio_clientes_inativos(usuario_email: str = Depends(get_current_user)):
    try:
        engine = get_engine()
        with engine.connect() as conn:
            # 1. Puxa a regra de recorrência atual
            sql_regra = text("SELECT TOP 1 TRY_CAST(valor AS INT) FROM dbo.nps_configuracoes WHERE chave = 'recorrencia_dias'")
            recorrencia_dias = conn.execute(sql_regra).scalar()
            recorrencia_dias = recorrencia_dias if recorrencia_dias is not None else 90

            # 2. Busca os clientes "Vencidos" (Disparados, não respondidos e que ultrapassaram a data limite)
            sql = text("""
                SELECT 
                    c.empresa, 
                    c.nome AS cliente_nome, 
                    c.email AS cliente_email, 
                    COALESCE(d.data_envio_inicial, c.ultimo_envio) AS data_envio,
                    DATEDIFF(day, COALESCE(d.data_envio_inicial, c.ultimo_envio), GETDATE()) AS dias_sem_resposta
                FROM dbo.nps_clientes c
                LEFT JOIN dbo.nps_disparos d ON c.cliente_id = d.cliente_id
                WHERE 
                    -- Apenas status de quem recebeu mas não finalizou a pesquisa
                    COALESCE(d.status, c.status_envio) IN ('Enviado', 'Pendente')
                    AND COALESCE(d.data_envio_inicial, c.ultimo_envio) IS NOT NULL
                    -- A MÁGICA: Apenas tempo de espera MAIOR que a regra de recorrência
                    AND DATEDIFF(day, COALESCE(d.data_envio_inicial, c.ultimo_envio), GETDATE()) > :recorrencia
                ORDER BY dias_sem_resposta DESC
            """)
            
            result = conn.execute(sql, {"recorrencia": recorrencia_dias}).fetchall()
            
            lista = [
                {
                    "empresa": r[0] or "Sem Empresa",
                    "cliente_nome": r[1],
                    "cliente_email": r[2],
                    "data_envio": r[3].isoformat() if r[3] else None,
                    "dias_sem_resposta": r[4]
                }
                for r in result
            ]
            
            return {"recorrencia_dias": recorrencia_dias, "lista": lista}
            
    except Exception as e:
        print(f"Erro no relatorio inativos: {e}")
        raise HTTPException(status_code=500, detail="Erro ao buscar clientes inativos.")

# ==========================================
# 🎯 ROTAS: PLANOS DE AÇÃO (Close the Loop)
# ==========================================

@app.post("/api/acoes")
def criar_acao(acao: AcaoCriar):
    try:
        engine = get_engine()
        with engine.begin() as conn:
            sql = text("""
                INSERT INTO dbo.nps_acoes 
                (resposta_id, empresa_id, gestor_id, titulo, descricao, prioridade, prazo_limite)
                VALUES (:rid, :eid, :gid, :t, :d, :p, :pl)
            """)
            conn.execute(sql, {
                "rid": acao.resposta_id, "eid": acao.empresa_id, "gid": acao.gestor_id,
                "t": acao.titulo, "d": acao.descricao, "p": acao.prioridade, 
                "pl": acao.prazo_limite if acao.prazo_limite else None
            })
        return {"status": "success", "message": "Ação criada com sucesso!"}
    except Exception as e:
        print(f"Erro ao criar ação: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/acoes")
def listar_acoes(gestor_id: Optional[int] = None, status: Optional[str] = None):
    try:
        engine = get_engine()
        with engine.connect() as conn:
            filtros = []
            params = {}
            
            if gestor_id:
                filtros.append("a.gestor_id = :gid")
                params["gid"] = gestor_id
            if status and status != "Todas":
                filtros.append("a.status = :status")
                params["status"] = status
                
            condicao = " WHERE " + " AND ".join(filtros) if filtros else ""

            # 👇 A QUERY DEFINITIVA: Traz a foto e dá prioridade ao gestor da ação!
            sql = text(f"""
                SELECT 
                    a.*,
                    COALESCE(e.nome, r.empresa, c.empresa, 'Conta Geral') as empresa_nome,
                    COALESCE(g.nome, e.gestor, 'Sem Gestor') as gestor_nome,
                    g.avatar as gestor_avatar,
                    r.nota as resposta_nota
                FROM dbo.nps_acoes a
                LEFT JOIN dbo.nps_empresas e ON a.empresa_id = e.id
                LEFT JOIN dbo.nps_respostas r ON a.resposta_id = r.resposta_id
                LEFT JOIN dbo.nps_clientes c ON r.cliente_id = c.cliente_id
                LEFT JOIN dbo.nps_gestores g ON a.gestor_id = g.id
                {condicao}
                ORDER BY 
                    CASE a.status 
                        WHEN 'Pendente' THEN 1 
                        WHEN 'Em Andamento' THEN 2 
                        WHEN 'Concluído' THEN 3 
                    END,
                    a.prazo_limite ASC, 
                    a.created_at DESC
            """)
                        
            resultados = conn.execute(sql, params).mappings().all()
            return [dict(r) for r in resultados]
    except Exception as e:
        print(f"ERRO CRÍTICO SQL: {str(e)}")
        raise HTTPException(status_code=500, detail="Erro interno ao processar a listagem de ações.")

@app.put("/api/acoes/{acao_id}")
def atualizar_acao(acao_id: int, acao: AcaoAtualizar):
    try:
        engine = get_engine()
        with engine.begin() as conn:
            sql = text("""
                UPDATE dbo.nps_acoes 
                SET status = COALESCE(:s, status),
                    prioridade = COALESCE(:p, prioridade),
                    descricao = COALESCE(:d, descricao),
                    prazo_limite = COALESCE(:pl, prazo_limite),
                    gestor_id = COALESCE(:gid, gestor_id),
                    empresa_id = COALESCE(:eid, empresa_id), -- 👈 ADICIONADO PARA ATUALIZAR EMPRESA
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = :id
            """)
            conn.execute(sql, {
                "id": acao_id, "s": acao.status, "p": acao.prioridade, 
                "d": acao.descricao, "pl": acao.prazo_limite, 
                "gid": acao.gestor_id, "eid": acao.empresa_id # 👈 PASSANDO O PARÂMETRO
            })
        return {"status": "success", "message": "Ação atualizada!"}
    except Exception as e:
        print(f"Erro ao atualizar ação: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    
@app.delete("/api/acoes/{acao_id}")
def excluir_acao(acao_id: int):
    try:
        engine = get_engine()
        with engine.begin() as conn:
            # Remove a ação pelo ID único
            sql = text("DELETE FROM dbo.nps_acoes WHERE id = :id")
            conn.execute(sql, {"id": acao_id})
        return {"status": "success", "message": "Ação excluída com sucesso!"}
    except Exception as e:
        print(f"Erro ao excluir ação: {e}")
        raise HTTPException(status_code=500, detail=str(e))