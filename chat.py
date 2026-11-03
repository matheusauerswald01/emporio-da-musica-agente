"""Chat com a Lia no terminal, só com Python: o jeito mais rápido de testar o agente.

Uso, na raiz do projeto:
  python chat.py                            conversa nova, salva no conversas.db
  python chat.py --conversa ID              continua uma conversa salva
  python chat.py --detalhes                 mostra as ferramentas chamadas e o modelo que respondeu
  python chat.py --agora 2026-11-08T15:00   fixa o relógio da loja (horário de Campo Grande)

A conversa segue as regras da API (api.py): o histórico fica no conversas.db pelo mesmo prazo, /apagar
apaga tudo na hora, e a resposta que falha é desfeita. A interface web continua sendo a outra opção.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from dotenv import load_dotenv
from langgraph.graph.state import CompiledStateGraph

from api import (
    COMANDO_APAGAR,
    MENSAGEM_DE_ERRO,
    MENSAGEM_FORA_DO_AR,
    MENSAGEM_HISTORICO_APAGADO,
    TAMANHO_MAXIMO_DA_MENSAGEM,
)
from emporio.agente import criar_agente
from emporio.conversas import MemoriaDasConversas
from emporio.expediente import VARIAVEL_DO_RELOGIO, agora_na_loja
from emporio.memoria import ARQUIVO_DAS_CONVERSAS, abrir_memoria
from emporio.modelos import ChaveDoGeminiRecusada, ModelosForaDoAr
from emporio.protocolo_ai_sdk import TEXTO_SEM_RESPOSTA, historico_para_a_interface
from emporio.sessao import rodar_turno, texto_da_consulta, usar_utf8_no_terminal

# A mesma apresentação da tela da web: a persona conta que ela já aparece, e não se apresenta de novo.
SAUDACAO = (
    "Oi! Eu sou a Lia, da Empório da Música. Posso ver preço e estoque dos instrumentos, acompanhar o seu "
    "pedido e tirar dúvidas sobre trocas, pagamento e entrega. Como posso ajudar?"
)
COMANDO_AJUDA = "/ajuda"
COMANDO_SAIR = "/sair"
AJUDA = (
    "Comandos:\n"
    f"  {COMANDO_AJUDA}   mostra esta lista\n"
    f"  {COMANDO_APAGAR}  apaga o histórico desta conversa e começa outra\n"
    f"  {COMANDO_SAIR}    sai (Ctrl+C também sai)"
)
MENSAGEM_LONGA_DEMAIS = f"Essa mensagem passou de {TAMANHO_MAXIMO_DA_MENSAGEM} caracteres. Pode resumir?"
MENSAGEM_SEM_CHAVE = (
    "Falta a chave do Gemini. Crie o arquivo .env na raiz do projeto (veja o .env.example) com a linha\n"
    "GOOGLE_API_KEY=sua-chave, que você gera em https://aistudio.google.com/apikey, e rode de novo."
)
# Mensagens do histórico mostradas ao continuar uma conversa.
MENSAGENS_DO_HISTORICO = 6

CriarAgente = Callable[..., CompiledStateGraph]
# Roda uma corrotina no loop do chat (o asyncio.Runner, que mantém aberta a conexão com o banco).
RodarNoLoop = Callable[[Awaitable[Any]], Any]


@dataclass
class Sessao:
    conversas: MemoriaDasConversas
    id_da_conversa: str
    detalhes: bool = False


class MostradorDaResposta:
    """Escreve a resposta da Lia chegando aos poucos, com as consultas em linhas à parte."""

    def __init__(self) -> None:
        self._no_meio_do_texto = False

    def escrever(self, pedaco: str) -> None:
        if not self._no_meio_do_texto:
            print("Lia: ", end="")
            self._no_meio_do_texto = True
        print(pedaco, end="", flush=True)

    def consultar(self, ferramenta: str) -> None:
        self.terminar()
        print(f"  ({texto_da_consulta(ferramenta)}…)", flush=True)

    def terminar(self) -> None:
        if self._no_meio_do_texto:
            print()
            self._no_meio_do_texto = False


async def responder(sessao: Sessao, texto: str) -> None:
    mostrador = MostradorDaResposta()
    try:
        resultado = await rodar_turno(
            sessao.conversas,
            sessao.id_da_conversa,
            texto,
            ao_escrever=mostrador.escrever,
            ao_consultar=mostrador.consultar,
        )
    except (ModelosForaDoAr, ChaveDoGeminiRecusada):
        mostrador.terminar()
        print(f"Lia: {MENSAGEM_FORA_DO_AR}")
        return
    except Exception as erro:
        mostrador.terminar()
        # Só o tipo do erro, como na API: a mensagem da exceção pode trazer dados do cliente.
        logging.getLogger(__name__).error("Falha ao gerar a resposta: %s", type(erro).__name__)
        print(f"Lia: {MENSAGEM_DE_ERRO}")
        return
    mostrador.terminar()
    if not resultado.texto:
        print(f"Lia: {TEXTO_SEM_RESPOSTA}")
    if sessao.detalhes:
        for chamada in resultado.ferramentas:
            print(f"  [ferramenta] {chamada.descrever()}")
        print(f"  [modelo] {resultado.modelo or 'desconhecido'}")


def conversar(sessao: Sessao, rodar_no_loop: RodarNoLoop, ler: Callable[[str], str] = input) -> None:
    """Lê as mensagens do cliente até /sair, Ctrl+Z ou Ctrl+D.

    A leitura fica fora do loop do asyncio: dentro dele, o Ctrl+C só seria atendido depois do Enter.
    """
    while True:
        try:
            texto = ler("\nVocê: ").strip()
        except EOFError:
            return
        if not texto:
            continue
        comando = texto.lower()
        if comando == COMANDO_SAIR:
            return
        if comando == COMANDO_AJUDA:
            print(AJUDA)
        elif comando == COMANDO_APAGAR:
            rodar_no_loop(sessao.conversas.apagar(sessao.id_da_conversa))
            sessao.id_da_conversa = str(uuid4())
            print(f"Lia: {MENSAGEM_HISTORICO_APAGADO}")
        elif len(texto) > TAMANHO_MAXIMO_DA_MENSAGEM:
            print(MENSAGEM_LONGA_DEMAIS)
        else:
            rodar_no_loop(responder(sessao, texto))


def executar(
    argumentos: argparse.Namespace,
    criar_agente_do_chat: CriarAgente = criar_agente,
    arquivo: Path = ARQUIVO_DAS_CONVERSAS,
    ler: Callable[[str], str] = input,
) -> None:
    """Abre o banco das conversas, mostra a saudação e conversa até o cliente sair."""
    with asyncio.Runner() as loop, contextlib.ExitStack() as ao_sair:
        recursos = contextlib.AsyncExitStack()
        memoria = loop.run(recursos.enter_async_context(abrir_memoria(arquivo)))
        ao_sair.callback(lambda: loop.run(recursos.aclose()))
        conversas = MemoriaDasConversas(criar_agente_do_chat(memoria=memoria))
        # O prazo de guarda vale também quando a conversa é só pelo terminal (LGPD).
        loop.run(conversas.apagar_antigas())
        sessao = Sessao(conversas, str(uuid4()), argumentos.detalhes)
        print(SAUDACAO)
        print(f"(Digite {COMANDO_AJUDA} para ver os comandos.)")
        if argumentos.conversa:
            loop.run(_retomar(sessao, str(argumentos.conversa)))
        try:
            conversar(sessao, loop.run, ler)
        finally:
            print(f"\nPra continuar esta conversa depois: python chat.py --conversa {sessao.id_da_conversa}")


async def _retomar(sessao: Sessao, id_da_conversa: str) -> None:
    historico = historico_para_a_interface(await sessao.conversas.mensagens(id_da_conversa))
    if not historico:
        print("\nNão encontrei essa conversa: ela pode ter sido apagada. Começando uma nova.")
        return
    sessao.id_da_conversa = id_da_conversa
    print("\n(Últimas mensagens desta conversa)")
    for mensagem in historico[-MENSAGENS_DO_HISTORICO:]:
        autor = "Você" if mensagem["role"] == "user" else "Lia"
        print(f"{autor}: " + "\n\n".join(parte["text"] for parte in mensagem["parts"]))


def _ler_argumentos(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python chat.py", description="Converse com a Lia pelo terminal.")
    parser.add_argument("--conversa", type=UUID, metavar="ID", help="continua uma conversa salva")
    parser.add_argument(
        "--detalhes", action="store_true", help="mostra as ferramentas chamadas e o modelo que respondeu"
    )
    parser.add_argument(
        "--agora", metavar="DATA", help="fixa o relógio da loja, ex.: 2026-11-08T15:00 (horário de Campo Grande)"
    )
    argumentos = parser.parse_args(argv)
    if argumentos.agora:
        os.environ[VARIAVEL_DO_RELOGIO] = argumentos.agora
        try:
            agora_na_loja()
        except ValueError as erro:
            parser.error(str(erro))
    return argumentos


def main(argv: Sequence[str] | None = None) -> int:
    usar_utf8_no_terminal()
    argumentos = _ler_argumentos(argv)
    load_dotenv()
    if not os.getenv("GOOGLE_API_KEY"):
        print(MENSAGEM_SEM_CHAVE)
        return 1
    # Com --detalhes, as trocas de modelo da cadeia (reservas) aparecem; sem ele, só os erros.
    nivel = logging.WARNING if argumentos.detalhes else logging.ERROR
    logging.basicConfig(level=nivel, format="  [%(message)s]")
    try:
        executar(argumentos)
    except KeyboardInterrupt:
        pass  # Ctrl+C: o turno em andamento já foi desfeito, e a dica de como continuar já apareceu.
    return 0


if __name__ == "__main__":
    sys.exit(main())
