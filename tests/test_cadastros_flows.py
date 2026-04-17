from tests.helpers import (
    api_request,
    create_cargo,
    create_gestor,
    create_perfil,
    create_segmento,
    delete_cliente,
    delete_empresa,
    delete_named_ref,
    response_debug,
    unique_email,
    unique_suffix,
)


def test_cadastros_base_crud(cleanup_stack):
    segmento_nome = f"QA Segmento {unique_suffix()}"
    perfil_nome = f"QA Perfil {unique_suffix()}"
    cargo_nome = f"QA Cargo {unique_suffix()}"
    gestor_nome = f"QA Gestor {unique_suffix()}"

    segmento_id = create_segmento(segmento_nome)
    perfil_id = create_perfil(perfil_nome)
    cargo_id = create_cargo(cargo_nome)
    gestor_id = create_gestor(gestor_nome, email="gestor.qa@example.com", papel="CS")

    cleanup_stack.extend(
        [
            lambda: delete_named_ref("nps_segmentos", item_id=segmento_id),
            lambda: delete_named_ref("nps_perfis", item_id=perfil_id),
            lambda: delete_named_ref("nps_cargos", item_id=cargo_id),
            lambda: delete_named_ref("nps_gestores", item_id=gestor_id),
        ]
    )

    for route, item_id, novo_nome in [
        ("segmentos", segmento_id, segmento_nome + " Atualizado"),
        ("perfis", perfil_id, perfil_nome + " Atualizado"),
        ("cargos", cargo_id, cargo_nome + " Atualizado"),
    ]:
        list_resp = api_request("get", f"/api/cadastros/{route}")
        assert list_resp.status_code == 200, response_debug(list_resp)
        assert any(item["id"] == item_id for item in list_resp.json())

        update_resp = api_request("put", f"/api/cadastros/{route}/{item_id}", json={"nome": novo_nome})
        assert update_resp.status_code == 200, response_debug(update_resp)

    gestor_update = api_request(
        "put",
        f"/api/cadastros/gestores/{gestor_id}",
        json={
            "nome": gestor_nome + " Atualizado",
            "papel": "Owner",
            "email": "gestor.atualizado@example.com",
            "teams_webhook": "",
            "avatar": None,
        },
    )
    assert gestor_update.status_code == 200, response_debug(gestor_update)


def test_clientes_empresas_e_refs_integrados(cleanup_stack, admin_headers, auth_headers_for):
    segmento_id = create_segmento(f"QA Segmento Cliente {unique_suffix()}")
    perfil_id = create_perfil(f"QA Perfil Cliente {unique_suffix()}")
    cargo_id = create_cargo(f"QA Cargo Cliente {unique_suffix()}")
    gestor_id = create_gestor(f"QA Gestor Cliente {unique_suffix()}", email="gestor.cliente@example.com")

    cleanup_stack.extend(
        [
            lambda: delete_named_ref("nps_segmentos", item_id=segmento_id),
            lambda: delete_named_ref("nps_perfis", item_id=perfil_id),
            lambda: delete_named_ref("nps_cargos", item_id=cargo_id),
            lambda: delete_named_ref("nps_gestores", item_id=gestor_id),
        ]
    )

    empresa_nome = f"QA Empresa {unique_suffix()}"
    empresa_resp = api_request(
        "post",
        "/api/cadastros/empresas",
        json={
            "nome": empresa_nome,
            "segmento": "Teste",
            "valor_contrato": 1234.5,
            "gestor": "",
            "gestor_id": gestor_id,
            "companhia_id": None,
        },
    )
    assert empresa_resp.status_code == 200, response_debug(empresa_resp)

    empresas = api_request("get", "/api/cadastros/empresas")
    assert empresas.status_code == 200, response_debug(empresas)
    empresa_id = next(item["id"] for item in empresas.json() if item["nome"] == empresa_nome)
    cleanup_stack.append(lambda: delete_empresa(empresa_id))

    update_empresa = api_request(
        "put",
        f"/api/cadastros/empresas/{empresa_id}",
        json={
            "nome": empresa_nome + " Upd",
            "segmento": "Teste2",
            "valor_contrato": 2222.0,
            "gestor": "",
            "gestor_id": gestor_id,
            "companhia_id": None,
        },
    )
    assert update_empresa.status_code == 200, response_debug(update_empresa)

    status_empresa = api_request("put", f"/api/empresas/{empresa_id}/status", json={"ativo": False})
    assert status_empresa.status_code == 200, response_debug(status_empresa)

    cliente_email = unique_email("qa_cliente")
    cliente_resp = api_request(
        "post",
        "/api/clientes",
        json={
            "nome": "Cliente QA",
            "email": cliente_email,
            "telefone": "11999999999",
            "empresa_id": empresa_id,
            "perfil_id": perfil_id,
            "segmento_id": segmento_id,
            "cargo_id": cargo_id,
            "gestor": "",
        },
    )
    assert cliente_resp.status_code == 200, response_debug(cliente_resp)
    cliente_id = cliente_resp.json()["cliente_id"]
    cleanup_stack.append(lambda: delete_cliente(cliente_id))

    list_clientes = api_request(
        "get",
        "/api/clientes",
        headers=admin_headers,
        params={"q": cliente_email, "ativo": "Todos", "perfil": "Todos", "topn": 50},
    )
    assert list_clientes.status_code == 200, response_debug(list_clientes)
    assert any(item["cliente_id"] == cliente_id for item in list_clientes.json())

    update_cliente = api_request(
        "put",
        f"/api/clientes/{cliente_id}",
        json={
            "nome": "Cliente QA Atualizado",
            "email": cliente_email,
            "telefone": "11888888888",
            "empresa_id": empresa_id,
            "perfil_id": perfil_id,
            "segmento_id": segmento_id,
            "cargo_id": cargo_id,
            "gestor": "",
            "ativo": True,
        },
    )
    assert update_cliente.status_code == 200, response_debug(update_cliente)

    status_cliente = api_request("put", f"/api/clientes/{cliente_id}/status", json={"ativo": False})
    assert status_cliente.status_code == 200, response_debug(status_cliente)

    delete_cliente_resp = api_request("delete", f"/api/clientes/{cliente_id}", headers=admin_headers)
    assert delete_cliente_resp.status_code == 200, response_debug(delete_cliente_resp)
