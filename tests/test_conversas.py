"""Testes de quando as conversas saem da memória: inatividade, apagar no meio da resposta e turno desfeito."""

import asyncio

from langchain_core.messages import AIMessage, HumanMessage

from emporio.agente import criar_agente
from emporio.conversas import MemoriaDasConversas
from tests.modelo_falso import ModeloRoteirizado, chamada

CONFIGURACAO = {"configurable": {"thread_id": "conversa"}}


def _memoria(*roteiro, relogio=None) -> MemoriaDasConversas:
    agente = criar_agente(ModeloRoteirizado(roteiro=list(roteiro)))
    if relogio is None:
        return MemoriaDasConversas(agente)
    return MemoriaDasConversas(agente, tempo_maximo_parada=60, relogio=relogio)


async def _iniciar(conversas: MemoriaDasConversas) -> set[str]:
    assert conversas.iniciar_resposta("conversa")
    return await conversas.ids_das_mensagens("conversa")


async def _responder(conversas: MemoriaDasConversas, texto: str) -> None:
    antes = await _iniciar(conversas)
    await conversas.agente.ainvoke({"messages": [HumanMessage(texto)]}, CONFIGURACAO)
    await conversas.terminar_resposta("conversa", antes, concluida=True)


def _mensagens(conversas: MemoriaDasConversas) -> list:
    return conversas.agente.get_state(CONFIGURACAO).values.get("messages", [])


def test_conversa_parada_sai_da_memoria_depois_do_tempo_maximo():
    agora = [0.0]
    conversas = _memoria(AIMessage("Oi!"), relogio=lambda: agora[0])
    asyncio.run(_responder(conversas, "Oi"))

    agora[0] = 59
    asyncio.run(conversas.apagar_paradas())
    assert _mensagens(conversas)

    agora[0] = 61
    asyncio.run(conversas.apagar_paradas())
    assert _mensagens(conversas) == []


def test_pedido_de_apagar_no_meio_da_resposta_vale_quando_ela_termina():
    conversas = _memoria(AIMessage("Oi!"))

    async def cenario():
        antes = await _iniciar(conversas)
        await conversas.agente.ainvoke({"messages": [HumanMessage("Oi")]}, CONFIGURACAO)
        await conversas.apagar("conversa")  # o cliente clicou em "Nova conversa"
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
