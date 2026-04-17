from tests.helpers import (
    ADMIN_EMAIL,
    api_request,
    create_operador_via_api,
    create_access_token,
    delete_user_by_email,
    ensure_user_ready_for_login,
    find_user_id,
    response_debug,
    unique_email,
)


def test_autenticacao_fluxos_principais(cleanup_stack, auth_headers_for):
    email = unique_email("qa_auth")
    senha_inicial = "Temp1234!Aa"
    senha_alterada = "NovaSenha123!Aa"
    senha_resetada = "ResetSenha123!Aa"

    cleanup_stack.append(lambda: delete_user_by_email(email))

    create_resp = create_operador_via_api("QA Auth", email, senha_inicial)
    assert create_resp.status_code == 200, response_debug(create_resp)

    ensure_user_ready_for_login(email, tipo="Usuário")
    usuario_id = find_user_id(email)
    assert usuario_id is not None

    login_invalido = api_request(
        "post",
        "/api/login",
        json={"email": email, "password": "senha_errada_123", "remember": False},
    )
    assert login_invalido.status_code == 401, response_debug(login_invalido)

    alterar_resp = api_request(
        "post",
        "/api/usuarios/alterar-senha",
        headers=auth_headers_for(email, "Usuário"),
        json={"senha_atual": "ignorada_pela_rota", "nova_senha": senha_alterada},
    )
    assert alterar_resp.status_code == 200, response_debug(alterar_resp)

    login_valido = api_request(
        "post",
        "/api/login",
        json={"email": email, "password": senha_alterada, "remember": False},
    )
    assert login_valido.status_code == 200, response_debug(login_valido)
    assert login_valido.json().get("access_token")

    esqueci_resp = api_request("post", "/api/esqueci-senha", json={"email": email})
    assert esqueci_resp.status_code == 200, response_debug(esqueci_resp)

    token_reset = create_access_token({"sub": email, "tipo": "reset"})
    reset_resp = api_request(
        "post",
        "/api/reset-password",
        json={"token": token_reset, "nova_senha": senha_resetada},
    )
    assert reset_resp.status_code == 200, response_debug(reset_resp)

    login_reset = api_request(
        "post",
        "/api/login",
        json={"email": email, "password": senha_resetada, "remember": True},
    )
    assert login_reset.status_code == 200, response_debug(login_reset)

    reset_manual = api_request("post", f"/api/usuarios/{usuario_id}/reset-manual")
    assert reset_manual.status_code == 200, response_debug(reset_manual)
    senha_provisoria = reset_manual.json().get("senha_provisoria")
    assert senha_provisoria

    login_manual = api_request(
        "post",
        "/api/login",
        json={"email": email, "password": senha_provisoria, "remember": False},
    )
    assert login_manual.status_code == 200, response_debug(login_manual)


def test_sessoes_ativas_listagem_e_revogacao(cleanup_stack):
    email = unique_email("qa_sessao")
    senha = "Sessao123!Aa"

    cleanup_stack.append(lambda: delete_user_by_email(email))

    create_resp = create_operador_via_api("QA Sessao", email, senha)
    assert create_resp.status_code == 200, response_debug(create_resp)

    ensure_user_ready_for_login(email, tipo="Usuário")
    usuario_id = int(find_user_id(email))

    login_resp = api_request(
        "post",
        "/api/login",
        json={"email": email, "password": senha, "remember": False},
        headers={"User-Agent": "pytest-suite"},
    )
    assert login_resp.status_code == 200, response_debug(login_resp)

    sessoes_resp = api_request("get", f"/api/usuarios/sessoes?usuario_id={usuario_id}")
    assert sessoes_resp.status_code == 200, response_debug(sessoes_resp)
    sessoes = sessoes_resp.json()
    assert sessoes, "nenhuma sessao ativa encontrada apos login"

    encerrar_resp = api_request("delete", f"/api/usuarios/sessoes/{sessoes[0]['id']}")
    assert encerrar_resp.status_code == 200, response_debug(encerrar_resp)
