"""Converte o stream do LangGraph no protocolo de stream de UI do Vercel AI SDK, lido pelo useChat.

O protocolo usa Server-Sent Events: cada evento é uma parte em JSON ("start", "text-delta",
"finish"...) e o stream termina com "data: [DONE]". Referência na documentação que vem no pacote:
web/node_modules/ai/docs/04-ai-sdk-ui/50-stream-protocol.mdx.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage

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

# Resposta sem nenhum texto depois da última consulta (acontece, raramente, depois de uma ferramenta).
TEXTO_SEM_RESPOSTA = "Desculpe, me perdi aqui. Pode repetir a pergunta?"


def evento_sse(parte: dict[str, Any]) -> str:
    """Uma parte do protocolo como evento SSE."""
    return f"data: {json.dumps(parte, ensure_ascii=False)}\n\n"


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

    def erro(self, texto: str) -> list[dict[str, Any]]:
        """Partes que avisam a interface de uma falha no meio da resposta."""
        self._respondeu = True  # A mensagem de erro já é a resposta.
        return [*self._fechar_texto(), {"type": "error", "errorText": texto}]

    def fim(self) -> list[dict[str, Any]]:
        partes = [] if self._respondeu else self._escrever(TEXTO_SEM_RESPOSTA)
        partes += self._fechar_passo()
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


def _ferramentas_chamadas(mensagem: AIMessage) -> list[str]:
    """Nomes das ferramentas pedidas. No stream, só o primeiro pedaço de cada chamada traz o nome."""
    chamadas = mensagem.tool_call_chunks if isinstance(mensagem, AIMessageChunk) else mensagem.tool_calls
    return [chamada["name"] for chamada in chamadas if chamada.get("name")]
