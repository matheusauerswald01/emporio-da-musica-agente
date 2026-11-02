"""API HTTP do agente, consumida pela interface de chat em web/ (Vercel AI SDK, useChat).

Rodar na raiz do projeto:  uvicorn api:app --port 8000

POST   /api/chat       recebe a nova mensagem do cliente e devolve a resposta em stream.
DELETE /api/chat/{id}  apaga da memória a conversa ("Nova conversa" e saída da página).

A interface manda só a última mensagem e o id da conversa: o histórico fica na memória do
LangGraph, no servidor, e não é reenviado pelo navegador a cada pergunta.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Literal
from uuid import UUID

import anyio
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from langchain_core.messages import HumanMessage
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel, Field, StringConstraints

from emporio.agente import criar_agente
from emporio.conversas import MemoriaDasConversas
from emporio.persona import WHATSAPP
from emporio.politicas import obter_indice
from emporio.protocolo_ai_sdk import CABECALHOS_DO_STREAM, FIM_DO_STREAM, TradutorDeStream, evento_sse

TAMANHO_MAXIMO_DA_MENSAGEM = 2000
INTERVALO_DA_LIMPEZA_EM_SEGUNDOS = 60
MENSAGEM_DE_ERRO = (
    "Tive um problema para responder agora. Tente de novo em instantes ou fale com a gente "
    f"pelo WhatsApp {WHATSAPP}."
)
# Uma conversa responde a uma mensagem por vez (ex.: reenvio logo depois do botão Parar).
MENSAGEM_OCUPADA = "Ainda estou terminando a resposta anterior. Tente de novo em instantes."

# Os logs nunca levam o texto das mensagens: ele pode ter nome, e-mail ou telefone (LGPD).
logger = logging.getLogger(__name__)

TextoDoCliente = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=TAMANHO_MAXIMO_DA_MENSAGEM)
]


class ParteDeTexto(BaseModel):
    type: Literal["text"]
    text: TextoDoCliente


class MensagemDoCliente(BaseModel):
    """Mensagem do cliente no formato UIMessage do AI SDK; só partes de texto são aceitas."""

    role: Literal["user"]
    parts: list[ParteDeTexto] = Field(min_length=1, max_length=1)


class PedidoDeResposta(BaseModel):
    # UUID aleatório gerado no navegador: difícil de adivinhar, então uma conversa não é lida por outra pessoa.
    id: UUID
    message: MensagemDoCliente


def criar_app(agente: CompiledStateGraph | None = None) -> FastAPI:
    """Cria a API. Sem agente, monta o de verdade (Gemini) ao subir; os testes passam um agente falso."""

    @asynccontextmanager
    async def ciclo_de_vida(app: FastAPI) -> AsyncIterator[None]:
        app.state.conversas = MemoriaDasConversas(agente or _agente_de_producao())
        limpeza = asyncio.create_task(_apagar_conversas_paradas(app.state.conversas))
        yield
        limpeza.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await limpeza

    app = FastAPI(title="Empório da Música: atendimento", lifespan=ciclo_de_vida)

    @app.post("/api/chat")
    async def responder(pedido: PedidoDeResposta, request: Request) -> StreamingResponse:
        texto = pedido.message.parts[0].text
        eventos = _responder_em_stream(request.app.state.conversas, str(pedido.id), texto)
        return StreamingResponse(eventos, media_type="text/event-stream", headers=CABECALHOS_DO_STREAM)

    @app.delete("/api/chat/{id_da_conversa}", status_code=204)
    async def apagar_conversa(id_da_conversa: UUID, request: Request) -> None:
        await request.app.state.conversas.apagar(str(id_da_conversa))

    return app


async def _responder_em_stream(conversas: MemoriaDasConversas, id_da_conversa: str, texto: str) -> AsyncIterator[str]:
    tradutor = TradutorDeStream()
    if not conversas.iniciar_resposta(id_da_conversa):
        for parte in [*tradutor.inicio(), *tradutor.erro(MENSAGEM_OCUPADA), *tradutor.fim()]:
            yield evento_sse(parte)
        yield FIM_DO_STREAM
        return

    mensagens_antes: set[str] | None = None
    concluida = False
    try:
        for parte in tradutor.inicio():
            yield evento_sse(parte)
        mensagens_antes = await conversas.ids_das_mensagens(id_da_conversa)
        try:
            async for mensagem, metadados in conversas.agente.astream(
                {"messages": [HumanMessage(texto)]},
                {"configurable": {"thread_id": id_da_conversa}},
                stream_mode="messages",
            ):
                for parte in tradutor.traduzir(mensagem, metadados):
                    yield evento_sse(parte)
            concluida = True
        except Exception as erro:
            # Só o tipo do erro: a mensagem de exceção pode trazer argumentos com dados do cliente.
            logger.error("Falha ao gerar a resposta: %s", type(erro).__name__)
            for parte in tradutor.erro(MENSAGEM_DE_ERRO):
                yield evento_sse(parte)
        for parte in tradutor.fim():
            yield evento_sse(parte)
        yield FIM_DO_STREAM
    finally:
        # Blindado: precisa rodar mesmo quando o cliente desconecta (botão Parar, aba fechada).
        with anyio.CancelScope(shield=True):
            await conversas.terminar_resposta(id_da_conversa, mensagens_antes, concluida)


async def _apagar_conversas_paradas(conversas: MemoriaDasConversas) -> None:
    while True:
        await asyncio.sleep(INTERVALO_DA_LIMPEZA_EM_SEGUNDOS)
        await conversas.apagar_paradas()


def _agente_de_producao() -> CompiledStateGraph:
    """Agente com o Gemini. Falha ao subir, e não na primeira pergunta, se faltar a chave."""
    load_dotenv()
    if not os.getenv("GOOGLE_API_KEY"):
        raise RuntimeError("Defina GOOGLE_API_KEY no arquivo .env (veja o .env.example).")
    # Monta o índice do manual já na subida, para a primeira pergunta sobre políticas não esperar.
    obter_indice()
    return criar_agente()


app = criar_app()
