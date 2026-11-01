"""Casa o que o cliente pediu com os produtos do catálogo.

Cada palavra buscada que o produto tem vale 1 ponto, e a do tipo de instrumento ("violão",
"bateria") vale 2. Assim "bateria elétrica" fica com as baterias, e não com as guitarras.
Uma palavra que não existe no catálogo também casa com o começo de outra ("strato" acha
Stratocaster). Palavras conhecidas não fazem isso, para "Kala" não achar "Kalani".
"""

from __future__ import annotations

import difflib

import pandas as pd

from emporio.texto import normalizar_texto

PESO_TIPO_DE_INSTRUMENTO = 2
TAMANHO_MINIMO_PREFIXO = 4
# Semelhança mínima (0 a 1) para sugerir um nome quando o cliente digita errado.
SEMELHANCA_MINIMA = 0.75


def termos_presentes(termos: frozenset[str], palavras: frozenset[str], vocabulario: frozenset[str]) -> set[str]:
    """Palavras buscadas que estão entre `palavras`, por inteiro ou, se desconhecidas, como começo."""
    return {
        termo
        for termo in termos
        if termo in palavras
        or (
            termo not in vocabulario
            and len(termo) >= TAMANHO_MINIMO_PREFIXO
            and any(palavra.startswith(termo) for palavra in palavras)
        )
    }


def pontuar(produtos: pd.DataFrame, termos: frozenset[str], vocabulario: frozenset[str]) -> pd.DataFrame:
    """Produtos com as colunas `pontos` e `completo` (tem todas as palavras buscadas)."""
    achados = [termos_presentes(termos, palavras, vocabulario) for palavras in produtos["palavras_busca"]]
    pontos = [
        sum(PESO_TIPO_DE_INSTRUMENTO if termo in tipo else 1 for termo in termos_achados)
        for termos_achados, tipo in zip(achados, produtos["palavras_tipo"])
    ]
    return produtos.assign(
        pontos=pd.Series(pontos, index=produtos.index, dtype=int),
        completo=pd.Series([len(termos_achados) == len(termos) for termos_achados in achados],
                           index=produtos.index, dtype=bool),
    )


def mais_proximos(pontuados: pd.DataFrame) -> pd.DataFrame:
    """Os produtos de maior pontuação, para quando nenhum tem todas as palavras buscadas."""
    maior = pontuados["pontos"].max()
    if pontuados.empty or maior == 0:
        return pontuados.iloc[0:0]
    return pontuados[pontuados["pontos"] == maior]


def casaram_tipo_de_instrumento(
    produtos: pd.DataFrame, termos: frozenset[str], vocabulario: frozenset[str]
) -> bool:
    """Diz se algum produto casou com o tipo de instrumento pedido ("baixo" em "baixo 5 cordas"),
    e não só com um adjetivo ("tenor" em "saxofone tenor")."""
    return any(termos_presentes(termos, tipo, vocabulario) for tipo in produtos["palavras_tipo"])


def termos_sem_produto(produtos: pd.DataFrame, termos: frozenset[str], vocabulario: frozenset[str]) -> frozenset[str]:
    """Palavras buscadas que nenhum produto tem, como "saxofone" ou "violino"."""
    com_produto = set().union(
        *(termos_presentes(termos, palavras, vocabulario) for palavras in produtos["palavras_busca"])
    )
    return termos - com_produto


def categorias_citadas(
    categorias: pd.DataFrame, termos: frozenset[str], vocabulario: frozenset[str]
) -> pd.DataFrame:
    """Categorias com mais palavras em comum com a busca ("saxofone" acha Sopro (Madeiras))."""
    em_comum = categorias["palavras"].map(lambda palavras: len(termos_presentes(termos, palavras, vocabulario)))
    if categorias.empty or em_comum.max() == 0:
        return categorias.iloc[0:0]
    return categorias[em_comum == em_comum.max()]


def mais_especificos(encontrados: pd.DataFrame, nome: str) -> pd.DataFrame:
    """Entre vários resultados, fica com o que tem o nome buscado exato ou como trecho inteiro.

    Assim "Martin D-28" escolhe o D-28, e não também o D-28E.
    """
    buscado = normalizar_texto(nome)
    criterios = (
        lambda nome_produto: nome_produto == buscado,
        lambda nome_produto: f" {buscado} " in f" {nome_produto} ",
    )
    nomes = encontrados["nome"].map(normalizar_texto)
    for criterio in criterios:
        unico = encontrados[nomes.map(criterio).astype(bool)]
        if len(unico) == 1:
            return unico
    return encontrados


def dentro_da_faixa(precos: pd.Series, preco_minimo: float | None, preco_maximo: float | None) -> pd.Series:
    dentro = pd.Series(True, index=precos.index, dtype=bool)
    if preco_minimo is not None:
        dentro &= precos >= preco_minimo
    if preco_maximo is not None:
        dentro &= precos <= preco_maximo
    return dentro


def ordenar_por_preco(
    produtos: pd.DataFrame, preco_minimo: float | None, preco_maximo: float | None
) -> pd.DataFrame:
    """Primeiro os que cabem na faixa de preço; depois, os mais perto dela. Sem faixa, os mais baratos."""
    precos = produtos["preco_final"]
    abaixo = (preco_minimo - precos).clip(lower=0) if preco_minimo is not None else 0
    acima = (precos - preco_maximo).clip(lower=0) if preco_maximo is not None else 0
    return (
        produtos.assign(_distancia_da_faixa=abaixo + acima)
        .sort_values(["_distancia_da_faixa", "preco_final"])
        .drop(columns="_distancia_da_faixa")
    )


def ordenar_por_semelhanca(candidatos: pd.DataFrame, referencia: pd.DataFrame) -> pd.DataFrame:
    """Os mais parecidos com a referência vêm primeiro (manual, item 7.3: modelos equivalentes).

    Ordem: preço na mesma faixa (da metade ao dobro do da referência), mais características em
    comum (cordas, nylon ou aço, formato, madeira, marca) e, por fim, o preço mais próximo.
    """
    palavras_da_referencia = frozenset().union(*referencia["palavras_busca"])
    preco_da_referencia = referencia["preco_final"].median()
    precos = candidatos["preco_final"]
    return (
        candidatos.assign(
            _fora_da_faixa=~dentro_da_faixa(precos, preco_da_referencia / 2, preco_da_referencia * 2),
            _em_comum=candidatos["palavras_busca"].map(lambda palavras: len(palavras & palavras_da_referencia)),
            _distancia=(precos - preco_da_referencia).abs(),
        )
        .sort_values(["_fora_da_faixa", "_em_comum", "_distancia"], ascending=[True, False, True])
        .drop(columns=["_fora_da_faixa", "_em_comum", "_distancia"])
    )


def nomes_parecidos(produtos: pd.DataFrame, nome: str, limite: int) -> list[str]:
    """Nomes que começam de forma parecida com o buscado, para corrigir erros de digitação."""
    buscado = normalizar_texto(nome)
    quantidade_palavras = len(buscado.split())

    def semelhanca(nome_produto: str) -> float:
        inicio = " ".join(normalizar_texto(nome_produto).split()[:quantidade_palavras])
        return difflib.SequenceMatcher(None, buscado, inicio).ratio()

    notas = produtos["nome"].map(semelhanca)
    parecidos = produtos.assign(nota=notas)[notas >= SEMELHANCA_MINIMA]
    return parecidos.sort_values("nota", ascending=False)["nome"].head(limite).tolist()
