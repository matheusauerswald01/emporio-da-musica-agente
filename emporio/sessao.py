"""Um turno da conversa com o agente fora da API: usado pelo chat no terminal, pela avaliação e pelos
exemplos de conversa.

O turno segue as mesmas regras da API (uma mensagem por vez na conversa; a resposta que falha ou é
interrompida é desfeita por inteiro) e devolve o que interessa para mostrar e conferir: o texto da
Lia, as ferramentas que ela chamou e o modelo que respondeu. Na descrição das ferramentas, e-mail e
telefone do cliente aparecem sempre mascarados (LGPD).
"""

from __future__ import annotations

import io
import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import anyio
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage

from emporio.conversas import MemoriaDasConversas

# O que o cliente lê enquanto a Lia consulta: os mesmos textos da interface web (CONSULTAS em
# web/components/chat.tsx; um teste confere que continuam iguais).
CONSULTAS = {
    "buscar_produtos": "Procurando no catálogo",
    "detalhes_produto": "Conferindo o produto",
    "promocoes_ativas": "Vendo as promoções",
    "status_pedido": "Consultando o pedido",
    "consultar_politicas": "Lendo as políticas da loja",
}
CONSULTA_SEM_NOME = "Consultando"

MASCARA = "***"
# Argumentos que sempre levam um contato do cliente.
ARGUMENTOS_PESSOAIS = frozenset({"email_ou_telefone"})
# E-mail ou telefone (8 dígitos ou mais, com ou sem separadores) em qualquer outro argumento de
# texto, para o caso de o modelo pôr o contato no lugar errado. O achado termina sempre num dígito
# ou numa letra, sem levar a pontuação que vem depois.
PARECE_CONTATO = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+|\+?\(?\d(?:[\s().-]*\d){7,}")


class ConversaOcupada(Exception):
    """A conversa ainda está respondendo a mensagem anterior."""


@dataclass(frozen=True)
class ChamadaDeFerramenta:
    nome: str
    argumentos: dict[str, Any]

    def descrever(self) -> str:
        """A chamada em uma linha, com os contatos mascarados.

        Ex.: status_pedido(numero_pedido=8, email_ou_telefone="***")
        """
        argumentos = ", ".join(f"{nome}={_valor_mostrado(nome, valor)}" for nome, valor in self.argumentos.items())
        return f"{self.nome}({argumentos})"


@dataclass(frozen=True)
class ResultadoDoTurno:
    # Os textos da Lia no turno, na ordem, como o cliente lê.
    texto: str
    ferramentas: list[ChamadaDeFerramenta]
    # Modelo que escreveu a resposta final (mostra quando quem respondeu foi um modelo reserva).
    modelo: str | None


def texto_da_consulta(ferramenta: str) -> str:
    return CONSULTAS.get(ferramenta, CONSULTA_SEM_NOME)


def usar_utf8_no_terminal() -> None:
    """Acentos certos no PowerShell, que por padrão escreve na página de código do Windows (cp1252)."""
    for saida in (sys.stdout, sys.stderr):
        if isinstance(saida, io.TextIOWrapper):
            saida.reconfigure(encoding="utf-8")


async def rodar_turno(
    conversas: MemoriaDasConversas,
    id_da_conversa: str,
    texto: str,
    ao_escrever: Callable[[str], None] | None = None,
    ao_consultar: Callable[[str], None] | None = None,
) -> ResultadoDoTurno:
    """Manda a mensagem do cliente ao agente e devolve o resultado do turno.

    ao_escrever recebe cada pedaço de texto da Lia assim que chega, e ao_consultar o nome de cada
    ferramenta que ela chama, para quem mostra a conversa ao vivo. O resultado vem do estado gravado
    da conversa, e não dos pedaços do stream: neles, os argumentos das ferramentas chegam picados.
    """
    if not conversas.iniciar_resposta(id_da_conversa):
        raise ConversaOcupada(id_da_conversa)
    mensagens_antes: set[str] | None = None
    concluida = False
    try:
        mensagens_antes = await conversas.ids_das_mensagens(id_da_conversa)
        async for mensagem, _ in conversas.agente.astream(
            {"messages": [HumanMessage(texto)]},
            {"configurable": {"thread_id": id_da_conversa}},
            stream_mode="messages",
        ):
            if not isinstance(mensagem, AIMessage):
                continue  # resultados das ferramentas
            if ao_escrever and mensagem.text:
                ao_escrever(mensagem.text)
            if ao_consultar:
                for nome in _ferramentas_pedidas(mensagem):
                    ao_consultar(nome)
        concluida = True
    finally:
        # Blindado, como na API: precisa rodar mesmo se o turno for cancelado (Ctrl+C no chat).
        with anyio.CancelScope(shield=True):
            await conversas.terminar_resposta(id_da_conversa, mensagens_antes, concluida)
    return _resultado_do_ultimo_turno(await conversas.mensagens(id_da_conversa))


def _ferramentas_pedidas(mensagem: AIMessage) -> list[str]:
    """Nomes das ferramentas pedidas. No stream, só o primeiro pedaço de cada chamada traz o nome."""
    chamadas = getattr(mensagem, "tool_call_chunks", None) or mensagem.tool_calls
    return [chamada["name"] for chamada in chamadas if chamada.get("name")]


def _resultado_do_ultimo_turno(mensagens: list[AnyMessage]) -> ResultadoDoTurno:
    perguntas = [indice for indice, mensagem in enumerate(mensagens) if isinstance(mensagem, HumanMessage)]
    respostas = [mensagem for mensagem in mensagens[perguntas[-1] + 1 :] if isinstance(mensagem, AIMessage)]
    return ResultadoDoTurno(
        texto="\n\n".join(resposta.text.strip() for resposta in respostas if resposta.text.strip()),
        ferramentas=[
            ChamadaDeFerramenta(chamada["name"], chamada["args"])
            for resposta in respostas
            for chamada in resposta.tool_calls
        ],
        modelo=respostas[-1].response_metadata.get("model_name") if respostas else None,
    )


def _valor_mostrado(nome: str, valor: Any) -> str:
    if nome in ARGUMENTOS_PESSOAIS or (isinstance(valor, str) and PARECE_CONTATO.search(valor)):
        return f'"{MASCARA}"'
    if isinstance(valor, str):
        return json.dumps(valor, ensure_ascii=False)
    if isinstance(valor, float) and valor.is_integer():
        return str(int(valor))  # o Gemini manda 1000.0 para "até R$ 1.000"
    return repr(valor)
