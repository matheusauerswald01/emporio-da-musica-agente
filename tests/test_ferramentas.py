"""Testes das ferramentas com os dados reais da loja (data/json), sem chamar o LLM."""

import pytest

from emporio.ferramentas import buscar_produtos, detalhes_produto, promocoes_ativas, status_pedido


def _nomes(resultado: dict) -> set[str]:
    return {produto["nome"] for produto in resultado["produtos"]}


def test_busca_por_categoria_e_preco_traz_so_os_disponiveis():
    violoes = buscar_produtos(categoria="violão", preco_maximo=1000)

    assert len(violoes["produtos"]) == violoes["total_disponiveis"] == 12
    for produto in violoes["produtos"]:
        assert produto["categoria"] == "Violões"
        assert produto["preco_tabela"] <= 1000
        assert produto["situacao"] == "disponível"
    # Sem estoque e descontinuado aparecem à parte, nunca como opção de compra.
    indisponiveis = {produto["nome"]: produto["situacao"] for produto in violoes["indisponiveis"]}
    assert indisponiveis == {
        "Giannini GF-3D Dreadnought Sunburst": "sem estoque",
        "Shelby SN-7C 7 Cordas Nylon Natural": "descontinuado",
    }

    # "Aço" vem das especificações: o nome do GD20 não tem essa palavra.
    assert _nomes(buscar_produtos(nome="Takamine", categoria="violão de aço")) == {
        "Takamine GD20 Dreadnought Natural",
        "Takamine GN51CE Elétrico Aço Natural",
    }
    # Categoria que existe, mas está sem produtos: responde vazio, sem erro.
    assert buscar_produtos(nome="Yamaha", categoria="saxofone")["produtos"] == []
    # Sem baixo de 5 cordas, sugere os baixos que existem (e não "Cordas Orquestrais").
    alternativas = buscar_produtos(nome="5 cordas", categoria="baixo")["alternativas"]
    assert alternativas and all(produto["categoria"] == "Baixos" for produto in alternativas)


@pytest.mark.parametrize(
    ("digitado", "nome_esperado", "preco_esperado"),
    [
        ("takamine gd-20", "Takamine GD20 Dreadnought Natural", 2199),
        ("Kala KA-T", "Kala KA-T Tenor Mogno", 449.9),  # não confunde com Kalani KAL-700T
        ("Martin D-28", "Martin D-28 Dreadnought Natural", 11499),  # nem com o D-28E
    ],
)
def test_detalhes_acha_o_produto_do_jeito_que_o_cliente_escreve(digitado, nome_esperado, preco_esperado):
    resultado = detalhes_produto(digitado)

    assert resultado["encontrado"] is True
    assert resultado["produto"]["nome"] == nome_esperado
    assert resultado["produto"]["preco_tabela"] == preco_esperado


def test_status_pedido_so_responde_ao_dono_do_pedido():
    negado = status_pedido(8, "outra.pessoa@jmail.com")
    assert negado["verificado"] is False
    assert "pedido" not in negado

    # Telefone do dono, digitado em outro formato.
    liberado = status_pedido(8, "+55 67 99654-7821")
    assert liberado["verificado"] is True
    pedido = liberado["pedido"]
    assert pedido["status"] == "enviado"
    assert pedido["codigo_rastreio"] == "BRJL5544332BR"
    assert pedido["previsao_entrega"] == "28/02/2026"
    # Minimização (LGPD): a resposta não devolve e-mail nem telefone do cliente.
    assert "anacarol" not in str(pedido) and "99654" not in str(pedido)

    # E-mail cadastrado para dois clientes (ids 11 e 43) não prova quem é o dono do pedido 10;
    # o dono ainda entra pelo telefone.
    assert status_pedido(10, "bruno.martins@jmail.com")["verificado"] is False
    assert status_pedido(10, "(67) 98543-2109")["verificado"] is True


def test_promocoes_ativas_mostram_preco_original_e_desconto():
    promocoes = {produto["nome"]: produto for produto in promocoes_ativas()["promocoes"]}

    # Só as 4 promoções com is_active = 1 na planilha.
    assert len(promocoes) == 4
    ohana = promocoes["Ohana CK-20 Concert Natural"]
    assert ohana["preco_tabela"] == 549
    assert ohana["promocao"]["desconto_percentual"] == 20
    assert ohana["promocao"]["preco_promocional"] == 439.2


def _preco_final(produto: dict) -> float:
    promocao = produto["promocao"]
    if promocao:
        return promocao["preco_promocional"]
    return produto["preco_tabela"]


def test_filtro_de_preco_usa_o_preco_com_promocao():
    violoes = buscar_produtos(categoria="violão", preco_maximo=1000)

    assert violoes["produtos"]
    for produto in violoes["produtos"]:
        assert _preco_final(produto) <= 1000

    # Nenhum violão entra em "até R$ 1.000" só pela promoção. O Crafter HT-100 entra em "até R$ 2.000":
    # custa R$ 2.399,00, mas sai por R$ 1.967,18 na promoção.
    crafter = buscar_produtos(nome="Crafter", categoria="violão", preco_maximo=2000)["produtos"]
    assert [produto["nome"] for produto in crafter] == ["Crafter HT-100 Folk Aço Natural"]
    assert crafter[0]["preco_tabela"] > 2000 >= _preco_final(crafter[0])


@pytest.mark.parametrize(("nome", "categoria"), [("palheta", None), ("cabo", "guitarra")])
def test_busca_por_acessorio_e_recusada(nome, categoria):
    resultado = buscar_produtos(nome=nome, categoria=categoria)

    assert resultado["fora_do_escopo"] is True
    assert resultado["produtos"] == []


@pytest.mark.parametrize(
    ("nome", "categoria"), [("pré-amplificador", "violão"), ("pedal duplo", "bateria"), ("case", "violão")]
)
def test_instrumento_descrito_com_palavra_de_acessorio_nao_e_recusado(nome, categoria):
    resultado = buscar_produtos(nome=nome, categoria=categoria)

    assert "fora_do_escopo" not in resultado
    # Sem produto com tudo o que foi pedido, a busca sugere instrumentos da mesma categoria.
    assert resultado["alternativas"]
