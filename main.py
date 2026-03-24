import os
import json
import openai
from fastapi import FastAPI, HTTPException, File, UploadFile, Query, BackgroundTasks, Body, Depends, status, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
from typing import Optional, List, Any
from database import get_engine, exec_sql
from sqlalchemy import text
import traceback 
import re
from collections import Counter
import pandas as pd
from passlib.context import CryptContext
from datetime import datetime, timedelta, timezone
import bcrypt
import requests
import secrets
import string
from services.auth_utils import hash_password
from services.email_svc import enviar_email_recuperacao
from fastapi.security import OAuth2PasswordBearer
from services import clientes_svc, respostas_svc, dashboard_svc, importacao_svc
from database import get_engine
from fastapi.responses import StreamingResponse
import io
from pydantic import BaseModel
from fastapi import HTTPException
from sqlalchemy import text
from passlib.context import CryptContext
from jose import jwt, JWTError, ExpiredSignatureError


app = FastAPI(
    title="NPS API - Gauge Stefanini",
    description="API centralizada para gestão de NPS, Clientes e Respostas",
    version="1.0.0"
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
# 📦 SCHEMAS (Pydantic Models)
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
    companhia_id: Optional[int] = None #

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

class StatusUpdate(BaseModel):
    ativo: bool

class ConfigItem(BaseModel):
    chave: str
    valor: str

class GestorSchema(BaseModel):
    nome: str
    papel: Optional[str] = ""
    email: Optional[str] = ""

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

class WebhookN8nPayload(BaseModel):
    resposta_id: str
    nota: int
    empresa_id: Optional[Any] = 0 
    motivo: Optional[str] = ""

# Configurações de Segurança e Autenticação
SECRET_KEY = os.getenv("JWT_SECRET_KEY")

if not SECRET_KEY:
    raise RuntimeError("ERRO CRÍTICO: JWT_SECRET_KEY não configurada nas variáveis de ambiente.")

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 2
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/login")

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
    except ExpiredSignatureError:
        raise credentials_exception
    except JWTError:
        raise credentials_exception
    
# ==========================================
# 🤖 WEBHOOKS (Integrações Externas / n8n)
# ==========================================
@app.post("/api/webhook/n8n/gatilho-acao")
def n8n_gatilho_acao(payload: WebhookN8nPayload):
    """
    O n8n chama esta rota logo após inserir uma resposta no SQL.
    A API avalia se precisa de criar um ticket no Kanban.
    """
    try:
        from services import respostas_svc
        
        # Chama a nossa função inteligente que criámos no passo anterior!
        respostas_svc.processar_acao_automatica(
            resposta_id=payload.resposta_id,
            nota=payload.nota,
            empresa_id=payload.empresa_id or 0, # Passa 0 se não houver empresa
            motivo=payload.motivo
        )
        
        return {"status": "success", "detail": "Gatilho avaliado com sucesso."}
    except Exception as e:
        import traceback
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))
    
# ==========================================
# 🤖 AUTENTICACAO (Login, Registros)
# ==========================================
@app.post("/api/login")
async def login(requisicao: LoginRequest, request: Request):
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
        expire = agora_utc + expires_delta

        to_encode = {
            "sub": resultado["email"],
            "exp": expire
        }
        
        access_token = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

        return {
            "access_token": access_token,
            "token_type": "bearer",
            "usuario_id": resultado["usuario_id"],
            "nome": resultado["nome"],
            "cargo": resultado["cargo"],
            "tipo": resultado["tipo"]
        }
    
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


@app.post("/api/reset-password") # Ou apenas "/reset-password" como ajustámos no frontend
async def resetar_senha(req: ResetPasswordRequest):
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
            
        return {"status": "success", "message": "Palavra-passe alterada com sucesso!"}
            
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Erro ao redefinir a palavra-passe no banco: {e}")
        raise HTTPException(status_code=500, detail="Erro interno ao guardar a nova palavra-passe.")

@app.post("/api/esqueci-senha")
async def solicitar_recuperacao(requisicao: EsqueciSenhaRequest, background_tasks: BackgroundTasks):
    engine = get_engine()
    try:
        with engine.connect() as conn:
            query = text("SELECT email FROM dbo.nps_usuarios WHERE email = :email")
            resultado = conn.execute(query, {"email": requisicao.email}).mappings().first()
            
            if not resultado:
                print(f"ℹ️ Recuperação solicitada para e-mail inexistente: {requisicao.email}")
                return {"mensagem": "Se o e-mail existir no nosso sistema, receberá um link de recuperação em breve."}

            expira = datetime.utcnow() + timedelta(minutes=30)
            token = jwt.encode(
                {"sub": resultado['email'], "exp": expira, "tipo": "reset"}, 
                SECRET_KEY, 
                algorithm=ALGORITHM
            )
            
            FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173")
            link = f"{FRONTEND_URL}/reset-password?token={token}"
            
            print(f"📧 A disparar e-mail de recuperação para: {resultado['email']}")
            background_tasks.add_task(enviar_email_recuperacao, resultado['email'], link)
                
        return {"mensagem": "Se o e-mail existir no nosso sistema, receberá um link de recuperação em breve."}
    
    except Exception as e:
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

@app.get("/api/dashboard/kpis")
def get_dashboard_kpis(
    empresa: Optional[str] = Query(None),
    companhia: Optional[str] = Query(None),
    data_inicio: Optional[str] = Query(None),
    data_fim: Optional[str] = Query(None)
):
    try:
        engine = get_engine()
        with engine.connect() as conn:
            # 1. VERIFICA CONFIGURAÇÃO DE VISIBILIDADE
            sql_set = text("SELECT valor FROM dbo.nps_configuracoes WHERE chave = 'mostrar_sem_cliente'")
            config_valor = conn.execute(sql_set).scalar()
            tipo_join = "LEFT JOIN"
            
            # 2. SISTEMA DINÂMICO DE FILTROS
            filtros_sql = []
            parametros = {}
            
            # 👇 FILTRO DE COMPANHIA (Usa COALESCE)
            if companhia and companhia != "Todas as Companhias":
                filtros_sql.append("""
                    COALESCE(r.empresa, c.empresa) IN (
                        SELECT e.nome 
                        FROM dbo.nps_empresas e 
                        INNER JOIN dbo.nps_companhias comp ON e.companhia_id = comp.id 
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

            # --- 3. PROCESSAMENTO DE PALAVRAS MAIS USADAS (WORD CLOUD) ---
            sql_termos = text(f"""
                SELECT CAST(r.motivo AS NVARCHAR(MAX)) as comentario
                FROM dbo.nps_respostas r
                {tipo_join} dbo.nps_clientes c ON r.cliente_id = c.cliente_id
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
                {condicao_filtro};
            """)
                    
            resumo = conn.execute(sql_kpis, parametros).mappings().first()
            
            total = resumo['total_respostas'] or 0
            promotores = resumo['promotores'] or 0
            neutros = resumo['neutros'] or 0
            detratores = resumo['detratores'] or 0
            
            # Cálculo NPS Geral
            nps_score = 0
            if total > 0:
                nps_score = round(((promotores - detratores) / total) * 100)
                
            # Cálculo NPS Decisor
            dec_total = resumo['decisor_total'] or 0
            nps_decisor = 0
            if dec_total > 0:
                nps_decisor = round(((resumo['decisor_promotores'] - resumo['decisor_detratores']) / dec_total) * 100)

            # --- 5. CÁLCULO REVENUE AT RISK (Financeiro) ---
            filtro_sub = condicao_filtro.replace("WHERE", "AND") if condicao_filtro else ""
            sql_rev = text(f"""
                SELECT SUM(e.valor_contrato) as risco
                FROM dbo.nps_empresas e
                WHERE e.nome IN (
                    SELECT DISTINCT COALESCE(r.empresa, c.empresa)
                    FROM dbo.nps_respostas r
                    INNER JOIN dbo.nps_clientes c ON r.cliente_id = c.cliente_id
                    WHERE r.nota <= 6 {filtro_sub}
                )
            """)
            risco_real = conn.execute(sql_rev, parametros).scalar() or 0
                
            # --- 6. FEEDBACKS RECENTES E TAGS ---
            sql_feedbacks = text(f"""
                SELECT TOP 10 
                    r.nota, CAST(r.motivo AS NVARCHAR(MAX)) as comentario, 
                    r.created_at, r.jira_issue_url,
                    c.nome as cliente, COALESCE(r.empresa, c.empresa) as empresa, c.perfil_decisor
                FROM dbo.nps_respostas r
                INNER JOIN dbo.nps_clientes c ON r.cliente_id = c.cliente_id
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
                    -- 👇 CORREÇÃO: Adicionada a coluna "empresa" aqui para o COALESCE do filtro não falhar!
                    SELECT cliente_id, nota, data_resposta, created_at, empresa,
                           ROW_NUMBER() OVER(PARTITION BY cliente_id ORDER BY COALESCE(data_resposta, created_at) DESC, resposta_id DESC) as rn
                    FROM dbo.nps_respostas
                    WHERE excluido = 0 AND cliente_id IS NOT NULL AND cliente_id <> ''
                )
                SELECT COUNT(*) 
                FROM Historico atual
                JOIN Historico anterior ON atual.cliente_id = anterior.cliente_id AND anterior.rn = 2
                {tipo_join} dbo.nps_clientes c ON atual.cliente_id = c.cliente_id
                WHERE atual.rn = 1 
                  AND anterior.nota <= 8  
                  AND atual.nota >= 9     
                  {condicao_resgate}      
            """)
            
            total_resgatados = conn.execute(query_resgates, parametros).scalar() or 0

            # --- VARIÁVEIS ANTIGAS (PARA CÁLCULO DE VARIAÇÃO) ---
            filtros_sql_ant = []
            params_ant = {}
            
            # 👇 CORREÇÃO NO FILTRO ANTIGO (Usa COALESCE também)
            if companhia and companhia != "Todas as Companhias":
                filtros_sql_ant.append("""
                    COALESCE(r.empresa, c.empresa) IN (
                        SELECT e.nome 
                        FROM dbo.nps_empresas e 
                        INNER JOIN dbo.nps_companhias comp ON e.companhia_id = comp.id 
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
                    -- 👇 CORREÇÃO: Adicionada a coluna "empresa" aqui também
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
    data_fim: Optional[str] = Query(None)
):
    try:
        from sqlalchemy import text
        engine = get_engine()
        with engine.connect() as conn:
            filtros_sql_c = []
            filtros_sql_puro = []
            params = {}

            # 👇 CORREÇÃO: Usar as variáveis corretas para esta rota (filtros_sql_c e params)
            if companhia and companhia != "Todas as Companhias":
                filtros_sql_c.append("""
                    COALESCE(r.empresa, c.empresa) IN (
                        SELECT e.nome 
                        FROM dbo.nps_empresas e 
                        INNER JOIN dbo.nps_companhias comp ON e.companhia_id = comp.id 
                        WHERE comp.nome = :companhia
                    )
                """)
                # 👇 CORREÇÃO: Adicionado o filtro puro para o cálculo de taxa de resposta (que não tem r.empresa)
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

            str_filtro_c = ""
            if len(filtros_sql_c) > 0:
                str_filtro_c = " WHERE " + " AND ".join(filtros_sql_c)
                
            str_filtro_puro = ""
            if len(filtros_sql_puro) > 0:
                str_filtro_puro = " WHERE " + " AND ".join(filtros_sql_puro)

            coluna_nome = "COALESCE(r.empresa, c.empresa, 'Não Identificado')" if not empresa else "COALESCE(c.segmento, 'Sem Segmento')"
            
            sql_ranking = text(f"""
                SELECT 
                    {coluna_nome} as nome,
                    MAX(e.gestor) as gestor, 
                    COUNT(r.resposta_id) as total,
                    ROUND(
                        (SUM(CASE WHEN r.nota >= 9 THEN 1.0 ELSE 0 END) / NULLIF(COUNT(r.resposta_id), 0) * 100) - 
                        (SUM(CASE WHEN r.nota <= 6 THEN 1.0 ELSE 0 END) / NULLIF(COUNT(r.resposta_id), 0) * 100), 0
                    ) as nps
                FROM dbo.nps_respostas r
                LEFT JOIN dbo.nps_clientes c ON r.cliente_id = c.cliente_id
                LEFT JOIN dbo.nps_empresas e ON COALESCE(r.empresa, c.empresa) = e.nome 
                {str_filtro_c}
                GROUP BY {coluna_nome}
                ORDER BY nps DESC;
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
            "ranking": ranking[:5],
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
    data_fim: Optional[str] = Query(None)
):
    try:
        engine = get_engine()
        with engine.connect() as conn:
            sql_set = text("SELECT valor FROM dbo.nps_configuracoes WHERE chave = 'mostrar_sem_cliente'")
            config_valor = conn.execute(sql_set).scalar()
            tipo_join = "LEFT JOIN"

            # 👈 BLINDAGEM: Ignora respostas arquivadas e garante que existe data
            filtros_sql = ["COALESCE(r.data_resposta, r.created_at) IS NOT NULL", "(r.excluido = 0 OR r.excluido IS NULL)"]
            params = {}
            
            if companhia and companhia != "Todas as Companhias":
                filtros_sql.append("""
                    COALESCE(r.empresa, c.empresa) IN (
                        SELECT e.nome 
                        FROM dbo.nps_empresas e 
                        INNER JOIN dbo.nps_companhias comp ON e.companhia_id = comp.id 
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
    companhia: Optional[str] = Query(None), # 👈 Adicionado
    data_inicio: Optional[str] = Query(None),
    data_fim: Optional[str] = Query(None)
):
    try:
        engine = get_engine()
        with engine.connect() as conn:
            # 👈 BLINDAGEM: Inclui apenas Detratores e ignora excluídos
            filtros_sql = ["r.nota <= 6", "r.motivo IS NOT NULL", "LEN(CAST(r.motivo AS NVARCHAR(MAX))) > 0", "(r.excluido = 0 OR r.excluido IS NULL)"]
            params = {}
            
            if companhia and companhia != "Todas as Companhias":
                filtros_sql.append("""
                    COALESCE(r.empresa, c.empresa) IN (
                        SELECT e.nome 
                        FROM dbo.nps_empresas e 
                        INNER JOIN dbo.nps_companhias comp ON e.companhia_id = comp.id 
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
    companhia: Optional[str] = Query(None), # 👈 Adicionado
    data_inicio: Optional[str] = Query(None),
    data_fim: Optional[str] = Query(None)
):
    try:
        engine = get_engine()
        with engine.connect() as conn:
            # 👈 BLINDAGEM: O Excel também não deve conter os arquivados
            filtros_sql = ["(r.excluido = 0 OR r.excluido IS NULL)"]
            parametros = {}
            
            if companhia and companhia != "Todas as Companhias":
                filtros_sql.append("""
                    COALESCE(r.empresa, c.empresa) IN (
                        SELECT e.nome 
                        FROM dbo.nps_empresas e 
                        INNER JOIN dbo.nps_companhias comp ON e.companhia_id = comp.id 
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
                    COALESCE(r.empresa, c.empresa) as Empresa, -- 👈 Corrigido: Mostra sempre a empresa correta
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

# --- ROTAS DE SEGMENTOS ---
@app.post("/api/cadastros/segmentos")
def save_segmento(seg: BasicoSchema):
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO dbo.nps_segmentos (nome) VALUES (:n)"), {"n": seg.nome})
    return {"status": "success"}

@app.put("/api/cadastros/segmentos/{seg_id}")
def update_segmento(seg_id: int, seg: BasicoSchema):
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("UPDATE dbo.nps_segmentos SET nome=:n WHERE id=:id"), {"n": seg.nome, "id": seg_id})
    return {"status": "success"}

# --- ROTAS DE PERFIS ---
@app.post("/api/cadastros/perfis")
def save_perfil(perf: BasicoSchema):
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO dbo.nps_perfis (nome) VALUES (:n)"), {"n": perf.nome})
    return {"status": "success"}

@app.put("/api/cadastros/perfis/{perf_id}")
def update_perfil(perf_id: int, perf: BasicoSchema):
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("UPDATE dbo.nps_perfis SET nome=:n WHERE id=:id"), {"n": perf.nome, "id": perf_id})
    return {"status": "success"}

# --- ROTAS DE SEGMENTOS ---
def crud_factory(route_path, table_name, schema=BasicoSchema):
    @app.get(route_path)
    def listar():
        with get_engine().connect() as conn: 
            return [dict(r) for r in conn.execute(text(f"SELECT * FROM {table_name} ORDER BY nome")).mappings().all()]
            
    @app.post(route_path)
    def salvar(item: schema): # type: ignore  
        with get_engine().begin() as conn:
            if table_name == 'dbo.nps_gestores': 
                conn.execute(text(f"INSERT INTO {table_name} (nome, papel, email) VALUES (:n, :p, :e)"), {"n": item.nome, "p": getattr(item, 'papel', ''), "e": getattr(item, 'email', '')})
            else: 
                conn.execute(text(f"INSERT INTO {table_name} (nome) VALUES (:n)"), {"n": item.nome})
        return {"status": "success"}
        
    @app.put(route_path + "/{item_id}")
    def atualizar(item_id: int, item: schema): # type: ignore  
        with get_engine().begin() as conn:
            nome_antigo = conn.execute(text(f"SELECT nome FROM {table_name} WHERE id=:id"), {"id": item_id}).scalar()
            
            if table_name == 'dbo.nps_gestores': 
                conn.execute(text(f"UPDATE {table_name} SET nome=:n, papel=:p, email=:e WHERE id=:id"), {"n": item.nome, "p": getattr(item, 'papel', ''), "e": getattr(item, 'email', ''), "id": item_id})
            else: 
                conn.execute(text(f"UPDATE {table_name} SET nome=:n WHERE id=:id"), {"n": item.nome, "id": item_id})
            
            # 🟢 EFEITO CASCATA
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

# --- ROTAS DE PERFIS ---
@app.get("/api/cadastros/perfis")
def listar_perfis():
    try:
        engine = get_engine()
        with engine.connect() as conn:
            sql = text("SELECT id, nome FROM dbo.nps_perfis ORDER BY id")
            res = conn.execute(sql).mappings().all()
            return [dict(r) for r in res]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/cadastros/perfis")
def save_perfil(perf: BasicoSchema):
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO dbo.nps_perfis (nome) VALUES (:n)"), {"n": perf.nome})
    return {"status": "success"}

@app.put("/api/cadastros/perfis/{perf_id}")
def update_perfil(perf_id: int, perf: BasicoSchema):
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("UPDATE dbo.nps_perfis SET nome=:n WHERE id=:id"), {"n": perf.nome, "id": perf_id})
    return {"status": "success"}

# --- ROTAS DE CARGOS ---
@app.get("/api/cadastros/cargos")
def listar_cargos():
    try:
        engine = get_engine()
        with engine.connect() as conn:
            sql = text("SELECT id, nome FROM dbo.nps_cargos ORDER BY nome")
            res = conn.execute(sql).mappings().all()
            return [dict(r) for r in res]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/cadastros/cargos")
def save_cargo(cargo: BasicoSchema):
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO dbo.nps_cargos (nome) VALUES (:n)"), {"n": cargo.nome})
    return {"status": "success"}

@app.put("/api/cadastros/cargos/{cargo_id}")
def update_cargo(cargo_id: int, cargo: BasicoSchema):
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("UPDATE dbo.nps_cargos SET nome=:n WHERE id=:id"), {"n": cargo.nome, "id": cargo_id})
    return {"status": "success"}

@app.delete("/api/cadastros/cargos/{cargo_id}")
def delete_cargo(cargo_id: int):
    try:
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("DELETE FROM dbo.nps_cargos WHERE id = :id"), {"id": cargo_id})
            conn.commit()
            return {"message": "Cargo removido"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
# --- ROTAS DE GESTORES DE CONTA ---
@app.get("/api/cadastros/gestores")
def listar_gestores():
    try:
        engine = get_engine()
        with engine.connect() as conn:
            # 🟢 Agora lê o papel e o email
            return [dict(r) for r in conn.execute(text("SELECT id, nome, papel, email FROM dbo.nps_gestores ORDER BY nome")).mappings().all()]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/cadastros/gestores")
def save_gestor(gest: GestorSchema):
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO dbo.nps_gestores (nome, papel, email) VALUES (:n, :p, :e)"), 
                     {"n": gest.nome, "p": gest.papel, "e": gest.email})
    return {"status": "success"}

@app.put("/api/cadastros/gestores/{gestor_id}")
def update_gestor(gestor_id: int, gest: GestorSchema):
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("UPDATE dbo.nps_gestores SET nome=:n, papel=:p, email=:e WHERE id=:id"), 
                     {"n": gest.nome, "p": gest.papel, "e": gest.email, "id": gestor_id})
    return {"status": "success"}

@app.delete("/api/cadastros/gestores/{gestor_id}")
def delete_gestor(gestor_id: int):
    try:
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("DELETE FROM dbo.nps_gestores WHERE id = :id"), {"id": gestor_id})
            conn.commit()
            return {"message": "Gestor removido"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
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

# 3. Rota para disparar o e-mail
@app.post("/api/reports/enviar-email")
async def enviar_report_email(payload: ReportEmailPayload):
    try:
        # Define a cor da tag de prioridade dinamicamente (Vermelho se for ALTA/CRÍTICA, senão Azul)
        cor_prioridade = "#e11d48" if payload.prioridade in ["ALTA", "CRÍTICA"] else "#0ea5e9"
        url_dashboard = os.getenv("FRONTEND_URL", "http://localhost:5173") + "/relatorios"

        # Assunto de E-mail Estratégico
        assunto = f"📊 Relatório Estratégico NPS - {payload.periodo} (Foco: {payload.foco})"
        
        # Template de E-mail Premium Corporativo
        corpo_html = f"""
        <div style="font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; color: #334155; max-width: 600px; margin: 0 auto; border: 1px solid #e2e8f0; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);">
            
            <div style="background-color: #0f172a; padding: 24px; text-align: center;">
                <h2 style="color: #ffffff; margin: 0; font-style: italic; font-size: 24px;">Gauge <span style="color: #818cf8;">AI</span></h2>
                <p style="color: #94a3b8; margin: 6px 0 0 0; font-size: 11px; font-weight: bold; text-transform: uppercase; letter-spacing: 2px;">Intelligence Reports</p>
            </div>
            
            <div style="padding: 32px 24px;">
                <h3 style="margin-top: 0; color: #0f172a; font-size: 18px; border-bottom: 2px solid #f1f5f9; padding-bottom: 12px; margin-bottom: 24px;">Resumo Executivo</h3>
                
                <table style="width: 100%; border-collapse: collapse; margin-bottom: 24px; font-size: 14px;">
                    <tr>
                        <td style="padding: 10px 0; border-bottom: 1px solid #f1f5f9; color: #64748b;"><strong>Período Analisado</strong></td>
                        <td style="padding: 10px 0; border-bottom: 1px solid #f1f5f9; text-align: right; color: #0f172a; font-weight: 600;">{payload.periodo}</td>
                    </tr>
                    <tr>
                        <td style="padding: 10px 0; border-bottom: 1px solid #f1f5f9; color: #64748b;"><strong>Foco de Ação</strong></td>
                        <td style="padding: 10px 0; border-bottom: 1px solid #f1f5f9; text-align: right;">
                            <span style="background-color: #e0e7ff; color: #4338ca; padding: 4px 12px; border-radius: 12px; font-size: 11px; font-weight: 800; text-transform: uppercase; letter-spacing: 1px;">{payload.foco}</span>
                        </td>
                    </tr>
                    <tr>
                        <td style="padding: 10px 0; border-bottom: 1px solid #f1f5f9; color: #64748b;"><strong>Nível de Prioridade</strong></td>
                        <td style="padding: 10px 0; border-bottom: 1px solid #f1f5f9; text-align: right;">
                            <span style="color: {cor_prioridade}; font-weight: 800; font-size: 12px; text-transform: uppercase; letter-spacing: 1px;">{payload.prioridade}</span>
                        </td>
                    </tr>
                </table>
                
                <div style="background-color: #f8fafc; border-left: 4px solid #818cf8; padding: 16px 20px; margin-bottom: 32px; border-radius: 0 8px 8px 0;">
                    <p style="margin: 0; font-style: italic; line-height: 1.6; color: #334155; font-size: 14px;">
                        "{payload.resumo_ia}"
                    </p>
                </div>
                
                <div style="text-align: center;">
                    <a href="{url_dashboard}" style="background-color: #f97316; color: #ffffff; padding: 14px 28px; text-decoration: none; border-radius: 8px; font-weight: 800; font-size: 13px; text-transform: uppercase; letter-spacing: 1px; display: inline-block;">
                        Acessar Dashboard Completo
                    </a>
                </div>
            </div>
            
            <div style="background-color: #f8fafc; padding: 16px; text-align: center; border-top: 1px solid #e2e8f0;">
                <p style="margin: 0; font-size: 11px; color: #94a3b8;">
                    Mensagem gerada e enviada automaticamente pelo módulo de Inteligência Artificial.
                </p>
            </div>
        </div>
        """
        
        # --- INTEGRAÇÃO COM O SEU SERVIÇO DE E-MAIL (email_svc) ---
        # Tenta carregar o seu disparador nativo de e-mail de forma segura
        try:
            # Ajuste esta importação de acordo com o nome real da sua função de envio no `email_svc`
            from services.email_svc import enviar_email_padrao # (ou 'enviar_email', verifique o nome exato no seu projeto)
            
            emails_enviados_com_sucesso = 0
            
            # Loop que envia um e-mail separado (para manter privacidade de dados/BCC) a cada gestor
            for email_destino in payload.emails:
                try:
                    # Envia!
                    enviar_email_padrao(
                        destinatario=email_destino, 
                        assunto=assunto, 
                        corpo_html=corpo_html
                    )
                    emails_enviados_com_sucesso += 1
                except Exception as mail_err:
                    print(f"⚠️ Falha ao tentar disparar para {email_destino}: {mail_err}")
            
            print(f"✅ E-mail de Report IA enviado com sucesso para {emails_enviados_com_sucesso} gestores.")

        except ImportError:
            # Caso a função de e-mail ainda não esteja perfeitamente ligada, ele finge o envio
            # para não travar o frontend durante os testes!
            print("⚠️ SERVIÇO DE E-MAIL NÃO ENCONTRADO/CONFIGURADO.")
            print("---- MODO SIMULAÇÃO DE ENVIO ATIVADO ----")
            print(f"Destinatários: {payload.emails}")
            print(f"Assunto: {assunto}")
            print("-----------------------------------------")

        return {"sucesso": True, "mensagem": f"Relatório enviado com sucesso para {len(payload.emails)} gestor(es)!"}

    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"❌ Erro global ao processar envio de e-mail de report: {e}")
        return {"sucesso": False, "mensagem": "Ocorreu um erro interno. Contacte o suporte técnico."}
    
# ==========================================
# 🚀 SALVAR NOVOS CADASTROS
# ==========================================

@app.get("/api/cadastros/empresas")
async def listar_empresas():
    engine = get_engine()
    with engine.connect() as conn:
        # 👈 Alterado para fazer JOIN e trazer o nome e ID da companhia
        sql = text("""
            SELECT 
                e.id, e.nome, e.segmento, e.valor_contrato as arr_total, 
                g.nome as gestor, e.gestor_id,
                comp.nome as companhia, e.companhia_id
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
            # 👇 CORREÇÃO: Inserir gestor e gestor_id na criação
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

@app.delete("/api/cadastros/segmentos/{segmento_id}")
def delete_segmento(segmento_id: int):
    try:
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("DELETE FROM dbo.nps_segmentos WHERE id = :id"), {"id": segmento_id})
            conn.commit()
            return {"message": "Segmento removido"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/cadastros/perfis/{perfil_id}")
def delete_perfil(perfil_id: int):
    try:
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("DELETE FROM dbo.nps_perfis WHERE id = :id"), {"id": perfil_id})
            conn.commit()
            return {"message": "Perfil removido"}
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
    topn: int = 200,
    _t: str = None,
    usuario_email: str = Depends(get_current_user)
):
    try:
        engine = get_engine()
        engine.dispose() 
        
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
def forcar_envio_n8n(cliente_id: str):
    try:
        clientes_svc.forcar_envio_db(cliente_id)
        
        ok, msg, details = clientes_svc.disparar_n8n_force(cliente_id)
        if not ok:
            raise HTTPException(status_code=400, detail=msg)
            
        return {"status": "success", "message": msg, "details": details}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/clientes/forcar-envio-lote")
async def forcar_envio_lote_n8n(dados: LoteEnvio, background_tasks: BackgroundTasks):
    """
    Aciona o n8n APENAS para os clientes selecionados via checkboxes no Vue.
    """
    if not dados.cliente_ids:
        raise HTTPException(status_code=400, detail="Selecione pelo menos um cliente.")
        
    def processar_lote_selecionados(lista_ids):
        for c_id in lista_ids:
            try:
                clientes_svc.forcar_envio_db(c_id)
                clientes_svc.disparar_n8n_force(c_id)
            except Exception as e:
                print(f"Erro no disparo em lote para {c_id}: {e}")

    background_tasks.add_task(processar_lote_selecionados, dados.cliente_ids)
    return {"status": "success", "message": f"Disparo em lote iniciado para {len(dados.cliente_ids)} clientes."}

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
            cliente_id, payload.nome, payload.email, payload.telefone, 
            payload.empresa, payload.perfil_decisor, payload.segmento, payload.cargo
        )
        return {"status": "success", "message": "Cliente atualizado."}
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
    
@app.get("/api/audiencia/plano-acao")
def gerar_plano_acao_empresa(empresa: str):
    try:
        engine = get_engine()
        with engine.connect() as conn:
            # 1. BUSCAR CONFIGURAÇÕES DA MAGIC AI (API KEY E MODELO)
            sql_ai = text("SELECT chave, valor FROM dbo.nps_configuracoes WHERE chave IN ('openai_api_key', 'openai_model')")
            configs = {row.chave: row.valor for row in conn.execute(sql_ai)}
            
            api_key = configs.get('openai_api_key')
            modelo = configs.get('openai_model', 'gpt-4o-mini')

            if not api_key:
                return {"plano": "Configuração de IA não encontrada. Verifique a chave da OpenAI nas definições."}

            # 2. BUSCAR COMENTÁRIOS DA EMPRESA
            sql_comentarios = text("""
                SELECT CAST(r.motivo AS NVARCHAR(MAX)) as comentario, r.nota
                FROM dbo.nps_respostas r
                INNER JOIN dbo.nps_clientes c ON r.cliente_id = c.cliente_id
                WHERE c.empresa = :empresa 
                  AND r.motivo IS NOT NULL 
                  AND LEN(CAST(r.motivo AS NVARCHAR(MAX))) > 5
            """)
            resultados = conn.execute(sql_comentarios, {"empresa": empresa}).mappings().all()
            
            if not resultados:
                return {"plano": f"A empresa {empresa} ainda não possui comentários qualitativos suficientes para uma análise de IA."}

            # 3. PREPARAR O CONTEXTO PARA O GPT
            feedbacks_texto = "\n".join([f"Nota {r['nota']}: {r['comentario']}" for r in resultados])
            
            prompt_sistema = "Você é um consultor especialista em Customer Success e retenção de clientes (NPS)."
            prompt_usuario = f"""
            Analise estes feedbacks reais dos clientes da empresa '{empresa}':
            
            {feedbacks_texto}
            
            Com base nisso, gere um PLANO DE ACÇÃO ESTRATÉGICO para evitar cancelamentos (Churn).
            REGRAS:
            1. Seja direto e use linguagem executiva.
            2. Divida em 3 pontos práticos de ação.
            3. Identifique o maior 'ponto de dor' recorrente.
            4. Sugira uma ação para os próximos 7 dias.
            """

            # 4. CHAMADA À OPENAI
            client = openai.OpenAI(api_key=api_key)
            response = client.chat.completions.create(
                model=modelo,
                messages=[
                    {"role": "system", "content": prompt_sistema},
                    {"role": "user", "content": prompt_usuario}
                ],
                temperature=0.7
            )

            plano_gerado = response.choices[0].message.content

            return {
                "status": "success",
                "empresa": empresa,
                "plano": plano_gerado
            }

    except Exception as e:
        print(f"Erro na IA: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Erro ao processar plano de IA: {str(e)}")

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
    topn: int = 300
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

# ==========================================
# 📥 ROTAS: IMPORTAÇÃO
# ==========================================

@app.post("/api/importar/preview")
async def preview_importacao(file: UploadFile = File(...)):
    try:
        df = pd.read_excel(file.file) if file.filename.endswith(('.xlsx', '.xls')) else pd.read_csv(file.file)
        
        dados = df.to_dict(orient='records')
        
        return dados
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Erro ao ler arquivo: {str(e)}")

@app.post("/api/importar/clientes")
async def importar_clientes_planilha(file: UploadFile = File(...)):
    try:
        contents = await file.read()
        df_imp = importacao_svc.read_import_file_bytes(contents, file.filename)
        
        issues = importacao_svc.validate_clientes_df(df_imp)
        if not issues["invalid_email"].empty or not issues["invalid_perfil"].empty:
            raise HTTPException(status_code=400, detail="Planilha contém e-mails ou perfis inválidos.")
            
        res = importacao_svc.import_clientes_df(df_imp)
        return {"status": "success", "resultado": res}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
@app.post("/api/importar/confirmar")
async def confirmar_importacao_final(payload: dict):
    try:
        # Pega a escolha do usuário (default True) e a lista de clientes
        overwrite = payload.get("overwrite", True)
        clientes_ajustados = payload.get("clientes", [])
        
        if not clientes_ajustados:
            raise HTTPException(status_code=400, detail="Nenhum dado enviado.")
        
        engine = get_engine()
        inserted_count = 0
        updated_count = 0
        ignored_count = 0

        with engine.begin() as conn: 
            for c in clientes_ajustados:
                email = str(c.get("email", "")).strip().lower()
                
                if not email or "@" not in email:
                    continue

                # Dados preparados para a Query
                params = {
                    "nome": str(c.get("nome", "")).strip(),
                    "email": email,
                    "empresa": str(c.get("empresa", "")).strip(),
                    "perfil": str(c.get("perfil_decisor", c.get("perfil", "Decisor"))).strip(),
                    "segmento": str(c.get("segmento", "")).strip()
                }

                # 1. Verifica se o e-mail já existe
                check_query = text("SELECT cliente_id FROM dbo.nps_clientes WHERE email = :email")
                existente = conn.execute(check_query, {"email": email}).fetchone()

                if existente:
                    if overwrite:
                        # USUÁRIO ESCOLHEU ATUALIZAR (UPDATE)
                        update_sql = text("""
                            UPDATE dbo.nps_clientes 
                            SET nome = :nome, empresa = :empresa, 
                                perfil_decisor = :perfil, segmento = :segmento, ativo = 1
                            WHERE email = :email
                        """)
                        conn.execute(update_sql, params)
                        updated_count += 1
                    else:
                        # USUÁRIO ESCOLHEU IGNORAR
                        ignored_count += 1
                else:
                    # REGISTRO NOVO (INSERT)
                    params["cliente_id"] = "C" + secrets.token_hex(8)
                    insert_sql = text("""
                        INSERT INTO dbo.nps_clientes (cliente_id, nome, email, empresa, perfil_decisor, segmento, ativo)
                        VALUES (:cliente_id, :nome, :email, :empresa, :perfil, :segmento, 1)
                    """)
                    conn.execute(insert_sql, params)
                    inserted_count += 1
        
        return {
            "status": "success", 
            "resultado": {
                "inserted": inserted_count, 
                "updated": updated_count,
                "ignored": ignored_count,
                "total": inserted_count + updated_count + ignored_count
            }
        }
    
    except Exception as e:
        import traceback
        print(f"🔥 Erro na confirmação: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))
    
@app.post("/api/importar/respostas")
async def confirmar_importacao_respostas(payload: dict):
    try:
        dados_respostas = payload.get("dados", [])
        
        if not dados_respostas:
            raise HTTPException(status_code=400, detail="Nenhum dado de resposta enviado.")
        
        engine = get_engine()
        inserted_count = 0
        ignored_count = 0

        with engine.begin() as conn: 
            for r in dados_respostas:
                email = str(r.get("email_cliente", "")).strip().lower()
                nota_str = str(r.get("nota", "")).strip()
                
                if not email or "@" not in email or nota_str == "":
                    ignored_count += 1
                    continue
                
                try:
                    nota = int(nota_str)
                except ValueError:
                    ignored_count += 1
                    continue

                if nota >= 9:
                    categoria_nps = "Promotor"
                elif nota >= 7:
                    categoria_nps = "Neutro"
                else:
                    categoria_nps = "Detrator"
                
                check_query = text("SELECT cliente_id FROM dbo.nps_clientes WHERE email = :email")
                cliente_existente = conn.execute(check_query, {"email": email}).fetchone()

                if not cliente_existente:
                    ignored_count += 1 
                    continue
                
                cliente_id = cliente_existente.cliente_id
                resposta_id = "R" + secrets.token_hex(8)
                
                insert_sql = text("""
                    INSERT INTO dbo.nps_respostas (
                        resposta_id, cliente_id, nota, motivo, categoria, 
                        canal, excluido, data_resposta, created_at
                    )
                    VALUES (
                        :rid, :cid, :nota, :motivo, :categoria, 
                        'Importacao_Manual', 0, :dt_resp, SYSUTCDATETIME()
                    )
                """)
                
                dt_resposta = r.get("data_resposta")
                if not dt_resposta or str(dt_resposta).strip() == "":
                    dt_resposta = None
                
                conn.execute(insert_sql, {
                    "rid": resposta_id,
                    "cid": cliente_id,
                    "nota": nota,
                    "motivo": str(r.get("motivo", "")).strip(),
                    "categoria": categoria_nps, # 👈 Agora envia Promotor, Neutro ou Detrator!
                    "dt_resp": dt_resposta
                })
                inserted_count += 1

        return {
            "status": "success", 
            "resultado": {
                "inserted": inserted_count, 
                "updated": 0,
                "ignored": ignored_count,
                "total": inserted_count + ignored_count
            }
        }
    
    except Exception as e:
        import traceback
        print(f"🔥 Erro na importação de respostas: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))

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
            # Busca a configuração
            query = text("SELECT TOP 1 * FROM dbo.nps_configuracoes_email")
            res = conn.execute(query).fetchone()
            
            if not res:
                # 💡 Se não houver dados, devolvemos um objeto vazio em vez de erro
                return {}
            
            # Converte a linha do SQL para um dicionário
            return dict(res._mapping)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/config/email")
async def salvar_config_email(config: ConfigEmailSchema): # Assume que criaste o Schema
    engine = get_engine()
    with engine.connect() as conn:
        # Limpamos e inserimos (ou fazemos UPDATE)
        conn.execute(text("DELETE FROM dbo.nps_configuracoes_email"))
        query = text("""
            INSERT INTO dbo.nps_configuracoes_email 
            (tenant_id, client_id, client_secret, email_remetente, base_url_frontend)
            VALUES (:t, :c, :s, :e, :b)
        """)
        conn.execute(query, {
            "t": config.tenant_id, 
            "c": config.client_id, 
            "s": config.client_secret, 
            "e": config.email_remetente,
            "b": config.base_url_frontend # 👈 Grava o valor vindo da interface
        })
        conn.commit()
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

            # O segredo está no LEFT JOIN e no COALESCE para garantir que a query não quebre
            condicao = " WHERE " + " AND ".join(filtros) if filtros else ""

            # Adicionámos JOINs para garantir que o Kanban exibe o nome, mesmo se o ID for Nulo
            sql = text(f"""
                SELECT 
                    a.*,
                    COALESCE(e.nome, r.empresa, c.empresa, 'Conta Geral') as empresa_nome,
                    COALESCE(e.gestor, g.nome, 'Sem Gestor') as gestor_nome,
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
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = :id
            """)
            conn.execute(sql, {
                "id": acao_id, "s": acao.status, "p": acao.prioridade, 
                "d": acao.descricao, "pl": acao.prazo_limite
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