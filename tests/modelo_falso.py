"""Modelo de chat falso, para testar o agente e a API sem chamar o Gemini."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.messages.tool import tool_call_chunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import Field


class ModeloRoteirizado(BaseChatModel):
    """Devolve as respostas do roteiro, em ordem, e guarda as mensagens que recebeu em cada chamada.

    No stream, imita o Gemini: primeiro o texto, palavra por palavra, depois as chamadas de
    ferramenta inteiras. Uma exceção no roteiro é lançada na chamada correspondente.
    """

    # Any: o pydantic tentaria converter a exceção do roteiro numa AIMessage.
    roteiro: list[Any]
    recebidas: list[list[BaseMessage]] = Field(default_factory=list)
    # Em cada chamada, se o modelo podia pedir ferramentas ("ferramentas") ou só responder ("so_texto").
    modos: list[str] = Field(default_factory=list)
    modo: str = "ferramentas"

    @property
    def _llm_type(self) -> str:
        return "roteirizado"

    def bind_tools(self, tools: Any, tool_choice: Any = None, **kwargs: Any) -> ModeloRoteirizado:
        # Cópia rasa: o roteiro e os registros continuam sendo as mesmas listas.
        return self.model_copy(update={"modo": "so_texto" if tool_choice == "none" else "ferramentas"})

    def _generate(self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any) -> ChatResult:
        self.recebidas.append(list(messages))
        self.modos.append(self.modo)
        resposta = self.roteiro.pop(0)
        if isinstance(resposta, Exception):
            raise resposta
        return ChatResult(generations=[ChatGeneration(message=resposta)])

    def _stream(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> Iterator[ChatGenerationChunk]:
        resposta = self._generate(messages).generations[0].message
        for pedaco in _em_pedacos(resposta):
            chunk = ChatGenerationChunk(message=pedaco)
            if run_manager:
                run_manager.on_llm_new_token(pedaco.text, chunk=chunk)
            yield chunk


def _em_pedacos(resposta: AIMessage) -> list[AIMessageChunk]:
    pedacos = [AIMessageChunk(content=palavra) for palavra in re.findall(r"\S+\s*", resposta.text)]
    if resposta.tool_calls:
        chamadas = [
            tool_call_chunk(name=chamada["name"], args=json.dumps(chamada["args"]), id=chamada["id"], index=indice)
            for indice, chamada in enumerate(resposta.tool_calls)
        ]
        pedacos.append(AIMessageChunk(content="", tool_call_chunks=chamadas))
    # Resposta vazia ainda chega como um pedaço vazio, como no Gemini.
    return pedacos or [AIMessageChunk(content="")]


def chamada(ferramenta: str, id_da_chamada: str = "chamada-1", **argumentos: Any) -> dict[str, Any]:
    """Chamada de ferramenta para montar o roteiro."""
    return {"name": ferramenta, "args": argumentos, "id": id_da_chamada}
