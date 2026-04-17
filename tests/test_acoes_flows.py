from tests.helpers import (
    api_request,
    create_gestor,
    delete_acao,
    delete_empresa,
    delete_named_ref,
    response_debug,
    unique_suffix,
)


def test_acoes_criacao_status_e_kanban(cleanup_stack):
    gestor_id = create_gestor(f"QA Gestor Acoes {unique_suffix()}", email="gestor.acoes@example.com")
    cleanup_stack.append(lambda: delete_named_ref("nps_gestores", item_id=gestor_id))

    empresa_nome = f"QA Empresa Acoes {unique_suffix()}"
    empresa_resp = api_request(
        "post",
        "/api/cadastros/empresas",
        json={
            "nome": empresa_nome,
            "segmento": "Teste",
            "valor_contrato": 5000,
            "gestor": "",
            "gestor_id": gestor_id,
            "companhia_id": None,
        },
    )
    assert empresa_resp.status_code == 200, response_debug(empresa_resp)

    empresas = api_request("get", "/api/cadastros/empresas")
    empresa_id = next(item["id"] for item in empresas.json() if item["nome"] == empresa_nome)
    cleanup_stack.append(lambda: delete_empresa(empresa_id))

    create_resp = api_request(
        "post",
        "/api/acoes",
        json={
            "resposta_id": None,
            "empresa_id": empresa_id,
            "gestor_id": gestor_id,
            "titulo": "Acao QA",
            "descricao": "Teste integrado de acoes",
            "prioridade": "Alta",
            "prazo_limite": None,
            "resolucao": "Pendente",
        },
    )
    assert create_resp.status_code == 200, response_debug(create_resp)

    list_resp = api_request("get", "/api/acoes")
    assert list_resp.status_code == 200, response_debug(list_resp)
    acao_id = next(item["id"] for item in list_resp.json() if item["titulo"] == "Acao QA")
    cleanup_stack.append(lambda: delete_acao(acao_id))

    pendentes = api_request("get", "/api/acoes", params={"status": "Pendente"})
    assert pendentes.status_code == 200, response_debug(pendentes)
    assert any(item["id"] == acao_id for item in pendentes.json())

    update_resp = api_request(
        "put",
        f"/api/acoes/{acao_id}",
        json={
            "status": "Concluído",
            "prioridade": "Baixa",
            "descricao": "Teste integrado de acoes atualizado",
            "prazo_limite": None,
            "gestor_id": gestor_id,
            "empresa_id": empresa_id,
            "resolucao": "Resolvida",
        },
    )
    assert update_resp.status_code == 200, response_debug(update_resp)

    concluidas = api_request("get", "/api/acoes", params={"status": "Concluído"})
    assert concluidas.status_code == 200, response_debug(concluidas)
    assert any(item["id"] == acao_id for item in concluidas.json())
