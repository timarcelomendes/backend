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


def test_disparos_email_fila_envio_status_e_erros(cleanup_stack, admin_headers):
    segmento_id = create_segmento(f"QA Segmento Email {unique_suffix()}")
    perfil_id = create_perfil(f"QA Perfil Email {unique_suffix()}")
    cargo_id = create_cargo(f"QA Cargo Email {unique_suffix()}")
    gestor_id = create_gestor(f"QA Gestor Email {unique_suffix()}")

    cleanup_stack.extend(
        [
            lambda: delete_named_ref("nps_segmentos", item_id=segmento_id),
            lambda: delete_named_ref("nps_perfis", item_id=perfil_id),
            lambda: delete_named_ref("nps_cargos", item_id=cargo_id),
            lambda: delete_named_ref("nps_gestores", item_id=gestor_id),
        ]
    )

    empresa_nome = f"QA Empresa Email {unique_suffix()}"
    empresa_resp = api_request(
        "post",
        "/api/cadastros/empresas",
        json={
            "nome": empresa_nome,
            "segmento": "Teste",
            "valor_contrato": 1800,
            "gestor": "",
            "gestor_id": gestor_id,
            "companhia_id": None,
        },
    )
    assert empresa_resp.status_code == 200, response_debug(empresa_resp)

    empresas = api_request("get", "/api/cadastros/empresas")
    empresa_id = next(item["id"] for item in empresas.json() if item["nome"] == empresa_nome)
    cleanup_stack.append(lambda: delete_empresa(empresa_id))

    cliente_email = unique_email("qa_email")
    cliente_resp = api_request(
        "post",
        "/api/clientes",
        json={
            "nome": "Cliente Email QA",
            "email": cliente_email,
            "telefone": "11911111111",
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

    fila_resp = api_request("get", "/api/config/nps/elegiveis")
    assert fila_resp.status_code == 200, response_debug(fila_resp)
    assert isinstance(fila_resp.json().get("total"), int)

    forcar_resp = api_request("post", f"/api/clientes/{cliente_id}/forcar-envio", headers=admin_headers)
    assert forcar_resp.status_code == 200, response_debug(forcar_resp)

    lote_resp = api_request(
        "post",
        "/api/clientes/forcar-envio-lote",
        headers=admin_headers,
        json={"cliente_ids": [cliente_id]},
    )
    assert lote_resp.status_code == 200, response_debug(lote_resp)

    disparo_resp = api_request("post", "/api/config/nps/forcar-disparo")
    assert disparo_resp.status_code == 200, response_debug(disparo_resp)

    integracoes_resp = api_request("get", "/api/configuracoes/integracoes", headers=admin_headers)
    assert integracoes_resp.status_code == 200, response_debug(integracoes_resp)

    regras_resp = api_request("get", "/api/config/regras", headers=admin_headers)
    assert regras_resp.status_code == 200, response_debug(regras_resp)

    email_cfg_resp = api_request("get", "/api/config/email")
    assert email_cfg_resp.status_code == 200, response_debug(email_cfg_resp)

    erro_controlado = api_request(
        "post",
        "/api/dashboard/acionar-gestor",
        json={"empresa": empresa_nome, "gestor": "Gestor Inexistente", "nps": 3},
    )
    assert erro_controlado.status_code == 400, response_debug(erro_controlado)
