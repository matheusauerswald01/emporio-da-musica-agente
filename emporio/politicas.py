"""Manual de políticas da loja (RAG): divide o PDF em seções e acha as mais parecidas com a dúvida.

Cada seção numerada do manual (1 a 10) é um trecho inteiro, com as subseções e as tabelas, para
que uma regra não chegue ao agente separada das suas condições: o prazo de 7 dias para devolver
(4.1) vem junto dos itens que não podem ser trocados (4.4).

A divisão usa os marcadores (sumário) do próprio PDF. Os embeddings são do Gemini e ficam em
cache em .cache/, para não recalcular a cada vez que a API sobe; o cache se renova sozinho se o
manual ou o modelo mudar.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import numpy as np
from google import genai
from google.genai import types
from pypdf import PageObject, PdfReader

PASTA_PROJETO = Path(__file__).resolve().parent.parent
ARQUIVO_MANUAL = PASTA_PROJETO / "data" / "políticas_da_loja.pdf"
PASTA_CACHE = PASTA_PROJETO / ".cache"

MODELO_EMBEDDING_PADRAO = "gemini-embedding-2"
# Bastam para separar 10 seções e ocupam um quarto das 3072 dimensões do modelo.
DIMENSOES_EMBEDDING = 768
# Uma dúvida pode cruzar seções (pagar no PIX um produto em promoção: seções 3 e 6).
LIMITE_SECOES = 3
TEMPO_LIMITE_EM_MS = 15_000
TENTATIVAS = 3

# Cabeçalho e rodapé que o PDF repete em toda página e que não fazem parte das seções.
CABECALHO = "Empório da Música Manual de Políticas e Procedimentos"
RODAPE = re.compile(r"Página \d+")

# Recebe textos e devolve um vetor normalizado por texto (uma linha da matriz).
Embeddings = Callable[[list[str]], np.ndarray]


@dataclass(frozen=True)
class Secao:
    """Uma seção numerada do manual, com todas as subseções."""

    titulo: str
    texto: str


@dataclass(frozen=True)
class IndiceDePoliticas:
    """Seções do manual com os embeddings, prontas para a busca por semelhança."""

    secoes: tuple[Secao, ...]
    vetores: np.ndarray
    embeddings: Embeddings

    def buscar(self, assunto: str, limite: int = LIMITE_SECOES) -> list[Secao]:
        """As seções mais parecidas com o assunto, da mais para a menos parecida."""
        consulta = self.embeddings([_como_consulta(assunto)])[0]
        # Vetores normalizados: o produto escalar é a semelhança de cosseno.
        semelhancas = self.vetores @ consulta
        ordem = np.argsort(-semelhancas, kind="stable")
        return [self.secoes[indice] for indice in ordem[:limite]]


def consultar_politicas(assunto: str) -> dict:
    """Busca no manual de políticas da loja as seções que respondem à dúvida do cliente.

    Use para regras e informações gerais da loja: trocas, devoluções e arrependimento, garantia,
    frete e prazos de entrega, formas de pagamento e parcelamento, horário de funcionamento,
    endereço, regras de promoções e privacidade de dados. Não serve para preço, estoque ou
    pedidos: para isso, use as outras ferramentas.

    Args:
        assunto: a dúvida do cliente em poucas palavras, por exemplo "prazo para devolver um produto".
    """
    secoes = obter_indice().buscar(assunto)
    return {"secoes": [{"titulo": secao.titulo, "texto": secao.texto} for secao in secoes]}


@cache
def obter_indice() -> IndiceDePoliticas:
    """Índice do manual com os embeddings do Gemini, montado só na primeira chamada."""
    modelo = os.getenv("GEMINI_EMBEDDING_MODEL", MODELO_EMBEDDING_PADRAO)
    secoes = ler_secoes()
    return criar_indice(secoes, embeddings_do_gemini(modelo), _arquivo_de_cache(modelo, secoes))


def criar_indice(secoes: list[Secao], embeddings: Embeddings, cache: Path | None = None) -> IndiceDePoliticas:
    """Calcula os embeddings das seções, ou os lê do arquivo de cache quando ele já existe."""
    if cache is not None and cache.exists():
        vetores = np.load(cache)
    else:
        vetores = embeddings([_como_documento(secao) for secao in secoes])
        if cache is not None:
            cache.parent.mkdir(exist_ok=True)
            np.save(cache, vetores)
    return IndiceDePoliticas(secoes=tuple(secoes), vetores=vetores, embeddings=embeddings)


def ler_secoes(arquivo: Path = ARQUIVO_MANUAL) -> list[Secao]:
    """Divide o manual nas seções numeradas, pelos títulos de primeiro nível dos marcadores do PDF.

    O texto antes da primeira seção (a capa) fica de fora: os mesmos dados estão na seção 1.
    """
    leitor = PdfReader(arquivo)
    # Subseções (1.1, 1.2...) vêm em listas aninhadas no sumário; só os títulos principais dividem.
    titulos = [marcador.title.strip() for marcador in leitor.outline if not isinstance(marcador, list)]
    linhas = [linha for pagina in leitor.pages for linha in _linhas_de_conteudo(pagina)]
    inicios = [_posicao_do_titulo(linhas, titulo) for titulo in titulos]
    fins = [*inicios[1:], len(linhas)]
    return [
        Secao(titulo=titulo, texto="\n".join(linhas[inicio + 1 : fim]))
        for titulo, inicio, fim in zip(titulos, inicios, fins)
    ]


def embeddings_do_gemini(modelo: str) -> Embeddings:
    """Função de embeddings do Gemini. A chave vem da variável de ambiente GOOGLE_API_KEY."""
    # Sem estas opções, o SDK não repete a chamada num erro passageiro (429, 503) e espera sem limite.
    cliente = genai.Client(
        http_options=types.HttpOptions(
            timeout=TEMPO_LIMITE_EM_MS, retry_options=types.HttpRetryOptions(attempts=TENTATIVAS)
        )
    )

    def calcular(textos: list[str]) -> np.ndarray:
        resposta = cliente.models.embed_content(
            model=modelo,
            # Um Content por texto: uma lista de strings soltas viraria um único embedding agregado.
            contents=[types.Content(parts=[types.Part(text=texto)]) for texto in textos],
            config=types.EmbedContentConfig(output_dimensionality=DIMENSOES_EMBEDDING),
        )
        vetores = np.array([embedding.values for embedding in resposta.embeddings])
        # Com menos dimensões que o máximo do modelo, os vetores não vêm normalizados.
        return vetores / np.linalg.norm(vetores, axis=1, keepdims=True)

    return calcular


def _linhas_de_conteudo(pagina: PageObject) -> list[str]:
    """Linhas da página sem cabeçalho, rodapé e linhas vazias.

    O modo layout mantém uma linha por linha do PDF; no modo padrão, este PDF sai com uma palavra
    por linha e as tabelas perdem as colunas.
    """
    linhas = [_limpar(linha) for linha in pagina.extract_text(extraction_mode="layout").splitlines()]
    return [linha for linha in linhas if linha and linha != CABECALHO and not RODAPE.fullmatch(linha)]


def _limpar(linha: str) -> str:
    """Tira o recuo e marca as colunas das tabelas, que no PDF são separadas só por espaços.

    Antes, junta o marcador de lista ("•", "1.") ao texto, para não virar coluna.
    """
    linha = re.sub(r"^(•|\d+\.)\s+", r"\1 ", linha.strip())
    return re.sub(r"\s{2,}", " | ", linha)


def _posicao_do_titulo(linhas: list[str], titulo: str) -> int:
    try:
        return linhas.index(titulo)
    except ValueError:
        raise ValueError(f"O título {titulo!r} do sumário do PDF não aparece no texto do manual.") from None


def _arquivo_de_cache(modelo: str, secoes: list[Secao]) -> Path:
    """Arquivo de cache próprio para este manual, modelo e dimensão: mudou algum, recalcula."""
    conteudo = "\n".join([modelo, str(DIMENSOES_EMBEDDING), *(_como_documento(secao) for secao in secoes)])
    assinatura = hashlib.sha256(conteudo.encode("utf-8")).hexdigest()[:16]
    return PASTA_CACHE / f"politicas-{assinatura}.npy"


# Formatos de texto recomendados para o gemini-embedding-2, que não aceita o campo task_type.
def _como_documento(secao: Secao) -> str:
    return f"title: {secao.titulo} | text: {secao.texto}"


def _como_consulta(assunto: str) -> str:
    return f"task: search result | query: {assunto}"
