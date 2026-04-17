import os
import uuid
from datetime import timedelta

import requests
from sqlalchemy import text

from database import get_engine
from services.auth_svc import create_access_token


BASE_URL = os.getenv("NPS_TEST_BASE_URL", "http://localhost:8000").rstrip("/")
ADMIN_EMAIL = os.getenv("NPS_TEST_ADMIN_EMAIL", "mmmendes@latam.stefanini.com")
ALLOWED_EMAIL_DOMAIN = os.getenv("NPS_TEST_ALLOWED_EMAIL_DOMAIN", "latam.stefanini.com")
DEFAULT_TIMEOUT = int(os.getenv("NPS_TEST_TIMEOUT", "20"))


def unique_suffix() -> str:
    return uuid.uuid4().hex[:8]


def unique_email(prefix: str) -> str:
    return f"{prefix}_{unique_suffix()}@{ALLOWED_EMAIL_DOMAIN}"


def build_url(path: str) -> str:
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return f"{BASE_URL}{path}"


def auth_headers(email: str, tipo: str = "Admin") -> dict[str, str]:
    token = create_access_token({"sub": email, "tipo": tipo}, timedelta(hours=1))
    return {"Authorization": f"Bearer {token}"}


def api_request(method: str, path: str, **kwargs) -> requests.Response:
    kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
    response = requests.request(method, build_url(path), **kwargs)
    return response


def response_debug(response: requests.Response) -> str:
    body = response.text.strip().replace("\n", " ")
    return f"status={response.status_code} body={body[:400]}"


def db_execute(sql: str, params: dict | None = None) -> None:
    with get_engine().begin() as conn:
        conn.execute(text(sql), params or {})


def db_scalar(sql: str, params: dict | None = None):
    with get_engine().connect() as conn:
        return conn.execute(text(sql), params or {}).scalar()


def db_row(sql: str, params: dict | None = None):
    with get_engine().connect() as conn:
        return conn.execute(text(sql), params or {}).mappings().first()


def db_rows(sql: str, params: dict | None = None):
    with get_engine().connect() as conn:
        return conn.execute(text(sql), params or {}).mappings().all()


def ensure_user_ready_for_login(email: str, tipo: str = "Usuário") -> None:
    db_execute(
        """
        UPDATE nps_usuarios
        SET ativo = 1,
            email_verificado = 1,
            tipo = :tipo
        WHERE email = :email
        """,
        {"email": email, "tipo": tipo},
    )


def find_user_id(email: str):
    return db_scalar("SELECT usuario_id FROM nps_usuarios WHERE email = :email", {"email": email})


def delete_user_by_email(email: str) -> None:
    user_id = find_user_id(email)
    if not user_id:
        return
    with get_engine().begin() as conn:
        conn.execute(text("DELETE FROM nps_sessoes_ativas WHERE usuario_id = :id"), {"id": user_id})
        conn.execute(text("DELETE FROM nps_usuarios WHERE usuario_id = :id"), {"id": user_id})


def delete_cliente(cliente_id: str) -> None:
    with get_engine().begin() as conn:
        conn.execute(text("DELETE FROM nps_acoes WHERE resposta_id IN (SELECT resposta_id FROM nps_respostas WHERE cliente_id = :id)"), {"id": cliente_id})
        conn.execute(text("DELETE FROM nps_disparos WHERE cliente_id = :id"), {"id": cliente_id})
        conn.execute(text("DELETE FROM nps_respostas WHERE cliente_id = :id"), {"id": cliente_id})
        conn.execute(text("DELETE FROM nps_clientes WHERE cliente_id = :id"), {"id": cliente_id})


def delete_empresa(empresa_id: int) -> None:
    with get_engine().begin() as conn:
        conn.execute(text("DELETE FROM nps_empresas WHERE id = :id"), {"id": empresa_id})


def delete_resposta(resposta_id: str) -> None:
    with get_engine().begin() as conn:
        conn.execute(text("DELETE FROM nps_acoes WHERE resposta_id = :id"), {"id": resposta_id})
        conn.execute(text("DELETE FROM nps_respostas WHERE resposta_id = :id"), {"id": resposta_id})


def delete_acao(acao_id: int) -> None:
    db_execute("DELETE FROM nps_acoes WHERE id = :id", {"id": acao_id})


def delete_named_ref(table_name: str, item_id: int | None = None, name: str | None = None) -> None:
    if item_id is None and name is None:
        return
    sql = f"DELETE FROM {table_name} WHERE " + ("id = :value" if item_id is not None else "nome = :value")
    db_execute(sql, {"value": item_id if item_id is not None else name})


def find_id_by_name(table_name: str, name: str):
    return db_scalar(f"SELECT id FROM {table_name} WHERE nome = :nome", {"nome": name})


def create_operador_via_api(nome: str, email: str, password: str, cargo: str = "Analista") -> requests.Response:
    return api_request(
        "post",
        "/api/usuarios",
        json={"nome": nome, "email": email, "password": password, "cargo": cargo},
    )


def create_segmento(nome: str) -> int:
    api_request("post", "/api/cadastros/segmentos", json={"nome": nome})
    item_id = find_id_by_name("nps_segmentos", nome)
    assert item_id is not None, f"segmento nao criado: {nome}"
    return int(item_id)


def create_perfil(nome: str) -> int:
    api_request("post", "/api/cadastros/perfis", json={"nome": nome})
    item_id = find_id_by_name("nps_perfis", nome)
    assert item_id is not None, f"perfil nao criado: {nome}"
    return int(item_id)


def create_cargo(nome: str) -> int:
    api_request("post", "/api/cadastros/cargos", json={"nome": nome})
    item_id = find_id_by_name("nps_cargos", nome)
    assert item_id is not None, f"cargo nao criado: {nome}"
    return int(item_id)


def create_gestor(nome: str, email: str = "", papel: str = "", teams_webhook: str = "") -> int:
    api_request(
        "post",
        "/api/cadastros/gestores",
        json={"nome": nome, "papel": papel, "email": email, "teams_webhook": teams_webhook, "avatar": None},
    )
    item_id = find_id_by_name("nps_gestores", nome)
    assert item_id is not None, f"gestor nao criado: {nome}"
    return int(item_id)
