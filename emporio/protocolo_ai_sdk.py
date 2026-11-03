"""Converte o stream do LangGraph no protocolo de stream de UI do Vercel AI SDK, lido pelo useChat.

O protocolo usa Server-Sent Events: cada evento é uma parte em JSON ("start", "text-delta",
"finish"...) e o stream termina com "data: [DONE]". Referência na documentação que vem no pacote:
web/node_modules/ai/docs/04-ai-sdk-ui/50-stream-protocol.mdx.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import anyio
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage

CABECALHOS_DO_STREAM = {
    # no-transform: impede que um proxy (o próprio Next.js, no repasse de /api) comprima o stream
    # em gzip, o que seguraria todo o texto até o fim da resposta.
    "cache-control": "no-cache, no-transform",
    "connection": "keep-alive",
    # Avisa o useChat que o corpo segue o protocolo de UI do AI SDK.
    "x-vercel-ai-ui-message-stream": "v1",
    # Desliga o buffer de proxies (nginx), que seguraria o texto até o fim da resposta.
    "x-accel-buffering": "no",
}
FIM_DO_STREAM = "data: [DONE]\n\n"

# Comentário SSE (o useChat ignora) mandado quando o stream fica parado, por exemplo enquanto o
# cliente do Gemini repete uma chamada que falhou. Sem ele, proxies derrubam a conexão parada:
# o do Next.js, no repasse de /api, corta com 30 s; o nginx, com 60 s.
SINAL_DE_VIDA = ": keep-alive\n\n"
INTERVALO_DO_SINAL_DE_VIDA_EM_SEGUNDOS = 10

# Resposta sem nenhum texto depois da última consulta (acontece, raramente, depois de uma ferramenta).
TEXTO_SEM_RESPOSTA = "Desculpe, me perdi aqui. Pode repetir a pergunta?"


def evento_sse(parte: dict[str, Any]) -> str:
    """Uma parte do protocolo como evento SSE."""
    return f"data: {json.dumps(parte, ensure_ascii=False)}\n\n"


async def com_sinal_de_vida(
    eventos: AsyncIterator[str], intervalo: float = INTERVALO_DO_SINAL_DE_VIDA_EM_SEGUNDOS
) -> AsyncIterator[str]:
    """Repassa os eventos e, a cada `intervalo` segundos sem nenhum, manda o SINAL_DE_VIDA.

    Se o cliente desconecta, a espera pelo próximo evento é cancelada, e o cancelamento chega à
    resposta em andamento (que então se desfaz); este gerador espera ela terminar de se desfazer.
    """
    proximo = asyncio.ensure_future(anext(eventos))
    try:
        while True:
            pronto, _ = await asyncio.wait({proximo}, timeout=intervalo)
            if not pronto:
                yield SINAL_DE_VIDA
                continue
            try:
                evento = proximo.result()
            except StopAsyncIteration:
                return
            yield evento
            proximo = asyncio.ensure_future(anext(eventos))
    finally:
        if not proximo.done():
            proximo.cancel()
            with anyio.CancelScope(shield=True), contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
                await proximo


class TradutorDeStream:
    """Gera as partes do protocolo, na ordem certa, para uma resposta do agente.

    Cada chamada ao LLM é um passo (start-step/finish-step) com o seu bloco de texto. As chamadas
    de ferramenta não vão para o navegador: vai só um aviso passageiro ("data-ferramenta",
    transient) com o nome da ferramenta, para a interface mostrar o que está sendo consultado sem
    receber os dados que a ferramenta devolveu (minimização, LGPD).
    """

    def __init__(self) -> None:
        self._passo: int | None = None
        self._id_do_texto: str | None = None
        # Se já veio texto depois da última consulta; a resposta de verdade vem depois das ferramentas.
        self._respondeu = False

    def inicio(self) -> list[dict[str, Any]]:
        return [{"type": "start", "messageId": f"msg-{uuid4().hex}"}]

    def traduzir(self, mensagem: BaseMessage, metadados: dict[str, Any]) -> list[dict[str, Any]]:
        """Partes para um item de `agente.astream(..., stream_mode="messages")`.

        Só as mensagens do LLM interessam; as de ferramenta (ToolMessage) ficam no servidor.
        """
        if not isinstance(mensagem, AIMessage):
            return []
        partes = self._mudar_de_passo(metadados["langgraph_step"])
        if mensagem.text:
            partes += self._escrever(mensagem.text)
        for nome in _ferramentas_chamadas(mensagem):
            partes += self._fechar_texto()
            partes.append({"type": "data-ferramenta", "data": {"nome": nome}, "transient": True})
            self._respondeu = False
        return partes

    def erro(self, texto: str, pode_tentar_de_novo: bool = True) -> list[dict[str, Any]]:
        """Partes que avisam a interface de uma falha no meio da resposta.

        O aviso passageiro "data-erro" leva o texto para a tela e diz se vale oferecer "Tentar de novo";
        sem ele, a tela entende que a falha foi no caminho (rede) e mostra um texto genérico. Ele vem
        antes do "error" porque o AI SDK para de ler o stream no "error". O "error" continua: é ele que
        põe o useChat em estado de erro.
        """
        self._respondeu = True  # A mensagem de erro já é a resposta.
        aviso = {
            "type": "data-erro",
            "data": {"texto": texto, "podeTentarDeNovo": pode_tentar_de_novo},
            "transient": True,
        }
        return [*self._fechar_texto(), aviso, {"type": "error", "errorText": texto}]

    def historico_apagado(self, texto: str) -> list[dict[str, Any]]:
        """Resposta ao comando /apagar: a confirmação e o aviso para a tela limpar a conversa."""
        return [*self._escrever(texto), {"type": "data-historico-apagado", "data": {}, "transient": True}]

    def fim(self) -> list[dict[str, Any]]:
        partes = [] if self._respondeu else self._escrever(TEXTO_SEM_RESPOSTA)
        partes += [*self._fechar_texto(), *self._fechar_passo()]
        partes.append({"type": "finish"})
        return partes

    def _mudar_de_passo(self, passo: int) -> list[dict[str, Any]]:
        if passo == self._passo:
            return []
        partes = self._fechar_passo()
        self._passo = passo
        partes.append({"type": "start-step"})
        return partes

    def _escrever(self, texto: str) -> list[dict[str, Any]]:
        partes = []
        if self._id_do_texto is None:
            self._id_do_texto = f"texto-{uuid4().hex}"
            partes.append({"type": "text-start", "id": self._id_do_texto})
        partes.append({"type": "text-delta", "id": self._id_do_texto, "delta": texto})
        self._respondeu = True
        return partes

    def _fechar_texto(self) -> list[dict[str, Any]]:
        if self._id_do_texto is None:
            return []
        parte = {"type": "text-end", "id": self._id_do_texto}
        self._id_do_texto = None
        return [parte]

    def _fechar_passo(self) -> list[dict[str, Any]]:
        if self._passo is None:
            return []
        self._passo = None
        return [*self._fechar_texto(), {"type": "finish-step"}]


def historico_para_a_interface(mensagens: list[BaseMessage]) -> list[dict[str, Any]]:
    """Histórico gravado no formato UIMessage do AI SDK, para a tela mostrar quando o cliente volta.

    Vão só as perguntas do cliente e os textos da Lia; chamadas e resultados de ferramentas ficam
    no servidor, como durante a resposta. Os textos da Lia num mesmo turno viram uma mensagem só.
    """
    interface: list[dict[str, Any]] = []
    for mensagem in mensagens:
        papel = "user" if isinstance(mensagem, HumanMessage) else "assistant"
        if not isinstance(mensagem, (HumanMessage, AIMessage)) or not mensagem.text:
            continue
        parte = {"type": "text", "text": mensagem.text, "state": "done"}
        if papel == "assistant" and interface and interface[-1]["role"] == "assistant":
            interface[-1]["parts"].append(parte)
        else:
            interface.append({"id": mensagem.id, "role": papel, "parts": [parte]})
    return interface


def _ferramentas_chamadas(mensagem: AIMessage) -> list[str]:
    """Nomes das ferramentas pedidas. No stream, só o primeiro pedaço de cada chamada traz o nome."""
    chamadas = mensagem.tool_call_chunks if isinstance(mensagem, AIMessageChunk) else mensagem.tool_calls
    return [chamada["name"] for chamada in chamadas if chamada.get("name")]
