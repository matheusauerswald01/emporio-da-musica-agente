"""Testes do manual de políticas: divisão do PDF em seções e busca, com embeddings falsos (sem o Gemini)."""

import numpy as np

from emporio import politicas
from emporio.politicas import CABECALHO, consultar_politicas, criar_indice, ler_secoes


def test_manual_dividido_nas_10_secoes_numeradas():
    secoes = ler_secoes()

    assert [secao.titulo.split(".")[0] for secao in secoes] == [str(numero) for numero in range(1, 11)]
    trocas = secoes[3]
    assert trocas.titulo == "4. Política de Trocas e Devoluções"
    # A seção chega inteira: o prazo de arrependimento (4.1) junto dos itens sem troca (4.4).
    assert "7 (sete) dias corridos" in trocas.texto and "Boquilhas" in trocas.texto
    # A 5.2 começa numa página e termina na outra; a tabela de prazos mantém as colunas.
    assert "PAC (Correios) | 5 a 12 dias úteis | Sim | Incluído" in secoes[4].texto
    # Cabeçalho e rodapé das páginas não entram no texto.
    assert not any(CABECALHO in secao.texto or "Página " in secao.texto for secao in secoes)


def test_consulta_traz_primeiro_a_secao_mais_parecida_e_usa_o_cache(tmp_path, monkeypatch):
    chamadas = []
    embeddings = _embeddings_por_palavras(("devolução", "garantia", "pix", "frete"), chamadas)
    cache = tmp_path / "politicas.npy"

    indice = criar_indice(ler_secoes(), embeddings, cache)
    monkeypatch.setattr(politicas, "obter_indice", lambda: indice)
    secoes = consultar_politicas("devolução")["secoes"]

    assert len(secoes) == politicas.LIMITE_SECOES
    assert secoes[0]["titulo"] == "4. Política de Trocas e Devoluções"
    # Com o cache gravado, as seções não são recalculadas; só a consulta passa pelo modelo.
    chamadas.clear()
    criar_indice(ler_secoes(), embeddings, cache)
    assert chamadas == []


def _embeddings_por_palavras(vocabulario, chamadas):
    """Embedding falso: quantas vezes cada palavra do vocabulário aparece no texto, normalizado."""

    def calcular(textos):
        chamadas.append(textos)
        vetores = np.array([[texto.lower().split().count(palavra) for palavra in vocabulario] for texto in textos])
        return vetores / np.maximum(np.linalg.norm(vetores, axis=1, keepdims=True), 1)

    return calcular
