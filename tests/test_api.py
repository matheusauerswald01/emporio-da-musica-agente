"""Testes da API com um modelo falso: stream no protocolo do Vercel AI SDK, validação e apagar a conversa."""

import json
import threading
import time
from uuid import uuid4

import anyio
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from api import MENSAGEM_DE_ERRO, MENSAGEM_OCUPADA, _responder_em_stream, criar_app
from emporio import politicas
from emporio.agente import criar_agente
from emporio.conversas import MemoriaDasConversas
from emporio.protocolo_ai_sdk import TEXTO_SEM_RESPOSTA
from tests.modelo_falso import ModeloRoteirizado, chamada


def _cliente(*roteiro) -> TestClient:
    return TestClient(criar_app(criar_agente(ModeloRoteirizado(roteiro=list(roteiro)))))


def _pedido(texto: str, id_da_conversa: str | None = None) -> dict:
    return {
        "id": id_da_conversa or str(uuid4()),
        "message": {"id": "m1", "role": "user", "parts": [{"type": "text", "text": texto}]},
    }


def _partes(resposta) -> list:
    """Partes do stream SSE, na ordem; o "[DONE]" final fica como texto."""
    eventos = [linha.removeprefix("data: ") for linha in resposta.text.split("\n\n") if linha]
    return [evento if evento == "[DONE]" else json.loads(evento) for evento in eventos]


def _texto(partes: list) -> str:
    return "".join(parte["delta"] for parte in partes if parte != "[DONE]" and parte["type"] == "text-delta")


def test_chat_responde_em_stream_no_protocolo_do_ai_sdk():
    with _cliente(
        AIMessage("", tool_calls=[chamada("promocoes_ativas")]),
        AIMessage("Temos 4 promoções ativas!"),
    ) as cliente:
        resposta = cliente.post("/api/chat", json=_pedido("Tem promoção?"))

    assert resposta.status_code == 200
    assert resposta.headers["x-vercel-ai-ui-message-stream"] == "v1"
    assert resposta.headers["content-type"].startswith("text/event-stream")
    partes = _partes(resposta)
    tipos = [parte if parte == "[DONE]" else parte["type"] for parte in partes]
    assert tipos[:4] == ["start", "start-step", "data-ferramenta", "finish-step"]
    assert tipos[-4:] == ["text-end", "finish-step", "finish", "[DONE]"]
    assert partes[2] == {"type": "data-ferramenta", "data": {"nome": "promocoes_ativas"}, "transient": True}
    assert _texto(partes) == "Temos 4 promoções ativas!"
    # O resultado da ferramenta fica no servidor; o navegador recebe só a resposta.
    assert "Ohana" not in resposta.text


def test_resposta_com_falha_vira_aviso_e_e_desfeita_sem_duplicar_a_pergunta():
    id_da_conversa = str(uuid4())
    configuracao = {"configurable": {"thread_id": id_da_conversa}}
    with _cliente(
        AIMessage("", tool_calls=[chamada("promocoes_ativas")]),
        RuntimeError("cota esgotada"),
        AIMessage("Temos 4 promoções ativas!"),
    ) as cliente:
        com_falha = _partes(cliente.post("/api/chat", json=_pedido("Tem promoção?", id_da_conversa)))
        agente = cliente.app.state.conversas.agente
        # A memória não guarda a resposta pela metade (nem a chamada de ferramenta sem resposta).
        assert agente.get_state(configuracao).values["messages"] == []

        # "Tentar de novo" reenvia a mesma pergunta: ela fica gravada uma vez só.
        cliente.post("/api/chat", json=_pedido("Tem promoção?", id_da_conversa))
        mensagens = agente.get_state(configuracao).values["messages"]

    assert {"type": "error", "errorText": MENSAGEM_DE_ERRO} in com_falha
    assert com_falha[-2:] == [{"type": "finish"}, "[DONE]"]
    assert "cota esgotada" not in json.dumps(com_falha)
    assert [mensagem.text for mensagem in mensagens] == ["Tem promoção?", "Temos 4 promoções ativas!"]


def test_parar_no_meio_da_ferramenta_logo_na_primeira_pergunta_desfaz_o_turno(monkeypatch):
    consulta_em_andamento = threading.Event()

    class IndiceLento:
        def buscar(self, assunto):
            consulta_em_andamento.set()
            time.sleep(0.5)
            return []

    monkeypatch.setattr(politicas, "obter_indice", IndiceLento)
    conversas = MemoriaDasConversas(criar_agente(ModeloRoteirizado(roteiro=[
        AIMessage("", tool_calls=[chamada("consultar_politicas", assunto="devolução")]),
    ])))

    async def cliente_clica_em_parar():
        async def receber():
            async for _ in _responder_em_stream(conversas, "conversa", "Posso devolver?"):
                pass

        # O Starlette cancela o stream assim, por um cancel scope do anyio, quando o cliente desconecta.
        async with anyio.create_task_group() as grupo:
            grupo.start_soon(receber)
            while not consulta_em_andamento.is_set():
                await anyio.sleep(0.01)
            grupo.cancel_scope.cancel()

    anyio.run(cliente_clica_em_parar)

    estado = conversas.agente.get_state({"configurable": {"thread_id": "conversa"}})
    assert estado.values["messages"] == []


def test_conversa_responde_uma_mensagem_por_vez():
    id_da_conversa = str(uuid4())
    with _cliente() as cliente:
        cliente.app.state.conversas.iniciar_resposta(id_da_conversa)  # uma resposta em andamento
        partes = _partes(cliente.post("/api/chat", json=_pedido("Oi de novo", id_da_conversa)))

    assert {"type": "error", "errorText": MENSAGEM_OCUPADA} in partes
    assert partes[-1] == "[DONE]"


def test_resposta_final_vazia_recebe_o_texto_padrao():
    with _cliente(
        AIMessage("Deixa eu ver.", tool_calls=[chamada("promocoes_ativas")]),
        AIMessage(""),
    ) as cliente:
        partes = _partes(cliente.post("/api/chat", json=_pedido("Tem promoção?")))

    assert _texto(partes) == f"Deixa eu ver.{TEXTO_SEM_RESPOSTA}"


@pytest.mark.parametrize(
    "pedido",
    [
        {**_pedido("Oi"), "id": "conversa-de-outra-pessoa"},  # id precisa ser um UUID
        _pedido("   "),
        _pedido("a" * 2001),
        {**_pedido("Oi"), "message": {"role": "assistant", "parts": [{"type": "text", "text": "Oi"}]}},
    ],
    ids=["id-sem-uuid", "texto-vazio", "texto-longo-demais", "papel-de-assistente"],
)
def test_chat_recusa_pedido_invalido(pedido):
    with _cliente() as cliente:
        assert cliente.post("/api/chat", json=pedido).status_code == 422


def test_nova_conversa_apaga_o_historico_da_memoria():
    id_da_conversa = str(uuid4())
    configuracao = {"configurable": {"thread_id": id_da_conversa}}
    with _cliente(AIMessage("Oi! Sou a Lia.")) as cliente:
        cliente.post("/api/chat", json=_pedido("Oi, sou o Bruno", id_da_conversa))
        agente = cliente.app.state.conversas.agente
        assert agente.get_state(configuracao).values["messages"]

        assert cliente.delete(f"/api/chat/{id_da_conversa}").status_code == 204
        assert agente.get_state(configuracao).values == {}
