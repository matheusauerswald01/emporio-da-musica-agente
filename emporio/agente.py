"""Agente de atendimento: grafo do LangGraph que junta o Gemini, as ferramentas, a persona e a memória.

O grafo é o ciclo de agente com ferramentas: o atendente (LLM) responde ou pede ferramentas; os
resultados voltam para ele até haver uma resposta. A memória é por conversa (thread_id): a API a
grava em SQLite (emporio/memoria.py), e emporio/conversas.py decide quando cada conversa sai dela.
A memória guarda a conversa inteira, mas o modelo recebe só as últimas mensagens. Quando o modelo
principal está indisponível, um modelo reserva responde no lugar dele.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx
from langchain_core.exceptions import (
    ModelAPIError,
    ModelConnectionError,
    ModelNotFoundError,
    ModelRateLimitError,
    ModelTimeoutError,
)
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    AnyMessage,
    HumanMessage,
    SystemMessage,
    message_chunk_to_message,
)
from langchain_core.runnables import Runnable
from langchain_core.tools import tool
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.prebuilt.tool_node import ToolInvocationError

from emporio.expediente import agora_na_loja
from emporio.ferramentas import buscar_produtos, detalhes_produto, promocoes_ativas, status_pedido
from emporio.persona import instrucoes_do_atendente
from emporio.politicas import consultar_politicas
from emporio.tentativas import LimiteDeTentativas, proteger_status_pedido

MODELO_PADRAO = "gemini-3.8-flash"
# Responde quando o modelo principal está indisponível: em horário de alta demanda, o 3.8 devolve
# 503. GEMINI_FALLBACK_MODEL vazio no .env desliga a reserva.
MODELO_RESERVA_PADRAO = "gemini-3.6-flash"
# Raciocínio baixo: respostas de chat rápidas; as ferramentas já entregam os dados prontos.
NIVEL_DE_RACIOCINIO = "low"

# O modelo principal tenta uma vez só e já passa a vez para a reserva: com novas tentativas, um 503
# segurava a resposta por minutos. A reserva, última chance, tenta 3 vezes (de 3 a 5 s de espera no
# total). Com as 6 tentativas padrão do langchain-google-genai, uma cota esgotada prendia o turno
# por ~35 s antes do aviso de erro.
TENTATIVAS_NO_MODELO_PRINCIPAL = 1
TENTATIVAS_NA_RESERVA = 3
# Sem receber nada do Gemini por esse tempo, a chamada falha; o stream não é cortado enquanto chegam
# pedaços da resposta.
TEMPO_LIMITE_DE_ESPERA_EM_SEGUNDOS = 30

# Falhas de disponibilidade, que a reserva contorna: sobrecarga ou falha do servidor (5xx), cota
# esgotada (429), modelo fora do ar (404) e rede. Um pedido inválido ou uma chave errada falharia
# igual na reserva. O langchain-google-genai só traduz as respostas de erro da API: rede e tempo
# limite chegam como exceções do httpx, o cliente HTTP padrão do SDK do Gemini (com o aiohttp
# instalado, o SDK passaria a usá-lo nas chamadas assíncronas, e esses erros mudariam de tipo).
ERROS_DE_INDISPONIBILIDADE = (
    ModelAPIError,
    ModelRateLimitError,
    ModelNotFoundError,
    ModelConnectionError,
    ModelTimeoutError,
    httpx.TransportError,
)

FERRAMENTAS = [
    tool(funcao, parse_docstring=True)
    for funcao in (buscar_produtos, detalhes_produto, promocoes_ativas, status_pedido, consultar_politicas)
]

# Mensagens da conversa enviadas ao modelo a cada resposta: o bastante para o contexto da
# conversa, sem deixar a chamada lenta nem passar do limite de tokens do Gemini.
LIMITE_DE_MENSAGENS_NO_CONTEXTO = 20

# Rodadas de ferramentas numa mesma resposta. Depois disso, o modelo é obrigado a responder em
# texto: sem teto, um modelo insistente faria milhares de chamadas ao Gemini por mensagem.
LIMITE_DE_CONSULTAS_POR_RESPOSTA = 6

# Vira o resultado da ferramenta que falhar (rede, cota do Gemini). Texto fixo: o detalhe da
# exceção não vai para o modelo, e a conversa segue com uma resposta em vez de um erro.
# Argumento inválido é diferente: o erro volta para o modelo corrigir a chamada.
MENSAGEM_FALHA_NA_FERRAMENTA = (
    "Não foi possível fazer esta consulta agora. Diga ao cliente que não conseguiu confirmar essa "
    "informação e passe o WhatsApp da loja."
)

# Os logs nunca levam o texto das mensagens nem o detalhe dos erros, que pode trazer trechos delas (LGPD).
logger = logging.getLogger(__name__)


def criar_agente(
    modelo: BaseChatModel | None = None,
    memoria: BaseCheckpointSaver | None = None,
    reserva: BaseChatModel | None = None,
) -> CompiledStateGraph:
    """Monta o agente. Sem argumentos, usa o Gemini, o modelo reserva do .env e a memória em RAM.

    Os testes passam um modelo falso e, quando precisam, uma reserva falsa.
    """
    if modelo is None:
        modelo = _gemini()
        reserva = reserva or _gemini_reserva()
    # Pares (modelo principal, reserva), já com as ferramentas.
    atendente = (_com_as_ferramentas(modelo), _com_as_ferramentas(reserva))
    atendente_so_texto = (
        _com_as_ferramentas(modelo, tool_choice="none"),
        _com_as_ferramentas(reserva, tool_choice="none"),
    )
    tentativas_de_pedido = LimiteDeTentativas()

    async def atender(estado: MessagesState) -> dict:
        # A data entra a cada resposta, para valer a do momento, mesmo numa conversa longa.
        instrucoes = SystemMessage(instrucoes_do_atendente(agora_na_loja()))
        pode_consultar = _consultas_desde_a_pergunta(estado["messages"]) < LIMITE_DE_CONSULTAS_POR_RESPOSTA
        principal, reserva_da_vez = atendente if pode_consultar else atendente_so_texto
        contexto = _janela_de_contexto(estado["messages"])
        return {"messages": [await _responder(principal, reserva_da_vez, [instrucoes, *contexto])]}

    ferramentas = ToolNode(
        FERRAMENTAS,
        awrap_tool_call=proteger_status_pedido(tentativas_de_pedido),
        handle_tool_errors=_tratar_erro_da_ferramenta,
    )
    grafo = StateGraph(MessagesState)
    grafo.add_node("atendente", atender)
    grafo.add_node("tools", ferramentas)
    grafo.add_edge(START, "atendente")
    grafo.add_conditional_edges("atendente", tools_condition)
    grafo.add_edge("tools", "atendente")
    return grafo.compile(checkpointer=memoria or InMemorySaver())


def _com_as_ferramentas(modelo: BaseChatModel | None, **opcoes: Any) -> Runnable | None:
    return modelo.bind_tools(FERRAMENTAS, **opcoes) if modelo else None


async def _responder(principal: Runnable, reserva: Runnable | None, mensagens: list[AnyMessage]) -> AIMessage:
    """Resposta do modelo principal; se ele estiver indisponível, a da reserva.

    A troca só acontece antes de a resposta começar a aparecer na tela: depois, a reserva repetiria o
    começo dela. Nesse caso o erro sobe, e a API avisa o cliente e desfaz o turno.
    """
    resposta: AIMessageChunk | None = None
    try:
        async for pedaco in principal.astream(mensagens):
            resposta = pedaco if resposta is None else resposta + pedaco
    except ERROS_DE_INDISPONIBILIDADE as erro:
        if reserva is None or _apareceu_na_tela(resposta):
            raise
        logger.warning("Modelo principal indisponível (%s); a reserva responde.", type(erro).__name__)
        return await reserva.ainvoke(mensagens)
    return message_chunk_to_message(resposta)


def _apareceu_na_tela(resposta: AIMessageChunk | None) -> bool:
    """Se a resposta já mandou texto ou o nome de uma ferramenta para a interface."""
    return resposta is not None and bool(resposta.text or resposta.tool_call_chunks)


def _tratar_erro_da_ferramenta(erro: Exception) -> str:
    if isinstance(erro, ToolInvocationError):
        return erro.message
    return MENSAGEM_FALHA_NA_FERRAMENTA


def _janela_de_contexto(mensagens: list[AnyMessage]) -> list[AnyMessage]:
    """As últimas mensagens da conversa, começando sempre numa pergunta do cliente.

    Cortar no meio de um turno deixaria o resultado de uma ferramenta sem a chamada que o pediu, e
    o Gemini recusa isso. Por isso o turno atual vai inteiro, mesmo que sozinho passe do limite.
    """
    perguntas = [indice for indice, mensagem in enumerate(mensagens) if isinstance(mensagem, HumanMessage)]
    cabem = [indice for indice in perguntas if len(mensagens) - indice <= LIMITE_DE_MENSAGENS_NO_CONTEXTO]
    if cabem:
        return mensagens[cabem[0]:]
    return mensagens[perguntas[-1]:] if perguntas else mensagens


def _consultas_desde_a_pergunta(mensagens: list[AnyMessage]) -> int:
    """Rodadas de ferramentas desde a última mensagem do cliente."""
    consultas = 0
    for mensagem in reversed(mensagens):
        if isinstance(mensagem, HumanMessage):
            break
        if isinstance(mensagem, AIMessage) and mensagem.tool_calls:
            consultas += 1
    return consultas


def _gemini() -> ChatGoogleGenerativeAI:
    # A chave vem da variável de ambiente GOOGLE_API_KEY. A temperatura fica no padrão do Gemini 3,
    # como o Google recomenda para esses modelos.
    return ChatGoogleGenerativeAI(
        model=os.getenv("GEMINI_MODEL", MODELO_PADRAO),
        thinking_level=NIVEL_DE_RACIOCINIO,
        max_retries=TENTATIVAS_NO_MODELO_PRINCIPAL,
        timeout=TEMPO_LIMITE_DE_ESPERA_EM_SEGUNDOS,
    )


def _gemini_reserva() -> ChatGoogleGenerativeAI | None:
    modelo = os.getenv("GEMINI_FALLBACK_MODEL", MODELO_RESERVA_PADRAO)
    if not modelo:
        return None
    return ChatGoogleGenerativeAI(
        model=modelo,
        thinking_level=NIVEL_DE_RACIOCINIO,
        max_retries=TENTATIVAS_NA_RESERVA,
        timeout=TEMPO_LIMITE_DE_ESPERA_EM_SEGUNDOS,
    )
