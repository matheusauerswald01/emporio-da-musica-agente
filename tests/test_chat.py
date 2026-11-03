"""Testes do chat no terminal com o modelo falso, um SQLite temporário e a digitação simulada."""

import asyncio
from collections.abc import Iterator
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage

import chat
from api import MENSAGEM_DE_ERRO, MENSAGEM_HISTORICO_APAGADO
from emporio.agente import criar_agente
from emporio.conversas import MemoriaDasConversas
from emporio.expediente import VARIAVEL_DO_RELOGIO
from emporio.memoria import abrir_memoria
from tests.modelo_falso import ModeloRoteirizado, chamada

EMAIL_DA_ANA = "anacarol.ferreira@coldmail.com"


def _digitacao(*linhas: str):
    """Simula o cliente digitando cada linha, em ordem."""
    restantes: Iterator[str] = iter(linhas)
    return lambda _convite: next(restantes)


def _conversar(tmp_path, modelo, *linhas, argumentos=()):
    chat.executar(
        chat._ler_argumentos(list(argumentos)),
        criar_agente_do_chat=lambda memoria: criar_agente(modelo, memoria=memoria),
        arquivo=tmp_path / "conversas.db",
        ler=_digitacao(*linhas),
    )


def _id_para_continuar(saida: str) -> str:
    """O id da conversa na dica que o chat mostra ao sair."""
    return saida.rstrip().rsplit("--conversa ", 1)[1]


def _mensagens_gravadas(tmp_path, id_da_conversa):
    async def ler():
        async with abrir_memoria(tmp_path / "conversas.db") as memoria:
            return await MemoriaDasConversas(criar_agente(ModeloRoteirizado(roteiro=[]), memoria=memoria)).mensagens(
                id_da_conversa
            )

    return asyncio.run(ler())


def test_pergunta_mostra_a_consulta_e_a_resposta(tmp_path, capsys):
    modelo = ModeloRoteirizado(roteiro=[
        AIMessage("", tool_calls=[chamada("detalhes_produto", nome="Takamine GD20")]),
        AIMessage("O Takamine GD20 custa R$ 2.199,00."),
    ])

    _conversar(tmp_path, modelo, "Quanto custa o Takamine GD20?", "/sair")

    saida = capsys.readouterr().out
    assert saida.startswith(chat.SAUDACAO)
    assert "(Conferindo o produto…)" in saida
    assert "Lia: O Takamine GD20 custa R$ 2.199,00." in saida
    assert "Pra continuar esta conversa depois: python chat.py --conversa " in saida


def test_apagar_apaga_o_historico_e_comeca_outra_conversa(tmp_path, capsys):
    modelo = ModeloRoteirizado(roteiro=[AIMessage("Oi, Bruno!")])
    _conversar(tmp_path, modelo, "Oi, sou o Bruno", "/sair")
    id_da_conversa = _id_para_continuar(capsys.readouterr().out)
    assert len(_mensagens_gravadas(tmp_path, id_da_conversa)) == 2

    _conversar(tmp_path, modelo, "/apagar", "/sair", argumentos=["--conversa", id_da_conversa])

    saida = capsys.readouterr().out
    assert f"Lia: {MENSAGEM_HISTORICO_APAGADO}" in saida
    assert _id_para_continuar(saida) != id_da_conversa
    assert _mensagens_gravadas(tmp_path, id_da_conversa) == []


def test_detalhes_mostram_a_ferramenta_e_o_modelo_sem_o_contato(tmp_path, capsys):
    modelo = ModeloRoteirizado(roteiro=[
        AIMessage("", tool_calls=[chamada("status_pedido", numero_pedido=8, email_ou_telefone=EMAIL_DA_ANA)]),
        AIMessage("Ana, seu pedido está atrasado.", response_metadata={"model_name": "gemini-3.5-flash-lite"}),
    ])

    _conversar(tmp_path, modelo, f"Pedido 8, e-mail {EMAIL_DA_ANA}", "/sair", argumentos=["--detalhes"])

    saida = capsys.readouterr().out
    assert '[ferramenta] status_pedido(numero_pedido=8, email_ou_telefone="***")' in saida
    assert "[modelo] gemini-3.5-flash-lite" in saida
    assert "coldmail" not in saida


def test_conversa_continua_de_onde_parou(tmp_path, capsys):
    modelo = ModeloRoteirizado(roteiro=[AIMessage("Oi, Bruno!"), AIMessage("Você é o Bruno.")])
    _conversar(tmp_path, modelo, "Oi, sou o Bruno", "/sair")
    id_da_conversa = _id_para_continuar(capsys.readouterr().out)

    _conversar(tmp_path, modelo, "Qual é o meu nome?", "/sair", argumentos=["--conversa", id_da_conversa])

    saida = capsys.readouterr().out
    assert "Você: Oi, sou o Bruno\nLia: Oi, Bruno!" in saida
    # O modelo recebeu a conversa anterior, gravada no SQLite.
    assert [mensagem.text for mensagem in modelo.recebidas[-1][1:]] == ["Oi, sou o Bruno", "Oi, Bruno!", "Qual é o meu nome?"]
    assert saida.rstrip().endswith(f"--conversa {id_da_conversa}")


def test_mensagem_longa_demais_e_recusada_sem_chamar_o_modelo(tmp_path, capsys):
    modelo = ModeloRoteirizado(roteiro=[])

    _conversar(tmp_path, modelo, "a" * 2001, "/sair")

    assert chat.MENSAGEM_LONGA_DEMAIS in capsys.readouterr().out
    assert modelo.recebidas == []


def test_falha_do_gemini_mostra_a_mensagem_de_erro_e_desfaz_o_turno(tmp_path, capsys):
    modelo = ModeloRoteirizado(roteiro=[RuntimeError("cota esgotada")])

    _conversar(tmp_path, modelo, "Oi", "/sair")

    saida = capsys.readouterr().out
    assert f"Lia: {MENSAGEM_DE_ERRO}" in saida
    assert _mensagens_gravadas(tmp_path, _id_para_continuar(saida)) == []


def test_conversa_que_nao_existe_comeca_outra(tmp_path, capsys):
    id_que_nao_existe = str(uuid4())

    _conversar(tmp_path, ModeloRoteirizado(roteiro=[]), "/sair", argumentos=["--conversa", id_que_nao_existe])

    saida = capsys.readouterr().out
    assert "Não encontrei essa conversa" in saida
    assert _id_para_continuar(saida) != id_que_nao_existe


def test_sem_chave_explica_o_que_fazer(monkeypatch, capsys):
    monkeypatch.setattr(chat, "load_dotenv", lambda: None)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    assert chat.main([]) == 1
    assert "GOOGLE_API_KEY" in capsys.readouterr().out


def test_relogio_em_formato_errado_e_recusado(monkeypatch):
    # O --agora grava a variável no ambiente; o setenv antes garante que o pytest a restaure no fim.
    monkeypatch.setenv(VARIAVEL_DO_RELOGIO, "2026-11-03T10:30")

    with pytest.raises(SystemExit):
        chat._ler_argumentos(["--agora", "08/11/2026"])
