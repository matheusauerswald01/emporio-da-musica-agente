"""Ferramentas que o agente chama para responder com dados reais da loja (function calling).

Cada função devolve um dicionário simples, pronto para ser enviado ao LLM como JSON.
As docstrings viram a descrição que o modelo lê para decidir qual ferramenta usar,
por isso dizem quando usar cada uma.
"""

from __future__ import annotations

import re
from datetime import date

import pandas as pd

from emporio.busca import (
    casaram_tipo_de_instrumento,
    categorias_citadas,
    dentro_da_faixa,
    do_mais_barato,
    mais_especificos,
    mais_proximos,
    nomes_parecidos,
    ordenar_por_preco,
    ordenar_por_semelhanca,
    pontuar,
    termos_sem_produto,
)
from emporio.dados import BaseDeDados, obter_base
from emporio.texto import normalizar_telefone, palavras_chave

LIMITE_RESULTADOS = 15
LIMITE_OPCOES = 5
DIGITOS_TELEFONE_COM_DDD = 10

MENSAGEM_PEDIR_CONTATO = (
    "Para consultar o pedido, peça ao cliente o e-mail ou o telefone com DDD usado na compra."
)
# Mesma resposta para "pedido não existe" e "contato não confere": assim não dá para
# descobrir números de pedido de outras pessoas por tentativa (LGPD).
MENSAGEM_PEDIDO_NAO_CONFERE = (
    "Não encontrei um pedido com esse número vinculado a esse e-mail ou telefone. "
    "Peça ao cliente para conferir o número do pedido ou tentar com o outro contato da compra "
    "(e-mail ou telefone com DDD). Por segurança, nenhum detalhe do pedido foi exibido."
)

STATUS_PEDIDO = {
    "pending": ("pendente", "Pedido recebido, aguardando a confirmação do pagamento."),
    "confirmed": ("confirmado", "Pagamento confirmado. O pedido está sendo preparado para envio."),
    "shipped": ("enviado", "Pedido despachado e a caminho do endereço de entrega."),
    "delivered": ("entregue", "Pedido entregue."),
    "cancelled": ("cancelado", "Pedido cancelado."),
}
FORMAS_PAGAMENTO = {"pix": "PIX", "debit": "Cartão de débito", "boleto": "Boleto bancário"}

# Palavras que só aparecem em pedido de acessório. Ficam de fora as que também descrevem um
# instrumento: "amplificador" ("violão com pré-amplificador", em 3 produtos), "pedal" ("bateria com
# pedal duplo") e "case" ("violão com case"). Quem pede esses acessórios é recusado pela persona.
ACESSORIOS = frozenset(palavras_chave("palheta cabo capa correia encordoamento"))

MENSAGEM_ACESSORIOS = (
    "A loja trabalha só com instrumentos musicais e não "
    "vende acessórios (cordas, palhetas, cabos, cases, "
    "pedais, amplificadores). Avise o cliente com "
    "gentileza e ofereça ajuda com instrumentos."
)


def buscar_produtos(
    nome: str | None = None,
    categoria: str | None = None,
    preco_maximo: float | None = None,
    preco_minimo: float | None = None,
) -> dict:
    """Busca instrumentos no catálogo da loja por nome, categoria e faixa de preço.

    Use para perguntas abertas, como "quais violões até R$ 1.000?" ou "tem guitarra Fender?".
    Devolve só os produtos que podem ser comprados agora, do mais barato ao mais caro, e lista
    à parte os que existem mas estão indisponíveis (sem estoque, descontinuados ou em breve).
    Se nada atender à busca, explica o motivo e sugere alternativas disponíveis.
    A loja não vende acessórios (cordas avulsas, cabos, pedais, amplificadores, capas):
    não use esta ferramenta para eles.

    Args:
        nome: marca, modelo ou característica, por exemplo "Yamaha", "GD20", "aço", "nylon",
            "elétrico" ou "7 cordas". Opcional.
        categoria: tipo de instrumento: violão, guitarra, baixo, bateria, teclado, piano,
            ukulele, sopro ou cordas orquestrais. Opcional.
        preco_maximo: preço máximo em reais, já considerando promoções. Opcional.
        preco_minimo: preço mínimo em reais, para ver opções mais caras. Opcional.
    """
    if preco_minimo is not None and preco_maximo is not None and preco_minimo > preco_maximo:
        return {"produtos": [], "mensagem": "O preço mínimo é maior que o máximo. Confirme a faixa com o cliente."}

    base = obter_base()
    termos = frozenset(palavras_chave(" ".join(texto for texto in (nome, categoria) if texto)))
    if termos & ACESSORIOS:
        return {
            "produtos": [],
            "fora_do_escopo": True,
            "mensagem": MENSAGEM_ACESSORIOS,
        }
    pontuados = pontuar(base.produtos, termos, base.vocabulario)
    encontrados = pontuados[pontuados["completo"]]
    if encontrados.empty:
        return _busca_sem_correspondencia(base, pontuados, termos, preco_minimo, preco_maximo)

    na_faixa = encontrados[dentro_da_faixa(encontrados["preco_final"], preco_minimo, preco_maximo)]
    disponiveis = do_mais_barato(na_faixa[na_faixa["disponivel"]])
    indisponiveis = do_mais_barato(na_faixa[~na_faixa["disponivel"]])
    resultado = {
        "total_disponiveis": len(disponiveis),
        "produtos": _resumos(disponiveis.head(LIMITE_RESULTADOS)),
        "indisponiveis": [
            {"nome": produto["nome"], "situacao": produto["situacao"]}
            for produto in _linhas(indisponiveis.head(LIMITE_OPCOES))
        ],
    }
    if len(disponiveis) > LIMITE_RESULTADOS:
        resultado["mensagem"] = (
            f"Mostrando os {LIMITE_RESULTADOS} mais baratos de {len(disponiveis)}. "
            "Para ver outros, refine pelo nome ou use preco_minimo."
        )
    elif disponiveis.empty:
        resultado["mensagem"] = "Nenhum produto disponível atende a todos os filtros."
        resultado["alternativas"] = _alternativas(base.produtos, encontrados, preco_minimo, preco_maximo)
    return resultado


def detalhes_produto(nome: str) -> dict:
    """Mostra preço, estoque, promoção, descrição e especificações de um produto específico.

    Use quando o cliente cita um modelo, por exemplo "quanto custa o Takamine GD20?".
    Se o produto estiver indisponível, já traz alternativas parecidas que estão à venda.
    Se o nome servir para mais de um produto, ou para nenhum exatamente, devolve as opções
    mais próximas para você confirmar com o cliente. As especificações valem mais que a descrição.

    Args:
        nome: nome ou modelo do produto, por exemplo "Takamine GD20".
    """
    base = obter_base()
    pontuados = pontuar(base.produtos, frozenset(palavras_chave(nome)), base.vocabulario)
    encontrados = mais_especificos(pontuados[pontuados["completo"]], nome)

    if len(encontrados) == 1:
        produto = _linhas(encontrados)[0]
        resposta = {"encontrado": True, "produto": _detalhes(produto)}
        if not produto["disponivel"]:
            resposta["alternativas"] = _alternativas(base.produtos, encontrados)
        return resposta
    if len(encontrados) > 1:
        return {
            "encontrado": False,
            "mensagem": f"{len(encontrados)} produtos correspondem a esse nome. Pergunte ao cliente qual deles.",
            "opcoes": _resumos(do_mais_barato(encontrados).head(LIMITE_OPCOES)),
        }
    proximos = mais_proximos(pontuados)
    if not proximos.empty:
        return {
            "encontrado": False,
            "mensagem": "Nenhum produto tem exatamente esse nome. Estes são os mais próximos; confirme com o cliente.",
            "opcoes": _resumos(do_mais_barato(proximos).head(LIMITE_OPCOES)),
        }
    return {
        "encontrado": False,
        "mensagem": "Produto não encontrado no catálogo.",
        "nomes_parecidos": nomes_parecidos(base.produtos, nome, LIMITE_OPCOES),
    }


def status_pedido(numero_pedido: int, email_ou_telefone: str) -> dict:
    """Consulta status, itens, código de rastreio e previsão de entrega de um pedido.

    Por causa da LGPD, só mostra o pedido se o e-mail ou o telefone informado for o do dono.
    Antes de chamar, peça ao cliente o número do pedido e o e-mail ou telefone com DDD da compra.
    Nunca invente esses dados nem use os de outra conversa.

    Args:
        numero_pedido: número do pedido, por exemplo 12.
        email_ou_telefone: e-mail ou telefone com DDD informado pelo cliente.
    """
    contato = email_ou_telefone.strip()
    if "@" not in contato and len(normalizar_telefone(contato)) < DIGITOS_TELEFONE_COM_DDD:
        return {"verificado": False, "mensagem": MENSAGEM_PEDIR_CONTATO}

    pedidos = obter_base().pedidos
    pedido = _linhas(pedidos[pedidos["id_pedido"] == numero_pedido])
    if not pedido or not _contato_confere(pedido[0], contato):
        return {"verificado": False, "mensagem": MENSAGEM_PEDIDO_NAO_CONFERE}
    return {"verificado": True, "pedido": _detalhes_pedido(pedido[0])}


def promocoes_ativas() -> dict:
    """Lista as promoções ativas agora, com preço original, desconto e preço promocional.

    Use quando o cliente perguntar por promoções, ofertas ou descontos. Ao responder,
    mostre sempre o preço original junto do percentual de desconto, como o manual exige.
    """
    produtos = obter_base().produtos
    em_promocao = produtos[produtos["desconto_percentual"].notna()].sort_values(
        ["desconto_percentual", "nome"], ascending=[False, True]
    )
    if em_promocao.empty:
        return {"promocoes": [], "mensagem": "Nenhuma promoção ativa no momento."}
    return {"promocoes": _resumos(em_promocao)}


def _busca_sem_correspondencia(
    base: BaseDeDados,
    pontuados: pd.DataFrame,
    termos: frozenset[str],
    preco_minimo: float | None,
    preco_maximo: float | None,
) -> dict:
    """Explica por que a busca não achou nada e sugere o que a loja tem de mais próximo.

    Na ordem:
    1. Os mais próximos casaram com o tipo de instrumento pedido ("baixo 5 cordas"): eles.
    2. A busca cita um instrumento de uma categoria sem produtos ("saxofone tenor"): a categoria,
       e não ukuleles só porque também existem ukuleles tenor.
    3. Algum produto casou com parte da busca ("Gibson SG"): os mais próximos.
    4. Nada casou: diz que não encontrou e lista as categorias da loja.
    """
    proximos = mais_proximos(pontuados)
    if not proximos.empty and casaram_tipo_de_instrumento(proximos, termos, base.vocabulario):
        return _mais_proximos(base, proximos, preco_minimo, preco_maximo)

    sem_produto = termos_sem_produto(base.produtos, termos, base.vocabulario)
    categorias = categorias_citadas(base.categorias, sem_produto, base.vocabulario)
    if not categorias.empty:
        return _categoria_sem_correspondencia(base, categorias, preco_minimo, preco_maximo)
    if not proximos.empty:
        return _mais_proximos(base, proximos, preco_minimo, preco_maximo)
    return {
        "produtos": [],
        "mensagem": "Não encontrei nada no catálogo para essa busca.",
        "categorias_da_loja": base.categorias["categoria"].tolist(),
    }


def _mais_proximos(
    base: BaseDeDados, proximos: pd.DataFrame, preco_minimo: float | None, preco_maximo: float | None
) -> dict:
    return {
        "produtos": [],
        "mensagem": "Nenhum produto tem todas as características pedidas. Estes são os mais próximos.",
        "alternativas": _alternativas(base.produtos, proximos, preco_minimo, preco_maximo),
    }


def _categoria_sem_correspondencia(
    base: BaseDeDados, categorias: pd.DataFrame, preco_minimo: float | None, preco_maximo: float | None
) -> dict:
    da_categoria = base.produtos[base.produtos["id_categoria"].isin(categorias["id_categoria"])]
    alternativas = _alternativas(base.produtos, da_categoria, preco_minimo, preco_maximo)
    mensagem = f"Nenhum produto corresponde à busca. A loja trabalha com {', '.join(categorias['categoria'])}"
    mensagem += "; veja as alternativas." if alternativas else ", mas no momento não há produtos disponíveis."
    return {"produtos": [], "mensagem": mensagem, "alternativas": alternativas}


def _alternativas(
    produtos: pd.DataFrame,
    referencia: pd.DataFrame,
    preco_minimo: float | None = None,
    preco_maximo: float | None = None,
) -> list[dict]:
    """Opções à venda para sugerir no lugar da referência (manual, item 7.3).

    Se a referência tem produtos disponíveis (ficaram de fora só pelo preço ou por um detalhe),
    são eles, primeiro os que cabem na faixa de preço. Se nenhum está disponível, os produtos
    da mesma categoria mais parecidos com a referência.
    """
    disponiveis = referencia[referencia["disponivel"]]
    if not disponiveis.empty:
        return _resumos(ordenar_por_preco(disponiveis, preco_minimo, preco_maximo).head(LIMITE_OPCOES))

    mesma_categoria = produtos["id_categoria"].isin(referencia["id_categoria"])
    candidatos = produtos[mesma_categoria & produtos["disponivel"]]
    if preco_minimo is not None or preco_maximo is not None:
        ordenados = ordenar_por_preco(candidatos, preco_minimo, preco_maximo)
    else:
        ordenados = ordenar_por_semelhanca(candidatos, referencia)
    return _resumos(ordenados.head(LIMITE_OPCOES))


def _linhas(tabela: pd.DataFrame) -> list[dict]:
    return tabela.to_dict("records")


def _resumos(produtos: pd.DataFrame) -> list[dict]:
    return [_resumo(produto) for produto in _linhas(produtos)]


def _resumo(produto: dict) -> dict:
    return {
        "nome": produto["nome"],
        "categoria": produto["categoria"],
        "preco_tabela": _numero(produto["preco_tabela"]),
        "promocao": _promocao(produto),
        "situacao": produto["situacao"],
        "estoque": int(produto["estoque"]),
    }


def _detalhes(produto: dict) -> dict:
    return {
        **_resumo(produto),
        "descricao": _texto_ou_nada(produto["descricao"]),
        # Cópia: quem usar o resultado não pode alterar a base compartilhada em cache.
        "especificacoes": dict(produto["especificacoes"]),
    }


def _promocao(produto: dict) -> dict | None:
    if pd.isna(produto["desconto_percentual"]):
        return None
    return {
        "campanha": produto["campanha"],
        "desconto_percentual": _numero(produto["desconto_percentual"]),
        "preco_promocional": _numero(produto["preco_final"]),
    }


def _contato_confere(pedido: dict, contato: str) -> bool:
    """Compara com o contato do cadastro. Contato apagado por ser de mais de um cliente nunca confere."""
    if "@" in contato:
        return contato.lower() == pedido["email_cliente"]
    return normalizar_telefone(contato) == pedido["telefone_cliente"]


def _detalhes_pedido(pedido: dict) -> dict:
    """Dados do pedido para o cliente. Do cadastro, só o primeiro nome (minimização de dados, LGPD)."""
    status, descricao_status = STATUS_PEDIDO.get(pedido["status"], (pedido["status"], ""))
    previsao = pedido["previsao_entrega"]
    return {
        "numero": int(pedido["id_pedido"]),
        "primeiro_nome_cliente": pedido["primeiro_nome"],
        "data_pedido": _data_br(pedido["data_pedido"]),
        "status": status,
        "descricao_status": descricao_status,
        "itens": [dict(item) for item in pedido["itens"]],
        "total": _numero(pedido["total"]),
        "forma_pagamento": _forma_pagamento(pedido["forma_pagamento"]),
        "codigo_rastreio": _texto_ou_nada(pedido["codigo_rastreio"]),
        "previsao_entrega": _data_br(previsao),
        "entrega_atrasada": bool(
            pedido["status"] == "shipped" and pd.notna(previsao) and previsao.date() < date.today()
        ),
        "observacao": _texto_ou_nada(pedido["observacao"]),
    }


def _forma_pagamento(codigo: object) -> str | None:
    if pd.isna(codigo):
        return None
    parcelado = re.fullmatch(r"credit_(\d+)x", codigo)
    if parcelado:
        return f"Cartão de crédito em {parcelado.group(1)}x"
    return FORMAS_PAGAMENTO.get(codigo, codigo)


def _data_br(data: pd.Timestamp) -> str | None:
    return None if pd.isna(data) else data.strftime("%d/%m/%Y")


def _texto_ou_nada(valor: object) -> str | None:
    return None if pd.isna(valor) else str(valor)


def _numero(valor: float) -> int | float:
    """Número sem casas decimais inúteis: 2199.0 vira 2199 e 599.9 continua 599.9."""
    valor = round(float(valor), 2)
    return int(valor) if valor.is_integer() else valor
