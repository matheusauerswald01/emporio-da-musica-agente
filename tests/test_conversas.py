"""Testes de quando as conversas saem da memória: prazo de guarda, apagar no meio da resposta e turno desfeito."""

import asyncio
from datetime import UTC, datetime

from langchain_core.messages import AIMessage, HumanMessage

from emporio.agente import criar_agente
from emporio.conversas import PRAZO_DE_GUARDA, MemoriaDasConversas
from tests.modelo_falso import ModeloRoteirizado, chamada

CONFIGURACAO = {"configurable": {"thread_id": "conversa"}}


def _memoria(*roteiro, agora=None) -> MemoriaDasConversas:
    agente = criar_agente(ModeloRoteirizado(roteiro=list(roteiro)))
    if agora is None:
        return MemoriaDasConversas(agente)
    return MemoriaDasConversas(agente, agora=agora)


async def _iniciar(conversas: MemoriaDasConversas) -> set[str]:
    assert conversas.iniciar_resposta("conversa")
    return await conversas.ids_das_mensagens("conversa")


async def _responder(conversas: MemoriaDasConversas, texto: str) -> None:
    antes = await _iniciar(conversas)
    await conversas.agente.ainvoke({"messages": [HumanMessage(texto)]}, CONFIGURACAO)
    await conversas.terminar_resposta("conversa", antes, concluida=True)


def _mensagens(conversas: MemoriaDasConversas) -> list:
    return conversas.agente.get_state(CONFIGURACAO).values.get("messages", [])


def test_conversa_sem_mensagens_alem_do_prazo_de_guarda_e_apagada():
    agora = [datetime.now(UTC)]
    conversas = _memoria(AIMessage("Oi!"), agora=lambda: agora[0])
    asyncio.run(_responder(conversas, "Oi"))
    ultima_mensagem = agora[0]

    agora[0] = ultima_mensagem + PRAZO_DE_GUARDA - (PRAZO_DE_GUARDA / 100)
    asyncio.run(conversas.apagar_antigas())
    assert _mensagens(conversas)

    agora[0] = ultima_mensagem + PRAZO_DE_GUARDA + (PRAZO_DE_GUARDA / 100)
    asyncio.run(conversas.apagar_antigas())
    assert _mensagens(conversas) == []


def test_pedido_de_apagar_no_meio_da_resposta_vale_quando_ela_termina():
    conversas = _memoria(AIMessage("Oi!"))

    async def cenario():
        antes = await _iniciar(conversas)
        await conversas.agente.ainvoke({"messages": [HumanMessage("Oi")]}, CONFIGURACAO)
        await conversas.apagar("conversa")  # o cliente mandou /apagar
        # O grafo ainda grava o fim da resposta, o que recriaria a conversa na memória.
        await conversas.agente.aupdate_state(CONFIGURACAO, {"messages": [AIMessage("Resto da resposta")]})
        await conversas.terminar_resposta("conversa", antes, concluida=True)

    asyncio.run(cenario())
    assert _mensagens(conversas) == []


def test_resposta_interrompida_e_desfeita_sem_deixar_ferramenta_sem_resultado():
    conversas = _memoria(AIMessage("Oi!"))
    asyncio.run(_responder(conversas, "Oi"))

    async def interrompida():
        antes = await _iniciar(conversas)
        # O cliente clicou em "Parar" logo depois de o modelo pedir uma ferramenta.
        pedido = AIMessage("", tool_calls=[chamada("promocoes_ativas")])
        await conversas.agente.aupdate_state(CONFIGURACAO, {"messages": [HumanMessage("Tem promoção?"), pedido]})
        await conversas.terminar_resposta("conversa", antes, concluida=False)

    asyncio.run(interrompida())
    assert [mensagem.text for mensagem in _mensagens(conversas)] == ["Oi", "Oi!"]
