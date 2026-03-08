from fastapi import FastAPI, HTTPException, File, UploadFile, Query, BackgroundTasks, Body, Depends, status, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
from typing import Optional, List
from database import get_engine, exec_sql
from sqlalchemy import text
import traceback 
import re
from collections import Counter
import pandas as pd
from passlib.context import CryptContext
import jwt
from datetime import datetime, timedelta
import bcrypt
import uuid
import requests
import secrets
import string
from services.auth_utils import hash_password
from services.email_svc import enviar_email_recuperacao
from fastapi.security import OAuth2PasswordBearer
from services import clientes_svc, respostas_svc, dashboard_svc, importacao_svc
from database import get_engine

app = FastAPI(
    title="NPS API - Gauge Stefanini",
    description="API centralizada para gestão de NPS, Clientes e Respostas",
    version="1.0.0"
)

# Configuração de CORS: Permite que o Vue.js (que vai rodar em outra porta) acesse a API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # Em produção, coloque o endereço do seu Vue
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ==========================================
# 📦 SCHEMAS (Pydantic Models)
# Validam os dados que chegam do Frontend
# ==========================================

class ClienteUpdate(BaseModel):
    nome: str
    email: str
    empresa: str
    perfil_decisor: str
    segmento: Optional[str] = ""

class RespostaUpdate(BaseModel):
    nota: int
    categoria: str
    motivo: Optional[str] = ""
    canal: Optional[str] = ""
    expectativas: Optional[str] = ""
    o_que_faltava: Optional[str] = ""

class StatusUpdate(BaseModel):
    ativo: int

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
    email: str  # Pode usar EmailStr se quiser que o FastAPI valide o formato do e-mail automaticamente
    password: str

class ResetPasswordRequest(BaseModel):
    token: str
    nova_senha: str

class LoginRequest(BaseModel):
    email: str
    password: str

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

class ClienteCreate(BaseModel):
    nome: str
    email: str
    empresa: str
    perfil_decisor: str = "Decisor"
    segmento: Optional[str] = ""

class ClienteUpdate(BaseModel):
    nome: str
    email: str
    empresa: str
    perfil_decisor: str
    segmento: Optional[str] = ""

class StatusUpdate(BaseModel):
    ativo: bool

# Configurações de Segurança
SECRET_KEY = "nps_intelligence_secret_key_gauge"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 480
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/login")

async def get_current_user(token: str = Depends(oauth2_scheme)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Não foi possível validar as credenciais",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        # SECRET_KEY e ALGORITHM devem ser os mesmos usados no login
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        if email is None:
            raise credentials_exception
        return email
    except jwt.PyJWTError:
        raise credentials_exception

@app.post("/api/login")
async def login(requisicao: LoginRequest, request: Request):
    engine = get_engine()
    with engine.connect() as conn:
        # 1. Procura o utilizador no SQL Server
        query = text("""
            SELECT usuario_id, nome, email, senha_hash, cargo, tipo, ativo 
            FROM dbo.nps_usuarios 
            WHERE email = :email
        """)
        resultado = conn.execute(query, {"email": requisicao.email}).mappings().first()

        # 💡 VALIDAÇÃO 1: Utilizador não existe
        if not resultado:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, 
                detail="Este e-mail não está registado na plataforma."
            )

        # 💡 VALIDAÇÃO 2: Utilizador inativo ou pendente
        ativo_val = str(resultado.ativo).strip().lower()
        if ativo_val not in ['1', 'true']:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, 
                detail="A sua conta está inativa ou aguarda aprovação do administrador."
            )

        # 💡 VALIDAÇÃO 3: Palavra-passe incorreta
        try:
            senha_correta = bcrypt.checkpw(
                requisicao.password.encode('utf-8'), 
                resultado.senha_hash.encode('utf-8')
            )
            if not senha_correta:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED, 
                    detail="A palavra-passe digitada está incorreta."
                )
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, 
                detail="Erro ao validar credenciais. Contacte o suporte."
            )

        # ==========================================
        # 🛡️ GESTÃO DE SESSÕES (CÓDIGO NOVO AQUI)
        # ==========================================
        # Captura os dados reais da máquina de quem fez login
        user_agent = request.headers.get("user-agent", "Dispositivo Desconhecido")
        ip_address = request.client.host if request.client else "IP Desconhecido"
        
        # Formata o nome do dispositivo para ficar bonito no Frontend Vue
        if "Mobile" in user_agent or "iPhone" in user_agent or "Android" in user_agent:
            tipo_disp = "Mobile"
        elif "Mac OS" in user_agent:
            tipo_disp = "Mac/Apple"
        elif "Windows" in user_agent:
            tipo_disp = "Windows/PC"
        else:
            tipo_disp = "Desktop/Browser"
            
        dispositivo_amigavel = f"{tipo_disp} • {user_agent[:20]}..."

        # Grava a sessão no SQL Server
        conn.execute(text("""
            INSERT INTO dbo.nps_sessoes_ativas (usuario_id, dispositivo, ip_address, localizacao)
            VALUES (:uid, :disp, :ip, 'Detetado Automaticamente')
        """), {
            "uid": resultado.usuario_id,
            "disp": dispositivo_amigavel,
            "ip": ip_address
        })
        conn.commit() # 💡 Muito importante para salvar o INSERT na base de dados!

        # ==========================================

        # Se chegar aqui, as credenciais estão certas. Geramos o Token.
        access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
        access_token = jwt.encode(
            {"sub": resultado.email, "exp": datetime.utcnow() + access_token_expires},
            SECRET_KEY, 
            algorithm=ALGORITHM
        )

        # Retorno esperado pelo teu Frontend (localStorage)
        return {
            "access_token": access_token,
            "token_type": "bearer",
            "usuario_id": str(resultado.usuario_id),
            "nome": resultado.nome,
            "cargo": resultado.cargo,
            "tipo": resultado.tipo
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
            VALUES (:nome, :email, :senha_hash, 'Analista', 'False', 'Usuário')
        """)
        
        conn.execute(query_insert, {
            "nome": requisicao.nome,
            "email": requisicao.email,
            "senha_hash": senha_hash
        })
        
    return {"mensagem": "Conta criada com sucesso e aguarda aprovação!"}

@app.post("/api/reset-password")
def redefinir_senha_com_token(requisicao: ResetPasswordRequest):
    # 1. Tenta decifrar e validar o Token JWT
    try:
        payload = jwt.decode(requisicao.token, SECRET_KEY, algorithms=[ALGORITHM])
        email_usuario = payload.get("sub")
        tipo_token = payload.get("tipo")
        
        # Garante que o token é de recuperação e não um token de login roubado
        if not email_usuario or tipo_token != "reset":
            raise HTTPException(status_code=401, detail="Token inválido ou corrompido.")
            
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="O link expirou (passou de 30 minutos). Solicite um novo.")
    except jwt.JWTError:
        raise HTTPException(status_code=401, detail="Link de recuperação inválido.")

    # 2. Se o token for válido, atualiza a palavra-passe no banco
    engine = get_engine()
    with engine.begin() as conn:
        nova_senha_hash = bcrypt.hashpw(requisicao.nova_senha.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
        
        query_update = text("""
            UPDATE dbo.nps_usuarios 
            SET senha_hash = :senha_hash 
            WHERE email = :email
        """)
        
        conn.execute(query_update, {
            "senha_hash": nova_senha_hash,
            "email": email_usuario
        })
        
    return {"mensagem": "Palavra-passe alterada com sucesso! Já pode fazer login."}

@app.post("/api/forgot-password")
async def solicitar_recuperacao(requisicao: EsqueciSenhaRequest, background_tasks: BackgroundTasks):
    engine = get_engine()
    try:
        with engine.connect() as conn:
            # 1. Corrigido: Usamos 'requisicao.email' que vem do Schema Pydantic
            query = text("SELECT email FROM dbo.nps_usuarios WHERE email = :email AND ativo = 'True'")
            resultado = conn.execute(query, {"email": requisicao.email}).fetchone()
            
            if resultado:
                # 2. Gera o token
                expira = datetime.utcnow() + timedelta(minutes=30)
                token = jwt.encode(
                    {"sub": resultado.email, "exp": expira, "tipo": "reset"}, 
                    SECRET_KEY, 
                    algorithm=ALGORITHM
                )
                
                link = f"http://localhost:5173/reset-password?token={token}"
                
                # 3. Dispara o envio real (O nome da função deve ser o que está no email_svc.py)
                background_tasks.add_task(enviar_email_recuperacao, resultado.email, link)
                
        # Por segurança, retornamos a mesma mensagem mesmo se o e-mail não existir (evita enumeração de usuários)
        return {"mensagem": "Se o e-mail existir no nosso sistema, receberá um link de recuperação em breve."}
    
    except Exception as e:
        print(f"Erro no Forgot Password: {e}")
        traceback.print_exc() # Ajuda a ver o erro detalhado no terminal
        raise HTTPException(status_code=500, detail="Erro interno no servidor.")

@app.post("/api/usuarios/alterar-senha")
async def alterar_minha_senha(requisicao: AlterarSenhaRequest, usuario_email: str = Depends(get_current_user)):
    engine = get_engine()
    with engine.connect() as conn:
        # 1. Busca a senha atual no banco
        user = conn.execute(
            text("SELECT senha_hash FROM dbo.nps_usuarios WHERE email = :email"),
            {"email": usuario_email}
        ).fetchone()

        # 2. Valida a senha atual
        if not bcrypt.checkpw(requisicao.senha_atual.encode('utf-8'), user.senha_hash.encode('utf-8')):
            raise HTTPException(status_code=400, detail="A senha atual está incorreta.")

        # 3. Gera o novo hash e salva
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
            # 💡 Adicionamos 'tipo' à consulta
            query = text("SELECT usuario_id, nome, email, cargo, tipo, ativo FROM dbo.nps_usuarios ORDER BY nome ASC")
            result = conn.execute(query).mappings().all()
            
            # Convertemos os resultados para dicionários
            return [dict(r) for r in result]
    except Exception as e:
        print(f"Erro ao listar usuários: {e}")
        raise HTTPException(status_code=500, detail="Erro ao carregar lista de usuários.")

# ==========================================
# 🏠 ROTAS: DASHBOARD (Home)
# ==========================================

@app.get("/api/dashboard/kpis")
def get_dashboard_kpis(empresa: Optional[str] = Query(None)):
    try:
        engine = get_engine()
        with engine.connect() as conn:
            sql_set = text("SELECT valor FROM dbo.nps_settings WHERE chave = 'mostrar_sem_cliente'")
            config_valor = conn.execute(sql_set).scalar()
            
            tipo_join = "LEFT JOIN" if config_valor == 'true' else "INNER JOIN"
            
            condicao_filtro = ""
            parametros = {}
            
            if empresa:
                if empresa == "Não Identificado":
                    condicao_filtro = " WHERE c.empresa IS NULL "
                else:
                    condicao_filtro = " WHERE c.empresa = :empresa "
                    parametros = {"empresa": empresa}

            sql_kpis = text(f"""
                SELECT 
                    COUNT(r.resposta_id) as total_respostas,
                    SUM(CASE WHEN r.nota >= 9 THEN 1 ELSE 0 END) as promotores,
                    SUM(CASE WHEN r.nota BETWEEN 7 AND 8 THEN 1 ELSE 0 END) as neutros,
                    SUM(CASE WHEN r.nota <= 6 THEN 1 ELSE 0 END) as detratores,
                    
                    -- NPS do Decisor
                    SUM(CASE WHEN c.perfil_decisor = 'Decisor' AND r.nota >= 9 THEN 1 ELSE 0 END) as decisor_promotores,
                    SUM(CASE WHEN c.perfil_decisor = 'Decisor' AND r.nota <= 6 THEN 1 ELSE 0 END) as decisor_detratores,
                    SUM(CASE WHEN c.perfil_decisor = 'Decisor' THEN 1 ELSE 0 END) as decisor_total,
                    
                    -- Ação no Jira (Close the Loop)
                    SUM(CASE WHEN r.nota <= 6 AND r.jira_issue_url IS NOT NULL AND LTRIM(RTRIM(r.jira_issue_url)) <> '' THEN 1 ELSE 0 END) as detratores_com_jira
                FROM dbo.nps_respostas r
                {tipo_join} dbo.nps_clientes c ON r.cliente_id = c.cliente_id
                {condicao_filtro};
            """)
                    
            resumo = conn.execute(sql_kpis, parametros).mappings().first()
            
            total = resumo['total_respostas'] if resumo and resumo['total_respostas'] else 0
            promotores = resumo['promotores'] if resumo and resumo['promotores'] else 0
            neutros = resumo['neutros'] if resumo and resumo['neutros'] else 0
            detratores = resumo['detratores'] if resumo and resumo['detratores'] else 0
            
            nps_score = 0
            if total > 0:
                pct_promotores = (promotores / total) * 100
                pct_detratores = (detratores / total) * 100
                nps_score = round(pct_promotores - pct_detratores)
                
            # 🚀 CÁLCULO: NPS do Decisor
            dec_total = resumo['decisor_total'] if resumo and resumo['decisor_total'] else 0
            nps_decisor = 0
            if dec_total > 0:
                pct_dec_prom = (resumo['decisor_promotores'] / dec_total) * 100
                pct_dec_detr = (resumo['decisor_detratores'] / dec_total) * 100
                nps_decisor = round(pct_dec_prom - pct_dec_detr)

            # 🚀 CÁLCULO: Taxa de Ação no Jira
            taxa_jira = 0
            if detratores > 0:
                qtd_jira = resumo['detratores_com_jira'] if resumo and resumo['detratores_com_jira'] else 0
                taxa_jira = round((qtd_jira / detratores) * 100)
                
            # 🚀 QUERY FEEDBACKS (Trazendo Perfil e URL do Jira)
            sql_feedbacks = text(f"""
                SELECT TOP 5 
                    r.nota, 
                    CAST(r.motivo AS NVARCHAR(MAX)) as comentario, 
                    r.created_at, 
                    r.jira_issue_url,
                    c.nome as cliente, 
                    c.empresa,
                    c.perfil_decisor
                FROM dbo.nps_respostas r
                INNER JOIN dbo.nps_clientes c ON r.cliente_id = c.cliente_id
                WHERE r.motivo IS NOT NULL 
                  AND LEN(CAST(r.motivo AS NVARCHAR(MAX))) > 0
                  {condicao_filtro.replace("WHERE", "AND") if empresa else ""} 
                ORDER BY r.created_at DESC;
            """)
            
            feedbacks_raw = conn.execute(sql_feedbacks, parametros).mappings().all()
            
            # 🚀 MOTOR DE CATEGORIZAÇÃO AUTOMÁTICA (TAGS)
            regras_tags = {
                "Performance": ["lento", "lentidão", "trava", "demora", "carregar", "peso", "carrega", "cai", "devagar"],
                "UX/UI": ["difícil", "layout", "design", "confuso", "achar", "tela", "interface", "ux", "ui", "cores", "navegação"],
                "Atendimento": ["suporte", "atendimento", "ajuda", "ticket", "cs", "demora para responder", "contato"],
                "Integração": ["integração", "jira", "api", "conectar", "n8n", "sincronizar", "integra", "sistema"],
                "Bugs/Erros": ["erro", "bug", "falha", "caiu", "quebrou", "não funciona", "problema"],
                "Preço": ["caro", "preço", "custo", "valor", "pagar", "mensalidade"]
            }
            
            feedbacks = []
            for f_raw in feedbacks_raw:
                f = dict(f_raw)
                comentario = str(f.get("comentario") or "").lower()
                tags_encontradas = []
                
                # Varrer as regras procurando palavras-chave no comentário
                if len(comentario) > 3:
                    for tag, palavras_chave in regras_tags.items():
                        if any(palavra in comentario for palavra in palavras_chave):
                            tags_encontradas.append(tag)
                
                # Adiciona as tags ao objeto que vai para o Vue.js
                f["tags"] = tags_encontradas
                feedbacks.append(f)
            
        return {
            "status": "success",
            "kpis": {
                "score": nps_score,
                "total_respostas": total,
                "promotores": promotores,
                "neutros": neutros,
                "detratores": detratores,
                "nps_decisor": nps_decisor, 
                "taxa_jira": taxa_jira      
            },
            "feedbacks": feedbacks
        }
    except Exception as e:
        import traceback
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/dashboard/detalhes")
def get_dashboard_detalhes(empresa: Optional[str] = Query(None)):
    try:
        engine = get_engine()
        with engine.connect() as conn:
            filtro_sql_c = ""
            filtro_sql_puro = ""
            params = {}

            if empresa:
                params = {"empresa": empresa}
                if empresa == "Não Identificado":
                    filtro_sql_c = " WHERE c.empresa IS NULL "
                    filtro_sql_puro = " WHERE empresa IS NULL "
                else:
                    filtro_sql_c = " WHERE c.empresa = :empresa "
                    filtro_sql_puro = " WHERE empresa = :empresa "

            coluna_nome = "COALESCE(c.empresa, 'Não Identificado')" if not empresa else "COALESCE(c.segmento, 'Sem Segmento')"
            
            sql_ranking = text(f"""
                SELECT 
                    {coluna_nome} as nome,
                    COUNT(r.resposta_id) as total,
                    ROUND(
                        (SUM(CASE WHEN r.nota >= 9 THEN 1.0 ELSE 0 END) / NULLIF(COUNT(r.resposta_id), 0) * 100) - 
                        (SUM(CASE WHEN r.nota <= 6 THEN 1.0 ELSE 0 END) / NULLIF(COUNT(r.resposta_id), 0) * 100), 0
                    ) as nps
                FROM dbo.nps_respostas r
                LEFT JOIN dbo.nps_clientes c ON r.cliente_id = c.cliente_id
                {filtro_sql_c}
                GROUP BY {coluna_nome}
                ORDER BY nps DESC;
            """)
            
            ranking_raw = conn.execute(sql_ranking, params).mappings().all()
            ranking = [dict(r) for r in ranking_raw]

            sql_taxa = text(f"""
                SELECT 
                    (SELECT COUNT(*) FROM dbo.nps_clientes {filtro_sql_puro}) as total_convidados,
                    (SELECT COUNT(DISTINCT r.cliente_id) 
                     FROM dbo.nps_respostas r
                     LEFT JOIN dbo.nps_clientes c ON r.cliente_id = c.cliente_id
                     {filtro_sql_c}) as total_responderam
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
        raise HTTPException(status_code=500, detail=str(e))
    
@app.get("/api/dashboard/trend")
def get_dashboard_trend(empresa: Optional[str] = Query(None)):
    try:
        engine = get_engine()
        with engine.connect() as conn:
            sql_set = text("SELECT valor FROM dbo.nps_settings WHERE chave = 'mostrar_sem_cliente'")
            config_valor = conn.execute(sql_set).scalar()
            tipo_join = "LEFT JOIN" if config_valor == 'true' else "INNER JOIN"

            condicao = ""
            params = {}
            if empresa:
                if empresa == "Não Identificado":
                    condicao = " AND c.empresa IS NULL "
                else:
                    condicao = " AND c.empresa = :empresa "
                    params = {"empresa": empresa}

            sql_trend = text(f"""
                WITH UltimosMeses AS (
                    SELECT TOP 6
                        -- Extrai apenas os primeiros 7 caracteres (YYYY-MM) de forma universal
                        LEFT(CAST(COALESCE(r.data_resposta, r.created_at) AS VARCHAR(10)), 7) as mes,
                        COUNT(r.resposta_id) as total,
                        SUM(CASE WHEN r.nota >= 9 THEN 1 ELSE 0 END) as promotores,
                        SUM(CASE WHEN r.nota <= 6 THEN 1 ELSE 0 END) as detratores
                    FROM dbo.nps_respostas r
                    {tipo_join} dbo.nps_clientes c ON r.cliente_id = c.cliente_id
                    WHERE COALESCE(r.data_resposta, r.created_at) IS NOT NULL
                    {condicao}
                    GROUP BY LEFT(CAST(COALESCE(r.data_resposta, r.created_at) AS VARCHAR(10)), 7)
                    ORDER BY LEFT(CAST(COALESCE(r.data_resposta, r.created_at) AS VARCHAR(10)), 7) DESC
                )
                SELECT * FROM UltimosMeses ORDER BY mes ASC;
            """)

            result = conn.execute(sql_trend, params).mappings().all()
            
            print("\n=== 📊 DADOS DO GRÁFICO DE EVOLUÇÃO ===")
            for r in result:
                print(dict(r))
            print("=========================================\n")
            
            labels = []
            scores = []
            
            meses_pt = {'01':'Jan', '02':'Fev', '03':'Mar', '04':'Abr', '05':'Mai', '06':'Jun', 
                        '07':'Jul', '08':'Ago', '09':'Set', '10':'Out', '11':'Nov', '12':'Dez'}

            for row in result:
                if not row['mes'] or '-' not in row['mes']: 
                    continue
                
                ano, mes_num = row['mes'].split('-')
                # get() garante que se o mês vier estranho, não quebra a aplicação
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
def get_nuvem_palavras(empresa: Optional[str] = Query(None)):
    try:
        engine = get_engine()
        with engine.connect() as conn:
            condicao = ""
            params = {}
            if empresa:
                if empresa == "Não Identificado":
                    condicao = " AND c.empresa IS NULL "
                else:
                    condicao = " AND c.empresa = :empresa "
                    params = {"empresa": empresa}

            sql = text(f"""
                SELECT CAST(r.motivo AS NVARCHAR(MAX)) as motivo
                FROM dbo.nps_respostas r
                LEFT JOIN dbo.nps_clientes c ON r.cliente_id = c.cliente_id
                WHERE r.nota <= 6 AND r.motivo IS NOT NULL 
                  AND LEN(CAST(r.motivo AS NVARCHAR(MAX))) > 0
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
            contagem = Counter(palavras_uteis).most_common(15) # Pega o Top 15 de termos
            
            nuvem = [{"texto": p[0], "peso": p[1]} for p in contagem]
            
            return {"status": "success", "nuvem": nuvem}
    except Exception as e:
        import traceback
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))
    
# Sugestão de lógica para o backend (main.py)
@app.get("/api/dashboard/valor-em-risco")
async def calcular_risco_financeiro():
    # Cruzamos Clientes Detratores com o perfil de faturamento
    # Isso permite dizer: "Temos X Milhões em risco de churn"
    sql = """
    SELECT 
        SUM(valor_contrato) as total_risco 
    FROM dbo.nps_clientes c
    JOIN dbo.nps_respostas r ON c.cliente_id = r.cliente_id
    WHERE r.nota <= 6 AND r.excluido = 0
    """
    # ... retorno do dado ...
    
# ==========================================
# 🚀 ROTAS DA CENTRAL DE CADASTROS (Opções)
# ==========================================

@app.get("/api/cadastros/empresas")
def get_empresas():
    try:
        engine = get_engine()
        with engine.connect() as conn:
            sql = text("""
                SELECT 
                    e.id, 
                    e.nome, 
                    e.segmento,
                    COALESCE(COUNT(c.cliente_id), 0) as total_clientes
                FROM dbo.nps_empresas e
                LEFT JOIN dbo.nps_clientes c ON CAST(e.nome AS VARCHAR) = CAST(c.empresa AS VARCHAR)
                GROUP BY e.id, e.nome, e.segmento
                ORDER BY total_clientes DESC, e.nome ASC
            """)
            result = conn.execute(sql)
            empresas = []
            for row in result:
                empresas.append({
                    "id": row[0],
                    "nome": row[1],
                    "segmento": row[2],
                    "total_clientes": int(row[3]) # Forçamos como inteiro
                })
            return {"empresas": empresas}
    except Exception as e:
        print(f"Erro SQL: {e}")
        return {"status": "error", "detail": str(e)}

@app.get("/api/cadastros/segmentos")
def get_segmentos():
    try:
        engine = get_engine()
        with engine.connect() as conn:
            sql = text("SELECT id, nome FROM dbo.nps_segmentos ORDER BY nome ASC")
            result = conn.execute(sql).mappings().all()
            return {"status": "success", "segmentos": [dict(r) for r in result]}
    except Exception as e:
        print(f"Erro ao buscar segmentos: {e}")
        return {"status": "error", "segmentos": []}

@app.get("/api/cadastros/perfis")
def get_perfis():
    try:
        engine = get_engine()
        with engine.connect() as conn:
            sql = text("SELECT id, nome FROM dbo.nps_perfis ORDER BY nome ASC")
            result = conn.execute(sql).mappings().all()
            return {"status": "success", "perfis": [dict(r) for r in result]}
    except Exception as e:
        print(f"Erro ao buscar perfis: {e}")
        return {"status": "error", "perfis": []}
    
# ==========================================
# 🚀 SALVAR NOVOS CADASTROS
# ==========================================

@app.post("/api/cadastros/empresas")
def save_empresa(data: dict):
    try:
        engine = get_engine()
        with engine.connect() as conn:
            # Verifica se já existe para evitar erro de duplicidade
            sql = text("INSERT INTO dbo.nps_empresas (nome, segmento) VALUES (:nome, :segmento)")
            conn.execute(sql, {"nome": data.get('nome'), "segmento": data.get('segmento')})
            conn.commit()
            return {"status": "success", "message": "Empresa cadastrada"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}

@app.post("/api/cadastros/segmentos")
def save_segmento(data: dict):
    try:
        engine = get_engine()
        with engine.connect() as conn:
            sql = text("INSERT INTO dbo.nps_segmentos (nome) VALUES (:nome)")
            conn.execute(sql, {"nome": data.get('nome')})
            conn.commit()
            return {"status": "success", "message": "Segmento cadastrado"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}

@app.post("/api/cadastros/perfis")
def save_perfil(data: dict):
    try:
        engine = get_engine()
        with engine.connect() as conn:
            sql = text("INSERT INTO dbo.nps_perfis (nome) VALUES (:nome)")
            conn.execute(sql, {"nome": data.get('nome')})
            conn.commit()
            return {"status": "success", "message": "Perfil cadastrado"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}
    
# ==========================================
# 🚀 ATUALIZAR CADASTROS (PUT)
# ==========================================    

@app.put("/api/clientes/{id}")
def update_cliente(id: int, data: dict):
    try:
        engine = get_engine()
        with engine.connect() as conn:
            sql = text("""
            UPDATE dbo.nps_clientes 
            SET nome = :nome, 
                email = :email, 
                telefone = :telefone,
                empresa = :empresa, 
                cargo = :cargo,
                segmento = :segmento, 
                perfil_decisor = :perfil_decisor
            WHERE cliente_id = :id
            """)
            conn.execute(sql, {
                "nome": data.get('nome'),
                "email": data.get('email'),
                "telefone": data.get('telefone'),
                "empresa": data.get('empresa'),
                "cargo": data.get('cargo'),
                "segmento": data.get('segmento'),
                "perfil_decisor": data.get('perfil_decisor'),
                "linkedin": data.get('linkedin'),
                "id": id
            })
            conn.commit()
            return {"message": "Cliente atualizado com sucesso"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/api/cadastros/segmentos/{id}")
def update_segmento(id: int, data: dict):
    try:
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("UPDATE dbo.nps_segmentos SET nome = :nome WHERE id = :id"), {"nome": data['nome'], "id": id})
            conn.commit()
            return {"message": "Atualizado"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/api/cadastros/perfis/{id}")
def update_perfil(id: int, data: dict):
    try:
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(
                text("UPDATE dbo.nps_perfis SET nome = :nome WHERE id = :id"),
                {"nome": data['nome'], "id": id}
            )
            conn.commit()
            return {"message": "Perfil atualizado com sucesso"}
    except Exception as e:
        print(f"Erro ao atualizar perfil: {e}")
        raise HTTPException(status_code=500, detail=str(e))
        
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

@app.get("/api/clientes")
def list_clientes(q: str = "", ativo: str = "Ativos", perfil: str = "Todos", topn: int = 200):
    try:
        df = clientes_svc.load_clientes(q, ativo, perfil, topn)
        return df.fillna("").to_dict(orient="records")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/api/clientes/{id}")
def update_cliente(id: str, data: dict):
    try:
        engine = get_engine()
        with engine.connect() as conn:
            sql = text("""
                UPDATE dbo.nps_clientes 
                SET nome = :nome, 
                    email = :email, 
                    empresa = :empresa, 
                    perfil_decisor = :perfil_decisor, 
                    segmento = :segmento,
                    proximo_envio = :proximo_envio,
                    ativo = :ativo
                WHERE cliente_id = :id
            """)
            conn.execute(sql, {
                "nome": data.get('nome'),
                "email": data.get('email'),
                "empresa": data.get('empresa'),
                "perfil_decisor": data.get('perfil_decisor'),
                "segmento": data.get('segmento'),
                "proximo_envio": data.get('proximo_envio'),
                "ativo": data.get('ativo'),
                "id": id
            })
            conn.commit()
            return {"status": "success"}
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
        novo_id = clientes_svc.insert_cliente(
            payload.nome, payload.email, payload.empresa, 
            payload.perfil_decisor, payload.segmento
        )
        return {"status": "success", "cliente_id": novo_id, "message": "Cliente cadastrado com sucesso!"}
    except Exception as e:
        if "2627" in str(e) or "2601" in str(e):
            raise HTTPException(status_code=400, detail="Já existe um cliente com este e-mail e empresa.")
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/api/clientes/{cliente_id}")
def update_cliente_route(cliente_id: str, payload: ClienteUpdate):
    try:
        clientes_svc.update_cliente(
            cliente_id, payload.nome, payload.email, 
            payload.empresa, payload.perfil_decisor, payload.segmento
        )
        return {"status": "success", "message": "Cliente atualizado."}
    except Exception as e:
        if "2627" in str(e) or "2601" in str(e):
            raise HTTPException(status_code=400, detail="Já existe outro cliente utilizando este mesmo e-mail.")
            
        raise HTTPException(status_code=500, detail=str(e))

# ==========================================
# 📋 LISTAR FEEDBACKS (Respostas)
# ==========================================
@app.get("/api/respostas")
async def listar_respostas(
    q: str = "",
    empresa: str = "",
    categoria: str = "Todas",
    perfil: str = "Todos",
    incluir_excluidas: bool = False,
    topn: int = 300
):
    try:
        from services import respostas_svc
        df = respostas_svc.load_respostas(q, empresa, categoria, perfil, incluir_excluidas, topn)
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
async def soft_delete_resposta_route(resposta_id: str): # 💡 GARANTIDO COMO STRING
    try:
        engine = get_engine()
        with engine.begin() as conn:
            conn.execute(text("UPDATE dbo.nps_respostas SET excluido = 1 WHERE resposta_id = :id"), {"id": resposta_id})
        return {"status": "success", "detail": "Arquivado com sucesso"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/respostas/{resposta_id}/restore")
async def restore_resposta_route(resposta_id: str): # 💡 GARANTIDO COMO STRING
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
        res = conn.execute(text("SELECT valor FROM dbo.nps_settings WHERE chave = 'mostrar_sem_cliente'")).scalar()
        return {"valor": res == 'true'}

@app.post("/api/settings/mostrar-sem-cliente")
def update_setting_mostrar(payload: SettingUpdate):
    engine = get_engine()
    with engine.connect() as conn:
        val_str = 'true' if payload.valor else 'false'
        conn.execute(text("UPDATE dbo.nps_settings SET valor = :v WHERE chave = 'mostrar_sem_cliente'"), {"v": val_str})
        conn.commit()
        return {"status": "success"}

def obter_tipo_join():
    engine = get_engine()
    with engine.connect() as conn:
        res = conn.execute(text("SELECT valor FROM dbo.nps_settings WHERE chave = 'mostrar_sem_cliente'")).scalar()
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
        # 💡 Agora buscamos também a base_url_frontend gravada
        config = conn.execute(text("""
            SELECT TOP 1 tenant_id, client_id, client_secret, base_url_frontend 
            FROM dbo.nps_configuracoes_email
        """)).fetchone()
        
        if not config:
            raise HTTPException(status_code=400, detail="Configure as chaves primeiro.")

        # Monta a URI de redirecionamento dinamicamente
        # .rstrip('/') evita que URLs com barra no final (ex: site.com/) quebrem o link
        base_url = config.base_url_frontend or "http://localhost:5173"
        redirect_uri = f"{base_url.rstrip('/')}/configuracoes"

        url = f"https://login.microsoftonline.com/{config.tenant_id}/oauth2/v2.0/token"
        payload = {
            'client_id': config.client_id,
            'client_secret': config.client_secret,
            'code': requisicao.code,
            'grant_type': 'authorization_code',
            'redirect_uri': redirect_uri, 
            'scope': 'offline_access mail.send'
        }
                
        res = requests.post(url, data=payload).json()
        if "refresh_token" not in res:
            raise HTTPException(status_code=400, detail="Falha ao obter token da Microsoft.")

        conn.execute(text("UPDATE dbo.nps_configuracoes_email SET refresh_token = :rt, atualizado_em = GETDATE()"), 
                     {"rt": res["refresh_token"]})
        conn.commit()
    return {"status": "conectado"}

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