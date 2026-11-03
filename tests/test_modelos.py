"""Testes da cadeia de modelos (principal e reservas) com modelos falsos e um relógio de teste."""

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from langchain_core.exceptions import (
    ModelAPIError,
    ModelAuthenticationError,
    ModelInvalidRequestError,
    ModelRateLimitError,
)
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_core.messages.tool import tool_call_chunk

from emporio.modelos import (
    PAUSA_PADRAO_EM_SEGUNDOS,
    RESERVAS_PADRAO,
    CadeiaDeModelos,
    ChaveDoGeminiRecusada,
    ModelosForaDoAr,
    modelos_do_gemini,
)
from tests.modelo_falso import ModeloQueCaiNoMeio, ModeloRoteirizado, chave_invalida, sem_cota


class Relogio:
    """Relógio de teste: o tempo só anda quando o teste manda."""

    def __init__(self) -> None:
        self.agora = 0.0

    def __call__(self) -> float:
        return self.agora


class ModeloSemPedacos(ModeloRoteirizado):
    """Stream que termina sem nenhum pedaço, como um corpo vazio vindo do Gemini."""

    def _stream(self, messages: list, stop: Any = None, run_manager: Any = None, **kwargs: Any) -> Iterator:
        self.recebidas.append(list(messages))
        yield from ()


def _cadeia(*modelos, relogio=None) -> CadeiaDeModelos:
    return CadeiaDeModelos(modelos, ferramentas=[], relogio=relogio or Relogio())


def _responder(cadeia: CadeiaDeModelos) -> str:
    return asyncio.run(cadeia.responder([HumanMessage("Oi")])).text


def test_modelo_sem_cota_passa_a_vez_para_o_proximo_da_cadeia():
    principal = ModeloRoteirizado(roteiro=[sem_cota()])
    reserva = ModeloRoteirizado(roteiro=[sem_cota()])
    ultima_reserva = ModeloRoteirizado(roteiro=[AIMessage("Oi! Como posso ajudar?")])

    assert _responder(_cadeia(principal, reserva, ultima_reserva)) == "Oi! Como posso ajudar?"
    # Todos recebem o mesmo pedido.
    assert principal.recebidas == reserva.recebidas == ultima_reserva.recebidas


def test_modelo_sem_cota_fica_fora_da_cadeia_pelo_tempo_que_o_google_pede():
    relogio = Relogio()
    principal = ModeloRoteirizado(roteiro=[sem_cota("3600s"), AIMessage("Voltei!")])
    reserva = ModeloRoteirizado(roteiro=[AIMessage("Reserva 1"), AIMessage("Reserva 2")])
    cadeia = _cadeia(principal, reserva, relogio=relogio)

    assert _responder(cadeia) == "Reserva 1"
    relogio.agora = 3599
    assert _responder(cadeia) == "Reserva 2"
    assert len(principal.recebidas) == 1  # o principal nem foi chamado
    relogio.agora = 3601
    assert _responder(cadeia) == "Voltei!"


def test_sobrecarga_tira_o_modelo_da_cadeia_por_um_minuto():
    relogio = Relogio()
    principal = ModeloRoteirizado(roteiro=[ModelAPIError("503 UNAVAILABLE"), AIMessage("Voltei!")])
    reserva = ModeloRoteirizado(roteiro=[AIMessage("Reserva 1"), AIMessage("Reserva 2")])
    cadeia = _cadeia(principal, reserva, relogio=relogio)

    assert _responder(cadeia) == "Reserva 1"
    relogio.agora = PAUSA_PADRAO_EM_SEGUNDOS - 1
    assert _responder(cadeia) == "Reserva 2"
    relogio.agora = PAUSA_PADRAO_EM_SEGUNDOS + 1
    assert _responder(cadeia) == "Voltei!"


def test_espera_ilegivel_no_429_vira_a_pausa_padrao():
    relogio = Relogio()
    principal = ModeloRoteirizado(roteiro=[sem_cota("amanhã"), AIMessage("Voltei!")])
    reserva = ModeloRoteirizado(roteiro=[AIMessage("Reserva")])
    cadeia = _cadeia(principal, reserva, relogio=relogio)

    assert _responder(cadeia) == "Reserva"
    relogio.agora = PAUSA_PADRAO_EM_SEGUNDOS + 1
    assert _responder(cadeia) == "Voltei!"


def test_pedido_recusado_passa_a_vez_sem_tirar_o_modelo_da_cadeia():
    # Um 400 pode ser deste pedido, e não do modelo: a próxima mensagem volta a tentar o principal.
    principal = ModeloRoteirizado(roteiro=[ModelInvalidRequestError("400 INVALID_ARGUMENT"), AIMessage("Principal")])
    reserva = ModeloRoteirizado(roteiro=[AIMessage("Reserva")])
    cadeia = _cadeia(principal, reserva)

    assert _responder(cadeia) == "Reserva"
    assert _responder(cadeia) == "Principal"


def test_stream_sem_nenhum_pedaco_passa_a_vez():
    principal = ModeloSemPedacos(roteiro=[])
    reserva = ModeloRoteirizado(roteiro=[AIMessage("Oi!")])

    assert _responder(_cadeia(principal, reserva)) == "Oi!"
    assert len(principal.recebidas) == 1


@pytest.mark.parametrize("erro", [chave_invalida, lambda: ModelAuthenticationError("401 UNAUTHENTICATED")])
def test_chave_recusada_nao_passa_a_vez(erro):
    # O Google recusa uma chave inválida com 400 (API_KEY_INVALID), e não com 401.
    principal = ModeloRoteirizado(roteiro=[erro()])
    reserva = ModeloRoteirizado(roteiro=[AIMessage("Oi!")])

    with pytest.raises(ChaveDoGeminiRecusada):
        _responder(_cadeia(principal, reserva))
    assert reserva.recebidas == []


def test_resposta_vazia_passa_a_vez():
    # Ex.: MALFORMED_FUNCTION_CALL, que chega como uma resposta sem texto e sem ferramenta.
    principal = ModeloRoteirizado(roteiro=[AIMessage(""), AIMessage("Voltei!")])
    reserva = ModeloRoteirizado(roteiro=[AIMessage("Reserva")])
    cadeia = _cadeia(principal, reserva)

    assert _responder(cadeia) == "Reserva"
    # Resposta vazia não é falta de disponibilidade: o principal não fica pausado.
    assert _responder(cadeia) == "Voltei!"


def test_resposta_vazia_de_todos_e_devolvida():
    # A API mostra o texto padrão ("me perdi aqui") no lugar de uma resposta vazia.
    cadeia = _cadeia(ModeloRoteirizado(roteiro=[AIMessage("")]), ModeloRoteirizado(roteiro=[ModelAPIError("503")]))

    assert _responder(cadeia) == ""


def test_falha_depois_do_nome_da_ferramenta_na_tela_nao_passa_a_vez_e_pausa_o_modelo():
    pedido_de_ferramenta = AIMessageChunk(
        content="", tool_call_chunks=[tool_call_chunk(name="promocoes_ativas", args="", id="c1", index=0)]
    )
    principal = ModeloQueCaiNoMeio(pedaco=pedido_de_ferramenta)
    reserva = ModeloRoteirizado(roteiro=[AIMessage("Reserva")])
    cadeia = _cadeia(principal, reserva)

    with pytest.raises(ModelAPIError):
        _responder(cadeia)
    assert reserva.recebidas == []
    # O principal caiu: a próxima mensagem já vai direto para a reserva.
    assert _responder(cadeia) == "Reserva"
    assert len(principal.recebidas) == 1


def test_reserva_que_cai_no_meio_do_stream_nao_passa_a_vez_para_a_seguinte():
    terceira = ModeloRoteirizado(roteiro=[AIMessage("Terceira")])

    with pytest.raises(ModelAPIError):
        _responder(_cadeia(ModeloRoteirizado(roteiro=[sem_cota()]), ModeloQueCaiNoMeio(), terceira))
    assert terceira.recebidas == []


def test_falha_curta_nao_encurta_uma_pausa_longa():
    relogio = Relogio()
    principal = ModeloRoteirizado(roteiro=[sem_cota(), ModelAPIError("503")])
    reserva = ModeloRoteirizado(roteiro=[ModelAPIError("503"), AIMessage("Reserva 1"), AIMessage("Reserva 2")])
    cadeia = _cadeia(principal, reserva, relogio=relogio)

    with pytest.raises(ModelAPIError):
        _responder(cadeia)
    assert _responder(cadeia) == "Reserva 1"  # os dois pausados: tenta os dois; o principal dá 503
    relogio.agora = 2 * PAUSA_PADRAO_EM_SEGUNDOS
    # O 503 não trocou a pausa da cota do dia pela de um minuto: o principal continua de fora.
    assert _responder(cadeia) == "Reserva 2"
    assert len(principal.recebidas) == 2


def test_modelo_pausado_por_outra_conversa_no_meio_da_chamada_fica_para_o_fim():
    relogio = Relogio()
    cadeia = _cadeia(*(ModeloRoteirizado(roteiro=[]) for _ in range(3)), relogio=relogio)
    proximos = cadeia._proximos([0, 1, 2])

    assert next(proximos) == 0
    cadeia._pausado_ate[1] = 100  # outra conversa pausou o segundo modelo enquanto esta esperava o primeiro
    assert list(proximos) == [2, 1]


def test_sem_cota_do_dia_com_outro_modelo_pausado_por_pouco_nao_e_fora_do_ar():
    relogio = Relogio()
    principal = ModeloRoteirizado(roteiro=[ModelAPIError("503 UNAVAILABLE"), AIMessage("Voltei!")])
    reserva = ModeloRoteirizado(roteiro=[AIMessage("Reserva"), sem_cota()])
    cadeia = _cadeia(principal, reserva, relogio=relogio)

    assert _responder(cadeia) == "Reserva"
    relogio.agora = 10
    # Só a reserva é tentada e fica sem cota do dia, mas o principal volta em segundos.
    with pytest.raises(ModelRateLimitError):
        _responder(cadeia)
    assert len(principal.recebidas) == 1
    relogio.agora = PAUSA_PADRAO_EM_SEGUNDOS + 1
    assert _responder(cadeia) == "Voltei!"


def test_cota_do_dia_esgotada_em_todos_deixa_a_lia_fora_do_ar():
    with pytest.raises(ModelosForaDoAr):
        _responder(_cadeia(ModeloRoteirizado(roteiro=[sem_cota()]), ModeloRoteirizado(roteiro=[sem_cota()])))


def test_outra_falha_com_todos_sem_cota_do_dia_nao_e_fora_do_ar():
    # Os dois já estão sem cota por horas; na nova tentativa, falham por outro motivo.
    principal = ModeloRoteirizado(roteiro=[sem_cota(), ModelAPIError("503")])
    reserva = ModeloRoteirizado(roteiro=[sem_cota(), ModelAPIError("503")])
    cadeia = _cadeia(principal, reserva)
    with pytest.raises(ModelosForaDoAr):
        _responder(cadeia)

    with pytest.raises(ModelAPIError):
        _responder(cadeia)


def test_limite_por_minuto_em_todos_e_falha_passageira():
    # Com poucos segundos de espera, tentar de novo daqui a pouco resolve: não é "fora do ar".
    principal = ModeloRoteirizado(roteiro=[sem_cota("20s")])
    reserva = ModeloRoteirizado(roteiro=[sem_cota("20s")])

    with pytest.raises(ModelRateLimitError):
        _responder(_cadeia(principal, reserva))


def test_com_todos_os_modelos_pausados_tenta_todos_assim_mesmo_e_em_ordem():
    principal = ModeloRoteirizado(roteiro=[ModelAPIError("503"), ModelAPIError("503")])
    reserva = ModeloRoteirizado(roteiro=[ModelAPIError("503"), AIMessage("Reserva")])
    cadeia = _cadeia(principal, reserva)

    with pytest.raises(ModelAPIError):
        _responder(cadeia)
    # Os dois estão pausados, mas o cliente não fica sem uma tentativa: o principal primeiro.
    assert _responder(cadeia) == "Reserva"
    assert len(principal.recebidas) == 2


def test_troca_de_modelo_fica_no_log_sem_a_mensagem_do_erro(caplog):
    principal = ModeloRoteirizado(roteiro=[sem_cota()])
    reserva = ModeloRoteirizado(roteiro=[AIMessage("Oi!")])

    _responder(_cadeia(principal, reserva))

    assert "ModelRateLimitError 429" in caplog.text and "25091 s" in caplog.text
    assert "exceeded" not in caplog.text


def test_cadeia_do_gemini_vem_do_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "chave-de-teste")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.8-flash")

    def nomes() -> list[str]:
        return [modelo.model.removeprefix("models/") for modelo in modelos_do_gemini()]

    monkeypatch.delenv("GEMINI_FALLBACK_MODELS", raising=False)
    assert nomes() == ["gemini-3.8-flash", *RESERVAS_PADRAO]
    # Espaços e itens vazios são ignorados, e um modelo repetido entra uma vez só.
    monkeypatch.setenv("GEMINI_FALLBACK_MODELS", " gemini-3.5-flash-lite, ,gemini-3.8-flash")
    assert nomes() == ["gemini-3.8-flash", "gemini-3.5-flash-lite"]
    monkeypatch.setenv("GEMINI_FALLBACK_MODELS", "")
    assert nomes() == ["gemini-3.8-flash"]


def test_cada_modelo_do_gemini_tenta_uma_vez_e_tem_tempo_limite(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "chave-de-teste")
    modelos = modelos_do_gemini()

    # 1 é só a chamada inicial (0 seria o padrão do Google, com 5 tentativas): assim o SDK não repete
    # o 429 da cota do dia, e quem tenta de novo é o próximo modelo da cadeia.
    assert [modelo.max_retries for modelo in modelos] == [1] * len(modelos)
    assert all(modelo.timeout == 30 for modelo in modelos)
