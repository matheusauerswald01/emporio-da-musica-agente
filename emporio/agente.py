"""Agente de atendimento: grafo do LangGraph que junta o Gemini, as ferramentas, a persona e a memória.

O grafo é o ciclo de agente com ferramentas: o atendente (LLM) responde ou pede ferramentas; os
resultados voltam para ele até haver uma resposta. A memória é por conversa (thread_id) e fica só
na RAM do processo: nada da conversa é gravado em disco (LGPD). Quando cada conversa sai da
memória é decidido em emporio/conversas.py.
"""

from __future__ import annotations

import os

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage
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
# Raciocínio baixo: respostas de chat rápidas; as ferramentas já entregam os dados prontos.
NIVEL_DE_RACIOCINIO = "low"

FERRAMENTAS = [
    tool(funcao, parse_docstring=True)
    for funcao in (buscar_produtos, detalhes_produto, promocoes_ativas, status_pedido, consultar_politicas)
]

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
    modelo: BaseChatModel | None = None, memoria: BaseCheckpointSaver | None = None
) -> CompiledStateGraph:
    """Monta o agente. Sem argumentos, usa o Gemini e a memória em RAM; os testes passam um modelo falso."""
    modelo = modelo or _gemini()
    atendente = modelo.bind_tools(FERRAMENTAS)
    atendente_so_texto = modelo.bind_tools(FERRAMENTAS, tool_choice="none")
    tentativas_de_pedido = LimiteDeTentativas()

    async def atender(estado: MessagesState) -> dict:
        # A data entra a cada resposta, para valer a do momento, mesmo numa conversa longa.
        instrucoes = SystemMessage(instrucoes_do_atendente(agora_na_loja()))
        pode_consultar = _consultas_desde_a_pergunta(estado["messages"]) < LIMITE_DE_CONSULTAS_POR_RESPOSTA
        modelo_da_vez = atendente if pode_consultar else atendente_so_texto
        return {"messages": [await modelo_da_vez.ainvoke([instrucoes, *estado["messages"]])]}

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
        model=os.getenv("GEMINI_MODEL", MODELO_PADRAO), thinking_level=NIVEL_DE_RACIOCINIO
    )
