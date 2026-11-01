"""Carrega os documentos JSON da loja e prepara as tabelas que as ferramentas consultam.

Os JSON em data/json/ são gerados e validados a partir dos CSV originais por
scripts/csv_para_json.py: cada produto já traz categoria, especificações e promoções, e cada
pedido já traz os itens. Por isso aqui não há validação de tipos, só as regras de negócio.
Tudo é lido uma única vez por processo (`obter_base`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import pandas as pd

from emporio.texto import normalizar_telefone, normalizar_texto, palavras_chave

PASTA_DADOS = Path(__file__).resolve().parent.parent / "data" / "json"

# Campos lidos de cada documento. Declarados para que um arquivo sem registros
# (uma loja ainda sem pedidos, por exemplo) carregue como tabela vazia, e não quebre.
CAMPOS = {
    "categorias": ["id_categoria", "categoria", "descricao"],
    "produtos": [
        "id_produto", "nome", "id_categoria", "categoria", "preco_tabela", "estoque",
        "status", "descricao", "especificacoes", "promocoes",
    ],
    "pedidos": [
        "id_pedido", "id_cliente", "data_pedido", "status", "total", "forma_pagamento",
        "codigo_rastreio", "previsao_entrega", "observacao", "itens",
    ],
    "clientes": ["id_cliente", "primeiro_nome", "email", "telefone"],
}

# Palavras de busca tiradas das especificações ("violão de aço" acha o Takamine GD20,
# que não tem "aço" no nome). Nylon é o violão clássico; aço, o folk.
TERMOS_DAS_ESPECIFICACOES = {
    ("cordas", "steel"): "aço folk",
    ("cordas", "nylon"): "nylon clássico",
    ("eletrônica", "yes"): "elétrico eletroacústico",
}
# Especificações numéricas que o cliente cita junto da unidade ("61 teclas", "5 peças").
CONTAGENS_NAS_ESPECIFICACOES = ("cordas", "teclas", "peças")
# Especificações cujo texto vale como palavra de busca ("violão de cedro", "corpo em mogno").
ESPECIFICACOES_PESQUISAVEIS = ("tampo", "fundo e laterais", "braço", "corpo", "cascos", "cor")

# Recursos que só aparecem na descrição e que o cliente pede ("violão com afinador", "para iniciante").
# Só eles entram na busca: o texto livre usa "clássico" e "violão" em outros sentidos
# ("timbre clássico", "uma ponte entre o ukulele e o violão").
RECURSOS_DA_DESCRICAO = frozenset(
    palavras_chave("afinador equalizador iniciante estudante intermediário profissional choro fingerstyle")
)

# Características que valem para a categoria inteira, mas não aparecem no nome dos produtos.
TERMOS_DA_CATEGORIA = {"Violões": "acústico", "Ukuleles": "acústico", "Guitarras": "elétrica"}
CORDAS_PADRAO = {"Violões": 6, "Ukuleles": 4}

# Marcas do catálogo. Uma descrição que cita outra marca que não a do nome está errada na planilha.
MARCAS = (
    "crafter", "fender", "giannini", "gibson", "ibanez", "kala", "kalani", "korg", "martin",
    "music man", "nord", "ohana", "pearl", "prs", "roland", "rozini", "shelby", "tagima",
    "takamine", "tama", "taylor", "yamaha",
)

SITUACAO_DISPONIVEL = "disponível"
SITUACAO_SEM_ESTOQUE = "sem estoque"
SITUACAO_POR_STATUS = {
    "discontinued": "descontinuado",
    "coming_soon": "em breve (ainda não está à venda)",
}


@dataclass(frozen=True)
class BaseDeDados:
    """Tabelas prontas para consulta.

    produtos: um instrumento por linha, com categoria, especificações, promoção ativa, preço final
        e as palavras que o acham na busca.
    categorias: uma categoria por linha, com as palavras que a reconhecem.
    pedidos: um pedido por linha, com os itens e os dados de contato do cliente.
    vocabulario: todas as palavras conhecidas do catálogo.
    """

    produtos: pd.DataFrame
    categorias: pd.DataFrame
    pedidos: pd.DataFrame
    vocabulario: frozenset[str]


@cache
def obter_base() -> BaseDeDados:
    """Base da loja carregada a partir de `data/json/`, lida só na primeira chamada."""
    return carregar_base(PASTA_DADOS)


def carregar_base(pasta: Path) -> BaseDeDados:
    """Lê os documentos JSON da pasta e prepara as tabelas."""
    categorias = _preparar_categorias(_ler_json(pasta, "categorias"))
    produtos = _preparar_produtos(_ler_json(pasta, "produtos"))
    pedidos = _preparar_pedidos(_ler_json(pasta, "pedidos"), _ler_json(pasta, "clientes"), produtos)
    vocabulario = frozenset().union(*produtos["palavras_busca"], *categorias["palavras"])
    return BaseDeDados(produtos=produtos, categorias=categorias, pedidos=pedidos, vocabulario=vocabulario)


def _ler_json(pasta: Path, entidade: str) -> pd.DataFrame:
    with open(pasta / f"{entidade}.json", encoding="utf-8") as arquivo:
        return pd.DataFrame(json.load(arquivo), columns=CAMPOS[entidade])


def _preparar_categorias(categorias: pd.DataFrame) -> pd.DataFrame:
    # A descrição entra para reconhecer instrumentos sem produto cadastrado ("saxofone", "violino").
    categorias["palavras"] = [
        frozenset(palavras_chave(f"{nome} {descricao}"))
        for nome, descricao in zip(categorias["categoria"], categorias["descricao"].fillna(""))
    ]
    return categorias


def _preparar_produtos(produtos: pd.DataFrame) -> pd.DataFrame:
    vigentes = produtos["promocoes"].map(_promocao_vigente)
    produtos["desconto_percentual"] = pd.Series(
        [promocao["desconto_percentual"] if promocao else None for promocao in vigentes],
        index=produtos.index, dtype=float,
    )
    produtos["campanha"] = [promocao["campanha"] if promocao else None for promocao in vigentes]

    produtos["descricao"] = [
        _descricao_confiavel(nome, descricao)
        for nome, descricao in zip(produtos["nome"], produtos["descricao"])
    ]
    produtos["situacao"] = [
        _situacao(status, estoque) for status, estoque in zip(produtos["status"], produtos["estoque"])
    ]
    produtos["disponivel"] = produtos["situacao"] == SITUACAO_DISPONIVEL
    produtos["preco_final"] = (
        produtos["preco_tabela"] * (1 - produtos["desconto_percentual"].fillna(0) / 100)
    ).round(2)
    produtos["palavras_busca"] = [
        _palavras_busca(nome, categoria, especificacoes, descricao)
        for nome, categoria, especificacoes, descricao in zip(
            produtos["nome"], produtos["categoria"], produtos["especificacoes"], produtos["descricao"]
        )
    ]
    # Palavras do tipo de instrumento ("violao", "bateria"), que pesam mais na busca que adjetivos.
    produtos["palavras_tipo"] = produtos["categoria"].map(lambda categoria: frozenset(palavras_chave(categoria)))
    return produtos.drop(columns="promocoes")


def _promocao_vigente(promocoes: list[dict]) -> dict | None:
    """A promoção ativa de maior desconto, pois promoções não são cumulativas."""
    ativas = [promocao for promocao in promocoes if promocao["ativa"]]
    return max(ativas, key=lambda promocao: promocao["desconto_percentual"], default=None)


def _descricao_confiavel(nome: str, descricao: object) -> str | None:
    """Descarta a descrição que cita uma marca diferente da do nome (ex.: Korg descrito como Roland).

    Repassar esse texto ao cliente seria informação errada sobre o produto.
    """
    if pd.isna(descricao) or _marcas_citadas(descricao) - _marcas_citadas(nome):
        return None
    return descricao


def _marcas_citadas(texto: str) -> set[str]:
    normalizado = f" {normalizar_texto(texto)} "
    return {marca for marca in MARCAS if f" {marca} " in normalizado}


def _palavras_busca(
    nome: str, categoria: str, especificacoes: dict[str, str], descricao: object
) -> frozenset[str]:
    """Palavras que acham o produto: nome, categoria, características das especificações e os
    recursos citados na descrição confiável ("afinador embutido").
    """
    # A descrição descartada vira NaN dentro da coluna do pandas.
    recursos = set() if pd.isna(descricao) else set(palavras_chave(descricao)) & RECURSOS_DA_DESCRICAO
    termos = [nome, categoria, TERMOS_DA_CATEGORIA.get(categoria, ""), *recursos]
    termos += [especificacoes.get(rotulo, "") for rotulo in ESPECIFICACOES_PESQUISAVEIS]
    termos += [
        termo for (rotulo, valor), termo in TERMOS_DAS_ESPECIFICACOES.items()
        if especificacoes.get(rotulo) == valor
    ]
    termos += [
        f"{valor} {rotulo}" for rotulo, valor in especificacoes.items()
        if rotulo in CONTAGENS_NAS_ESPECIFICACOES and valor.isdigit()
    ]
    if categoria in CORDAS_PADRAO and "cordas" not in normalizar_texto(nome):
        termos.append(f"{CORDAS_PADRAO[categoria]} cordas")
    return frozenset(palavras_chave(" ".join(termos)))


def _situacao(status: str, estoque: int) -> str:
    if status in SITUACAO_POR_STATUS:
        return SITUACAO_POR_STATUS[status]
    return SITUACAO_DISPONIVEL if estoque > 0 else SITUACAO_SEM_ESTOQUE


def _preparar_pedidos(pedidos: pd.DataFrame, clientes: pd.DataFrame, produtos: pd.DataFrame) -> pd.DataFrame:
    for coluna in ("data_pedido", "previsao_entrega"):
        pedidos[coluna] = pd.to_datetime(pedidos[coluna], errors="coerce")

    nomes_produtos = dict(zip(produtos["id_produto"], produtos["nome"]))
    pedidos["itens"] = pedidos["itens"].map(lambda itens: _itens_com_nome(itens, nomes_produtos))
    return pedidos.merge(_contatos_clientes(clientes), on="id_cliente", how="left")


def _itens_com_nome(itens: list[dict], nomes_produtos: dict[int, str]) -> list[dict]:
    """Itens do pedido como o cliente os reconhece: nome do produto e quantidade."""
    return [{"produto": nomes_produtos[item["id_produto"]], "quantidade": item["quantidade"]} for item in itens]


def _contatos_clientes(clientes: pd.DataFrame) -> pd.DataFrame:
    """Primeiro nome e contatos do cliente, normalizados para conferir quem pergunta pelo pedido (LGPD)."""
    clientes["email_cliente"] = _so_os_unicos(clientes["email"].str.strip().str.lower())
    clientes["telefone_cliente"] = _so_os_unicos(clientes["telefone"].map(normalizar_telefone, na_action="ignore"))
    return clientes[["id_cliente", "primeiro_nome", "email_cliente", "telefone_cliente"]]


def _so_os_unicos(contatos: pd.Series) -> pd.Series:
    """Apaga o contato cadastrado para mais de um cliente: ele não prova quem é o dono do pedido.

    No cadastro, bruno.martins@jmail.com é de dois clientes diferentes (ids 11 e 43).
    O dono ainda consegue se identificar pelo outro contato.
    """
    return contatos.mask(contatos.duplicated(keep=False))
