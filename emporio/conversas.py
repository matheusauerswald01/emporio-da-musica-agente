"""Quando cada conversa entra e sai da memória do agente (LGPD: guardar só pelo tempo necessário).

- A conversa fica guardada para o cliente continuar de onde parou, mesmo depois de fechar a página.
- O cliente apaga o próprio histórico quando quiser, com o comando /apagar.
- Uma conversa sem mensagens há mais de PRAZO_DE_GUARDA é apagada sozinha. O prazo conta a partir
  do último registro gravado no banco, então vale também depois de a API reiniciar.
- Se o pedido de apagar chega no meio de uma resposta, a conversa é apagada de novo quando a
  resposta termina: até lá, o grafo ainda grava o estado e a recriaria.
- Uma resposta que falha ou é interrompida (botão Parar, aba fechada) é desfeita por inteiro, com a
  pergunta. Assim não sobra uma chamada de ferramenta sem resultado, que o Gemini recusaria nas
  próximas mensagens, e o "Tentar de novo" não grava a pergunta duas vezes.
- Uma conversa responde a uma mensagem por vez; desfazer uma resposta não pode apagar a de outra.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from langchain_core.messages import AnyMessage, RemoveMessage
from langgraph.graph.state import CompiledStateGraph

PRAZO_DE_GUARDA = timedelta(days=30)

# Nó do grafo em nome do qual o turno é desfeito. A saída dele vai sempre para o atendente; se o
# LangGraph tomasse a remoção como saída do atendente, avaliaria a aresta tools_condition, que
# falha quando a conversa fica vazia (turno desfeito logo na primeira pergunta).
NO_DAS_FERRAMENTAS = "tools"


class MemoriaDasConversas:
    """Acompanha as conversas do agente e as apaga da memória na hora certa."""

    def __init__(
        self,
        agente: CompiledStateGraph,
        prazo_de_guarda: timedelta = PRAZO_DE_GUARDA,
        agora: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.agente = agente
        self._prazo_de_guarda = prazo_de_guarda
        self._agora = agora
        self._respondendo: set[str] = set()
        self._apagar_ao_terminar: set[str] = set()

    def iniciar_resposta(self, id_da_conversa: str) -> bool:
        """Marca a conversa como em resposta. Devolve False se ela já está respondendo outra mensagem."""
        if id_da_conversa in self._respondendo:
            return False
        self._respondendo.add(id_da_conversa)
        return True

    async def mensagens(self, id_da_conversa: str) -> list[AnyMessage]:
        """Mensagens já gravadas na conversa (o histórico que o cliente vê ao voltar)."""
        gravado = await self.agente.checkpointer.aget_tuple(_configuracao(id_da_conversa))
        # Lê do checkpoint, e não do aget_state: o aget_state soma escritas do passo que ainda não
        # foi gravado, e o aupdate_state que desfaz o turno parte só do que já foi gravado.
        return gravado.checkpoint["channel_values"].get("messages", []) if gravado else []

    async def ids_das_mensagens(self, id_da_conversa: str) -> set[str]:
        """Ids das mensagens já gravadas, para saber depois o que a resposta acrescentou."""
        return {mensagem.id for mensagem in await self.mensagens(id_da_conversa)}

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
            if id_da_conversa in self._apagar_ao_terminar:
                self._apagar_ao_terminar.discard(id_da_conversa)
                await self.agente.checkpointer.adelete_thread(id_da_conversa)

    async def apagar(self, id_da_conversa: str) -> None:
        """Apaga o histórico agora e, se a conversa ainda está respondendo, também quando terminar."""
        await self.agente.checkpointer.adelete_thread(id_da_conversa)
        if id_da_conversa in self._respondendo:
            self._apagar_ao_terminar.add(id_da_conversa)

    async def apagar_antigas(self) -> None:
        """Apaga as conversas cuja última mensagem é mais antiga que o prazo de guarda."""
        limite = self._agora() - self._prazo_de_guarda
        ultima_atividade: dict[str, datetime] = {}
        async for gravado in self.agente.checkpointer.alist(None):
            id_da_conversa = gravado.config["configurable"]["thread_id"]
            momento = datetime.fromisoformat(gravado.checkpoint["ts"])
            ultima_atividade[id_da_conversa] = max(momento, ultima_atividade.get(id_da_conversa, momento))
        for id_da_conversa, momento in ultima_atividade.items():
            if momento < limite and id_da_conversa not in self._respondendo:
                await self.agente.checkpointer.adelete_thread(id_da_conversa)

    async def _desfazer_turno(self, id_da_conversa: str, mensagens_antes: set[str]) -> None:
        novas = [mensagem for mensagem in await self.mensagens(id_da_conversa) if mensagem.id not in mensagens_antes]
        if novas:
            await self.agente.aupdate_state(
                _configuracao(id_da_conversa),
                {"messages": [RemoveMessage(id=mensagem.id) for mensagem in novas]},
                as_node=NO_DAS_FERRAMENTAS,
            )


def _configuracao(id_da_conversa: str) -> dict:
    return {"configurable": {"thread_id": id_da_conversa}}
