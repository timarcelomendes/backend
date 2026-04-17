from tests.helpers import api_request, db_scalar, response_debug, unique_suffix


def test_configuracoes_leitura_e_persistencia(admin_headers):
    chave_probe = "qa_probe"
    valor_probe = f"ok_{unique_suffix()}"

    leitura_resp = api_request("get", "/api/configuracoes")
    assert leitura_resp.status_code == 200, response_debug(leitura_resp)
    assert "data" in leitura_resp.json()

    escrita_resp = api_request(
        "post",
        "/api/configuracoes",
        json=[{"chave": chave_probe, "valor": valor_probe}],
    )
    assert escrita_resp.status_code == 200, response_debug(escrita_resp)

    persisted = db_scalar("SELECT valor FROM nps_configuracoes WHERE chave = :chave", {"chave": chave_probe})
    assert persisted == valor_probe

    seguranca_get = api_request("get", "/api/config/seguranca", headers=admin_headers)
    assert seguranca_get.status_code == 200, response_debug(seguranca_get)
    tempo_original = seguranca_get.json()["tempo_minutos"]

    seguranca_put = api_request(
        "put",
        "/api/config/seguranca",
        headers=admin_headers,
        json={"tempo_minutos": tempo_original + 5},
    )
    assert seguranca_put.status_code == 200, response_debug(seguranca_put)

    restaurar_seg = api_request(
        "put",
        "/api/config/seguranca",
        headers=admin_headers,
        json={"tempo_minutos": tempo_original},
    )
    assert restaurar_seg.status_code == 200, response_debug(restaurar_seg)

    mostrar_get = api_request("get", "/api/settings/mostrar-sem-cliente")
    assert mostrar_get.status_code == 200, response_debug(mostrar_get)
    valor_original = bool(mostrar_get.json()["valor"])

    mostrar_put = api_request("post", "/api/settings/mostrar-sem-cliente", json={"valor": (not valor_original)})
    assert mostrar_put.status_code == 200, response_debug(mostrar_put)

    mostrar_restore = api_request("post", "/api/settings/mostrar-sem-cliente", json={"valor": valor_original})
    assert mostrar_restore.status_code == 200, response_debug(mostrar_restore)

    dominios_get = api_request("get", "/api/configuracoes/dominios")
    assert dominios_get.status_code == 200, response_debug(dominios_get)
    dominios_originais = dominios_get.json().get("dominios", "")

    novos_dominios = f"qa.{unique_suffix()}.example.com"
    dominios_put = api_request("put", "/api/configuracoes/dominios", json={"dominios": novos_dominios})
    assert dominios_put.status_code == 200, response_debug(dominios_put)

    dominios_restore = api_request("put", "/api/configuracoes/dominios", json={"dominios": dominios_originais})
    assert dominios_restore.status_code == 200, response_debug(dominios_restore)

    integracoes_get = api_request("get", "/api/configuracoes/integracoes", headers=admin_headers)
    assert integracoes_get.status_code == 200, response_debug(integracoes_get)

    regras_get = api_request("get", "/api/config/regras", headers=admin_headers)
    assert regras_get.status_code == 200, response_debug(regras_get)
