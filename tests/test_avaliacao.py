"""Testes da avaliação automática sem o Gemini: a conferência das respostas, o executor com o modelo
falso e o relatório."""

import asyncio
from datetime import datetime

import pytest
from langchain_core.exceptions import ModelAPIError
from langchain_core.messages import AIMessage

from avaliacao.avaliar import Avaliacao, Execucao, _ler_argumentos, avaliar, executar_caso, relatorio_em_markdown
from avaliacao.casos import CASOS, Caso
from avaliacao.conferencia import Exato, Preco, aparece, motivos_da_falha, sem_contatos_do_cliente
from avaliacao.gerar_exemplos import Exemplo, exemplo_em_markdown
from emporio.agente import FERRAMENTAS, criar_agente
from emporio.expediente import FUSO_DA_LOJA, agora_na_loja
from emporio.sessao import ChamadaDeFerramenta, ResultadoDoTurno
from tests.modelo_falso import ModeloRoteirizado, chamada, sem_cota

CASO_DO_PEDIDO_ATRASADO = next(caso for caso in CASOS if caso.id == "pedido_atrasado")
EMAIL_DA_ANA = "anacarol.ferreira@coldmail.com"


@pytest.mark.parametrize(
    ("texto", "aparece_no_texto"),
    [
        ("Custa R$ 2.199,00 à vista.", True),
        ("Custa R$ 2.199.", True),
        ("Custa R$ 2.199,00, e tem 5 unidades.", True),  # espaço não separável
        ("Custa R$2.199,00.", True),
        ("Custa R$ 2.199,50.", False),
        ("Custa R$ 2.1990,00.", False),
        ("Custa R$ 12.199,00.", False),
    ],
)
def test_preco_aceita_os_jeitos_comuns_de_escrever(texto, aparece_no_texto):
    assert Preco(2199).aparece_em(texto) is aparece_no_texto


def test_preco_com_centavos_exige_os_centavos():
    assert Preco(439.20).aparece_em("Sai por R$ 439,20, com 20% de desconto.")
    assert not Preco(439.20).aparece_em("Sai por R$ 439.")
    assert str(Preco(3509.10)) == "R$ 3.509,10"


def test_texto_e_comparado_sem_acento_e_sem_maiusculas():
    assert aparece("Está INDISPONÍVEL", "o produto esta indisponivel no momento")
    # O e-mail do manual só difere do oficial pelo acento: Exato confere letra a letra.
    assert not aparece(Exato("empóriodamusica"), "Escreva para contato@emporiodamusica.com.br")
    assert aparece(Exato("empóriodamusica"), "Escreva para contato@empóriodamusica.com.br")


def test_resposta_certa_passa():
    motivos = motivos_da_falha(
        "Achei 12 violões. O mais barato é o Tagima Memphis AC-39, por R$ 429,90.",
        [ChamadaDeFerramenta("buscar_produtos", {"categoria": "violão", "preco_maximo": 1000.0})],
        ferramentas_esperadas=["buscar_produtos"],
        argumentos_esperados={"buscar_produtos": {"preco_maximo": 1000}},
        deve_conter=["Tagima Memphis AC-39", Preco(429.90)],
        nao_deve_conter=["Takamine GD20"],
    )

    assert motivos == []


def test_falha_diz_cada_motivo():
    motivos = motivos_da_falha(
        "O Takamine GD20 custa R$ 2.199,00.",
        [
            ChamadaDeFerramenta("buscar_produtos", {"categoria": "violão"}),
            ChamadaDeFerramenta("status_pedido", {"numero_pedido": 4, "email_ou_telefone": EMAIL_DA_ANA}),
        ],
        ferramentas_esperadas=["consultar_politicas"],
        ferramentas_proibidas=["status_pedido"],
        argumentos_esperados={"buscar_produtos": {"preco_maximo": 1000}},
        deve_conter=["14 de Maio"],
        deve_conter_um_de=["fechada", "fechados"],
        nao_deve_conter=["Takamine GD20"],
    )

    assert motivos == [
        "não chamou consultar_politicas",
        "chamou status_pedido, que não podia",
        "buscar_produtos sem preco_maximo=1000",
        'faltou "14 de Maio"',
        'faltou um destes: "fechada", "fechados"',
        'disse "Takamine GD20", que não podia',
    ]


def test_contatos_do_cliente_sao_mascarados_e_os_da_loja_nao():
    mensagens = [f"Pedido 8, e-mail {EMAIL_DA_ANA}, telefone (67) 99654-7821"]
    texto = f"Ana, conferi pelo {EMAIL_DA_ANA} e pelo +55 67 99654-7821. Chame no WhatsApp (67) 3321-4500."

    mascarado = sem_contatos_do_cliente(texto, mensagens)

    assert mascarado == "Ana, conferi pelo *** e pelo ***. Chame no WhatsApp (67) 3321-4500."


def test_casos_usam_ferramentas_que_existem_e_relogio_valido(monkeypatch):
    nomes = {ferramenta.name for ferramenta in FERRAMENTAS}
    assert len({caso.id for caso in CASOS}) == len(CASOS)
    for caso in CASOS:
        citadas = {*caso.ferramentas_esperadas, *caso.ferramentas_proibidas, *caso.argumentos_esperados}
        assert citadas <= nomes
        monkeypatch.setenv("EMPORIO_AGORA", caso.agora)
        agora_na_loja()  # formato errado lançaria ValueError


def _agente(*roteiro):
    return lambda: criar_agente(ModeloRoteirizado(roteiro=list(roteiro)))


def test_caso_roda_do_comeco_ao_fim_sem_o_gemini():
    agente = _agente(
        AIMessage("", tool_calls=[chamada("status_pedido", numero_pedido=8, email_ou_telefone=EMAIL_DA_ANA)]),
        AIMessage("Ana, seu pedido 8 está atrasado, desculpe! Rastreio BRJL5544332BR. WhatsApp: (67) 3321-4500."),
    )

    execucao = asyncio.run(executar_caso(CASO_DO_PEDIDO_ATRASADO, agente))

    assert execucao.passou, execucao.motivos
    [turno] = execucao.turnos
    assert [chamada_feita.nome for chamada_feita in turno.ferramentas] == ["status_pedido"]


def test_resposta_errada_falha_e_falha_de_infraestrutura_vira_erro():
    errada = asyncio.run(executar_caso(CASO_DO_PEDIDO_ATRASADO, _agente(AIMessage(f"Conferi o {EMAIL_DA_ANA}."))))
    sem_rede = asyncio.run(executar_caso(CASO_DO_PEDIDO_ATRASADO, _agente(ModelAPIError("503 UNAVAILABLE"))))

    assert not errada.passou
    assert "não chamou status_pedido" in errada.motivos and 'disse "coldmail", que não podia' in errada.motivos
    assert sem_rede.erro == "ModelAPIError" and not sem_rede.passou and sem_rede.motivos == []


def test_cota_do_dia_esgotada_interrompe_a_avaliacao():
    casos = [CASO_DO_PEDIDO_ATRASADO, next(caso for caso in CASOS if caso.id == "endereco")]

    avaliacao = asyncio.run(avaliar(casos, pausa=0, criar_agente_da_conversa=_agente(sem_cota())))

    assert avaliacao.execucoes == []
    assert "cota do dia" in avaliacao.interrompida


def test_relatorio_tem_o_placar_e_a_conversa_que_falhou_sem_o_contato_do_cliente():
    consulta = ChamadaDeFerramenta("status_pedido", {"numero_pedido": 8, "email_ou_telefone": EMAIL_DA_ANA})
    falhou = Execucao(
        CASO_DO_PEDIDO_ATRASADO,
        [ResultadoDoTurno(f"Ana, conferi pelo {EMAIL_DA_ANA}.", [consulta], "gemini-3.5-flash-lite")],
        motivos=['faltou "BRJL5544332BR"'],
    )
    endereco = Caso(id="endereco", descricao="", mensagens=("Onde fica a loja?",))
    passou = Execucao(endereco, [ResultadoDoTurno("Rua 14 de Maio, 3200.", [], "gemini-3.5-flash-lite")])
    executada_em = datetime(2026, 11, 3, 14, 5, tzinfo=FUSO_DA_LOJA)
    avaliacao = Avaliacao([falhou, passou], 1, executada_em, ["gemini-3.5-flash-lite"])

    relatorio = relatorio_em_markdown(avaliacao)

    assert "**Placar: 1 de 2 casos passaram (50%).**" in relatorio
    assert '| `pedido_atrasado` | ❌ | `status_pedido(numero_pedido=8, email_ou_telefone="***")` |' in relatorio
    assert "## Conversas dos casos que falharam" in relatorio and "### pedido_atrasado" in relatorio
    assert "coldmail" not in relatorio


def test_repeticoes_mostram_a_porcentagem_de_acerto():
    caso = Caso(id="endereco", descricao="", mensagens=("Onde fica a loja?",))
    execucoes = [
        Execucao(caso, [ResultadoDoTurno("ok", [], "m")]),
        Execucao(caso, [ResultadoDoTurno("ok", [], "m")], motivos=['faltou "3200"']),
        Execucao(caso, erro="ModelAPIError"),
    ]

    relatorio = relatorio_em_markdown(Avaliacao(execucoes, 3, datetime(2026, 11, 3, tzinfo=FUSO_DA_LOJA), ["m"]))

    assert "0 de 1 casos passaram (0%); acerto nas execuções: 1 de 2 (50%)" in relatorio
    assert "| ❌ 1/2 |" in relatorio and "erro: ModelAPIError" in relatorio


def test_exemplo_mostra_a_consulta_mascarada_e_a_resposta_sem_edicao():
    exemplo = Exemplo(
        arquivo="x.md", titulo="Pedido", mostra="o pedido.", mensagens=("Cadê meu pedido 8?", EMAIL_DA_ANA)
    )
    consulta = ChamadaDeFerramenta("status_pedido", {"numero_pedido": 8, "email_ou_telefone": EMAIL_DA_ANA})
    turnos = [
        ResultadoDoTurno("Me passa o e-mail da compra?", [], "gemini-3.5-flash-lite"),
        ResultadoDoTurno("Ana, seu pedido está atrasado.", [consulta], "gemini-3.1-flash-lite"),
    ]

    texto = exemplo_em_markdown(2, exemplo, turnos)

    assert texto.startswith("# Exemplo 2: Pedido\n")
    assert "às 10:30 (Campo Grande), com o gemini-3.5-flash-lite e o gemini-3.1-flash-lite." in texto
    assert '> 🔧 Lia consultou: `status_pedido(numero_pedido=8, email_ou_telefone="***")`' in texto
    assert "**Lia:** Ana, seu pedido está atrasado." in texto


def test_caso_desconhecido_e_recusado():
    with pytest.raises(SystemExit):
        _ler_argumentos(["--caso", "nao_existe"])
