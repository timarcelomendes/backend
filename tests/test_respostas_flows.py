from tests.helpers import (
    api_request,
    create_cargo,
    create_gestor,
    create_perfil,
    create_segmento,
    delete_cliente,
    delete_empresa,
    delete_named_ref,
    delete_resposta,
    response_debug,
    unique_email,
    unique_suffix,
)


def test_respostas_listagem_filtros_edicao_e_exclusao_logica(cleanup_stack, admin_headers):
    segmento_id = create_segmento(f"QA Segmento Resp {unique_suffix()}")
    perfil_nome = f"QA Perfil Resp {unique_suffix()}"
    perfil_id = create_perfil(perfil_nome)
    cargo_id = create_cargo(f"QA Cargo Resp {unique_suffix()}")
    gestor_id = create_gestor(f"QA Gestor Resp {unique_suffix()}", email="gestor.resp@example.com")

    cleanup_stack.extend(
        [
            lambda: delete_named_ref("nps_segmentos", item_id=segmento_id),
            lambda: delete_named_ref("nps_perfis", item_id=perfil_id),
            lambda: delete_named_ref("nps_cargos", item_id=cargo_id),
            lambda: delete_named_ref("nps_gestores", item_id=gestor_id),
        ]
    )

    empresa_nome = f"QA Empresa Resp {unique_suffix()}"
    empresa_resp = api_request(
        "post",
        "/api/cadastros/empresas",
        json={
            "nome": empresa_nome,
            "segmento": "Teste",
            "valor_contrato": 1500,
            "gestor": "",
            "gestor_id": gestor_id,
            "companhia_id": None,
        },
    )
    assert empresa_resp.status_code == 200, response_debug(empresa_resp)

    empresas = api_request("get", "/api/cadastros/empresas")
    empresa_id = next(item["id"] for item in empresas.json() if item["nome"] == empresa_nome)
    cleanup_stack.append(lambda: delete_empresa(empresa_id))

    cliente_email = unique_email("qa_resp")
    cliente_resp = api_request(
        "post",
        "/api/clientes",
        json={
            "nome": "Cliente Resposta QA",
            "email": cliente_email,
            "telefone": "11999990000",
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

    manual_resp = api_request(
        "post",
        "/api/respostas/manual",
        headers=admin_headers,
        json={"cliente_id": cliente_id, "nota": 8, "motivo": "Teste respostas QA", "canal": "Manual"},
    )
    assert manual_resp.status_code == 200, response_debug(manual_resp)
    resposta_id = manual_resp.json()["id"]
    cleanup_stack.append(lambda: delete_resposta(resposta_id))

    listagem = api_request("get", "/api/respostas", params={"q": "Cliente Resposta QA", "topn": 20})
    assert listagem.status_code == 200, response_debug(listagem)
    assert any(item["resposta_id"] == resposta_id for item in listagem.json())

    filtrado = api_request(
        "get",
        "/api/respostas",
        params={"perfil": perfil_nome, "empresa": empresa_nome, "topn": 20},
    )
    assert filtrado.status_code == 200, response_debug(filtrado)
    assert any(item["resposta_id"] == resposta_id for item in filtrado.json())

    update_resp = api_request(
        "put",
        f"/api/respostas/{resposta_id}",
        json={
            "nota": 9,
            "categoria": "Atendimento",
            "motivo": "Teste respostas QA atualizado",
            "canal": "Manual",
            "expectativas": "OK",
            "o_que_faltava": "Nada",
        },
    )
    assert update_resp.status_code == 200, response_debug(update_resp)

    soft_delete = api_request("post", f"/api/respostas/{resposta_id}/soft-delete")
    assert soft_delete.status_code == 200, response_debug(soft_delete)

    listagem_excluidas = api_request(
        "get",
        "/api/respostas",
        params={"q": "Cliente Resposta QA", "topn": 20, "incluir_excluidas": True},
    )
    assert listagem_excluidas.status_code == 200, response_debug(listagem_excluidas)
    assert any(item["resposta_id"] == resposta_id for item in listagem_excluidas.json())

    restore_resp = api_request("post", f"/api/respostas/{resposta_id}/restore")
    assert restore_resp.status_code == 200, response_debug(restore_resp)
