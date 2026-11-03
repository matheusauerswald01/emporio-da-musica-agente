"""Gera os exemplos de conversa (exemplos/*.md) com o Gemini de verdade.

Uso, na raiz do projeto:  python -m avaliacao.gerar_exemplos [--exemplo N] [--pausa S]

As respostas da Lia não são editadas. Cada exemplo fixa a data e a hora da conversa, para a história
bater com os dados da planilha (prazos, loja aberta ou fechada). As chamadas de ferramenta aparecem
com o contato mascarado (LGPD); as mensagens do cliente usam os dados fictícios da planilha.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from avaliacao.avaliar import CriarAgente, conversar
from avaliacao.casos import AGORA_PADRAO
from emporio.agente import criar_agente
from emporio.sessao import ResultadoDoTurno, usar_utf8_no_terminal

PASTA_DOS_EXEMPLOS = Path(__file__).resolve().parent.parent / "exemplos"
PAUSA_PADRAO_EM_SEGUNDOS = 6.0


@dataclass(frozen=True)
class Exemplo:
    arquivo: str
    titulo: str
    # O que o avaliador deve reparar na conversa.
    mostra: str
    mensagens: tuple[str, ...]
    agora: str = AGORA_PADRAO
    # Explicação extra para o cabeçalho, quando o relógio fixado precisa de justificativa.
    observacao: str = ""


EXEMPLOS = (
    Exemplo(
        arquivo="01-catalogo-violoes-ate-1000.md",
        titulo="Violões até R$ 1.000 (consulta ao catálogo)",
        mostra="a Lia decide consultar o catálogo com o filtro de preço (que já usa o preço com promoção, "
        "quando há). Na segunda pergunta, ela lembra da conversa: o cliente não repete que quer um violão até "
        "R$ 1.000.",
        mensagens=(
            "Quais opções de violões disponíveis custando até R$1000?",
            "E qual o mais barato de cordas de aço?",
        ),
    ),
    Exemplo(
        arquivo="02-endereco-e-horario.md",
        titulo="Endereço e horário com a loja fechada (consulta às políticas)",
        mostra="informações gerais da loja vêm do manual de políticas, e não do catálogo. Com a loja fechada "
        "(domingo à noite), a Lia diz quando ela volta a abrir.",
        mensagens=("Qual o endereço da loja?", "Vocês abrem amanhã? Que horas?"),
        agora="2026-11-08T20:00",
    ),
    Exemplo(
        arquivo="03-preco-e-promocao.md",
        titulo="Preço, promoções e uma promoção que já acabou",
        mostra="consulta de preço de um modelo específico, as promoções ativas com o preço original, o "
        "desconto e o preço final, e a resposta honesta para uma promoção que não está mais valendo.",
        mensagens=(
            "Quanto custa o Takamine GD20?",
            "Tem algum violão em promoção?",
            "Vi o Crafter CE-15 na Black Friday com 10% de desconto, ainda vale?",
        ),
    ),
    Exemplo(
        arquivo="04-devolucao-do-pedido.md",
        titulo="Devolução de um pedido (dados do pedido + regra do manual)",
        mostra="o caso não trivial: a Lia busca a regra do arrependimento no manual, confere no pedido que o "
        "cliente é o dono (pelo telefone da compra, LGPD) e aplica os 7 dias à data em que ele recebeu o produto.",
        mensagens=(
            "Me arrependi da minha compra, posso devolver meu pedido?",
            "É o pedido 4, telefone (67) 99812-3456",
            "Chegou dia 22, ainda tá na caixa original",
        ),
        agora="2025-12-26T10:30",
        observacao="O relógio foi fixado em 26/12/2025 (sexta-feira), poucos dias depois da entrega do pedido 4 "
        "(previsão de entrega em 22/12/2025, na planilha), para a história bater com as datas dos dados.",
    ),
    Exemplo(
        arquivo="05-pedido-atrasado-e-fora-do-escopo.md",
        titulo="Pedido atrasado, acessório e assunto fora da loja",
        mostra="a confirmação do dono do pedido antes de mostrar qualquer dado, o pedido de desculpas pelo "
        "atraso com o caminho para o WhatsApp, a recusa educada de um acessório (a loja só vende "
        "instrumentos) e a volta para o assunto da loja numa pergunta fora do escopo.",
        mensagens=(
            "Cadê meu pedido 8?",
            "anacarol.ferreira@coldmail.com",
            "Aproveitando, vocês vendem cabo pra guitarra?",
            "E quem ganhou o jogo ontem?",
        ),
    ),
)


def exemplo_em_markdown(numero: int, exemplo: Exemplo, turnos: Sequence[ResultadoDoTurno]) -> str:
    """A conversa no formato dos exemplos: cliente, ferramentas consultadas e a resposta da Lia, sem edição."""
    agora = datetime.fromisoformat(exemplo.agora)
    modelos = " e o ".join(dict.fromkeys(turno.modelo or "modelo desconhecido" for turno in turnos))
    linhas = [
        f"# Exemplo {numero}: {exemplo.titulo}",
        "",
        f"> Gerado automaticamente com o horário fixado em {agora:%d/%m/%Y} às {agora:%H:%M} (Campo Grande), "
        f"com o {modelos}. As respostas não foram editadas.",
    ]
    if exemplo.observacao:
        linhas += [">", f"> {exemplo.observacao}"]
    linhas += ["", f"**O que este exemplo mostra:** {exemplo.mostra}", ""]
    for mensagem, turno in zip(exemplo.mensagens, turnos, strict=True):
        linhas += [f"**Cliente:** {mensagem}", ""]
        if turno.ferramentas:
            linhas += [f"> 🔧 Lia consultou: `{chamada.descrever()}`" for chamada in turno.ferramentas] + [""]
        linhas += [f"**Lia:** {turno.texto}", ""]
    return "\n".join(linhas)


async def gerar(
    numeros: Sequence[int], pausa: float = PAUSA_PADRAO_EM_SEGUNDOS, criar_agente_da_conversa: CriarAgente = criar_agente
) -> list[Path]:
    """Roda as conversas pedidas (numeradas a partir de 1) e grava um arquivo por conversa."""
    PASTA_DOS_EXEMPLOS.mkdir(exist_ok=True)
    gravados = []
    for ordem, numero in enumerate(numeros):
        if ordem:
            await asyncio.sleep(pausa)
        exemplo = EXEMPLOS[numero - 1]
        turnos = await conversar(exemplo.mensagens, exemplo.agora, criar_agente_da_conversa)
        arquivo = PASTA_DOS_EXEMPLOS / exemplo.arquivo
        arquivo.write_text(exemplo_em_markdown(numero, exemplo, turnos), encoding="utf-8")
        gravados.append(arquivo)
    return gravados


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m avaliacao.gerar_exemplos", description="Gera as conversas de exemplo com o Gemini."
    )
    parser.add_argument(
        "--exemplo", type=int, action="append", choices=range(1, len(EXEMPLOS) + 1), metavar="N",
        help=f"gera só este exemplo, de 1 a {len(EXEMPLOS)} (pode repetir)",
    )
    parser.add_argument("--pausa", type=float, default=PAUSA_PADRAO_EM_SEGUNDOS, metavar="S", help="segundos entre os exemplos")
    argumentos = parser.parse_args(argv)
    usar_utf8_no_terminal()
    load_dotenv()
    if not os.getenv("GOOGLE_API_KEY"):
        print("Defina GOOGLE_API_KEY no arquivo .env (veja o .env.example) para gerar os exemplos com o Gemini.")
        return 1
    numeros = argumentos.exemplo or range(1, len(EXEMPLOS) + 1)
    for arquivo in asyncio.run(gerar(numeros, argumentos.pausa)):
        print(f"Gravado: {arquivo}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
