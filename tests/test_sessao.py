"""Testes do turno fora da API (chat no terminal, avaliação e exemplos) com o modelo falso."""

import asyncio
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from emporio.agente import criar_agente
from emporio.conversas import MemoriaDasConversas
from emporio.sessao import CONSULTAS, ChamadaDeFerramenta, rodar_turno
from tests.modelo_falso import ModeloRoteirizado, chamada

TELEFONE_DO_DONO_DO_PEDIDO_8 = "+55 67 99654-7821"
CHAT_DA_WEB = Path(__file__).resolve().parent.parent / "web" / "components" / "chat.tsx"


def _conversas(*roteiro) -> MemoriaDasConversas:
    return MemoriaDasConversas(criar_agente(ModeloRoteirizado(roteiro=list(roteiro))))


def test_turno_devolve_o_texto_as_ferramentas_e_o_modelo():
    consulta_do_pedido = chamada("status_pedido", numero_pedido=8, email_ou_telefone=TELEFONE_DO_DONO_DO_PEDIDO_8)
    conversas = _conversas(
        AIMessage("", tool_calls=[consulta_do_pedido]),
        AIMessage("Ana, seu pedido está atrasado. Desculpe!", response_metadata={"model_name": "gemini-3.5-flash-lite"}),
    )
    escrito, consultas = [], []

    resultado = asyncio.run(
        rodar_turno(
            conversas, "conversa", "Cadê meu pedido 8?", ao_escrever=escrito.append, ao_consultar=consultas.append
        )
    )

    assert resultado.texto == "Ana, seu pedido está atrasado. Desculpe!"
    assert "".join(escrito) == resultado.texto
    assert consultas == ["status_pedido"]
    assert resultado.modelo == "gemini-3.5-flash-lite"
    [consulta] = resultado.ferramentas
    assert consulta.argumentos == {"numero_pedido": 8, "email_ou_telefone": TELEFONE_DO_DONO_DO_PEDIDO_8}
    # LGPD: a descrição para mostrar ou gravar nunca leva o contato.
    assert consulta.descrever() == 'status_pedido(numero_pedido=8, email_ou_telefone="***")'


def test_turno_traz_so_as_ferramentas_da_ultima_pergunta():
    conversas = _conversas(
        AIMessage("", tool_calls=[chamada("promocoes_ativas")]),
        AIMessage("Temos 4 promoções!"),
        AIMessage("De nada!"),
    )
    asyncio.run(rodar_turno(conversas, "conversa", "Tem promoção?"))

    resultado = asyncio.run(rodar_turno(conversas, "conversa", "Valeu!"))

    assert resultado.texto == "De nada!"
    assert resultado.ferramentas == []


def test_turno_que_falha_e_desfeito_e_libera_a_conversa():
    conversas = _conversas(RuntimeError("cota esgotada"), AIMessage("Oi!"))

    with pytest.raises(RuntimeError):
        asyncio.run(rodar_turno(conversas, "conversa", "Oi"))

    assert asyncio.run(conversas.mensagens("conversa")) == []
    assert asyncio.run(rodar_turno(conversas, "conversa", "Oi")).texto == "Oi!"


@pytest.mark.parametrize(
    ("chamada_feita", "descricao"),
    [
        (
            ChamadaDeFerramenta("buscar_produtos", {"categoria": "violão", "preco_maximo": 1000.0}),
            'buscar_produtos(categoria="violão", preco_maximo=1000)',
        ),
        (ChamadaDeFerramenta("promocoes_ativas", {}), "promocoes_ativas()"),
        # O contato no argumento errado também é mascarado.
        (ChamadaDeFerramenta("buscar_produtos", {"nome": "anacarol.ferreira@coldmail.com"}), 'buscar_produtos(nome="***")'),
        (ChamadaDeFerramenta("buscar_produtos", {"nome": "(67) 99654-7821"}), 'buscar_produtos(nome="***")'),
    ],
)
def test_descricao_da_chamada(chamada_feita, descricao):
    assert chamada_feita.descrever() == descricao


def test_textos_das_consultas_sao_os_mesmos_da_web():
    chat = CHAT_DA_WEB.read_text(encoding="utf-8")

    for ferramenta, texto in CONSULTAS.items():
        assert f'{ferramenta}: "{texto}"' in chat
