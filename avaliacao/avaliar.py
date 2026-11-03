"""Avaliação automática do agente com o Gemini de verdade: roda os casos de avaliacao/casos.py,
confere cada resposta e grava o placar em avaliacao/resultados.md.

Uso, na raiz do projeto:  python -m avaliacao.avaliar [--caso ID] [--repeticoes N] [--pausa S]

Cada caso roda numa conversa nova, com um agente novo e a memória em RAM: o limite de tentativas do
status_pedido fica no agente, e um caso atrapalharia o outro; o conversas.db não é tocado. Falha de
infraestrutura (cota, rede) aparece como "erro" e não conta contra o agente. Como a resposta de um
LLM varia, --repeticoes roda cada caso mais vezes e mostra a porcentagem de acerto.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv
from langgraph.graph.state import CompiledStateGraph

from avaliacao.casos import CASOS, Caso
from avaliacao.conferencia import motivos_da_falha, sem_contatos_do_cliente
from emporio.agente import criar_agente
from emporio.conversas import MemoriaDasConversas
from emporio.expediente import FUSO_DA_LOJA, relogio_fixado
from emporio.modelos import ChaveDoGeminiRecusada, ModelosForaDoAr, nomes_dos_modelos
from emporio.sessao import ResultadoDoTurno, rodar_turno, usar_utf8_no_terminal

ARQUIVO_DE_RESULTADOS = Path(__file__).resolve().parent / "resultados.md"
# O plano grátis do Gemini limita as chamadas por minuto; um caso faz de 1 a 3 chamadas.
PAUSA_PADRAO_EM_SEGUNDOS = 6.0

CriarAgente = Callable[[], CompiledStateGraph]


class AvaliacaoInterrompida(Exception):
    """Falha que vale para todos os casos (cota do dia esgotada, chave recusada): não adianta seguir."""


@dataclass
class Execucao:
    caso: Caso
    turnos: list[ResultadoDoTurno] = field(default_factory=list)
    motivos: list[str] = field(default_factory=list)
    # Tipo da falha de infraestrutura (rede, limite por minuto), que não conta contra o agente.
    erro: str | None = None

    @property
    def passou(self) -> bool:
        return self.erro is None and not self.motivos


@dataclass
class Avaliacao:
    execucoes: list[Execucao]
    repeticoes: int
    executada_em: datetime
    modelos: list[str]
    # Por que a avaliação parou antes do fim, se parou.
    interrompida: str | None = None


async def conversar(
    mensagens: Sequence[str], agora: str, criar_agente_da_conversa: CriarAgente = criar_agente
) -> list[ResultadoDoTurno]:
    """Uma conversa nova, com o relógio fixado, mandando as mensagens do cliente em ordem."""
    with relogio_fixado(agora):
        conversas = MemoriaDasConversas(criar_agente_da_conversa())
        id_da_conversa = str(uuid4())
        return [await rodar_turno(conversas, id_da_conversa, mensagem) for mensagem in mensagens]


async def executar_caso(caso: Caso, criar_agente_da_conversa: CriarAgente = criar_agente) -> Execucao:
    try:
        turnos = await conversar(caso.mensagens, caso.agora, criar_agente_da_conversa)
    except ModelosForaDoAr as erro:
        raise AvaliacaoInterrompida("a cota do dia acabou em todos os modelos do Gemini") from erro
    except ChaveDoGeminiRecusada as erro:
        raise AvaliacaoInterrompida("o Gemini recusou a chave (GOOGLE_API_KEY)") from erro
    except Exception as erro:
        return Execucao(caso, erro=type(erro).__name__)
    motivos = motivos_da_falha(
        turnos[-1].texto,
        [chamada for turno in turnos for chamada in turno.ferramentas],
        ferramentas_esperadas=caso.ferramentas_esperadas,
        ferramentas_proibidas=caso.ferramentas_proibidas,
        argumentos_esperados=caso.argumentos_esperados,
        deve_conter=caso.deve_conter,
        deve_conter_um_de=caso.deve_conter_um_de,
        nao_deve_conter=caso.nao_deve_conter,
    )
    return Execucao(caso, turnos, motivos)


async def avaliar(
    casos: Sequence[Caso],
    repeticoes: int = 1,
    pausa: float = PAUSA_PADRAO_EM_SEGUNDOS,
    criar_agente_da_conversa: CriarAgente = criar_agente,
    ao_terminar: Callable[[Execucao], None] | None = None,
) -> Avaliacao:
    """Roda cada caso `repeticoes` vezes, com uma pausa entre as execuções."""
    avaliacao = Avaliacao([], repeticoes, datetime.now(FUSO_DA_LOJA), nomes_dos_modelos())
    rodadas = [caso for caso in casos for _ in range(repeticoes)]
    for numero, caso in enumerate(rodadas):
        if numero:
            await asyncio.sleep(pausa)
        try:
            execucao = await executar_caso(caso, criar_agente_da_conversa)
        except AvaliacaoInterrompida as motivo:
            avaliacao.interrompida = str(motivo)
            break
        avaliacao.execucoes.append(execucao)
        if ao_terminar:
            ao_terminar(execucao)
    return avaliacao


@dataclass(frozen=True)
class ResumoDoCaso:
    caso: Caso
    execucoes: list[Execucao]

    @property
    def validas(self) -> list[Execucao]:
        return [execucao for execucao in self.execucoes if execucao.erro is None]

    @property
    def acertos(self) -> int:
        return sum(execucao.passou for execucao in self.validas)

    @property
    def passou(self) -> bool:
        return bool(self.validas) and self.acertos == len(self.validas)

    @property
    def simbolo(self) -> str:
        if not self.validas:
            return "⚠️"
        return "✅" if self.passou else "❌"


def resumos_por_caso(execucoes: Sequence[Execucao]) -> list[ResumoDoCaso]:
    por_caso: dict[str, list[Execucao]] = {}
    for execucao in execucoes:
        por_caso.setdefault(execucao.caso.id, []).append(execucao)
    return [ResumoDoCaso(do_caso[0].caso, do_caso) for do_caso in por_caso.values()]


def placar(resumos: Sequence[ResumoDoCaso]) -> str:
    avaliados = [resumo for resumo in resumos if resumo.validas]
    aprovados = sum(resumo.passou for resumo in avaliados)
    texto = f"{aprovados} de {len(avaliados)} casos passaram"
    if avaliados:
        texto += f" ({aprovados / len(avaliados):.0%})"
    validas = [execucao for resumo in avaliados for execucao in resumo.validas]
    if len(validas) > len(avaliados):
        acertos = sum(execucao.passou for execucao in validas)
        texto += f"; acerto nas execuções: {acertos} de {len(validas)} ({acertos / len(validas):.0%})"
    return texto


def relatorio_em_markdown(avaliacao: Avaliacao) -> str:
    resumos = resumos_por_caso(avaliacao.execucoes)
    erros = sum(execucao.erro is not None for execucao in avaliacao.execucoes)
    linhas = [
        "# Avaliação automática da Lia",
        "",
        f"- Executada em {avaliacao.executada_em:%d/%m/%Y às %H:%M} (horário de Campo Grande). Cada caso fixa a "
        "própria data e hora da conversa (veja `avaliacao/casos.py`).",
        f"- Modelos, em ordem: {' → '.join(avaliacao.modelos)} (o primeiro responde; os outros são reservas).",
        f"- Repetições por caso: {avaliacao.repeticoes}.",
        "",
        f"**Placar: {placar(resumos)}.** Falhas de infraestrutura (cota, rede), que não contam: {erros}.",
        "",
    ]
    if avaliacao.interrompida:
        linhas += [f"> A avaliação parou antes do fim: {avaliacao.interrompida}.", ""]
    linhas += ["| Caso | Resultado | Ferramentas chamadas | Modelo | Motivo da falha |", "|---|---|---|---|---|"]
    linhas += [_linha_da_tabela(resumo) for resumo in resumos]
    falhas = [_primeira_falha(resumo) for resumo in resumos if resumo.validas and not resumo.passou]
    if falhas:
        linhas += ["", "## Conversas dos casos que falharam", ""]
        for execucao in falhas:
            linhas += _conversa_em_markdown(execucao)
    return "\n".join(linhas) + "\n"


def _linha_da_tabela(resumo: ResumoDoCaso) -> str:
    resultado = resumo.simbolo
    if len(resumo.execucoes) > 1:
        resultado += f" {resumo.acertos}/{len(resumo.validas)}"
    ultima = resumo.validas[-1] if resumo.validas else None
    ferramentas = "<br>".join(
        f"`{chamada.descrever()}`" for turno in (ultima.turnos if ultima else []) for chamada in turno.ferramentas
    )
    modelos = sorted({execucao.turnos[-1].modelo or "?" for execucao in resumo.validas})
    motivos = list(dict.fromkeys(motivo for execucao in resumo.validas for motivo in execucao.motivos))
    motivos += [f"erro: {execucao.erro}" for execucao in resumo.execucoes if execucao.erro]
    celulas = [f"`{resumo.caso.id}`", resultado, ferramentas or "nenhuma", ", ".join(modelos) or "—"]
    return "| " + " | ".join(_celula(celula) for celula in [*celulas, "; ".join(motivos)]) + " |"


def _primeira_falha(resumo: ResumoDoCaso) -> Execucao:
    return next(execucao for execucao in resumo.validas if not execucao.passou)


def _conversa_em_markdown(execucao: Execucao) -> list[str]:
    mensagens = execucao.caso.mensagens
    linhas = [f"### {execucao.caso.id}", "", f"Motivo: {'; '.join(execucao.motivos)}.", ""]
    for mensagem, turno in zip(mensagens, execucao.turnos, strict=True):
        linhas += [f"**Cliente:** {sem_contatos_do_cliente(mensagem, mensagens)}", ""]
        linhas += [f"> Lia consultou: `{chamada.descrever()}`" for chamada in turno.ferramentas]
        resposta = sem_contatos_do_cliente(turno.texto, mensagens)
        linhas += ["", f"**Lia** ({turno.modelo or 'modelo desconhecido'}):", "", resposta, ""]
    return linhas


def _celula(texto: str) -> str:
    return texto.replace("|", "\\|").replace("\n", " ")


def _mostrar_execucao(execucao: Execucao) -> None:
    situacao = "ERRO" if execucao.erro else ("passou" if execucao.passou else "FALHOU")
    detalhe = execucao.erro or "; ".join(execucao.motivos)
    print(f"{situacao:<7} {execucao.caso.id:<26} {detalhe}", flush=True)


def _ler_argumentos(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m avaliacao.avaliar", description="Avalia a Lia com o Gemini de verdade."
    )
    parser.add_argument("--caso", action="append", metavar="ID", help="roda só este caso (pode repetir)")
    parser.add_argument("--repeticoes", type=int, default=1, metavar="N", help="vezes que cada caso roda (padrão: 1)")
    parser.add_argument(
        "--pausa", type=float, default=PAUSA_PADRAO_EM_SEGUNDOS, metavar="S",
        help="segundos entre as execuções, por causa do limite de chamadas por minuto do Gemini "
        f"(padrão: {PAUSA_PADRAO_EM_SEGUNDOS:g})",
    )
    parser.add_argument(
        "--saida", type=Path, metavar="ARQUIVO",
        help="onde gravar o relatório (padrão: avaliacao/resultados.md, só quando roda todos os casos)",
    )
    argumentos = parser.parse_args(argv)
    if argumentos.repeticoes < 1:
        parser.error("--repeticoes precisa ser 1 ou mais")
    ids_conhecidos = {caso.id for caso in CASOS}
    desconhecidos = [id_do_caso for id_do_caso in argumentos.caso or [] if id_do_caso not in ids_conhecidos]
    if desconhecidos:
        casos = ", ".join(sorted(ids_conhecidos))
        parser.error(f"caso desconhecido: {', '.join(desconhecidos)}. Os casos são: {casos}")
    return argumentos


def main(argv: Sequence[str] | None = None) -> int:
    argumentos = _ler_argumentos(argv)
    usar_utf8_no_terminal()
    load_dotenv()
    if not os.getenv("GOOGLE_API_KEY"):
        print("Defina GOOGLE_API_KEY no arquivo .env (veja o .env.example) para avaliar com o Gemini.")
        return 1
    casos = [caso for caso in CASOS if not argumentos.caso or caso.id in argumentos.caso]
    avaliacao = asyncio.run(avaliar(casos, argumentos.repeticoes, argumentos.pausa, ao_terminar=_mostrar_execucao))

    print(f"\nPlacar: {placar(resumos_por_caso(avaliacao.execucoes))}.")
    if avaliacao.interrompida:
        print(f"A avaliação parou antes do fim: {avaliacao.interrompida}. Tente de novo mais tarde.")
    saida = argumentos.saida or (None if argumentos.caso else ARQUIVO_DE_RESULTADOS)
    if saida:
        saida.write_text(relatorio_em_markdown(avaliacao), encoding="utf-8")
        print(f"Relatório gravado em {saida}.")
    return 2 if avaliacao.interrompida else 0


if __name__ == "__main__":
    sys.exit(main())
