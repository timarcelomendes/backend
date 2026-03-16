import os
import re
import time
import hashlib
import requests
import pandas as pd
from datetime import date
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from database import get_engine, exec_sql

PERFIS = ["Decisor", "Influenciador"]

def normalize(s: str) -> str:
    s = (s or "").strip().lower()
    return re.sub(r"\s+", " ", s)

def make_cliente_id(email: str, empresa: str) -> str:
    base = f"{normalize(empresa)}|{normalize(email)}"
    digest = hashlib.sha256(base.encode("utf-8")).hexdigest()
    return "C" + digest[:16]

def read_df(sql: str, params: dict = None) -> pd.DataFrame:
    engine = get_engine()
    with engine.connect() as conn:
        result = conn.execute(text(sql), params or {})
        rows = result.fetchall()
        cols = list(result.keys())
    return pd.DataFrame(rows, columns=cols)

def disparar_n8n_force(cliente_id: str) -> tuple[bool, str, dict]:
    base_url = (os.getenv("N8N_FORCE_URL") or "").strip()
    if not base_url:
        return False, "N8N_FORCE_URL não configurada.", {"stage": "config"}

    payload = {"cliente_id": cliente_id}
    urls_to_try = [base_url]
    if "/webhook-test/" in base_url:
        urls_to_try.append(base_url.replace("/webhook-test/", "/webhook/"))

    last_details = {}
    for url in urls_to_try:
        t0 = time.time()
        try:
            resp = requests.post(url, json=payload, timeout=40)
            ms = int((time.time() - t0) * 1000)

            if resp.status_code in (404, 410):
                last_details = {"stage": "webhook_not_listening", "http": resp.status_code, "ms": ms, "url": url}
                continue

            if not (200 <= resp.status_code < 300):
                return False, f"Falha webhook (HTTP {resp.status_code}).", {"stage": "http_error"}

            data = resp.json() if resp.text else {}
            if isinstance(data, dict) and data.get("ok") is True:
                return True, f"Fluxo iniciado no n8n ✅", {"stage": "started", "ms": ms, "data": data}

            return True, "Envio concluído ✅", {"stage": "ok_no_confirm", "ms": ms}

        except Exception as e:
            return False, "Erro ao conectar no n8n.", {"stage": "exception", "error": str(e)}

    return False, "Webhook do n8n não está disponível (404/410).", last_details

def load_clientes(q: str, ativo: str, perfil: str, topn: int) -> pd.DataFrame:
    where = []
    params = {}

    if (q or "").strip():
        where.append("""
        (LOWER(c.nome) LIKE :like OR 
         LOWER(c.email) LIKE :like OR 
         LOWER(c.empresa) LIKE :like OR 
         CAST(c.cliente_id AS NVARCHAR(100)) LIKE :like_id)
        """)
        params["like"] = f"%{q.strip().lower()}%"
        params["like_id"] = f"%{q.strip()}%"

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    sql = f"""
    SELECT TOP ({int(topn)})
        c.cliente_id, 
        c.nome, 
        c.email, 
        c.telefone,          /* 👈 Agora lê o telefone REAL do banco */
        c.cargo,             /* 👈 Agora lê o cargo REAL do banco */
        c.empresa, 
        c.perfil_decisor, 
        c.segmento,
        
        c.status_envio,      /* 👈 FIM DO BUG! Agora lê o status exato que o n8n e o banco definem */
        
        c.ultimo_envio, 
        c.proximo_envio, 
        c.ativo, 
        c.updated_at,
        (SELECT COUNT(1) FROM dbo.nps_respostas r WHERE r.cliente_id = c.cliente_id) AS respostas_cliente,
        (SELECT COUNT(1) FROM dbo.nps_respostas r2 WHERE r2.empresa = c.empresa) AS respostas_empresa
    FROM dbo.nps_clientes c
    {where_sql}
    ORDER BY c.updated_at DESC;
    """

    return read_df(sql, params)

def insert_cliente(nome: str, email: str, telefone: str, empresa: str, perfil_decisor: str, segmento: str, cargo: str):
    cliente_id = make_cliente_id(email, empresa)

    sql = """
    INSERT INTO dbo.nps_clientes
      (cliente_id, nome, email, telefone, cargo, empresa, perfil_decisor, segmento,
       ativo, status_envio, ultimo_envio, proximo_envio, ultimo_erro,
       created_at, updated_at)
    VALUES
      (:cliente_id, :nome, :email, :telefone, :cargo, :empresa, :perfil_decisor, :segmento,
       1, 'Pendente', NULL, CAST(GETDATE() AS DATE), NULL,
       SYSUTCDATETIME(), SYSUTCDATETIME());
    """

    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(sql),
            {
                "cliente_id": cliente_id,
                "nome": (nome or "").strip(),
                "email": (email or "").strip(),
                "telefone": (telefone or "").strip() or None,
                "cargo": (cargo or "").strip() or None,
                "empresa": (empresa or "").strip(),
                "perfil_decisor": perfil_decisor,
                "segmento": (segmento or "").strip() or None,
            }
        )

    return cliente_id

def update_cliente(cliente_id, nome, email, telefone, empresa, perfil_decisor, segmento, cargo):
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE dbo.nps_clientes
            SET nome = :nome,
                email = :email,
                telefone = :telefone,
                empresa = :empresa,
                perfil_decisor = :perfil_decisor,
                cargo = :cargo,
                updated_at = GETDATE()
            WHERE cliente_id = :id
        """), {
            "nome": nome, 
            "email": email, 
            "telefone": telefone or "", # Proteção contra valores nulos
            "empresa": empresa or "", 
            "perfil_decisor": perfil_decisor or "",
            "cargo": cargo or "", 
            "id": cliente_id
        })

def set_ativo(cliente_id: str, ativo: int):
    sql = "UPDATE dbo.nps_clientes SET ativo = :ativo, updated_at = SYSUTCDATETIME() WHERE cliente_id = :cliente_id;"
    exec_sql(sql, {"cliente_id": cliente_id, "ativo": int(ativo)})

def delete_cliente(cliente_id: str, delete_respostas: bool = False) -> tuple[bool, str]:
    if delete_respostas:
        sql = "BEGIN TRANSACTION; DELETE FROM dbo.nps_respostas WHERE cliente_id = :cliente_id; DELETE FROM dbo.nps_clientes WHERE cliente_id = :cliente_id; IF @@ROWCOUNT = 1 COMMIT; ELSE ROLLBACK;"
    else:
        sql = "BEGIN TRANSACTION; DELETE FROM dbo.nps_clientes WHERE cliente_id = :cliente_id; IF @@ROWCOUNT = 1 COMMIT; ELSE ROLLBACK;"
    exec_sql(sql, {"cliente_id": cliente_id})
    return True, "Exclusão concluída ✅"

def forcar_envio_db(cliente_id: str):
    sql = """
    UPDATE dbo.nps_clientes
    SET
      ativo = 1,
      status_envio = 'Pendente',
      ultimo_envio = NULL,  -- força reenviar hoje burlando a trava do n8n
      proximo_envio = CAST(GETDATE() AS DATE),
      ultimo_erro = NULL,
      updated_at = SYSUTCDATETIME()
    WHERE cliente_id = :cliente_id;
    """
    exec_sql(sql, {"cliente_id": cliente_id})