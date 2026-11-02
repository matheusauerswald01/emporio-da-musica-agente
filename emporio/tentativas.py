"""Limite de tentativas de verificação do status_pedido (LGPD).

Os números de pedido são sequenciais. Sem limite, quem sabe o telefone de outra pessoa poderia
testar os pedidos um a um até achar o dela, ou testar contatos num mesmo pedido. Por isso, as
tentativas que não confirmam o dono contam por contato e por número de pedido, numa janela de
tempo, na memória do processo: começar outra conversa não zera a contagem. Consultas que dão
certo não contam, para o dono do pedido nunca ser barrado.

O contato fica guardado como hash, e não em texto, para não manter mais um dado pessoal na memória.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import time
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from emporio.ferramentas import MENSAGEM_PEDIDO_NAO_CONFERE, status_pedido
from emporio.texto import normalizar_telefone

LIMITE_DE_TENTATIVAS = 3
JANELA_EM_SEGUNDOS = 60 * 60
MENSAGEM_LIMITE_ATINGIDO = (
    "Limite de tentativas de verificação atingido para este pedido ou contato. Por segurança, não é "
    "possível consultá-lo aqui por enquanto. Oriente o cliente a falar com a equipe pelo WhatsApp."
)

ExecutarFerramenta = Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]]
ProtecaoDeFerramenta = Callable[[ToolCallRequest, ExecutarFerramenta], Awaitable[ToolMessage | Command]]


class LimiteDeTentativas:
    """Tentativas de verificação por chave, dentro de uma janela de tempo. Só na RAM do processo."""

    def __init__(
        self,
        limite: int = LIMITE_DE_TENTATIVAS,
        janela_em_segundos: float = JANELA_EM_SEGUNDOS,
        relogio: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limite = limite
        self._janela = janela_em_segundos
        self._relogio = relogio
        self._tentativas: dict[str, list[float]] = {}

    def reservar(self, chaves: Iterable[str]) -> bool:
        """Conta uma tentativa em cada chave, se nenhuma delas já atingiu o limite.

        A reserva acontece antes da consulta, sem pausa entre conferir e contar: chamadas em
        paralelo na mesma resposta não passam todas antes de a primeira ser contada.
        """
        agora = self._relogio()
        self._esquecer_antigas(agora)
        chaves = list(chaves)
        if any(len(self._tentativas.get(chave, [])) >= self._limite for chave in chaves):
            return False
        for chave in chaves:
            self._tentativas.setdefault(chave, []).append(agora)
        return True

    def devolver(self, chaves: Iterable[str]) -> None:
        """Desconta a tentativa reservada que não falhou: o dono do pedido não gasta o limite."""
        for chave in chaves:
            tentativas = self._tentativas.get(chave)
            if tentativas:
                tentativas.pop()
            if not tentativas:
                self._tentativas.pop(chave, None)

    def _esquecer_antigas(self, agora: float) -> None:
        """Tira as tentativas fora da janela e as chaves que ficaram vazias, para a memória não crescer."""
        recentes = {
            chave: [momento for momento in tentativas if agora - momento < self._janela]
            for chave, tentativas in self._tentativas.items()
        }
        self._tentativas = {chave: tentativas for chave, tentativas in recentes.items() if tentativas}


def proteger_status_pedido(limite: LimiteDeTentativas) -> ProtecaoDeFerramenta:
    """Interceptador do ToolNode que aplica o limite às chamadas do status_pedido."""

    async def proteger(requisicao: ToolCallRequest, executar: ExecutarFerramenta) -> ToolMessage | Command:
        chamada = requisicao.tool_call
        if chamada["name"] != status_pedido.__name__:
            return await executar(requisicao)
        chaves = _chaves(chamada["args"])
        if not limite.reservar(chaves):
            return ToolMessage(
                content=json.dumps({"verificado": False, "mensagem": MENSAGEM_LIMITE_ATINGIDO}, ensure_ascii=False),
                name=chamada["name"],
                tool_call_id=chamada["id"],
            )
        try:
            resultado = await executar(requisicao)
        except BaseException:
            # Consulta cancelada (botão Parar) ou com erro: não conta como tentativa errada.
            limite.devolver(chaves)
            raise
        if not _verificacao_falhou(resultado):
            limite.devolver(chaves)
        return resultado

    return proteger


def _chaves(argumentos: dict[str, Any]) -> list[str]:
    """Uma chave para o número do pedido e outra para o contato, no formato em que são comparados.

    O número é normalizado como a ferramenta o lê ("08", "8" e 8.0 são o pedido 8), para que
    variações da mesma consulta não escapem do limite.
    """
    numero = str(argumentos.get("numero_pedido", "")).strip()
    with contextlib.suppress(ValueError, OverflowError):
        numero = str(int(float(numero)))
    contato = str(argumentos.get("email_ou_telefone", "")).strip()
    normalizado = contato.lower() if "@" in contato else normalizar_telefone(contato)
    return [
        f"pedido:{numero}",
        f"contato:{hashlib.sha256(normalizado.encode('utf-8')).hexdigest()}",
    ]


def _verificacao_falhou(resultado: ToolMessage | Command) -> bool:
    return isinstance(resultado, ToolMessage) and MENSAGEM_PEDIDO_NAO_CONFERE in resultado.text
