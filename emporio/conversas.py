"""Quando cada conversa entra e sai da memória do agente (LGPD: guardar só pelo tempo necessário).

- O cliente apaga a conversa ao clicar em "Nova conversa" ou ao sair da página.
- Uma conversa parada há mais de TEMPO_MAXIMO_PARADA_EM_SEGUNDOS é apagada sozinha, para o caso de
  o pedido de apagar não chegar (navegador fechado à força, sem rede).
- Se o pedido de apagar chega no meio de uma resposta, a conversa é apagada de novo quando a
  resposta termina: até lá, o grafo ainda grava o estado e a recriaria na memória.
- Uma resposta que falha ou é interrompida (botão Parar, aba fechada) é desfeita por inteiro, com a
  pergunta. Assim não sobra uma chamada de ferramenta sem resultado, que o Gemini recusaria nas
  próximas mensagens, e o "Tentar de novo" não grava a pergunta duas vezes.
- Uma conversa responde a uma mensagem por vez; desfazer uma resposta não pode apagar a de outra.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from langchain_core.messages import AnyMessage, RemoveMessage
from langgraph.graph.state import CompiledStateGraph

TEMPO_MAXIMO_PARADA_EM_SEGUNDOS = 30 * 60

# Nó do grafo em nome do qual o turno é desfeito. A saída dele vai sempre para o atendente; se o
# LangGraph tomasse a remoção como saída do atendente, avaliaria a aresta tools_condition, que
# falha quando a conversa fica vazia (turno desfeito logo na primeira pergunta).
NO_DAS_FERRAMENTAS = "tools"


class MemoriaDasConversas:
    """Acompanha as conversas do agente e as apaga da memória na hora certa."""

    def __init__(
        self,
        agente: CompiledStateGraph,
        tempo_maximo_parada: float = TEMPO_MAXIMO_PARADA_EM_SEGUNDOS,
        relogio: Callable[[], float] = time.monotonic,
    ) -> None:
        self.agente = agente
        self._tempo_maximo_parada = tempo_maximo_parada
        self._relogio = relogio
        self._ultima_atividade: dict[str, float] = {}
        self._respondendo: set[str] = set()
        self._apagar_ao_terminar: set[str] = set()

    def iniciar_resposta(self, id_da_conversa: str) -> bool:
        """Marca a conversa como em resposta. Devolve False se ela já está respondendo outra mensagem."""
        if id_da_conversa in self._respondendo:
            return False
        self._respondendo.add(id_da_conversa)
        self._ultima_atividade[id_da_conversa] = self._relogio()
        return True

    async def ids_das_mensagens(self, id_da_conversa: str) -> set[str]:
        """Ids das mensagens já gravadas na conversa, para saber depois o que a resposta acrescentou."""
        return {mensagem.id for mensagem in await self._mensagens_gravadas(id_da_conversa)}

    async def terminar_resposta(
        self, id_da_conversa: str, mensagens_antes: set[str] | None, concluida: bool
    ) -> None:
        """Fecha a resposta: desfaz o turno se ele não foi concluído e cumpre o pedido de apagar pendente.

        mensagens_antes é None quando a resposta parou antes de ler a conversa: aí nada foi gravado.
        """
        try:
            if not concluida and mensagens_antes is not None:
                await self._desfazer_turno(id_da_conversa, mensagens_antes)
        finally:
            self._respondendo.discard(id_da_conversa)
            self._ultima_atividade[id_da_conversa] = self._relogio()
            if id_da_conversa in self._apagar_ao_terminar:
                self._apagar_ao_terminar.discard(id_da_conversa)
                await self._apagar_da_memoria(id_da_conversa)

    async def apagar(self, id_da_conversa: str) -> None:
        """Apaga a conversa agora e, se ela ainda está respondendo, também quando a resposta terminar."""
        await self._apagar_da_memoria(id_da_conversa)
        if id_da_conversa in self._respondendo:
            self._apagar_ao_terminar.add(id_da_conversa)

    async def apagar_paradas(self) -> None:
        """Apaga as conversas sem atividade há mais tempo que o permitido."""
        limite = self._relogio() - self._tempo_maximo_parada
        paradas = [
            id_da_conversa
            for id_da_conversa, momento in self._ultima_atividade.items()
            if momento < limite and id_da_conversa not in self._respondendo
        ]
        for id_da_conversa in paradas:
            await self._apagar_da_memoria(id_da_conversa)

    async def _desfazer_turno(self, id_da_conversa: str, mensagens_antes: set[str]) -> None:
        gravadas = await self._mensagens_gravadas(id_da_conversa)
        novas = [mensagem for mensagem in gravadas if mensagem.id not in mensagens_antes]
        if novas:
            await self.agente.aupdate_state(
                _configuracao(id_da_conversa),
                {"messages": [RemoveMessage(id=mensagem.id) for mensagem in novas]},
                as_node=NO_DAS_FERRAMENTAS,
            )

    async def _apagar_da_memoria(self, id_da_conversa: str) -> None:
        self._ultima_atividade.pop(id_da_conversa, None)
        await self.agente.checkpointer.adelete_thread(id_da_conversa)

    async def _mensagens_gravadas(self, id_da_conversa: str) -> list[AnyMessage]:
        """Mensagens do último checkpoint gravado.

        Lê do checkpoint, e não do aget_state: o aget_state soma escritas do passo que ainda não
        foi gravado, e o aupdate_state que desfaz o turno parte só do que já foi gravado.
        """
        gravado = await self.agente.checkpointer.aget_tuple(_configuracao(id_da_conversa))
        return gravado.checkpoint["channel_values"].get("messages", []) if gravado else []


def _configuracao(id_da_conversa: str) -> dict:
    return {"configurable": {"thread_id": id_da_conversa}}
