"""Testes do agente com um modelo falso: ferramentas, memória, limites e persona, sem o Gemini."""

import asyncio
from datetime import datetime

import httpx
import pytest
from langchain_core.exceptions import ModelAPIError, ModelAuthenticationError
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from emporio import politicas
from emporio.agente import (
    LIMITE_DE_CONSULTAS_POR_RESPOSTA,
    LIMITE_DE_MENSAGENS_NO_CONTEXTO,
    MENSAGEM_FALHA_NA_FERRAMENTA,
    criar_agente,
)
from emporio.expediente import FUSO_DA_LOJA
from emporio.modelos import ChaveDoGeminiRecusada
from emporio.ferramentas import MENSAGEM_PEDIDO_NAO_CONFERE
from emporio.persona import EMAIL, WHATSAPP, instrucoes_do_atendente
from emporio.tentativas import LIMITE_DE_TENTATIVAS, MENSAGEM_LIMITE_ATINGIDO
from tests.modelo_falso import ModeloRoteirizado, chamada

TELEFONE_DO_DONO_DO_PEDIDO_8 = "+55 67 99654-7821"


def _conversar(agente, id_da_conversa: str, texto: str) -> list:
    estado = asyncio.run(
        agente.ainvoke({"messages": [HumanMessage(texto)]}, {"configurable": {"thread_id": id_da_conversa}})
    )
    return estado["messages"]


def _consultar_pedido(agente, modelo, id_da_conversa: str, numero: int, contato: str) -> str:
    """Resultado do status_pedido numa nova pergunta da conversa."""
    modelo.roteiro += [
        AIMessage("", tool_calls=[chamada("status_pedido", numero_pedido=numero, email_ou_telefone=contato)]),
        AIMessage("Pronto."),
    ]
    return _conversar(agente, id_da_conversa, f"Pedido {numero}, contato {contato}")[-2].text


def test_agente_consulta_a_ferramenta_e_lembra_so_da_propria_conversa():
    modelo = ModeloRoteirizado(roteiro=[
        AIMessage("", tool_calls=[chamada("detalhes_produto", nome="Takamine GD20")]),
        AIMessage("O Takamine GD20 sai por R$ 2.199,00."),
        AIMessage("De nada!"),
        AIMessage("Oi! Como posso ajudar?"),
    ])
    agente = criar_agente(modelo)

    mensagens = _conversar(agente, "conversa-1", "Quanto custa o Takamine GD20?")
    resultado = next(mensagem for mensagem in mensagens if isinstance(mensagem, ToolMessage))
    assert "Takamine GD20 Dreadnought Natural" in resultado.text and "2199" in resultado.text
    assert mensagens[-1].text == "O Takamine GD20 sai por R$ 2.199,00."

    # Na mesma conversa, o modelo recebe as instruções e todo o histórico.
    _conversar(agente, "conversa-1", "Valeu!")
    instrucoes, *historico = modelo.recebidas[-1]
    assert isinstance(instrucoes, SystemMessage)
    assert historico[0].text == "Quanto custa o Takamine GD20?"

    # Outra conversa começa do zero: nada da primeira vaza para ela.
    _conversar(agente, "conversa-2", "Oi")
    assert [mensagem.text for mensagem in modelo.recebidas[-1][1:]] == ["Oi"]


def test_tentativas_erradas_bloqueiam_o_pedido_mesmo_em_outra_conversa():
    modelo = ModeloRoteirizado(roteiro=[])
    agente = criar_agente(modelo)

    for tentativa in range(LIMITE_DE_TENTATIVAS):
        resultado = _consultar_pedido(agente, modelo, f"conversa-{tentativa}", 8, f"pessoa{tentativa}@x.com")
        assert MENSAGEM_PEDIDO_NAO_CONFERE in resultado

    # Começar outra conversa não zera a contagem, e nem o contato certo libera o pedido agora.
    resultado = _consultar_pedido(agente, modelo, "conversa-nova", 8, TELEFONE_DO_DONO_DO_PEDIDO_8)
    assert MENSAGEM_LIMITE_ATINGIDO in resultado
    assert "BRJL5544332BR" not in resultado


def test_tentativas_erradas_bloqueiam_o_contato_em_qualquer_pedido():
    modelo = ModeloRoteirizado(roteiro=[])
    agente = criar_agente(modelo)

    # Quem sabe o telefone de alguém não consegue testar os números de pedido um a um.
    for numero in range(1, LIMITE_DE_TENTATIVAS + 1):
        assert MENSAGEM_PEDIDO_NAO_CONFERE in _consultar_pedido(
            agente, modelo, f"conversa-{numero}", numero, TELEFONE_DO_DONO_DO_PEDIDO_8
        )
    assert MENSAGEM_LIMITE_ATINGIDO in _consultar_pedido(agente, modelo, "conversa-8", 8, TELEFONE_DO_DONO_DO_PEDIDO_8)


def test_numero_do_pedido_escrito_de_outro_jeito_conta_para_o_mesmo_limite():
    modelo = ModeloRoteirizado(roteiro=[])
    agente = criar_agente(modelo)

    for tentativa, numero in enumerate(["08", 8.0, " 8"]):
        assert MENSAGEM_PEDIDO_NAO_CONFERE in _consultar_pedido(agente, modelo, "c", numero, f"p{tentativa}@x.com")
    assert MENSAGEM_LIMITE_ATINGIDO in _consultar_pedido(agente, modelo, "c", 8, "outra@x.com")


def test_argumento_invalido_volta_para_o_modelo_corrigir():
    modelo = ModeloRoteirizado(roteiro=[
        AIMessage("", tool_calls=[chamada("status_pedido", numero_pedido="oito", email_ou_telefone="x@y.com")]),
        AIMessage("Qual é o número do pedido?"),
    ])

    resultado = _conversar(criar_agente(modelo), "c", "Meu pedido oito")[-2].text

    assert resultado != MENSAGEM_FALHA_NA_FERRAMENTA and "numero_pedido" in resultado


def test_dono_do_pedido_consulta_quantas_vezes_quiser():
    modelo = ModeloRoteirizado(roteiro=[])
    agente = criar_agente(modelo)

    for _ in range(LIMITE_DE_TENTATIVAS + 1):
        assert "BRJL5544332BR" in _consultar_pedido(agente, modelo, "dono", 8, TELEFONE_DO_DONO_DO_PEDIDO_8)


def test_chamadas_em_paralelo_contam_para_o_limite():
    chamadas = [
        chamada("status_pedido", f"paralela-{numero}", numero_pedido=numero, email_ou_telefone="x@y.com")
        for numero in range(1, LIMITE_DE_TENTATIVAS + 2)
    ]
    agente = criar_agente(ModeloRoteirizado(roteiro=[AIMessage("", tool_calls=chamadas), AIMessage("Pronto.")]))

    mensagens = _conversar(agente, "paralelo", "Consulta os pedidos de 1 a 4")

    resultados = [mensagem.text for mensagem in mensagens if isinstance(mensagem, ToolMessage)]
    assert MENSAGEM_LIMITE_ATINGIDO not in " ".join(resultados[:LIMITE_DE_TENTATIVAS])
    assert MENSAGEM_LIMITE_ATINGIDO in resultados[-1]


def test_modelo_recebe_so_as_ultimas_mensagens_comecando_numa_pergunta():
    modelo = ModeloRoteirizado(roteiro=[])
    agente = criar_agente(modelo)

    for numero in range(LIMITE_DE_MENSAGENS_NO_CONTEXTO):
        modelo.roteiro.append(AIMessage(f"Resposta {numero}"))
        _conversar(agente, "longa", f"Pergunta {numero}")

    contexto = modelo.recebidas[-1][1:]  # sem as instruções
    assert len(contexto) <= LIMITE_DE_MENSAGENS_NO_CONTEXTO
    assert isinstance(contexto[0], HumanMessage)
    assert contexto[-1].text == f"Pergunta {LIMITE_DE_MENSAGENS_NO_CONTEXTO - 1}"


def test_turno_atual_vai_inteiro_mesmo_maior_que_o_limite():
    # Uma pergunta, uma resposta com muitas consultas e os resultados delas passam do limite.
    chamadas = [chamada("promocoes_ativas", f"consulta-{numero}") for numero in range(LIMITE_DE_MENSAGENS_NO_CONTEXTO)]
    modelo = ModeloRoteirizado(roteiro=[AIMessage("", tool_calls=chamadas), AIMessage("Pronto.")])

    _conversar(criar_agente(modelo), "consultas", "Quais as promoções?")

    contexto = modelo.recebidas[-1][1:]
    assert len(contexto) == LIMITE_DE_MENSAGENS_NO_CONTEXTO + 2
    assert contexto[0].text == "Quais as promoções?"


def test_depois_de_muitas_consultas_o_modelo_tem_que_responder_em_texto():
    consultas = [
        AIMessage("", tool_calls=[chamada("promocoes_ativas", f"consulta-{numero}")])
        for numero in range(LIMITE_DE_CONSULTAS_POR_RESPOSTA)
    ]
    modelo = ModeloRoteirizado(roteiro=[*consultas, AIMessage("Essas são as promoções.")])

    mensagens = _conversar(criar_agente(modelo), "insistente", "Tem promoção?")

    assert modelo.modos == ["ferramentas"] * LIMITE_DE_CONSULTAS_POR_RESPOSTA + ["so_texto"]
    assert mensagens[-1].text == "Essas são as promoções."


def test_reserva_responde_quando_o_modelo_principal_esta_indisponivel():
    principal = ModeloRoteirizado(roteiro=[ModelAPIError("503 UNAVAILABLE")])
    reserva = ModeloRoteirizado(roteiro=[
        AIMessage("", tool_calls=[chamada("promocoes_ativas")]),
        AIMessage("Temos 4 promoções ativas!"),
    ])

    mensagens = _conversar(criar_agente(principal, reservas=[reserva]), "sobrecarga", "Tem promoção?")

    assert mensagens[-1].text == "Temos 4 promoções ativas!"
    # A reserva recebe o mesmo pedido que o modelo principal: instruções e histórico.
    assert reserva.recebidas[0] == principal.recebidas[0]
    # Depois da falha, o principal fica fora da cadeia: a resposta depois da consulta já vai direto à reserva.
    assert len(principal.recebidas) == 1


def test_reserva_tambem_tem_que_responder_em_texto_depois_de_muitas_consultas():
    consultas = [
        AIMessage("", tool_calls=[chamada("promocoes_ativas", f"consulta-{numero}")])
        for numero in range(LIMITE_DE_CONSULTAS_POR_RESPOSTA)
    ]
    principal = ModeloRoteirizado(roteiro=[ModelAPIError("503")])
    reserva = ModeloRoteirizado(roteiro=[*consultas, AIMessage("Essas são as promoções.")])

    _conversar(criar_agente(principal, reservas=[reserva]), "insistente", "Tem promoção?")

    assert reserva.modos == ["ferramentas"] * LIMITE_DE_CONSULTAS_POR_RESPOSTA + ["so_texto"]


def test_reserva_responde_quando_a_conexao_com_o_principal_cai_e_a_troca_fica_no_log(caplog):
    # Tempo esgotado e rede chegam como exceções do httpx, e não como erros do langchain.
    principal = ModeloRoteirizado(roteiro=[httpx.ReadTimeout("tempo esgotado")])
    reserva = ModeloRoteirizado(roteiro=[AIMessage("Oi! Em que posso ajudar?")])

    mensagens = _conversar(criar_agente(principal, reservas=[reserva]), "sem-rede", "Oi")

    assert mensagens[-1].text == "Oi! Em que posso ajudar?"
    assert "ReadTimeout" in caplog.text and "tempo esgotado" not in caplog.text


def test_chave_errada_nao_passa_a_vez_para_a_reserva():
    principal = ModeloRoteirizado(roteiro=[ModelAuthenticationError("401 UNAUTHENTICATED")])
    reserva = ModeloRoteirizado(roteiro=[AIMessage("Oi!")])

    with pytest.raises(ChaveDoGeminiRecusada):
        _conversar(criar_agente(principal, reservas=[reserva]), "chave-errada", "Oi")

    assert reserva.recebidas == []


def test_falha_numa_ferramenta_vira_aviso_para_o_modelo(monkeypatch):
    def sem_rede():
        raise ConnectionError("sem rede")

    monkeypatch.setattr(politicas, "obter_indice", sem_rede)
    modelo = ModeloRoteirizado(roteiro=[
        AIMessage("", tool_calls=[chamada("consultar_politicas", assunto="trocas")]),
        AIMessage("Não consegui ver isso agora; chama a gente no WhatsApp."),
    ])

    mensagens = _conversar(criar_agente(modelo), "falha", "Posso trocar?")

    # A conversa segue com uma resposta, e o detalhe da exceção não chega ao modelo.
    assert mensagens[-2].text == MENSAGEM_FALHA_NA_FERRAMENTA
    assert mensagens[-1].text.startswith("Não consegui ver isso agora")


def test_instrucoes_trazem_a_data_a_situacao_da_loja_e_os_contatos_oficiais():
    instrucoes = instrucoes_do_atendente(datetime(2026, 11, 2, 14, 30, tzinfo=FUSO_DA_LOJA))

    assert "segunda-feira, 02/11/2026, 14:30" in instrucoes
    assert "fechada agora (feriado: Finados)" in instrucoes
    assert WHATSAPP in instrucoes and EMAIL in instrucoes
