"""Modelos do Gemini que respondem pelo atendente: o principal e as reservas, tentados em ordem.

Se um modelo falha antes de a resposta aparecer na tela, o próximo da cadeia responde ao mesmo
pedido. O modelo que ficou indisponível (cota esgotada, sobrecarga, rede) sai da cadeia por um
tempo: com a cota do dia esgotada, o Google pede horas de espera, e chamá-lo a cada mensagem só
atrasaria a resposta.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Iterator, Sequence
from typing import Any

import httpx
from google.genai import errors as erros_do_gemini
from langchain_core.exceptions import (
    ModelAPIError,
    ModelAuthenticationError,
    ModelConnectionError,
    ModelNotFoundError,
    ModelPermissionDeniedError,
    ModelRateLimitError,
    ModelTimeoutError,
)
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, AnyMessage, message_chunk_to_message
from langchain_google_genai import ChatGoogleGenerativeAI

MODELO_PADRAO = "gemini-3.8-flash"
# Em ordem de preferência. Cada reserva precisa ter a própria cota diária no Google: o gemini-3.7-flash
# e o gemini-3.5-flash não servem, porque gastam a cota do 3.8 e do 3.6. Os flash-lite ficam por
# último: respondem com menos cuidado, mas têm cota separada. GEMINI_FALLBACK_MODELS no .env troca a
# lista; vazio, desliga as reservas.
RESERVAS_PADRAO = ("gemini-3.6-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite")
# Raciocínio baixo: respostas de chat rápidas; as ferramentas já entregam os dados prontos.
NIVEL_DE_RACIOCINIO = "low"
# Cada modelo tenta uma vez só: a nova tentativa é o próximo da cadeia. O SDK do Gemini repete até o
# 429 de cota diária, que só passa em horas, e um tempo esgotado, que prendia o turno por mais 30 s.
TENTATIVAS_POR_MODELO = 1
# Sem receber nada do Gemini por esse tempo, a chamada falha; o stream não é cortado enquanto chegam
# pedaços da resposta.
TEMPO_LIMITE_DE_ESPERA_EM_SEGUNDOS = 30

# Tempo fora da cadeia de um modelo indisponível quando o Google não diz quanto esperar.
PAUSA_PADRAO_EM_SEGUNDOS = 60
# Com todos os modelos sem cota e pausados por mais que isso, a Lia fica fora do ar, e não adianta o
# cliente tentar de novo logo em seguida.
PAUSA_LONGA_EM_SEGUNDOS = 10 * 60

# Falhas de disponibilidade, que tiram o modelo da cadeia por um tempo: cota esgotada (429), modelo
# sem permissão para este projeto (403) ou fora do ar (404), sobrecarga ou falha do servidor (5xx) e
# rede. O langchain-google-genai só traduz as respostas de erro da API: rede e tempo limite chegam
# como exceções do httpx, o cliente HTTP padrão do SDK do Gemini.
ERROS_DE_INDISPONIBILIDADE = (
    ModelAPIError,
    ModelRateLimitError,
    ModelPermissionDeniedError,
    ModelNotFoundError,
    ModelConnectionError,
    ModelTimeoutError,
    httpx.TransportError,
)
# Motivos com que o Google recusa a chave. Ele responde 400, e não 401, para uma chave inválida.
MOTIVOS_DE_CHAVE_RECUSADA = frozenset({"API_KEY_INVALID", "API_KEY_EXPIRED"})

# Os logs levam o nome do modelo, o tipo e o código do erro; nunca a mensagem do erro, que pode trazer
# trechos da conversa (LGPD).
logger = logging.getLogger(__name__)


class ModelosForaDoAr(Exception):
    """Todos os modelos da cadeia estão sem cota por mais de 10 minutos: não adianta tentar de novo já."""


class ChaveDoGeminiRecusada(Exception):
    """O Google recusou a GOOGLE_API_KEY: falharia igual em todos os modelos."""


class CadeiaDeModelos:
    """Os modelos do atendente em ordem de preferência, com as pausas dos que ficaram indisponíveis.

    Uma cadeia só atende todas as conversas da API: a pausa que uma conversa registra vale para as outras.
    """

    def __init__(
        self,
        modelos: Sequence[BaseChatModel],
        ferramentas: Sequence[Any],
        relogio: Callable[[], float] = time.monotonic,
    ) -> None:
        self._nomes = [_nome(modelo) for modelo in modelos]
        self._com_ferramentas = [modelo.bind_tools(ferramentas) for modelo in modelos]
        self._so_texto = [modelo.bind_tools(ferramentas, tool_choice="none") for modelo in modelos]
        self._pausado_ate = [0.0] * len(modelos)
        self._relogio = relogio

    async def responder(self, mensagens: list[AnyMessage], *, pode_consultar: bool = True) -> AIMessage:
        """Resposta do primeiro modelo da cadeia que conseguir responder.

        Qualquer falha passa a vez para o próximo modelo, menos a chave recusada, que falharia em todos.
        Uma resposta vazia (sem texto e sem ferramenta) também passa a vez, mas é devolvida se nenhum
        outro modelo responder melhor. A troca só acontece antes de a resposta começar a aparecer na
        tela: depois, o próximo modelo repetiria o começo dela. Nesse caso o erro sobe, e a API avisa o
        cliente e desfaz o turno.
        """
        modelos = self._com_ferramentas if pode_consultar else self._so_texto
        ultimo_erro: Exception | None = None
        resposta_vazia: AIMessage | None = None
        so_falhas_de_cota = True
        for indice in self._proximos(self._ordem_de_tentativa()):
            resposta: AIMessageChunk | None = None
            try:
                # Sem nenhum pedaço, o próprio astream lança ValueError, e o próximo modelo responde.
                async for pedaco in modelos[indice].astream(mensagens):
                    resposta = pedaco if resposta is None else resposta + pedaco
            except Exception as erro:
                if _chave_recusada(erro):
                    raise ChaveDoGeminiRecusada from erro
                pausa = self._registrar_falha(indice, erro)
                if _apareceu_na_tela(resposta):
                    raise
                so_falhas_de_cota = so_falhas_de_cota and pausa > PAUSA_LONGA_EM_SEGUNDOS
                ultimo_erro = erro
                continue
            if _apareceu_na_tela(resposta):
                return message_chunk_to_message(resposta)
            # Ex.: MALFORMED_FUNCTION_CALL ou resposta bloqueada. Não é falta de disponibilidade: sem pausa.
            motivo = resposta.response_metadata.get("finish_reason", "sem motivo")
            nome = self._nomes[indice]
            logger.warning("Modelo %s respondeu vazio (%s); o próximo da cadeia responde.", nome, motivo)
            resposta_vazia = resposta_vazia or message_chunk_to_message(resposta)
        if resposta_vazia is not None:
            return resposta_vazia
        # Fora do ar só se o motivo desta vez também foi a cota: outra falha pode passar já na próxima tentativa.
        if so_falhas_de_cota and self._todos_pausados_por_muito_tempo():
            raise ModelosForaDoAr from ultimo_erro
        raise ultimo_erro

    def _ordem_de_tentativa(self) -> list[int]:
        """Os modelos fora de pausa, em ordem. Com todos pausados, tenta todos: a pausa é uma estimativa,
        e recusar o cliente sem tentar seria pior que algumas chamadas recusadas na hora pelo Google."""
        agora = self._relogio()
        disponiveis = [indice for indice, ate in enumerate(self._pausado_ate) if ate <= agora]
        return disponiveis or list(range(len(self._pausado_ate)))

    def _proximos(self, candidatos: list[int]) -> Iterator[int]:
        """Os candidatos em ordem, deixando para o fim os que outra conversa pausou enquanto esta esperava
        a resposta de um modelo. Se todos os que faltam estiverem pausados, tenta assim mesmo."""
        restantes = list(candidatos)
        while restantes:
            agora = self._relogio()
            disponiveis = [indice for indice in restantes if self._pausado_ate[indice] <= agora]
            indice = (disponiveis or restantes)[0]
            restantes.remove(indice)
            yield indice

    def _registrar_falha(self, indice: int, erro: Exception) -> float:
        """Tira o modelo da cadeia pelo tempo que a falha pede e devolve esse tempo."""
        pausa = _pausa_depois_da_falha(erro)
        nome, descricao = self._nomes[indice], _descricao(erro)
        if pausa:
            self._pausado_ate[indice] = max(self._pausado_ate[indice], self._relogio() + pausa)
            logger.warning("Modelo %s falhou (%s); fora da cadeia por %.0f s.", nome, descricao, pausa)
        else:
            logger.warning("Modelo %s falhou (%s); o próximo da cadeia responde.", nome, descricao)
        return pausa

    def _todos_pausados_por_muito_tempo(self) -> bool:
        agora = self._relogio()
        return all(ate - agora > PAUSA_LONGA_EM_SEGUNDOS for ate in self._pausado_ate)


def nomes_dos_modelos() -> list[str]:
    """O modelo principal e as reservas, em ordem, do .env (GEMINI_MODEL e GEMINI_FALLBACK_MODELS)."""
    lista = os.getenv("GEMINI_FALLBACK_MODELS")
    reservas = RESERVAS_PADRAO if lista is None else [nome.strip() for nome in lista.split(",") if nome.strip()]
    # Sem repetir modelo: o mesmo modelo duas vezes na cadeia gastaria a mesma cota duas vezes.
    return list(dict.fromkeys([os.getenv("GEMINI_MODEL", MODELO_PADRAO), *reservas]))


def modelos_do_gemini() -> list[BaseChatModel]:
    return [_gemini(nome) for nome in nomes_dos_modelos()]


def _gemini(nome: str) -> ChatGoogleGenerativeAI:
    # A chave vem da variável de ambiente GOOGLE_API_KEY. A temperatura fica no padrão do Gemini 3,
    # como o Google recomenda para esses modelos.
    return ChatGoogleGenerativeAI(
        model=nome,
        thinking_level=NIVEL_DE_RACIOCINIO,
        max_retries=TENTATIVAS_POR_MODELO,
        timeout=TEMPO_LIMITE_DE_ESPERA_EM_SEGUNDOS,
    )


def _pausa_depois_da_falha(erro: Exception) -> float:
    """Segundos fora da cadeia: o que o Google pedir num 429, um minuto nas outras falhas de
    disponibilidade e nenhum nas demais, que podem ser deste pedido e não do modelo."""
    if isinstance(erro, ModelRateLimitError):
        return _espera_pedida_pelo_google(erro) or PAUSA_PADRAO_EM_SEGUNDOS
    if isinstance(erro, ERROS_DE_INDISPONIBILIDADE):
        return PAUSA_PADRAO_EM_SEGUNDOS
    return 0


def _chave_recusada(erro: Exception) -> bool:
    if isinstance(erro, ModelAuthenticationError):
        return True
    return any(
        str(detalhe.get("@type", "")).endswith("ErrorInfo") and detalhe.get("reason") in MOTIVOS_DE_CHAVE_RECUSADA
        for detalhe in _detalhes_do_google(erro)
    )


def _espera_pedida_pelo_google(erro: Exception) -> float | None:
    """O retryDelay do 429 ("25091s" com a cota do dia esgotada)."""
    for detalhe in _detalhes_do_google(erro):
        if str(detalhe.get("@type", "")).endswith("RetryInfo"):
            try:
                return float(str(detalhe.get("retryDelay")).removesuffix("s"))
            except ValueError:
                return None
    return None


def _detalhes_do_google(erro: Exception) -> list[dict]:
    """Os detalhes estruturados do erro da API (google.rpc), que ficam no erro original do SDK."""
    original = erro.__cause__
    if not isinstance(original, erros_do_gemini.APIError) or not isinstance(original.details, dict):
        return []
    corpo = original.details.get("error", original.details)
    detalhes = corpo.get("details") if isinstance(corpo, dict) else None
    return [detalhe for detalhe in detalhes if isinstance(detalhe, dict)] if isinstance(detalhes, list) else []


def _descricao(erro: Exception) -> str:
    """Tipo do erro e, quando ele vem da API do Gemini, o código HTTP."""
    original = erro.__cause__ if isinstance(erro.__cause__, erros_do_gemini.APIError) else erro
    codigo = original.code if isinstance(original, erros_do_gemini.APIError) else None
    return f"{type(erro).__name__} {codigo}" if codigo else type(erro).__name__


def _nome(modelo: BaseChatModel) -> str:
    return str(getattr(modelo, "model", None) or type(modelo).__name__).removeprefix("models/")


def _apareceu_na_tela(resposta: AIMessageChunk | None) -> bool:
    """Se a resposta já mandou texto ou o nome de uma ferramenta para a interface."""
    return resposta is not None and bool(resposta.text or resposta.tool_call_chunks)
