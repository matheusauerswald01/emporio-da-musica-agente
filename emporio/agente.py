"""Agente de atendimento: grafo do LangGraph que junta o Gemini, as ferramentas, a persona e a memória.

O grafo é o ciclo de agente com ferramentas: o atendente (LLM) responde ou pede ferramentas; os
resultados voltam para ele até haver uma resposta. A memória é por conversa (thread_id): a API a
grava em SQLite (emporio/memoria.py), e emporio/conversas.py decide quando cada conversa sai dela.
A memória guarda a conversa inteira, mas o modelo recebe só as últimas mensagens. Quem responde é
o primeiro modelo disponível da cadeia de modelos do Gemini (emporio/modelos.py).
"""

from __future__ import annotations

from collections.abc import Sequence

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage
from langchain_core.tools import tool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.prebuilt.tool_node import ToolInvocationError

from emporio.expediente import agora_na_loja
from emporio.ferramentas import buscar_produtos, detalhes_produto, promocoes_ativas, status_pedido
from emporio.modelos import CadeiaDeModelos, modelos_do_gemini
from emporio.persona import instrucoes_do_atendente
from emporio.politicas import consultar_politicas
from emporio.tentativas import LimiteDeTentativas, proteger_status_pedido

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


def criar_agente(
    modelo: BaseChatModel | None = None,
    memoria: BaseCheckpointSaver | None = None,
    reservas: Sequence[BaseChatModel] = (),
) -> CompiledStateGraph:
    """Monta o agente. Sem modelo, usa a cadeia do Gemini do .env (principal e reservas) e a memória em RAM.

    Os testes passam um modelo falso e, quando precisam, reservas falsas.
    """
    cadeia = CadeiaDeModelos([modelo, *reservas] if modelo is not None else modelos_do_gemini(), FERRAMENTAS)
    tentativas_de_pedido = LimiteDeTentativas()

    async def atender(estado: MessagesState) -> dict:
        # A data entra a cada resposta, para valer a do momento, mesmo numa conversa longa.
        instrucoes = SystemMessage(instrucoes_do_atendente(agora_na_loja()))
        pode_consultar = _consultas_desde_a_pergunta(estado["messages"]) < LIMITE_DE_CONSULTAS_POR_RESPOSTA
        contexto = _janela_de_contexto(estado["messages"])
        resposta = await cadeia.responder([instrucoes, *contexto], pode_consultar=pode_consultar)
        return {"messages": [resposta]}

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
