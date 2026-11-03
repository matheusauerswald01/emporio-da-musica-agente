"""Regras para conferir uma resposta da Lia: o que ela tinha que fazer, o que não podia fazer e por quê.

A resposta de um LLM varia nas palavras de uma vez para outra, então a conferência procura trechos
curtos, sem acento e em minúsculas, e aceita os jeitos comuns de escrever um preço. O que só pode ser
conferido letra a letra (o e-mail do manual, que difere do oficial só pelo acento) usa Exato.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from emporio.sessao import MASCARA, PARECE_CONTATO, ChamadaDeFerramenta


@dataclass(frozen=True)
class Preco:
    """Um preço em reais, aceito como "R$ 2.199" ou "R$ 2.199,00", com espaço comum ou não separável."""

    valor: float

    def __str__(self) -> str:
        inteiro, centavos = f"{self.valor:.2f}".split(".")
        return f"R$ {int(inteiro):,}".replace(",", ".") + f",{centavos}"

    def aparece_em(self, texto: str) -> bool:
        inteiro, centavos = f"{self.valor:.2f}".split(".")
        reais = re.escape(f"{int(inteiro):,}".replace(",", "."))
        # Centavos zerados podem ser omitidos; os outros, não ("R$ 439,20").
        sufixo = f"(?:,{centavos})?" if centavos == "00" else f",{centavos}"
        # Depois do preço não pode vir outro dígito nem outros centavos; vírgula de pontuação pode.
        return re.search(rf"R\$\s?{reais}{sufixo}(?!\d|,\d)", texto) is not None


@dataclass(frozen=True)
class Exato:
    """Trecho conferido letra a letra (só sem diferença de maiúsculas), com acento e tudo."""

    trecho: str

    def __str__(self) -> str:
        return self.trecho

    def aparece_em(self, texto: str) -> bool:
        return self.trecho.lower() in texto.lower()


Trecho = str | Preco | Exato


def aparece(trecho: Trecho, texto: str) -> bool:
    if isinstance(trecho, str):
        return normalizar(trecho) in normalizar(texto)
    return trecho.aparece_em(texto)


def normalizar(texto: str) -> str:
    """Minúsculas, sem acento e com um espaço comum no lugar de qualquer sequência de espaços."""
    sem_acento = "".join(
        letra for letra in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(letra)
    )
    return " ".join(sem_acento.lower().split())


def motivos_da_falha(
    resposta: str,
    ferramentas: Sequence[ChamadaDeFerramenta],
    *,
    ferramentas_esperadas: Iterable[str] = (),
    ferramentas_proibidas: Iterable[str] = (),
    argumentos_esperados: Mapping[str, Mapping[str, Any]] | None = None,
    deve_conter: Iterable[Trecho] = (),
    deve_conter_um_de: Sequence[Trecho] = (),
    nao_deve_conter: Iterable[Trecho] = (),
) -> list[str]:
    """Por que a conversa não passou; lista vazia se passou.

    As ferramentas valem para a conversa inteira; os trechos, só para a última resposta da Lia.
    """
    chamadas = {chamada.nome for chamada in ferramentas}
    motivos = [f"não chamou {nome}" for nome in ferramentas_esperadas if nome not in chamadas]
    motivos += [f"chamou {nome}, que não podia" for nome in ferramentas_proibidas if nome in chamadas]
    for nome, esperados in (argumentos_esperados or {}).items():
        if nome in chamadas and not any(
            _argumentos_conferem(chamada.argumentos, esperados) for chamada in ferramentas if chamada.nome == nome
        ):
            motivos.append(f"{nome} sem {_como_argumentos(esperados)}")
    motivos += [f'faltou "{trecho}"' for trecho in deve_conter if not aparece(trecho, resposta)]
    if deve_conter_um_de and not any(aparece(trecho, resposta) for trecho in deve_conter_um_de):
        motivos.append("faltou um destes: " + ", ".join(f'"{trecho}"' for trecho in deve_conter_um_de))
    motivos += [f'disse "{trecho}", que não podia' for trecho in nao_deve_conter if aparece(trecho, resposta)]
    return motivos


def sem_contatos_do_cliente(texto: str, mensagens_do_cliente: Iterable[str]) -> str:
    """O texto com os e-mails e telefones que o cliente mandou trocados por "***" (LGPD).

    Telefones são comparados pelos últimos 8 dígitos, para pegar o número escrito de outro jeito.
    """
    contatos = [contato for mensagem in mensagens_do_cliente for contato in PARECE_CONTATO.findall(mensagem)]
    emails = {contato.lower().rstrip(".,;!?") for contato in contatos if "@" in contato}
    finais_de_telefone = {_digitos(contato)[-8:] for contato in contatos if "@" not in contato}

    def mascarar(achado: re.Match[str]) -> str:
        contato = achado.group()
        if "@" in contato:
            return MASCARA if contato.lower().rstrip(".,;!?") in emails else contato
        return MASCARA if _digitos(contato)[-8:] in finais_de_telefone else contato

    return PARECE_CONTATO.sub(mascarar, texto)


def _argumentos_conferem(argumentos: Mapping[str, Any], esperados: Mapping[str, Any]) -> bool:
    return all(_mesmo_valor(argumentos.get(nome), valor) for nome, valor in esperados.items())


def _mesmo_valor(recebido: Any, esperado: Any) -> bool:
    if isinstance(esperado, (int, float)) and isinstance(recebido, (int, float)):
        return float(recebido) == float(esperado)  # o Gemini manda 1000.0 no lugar de 1000
    if isinstance(esperado, str) and isinstance(recebido, str):
        return normalizar(recebido) == normalizar(esperado)
    return recebido == esperado


def _como_argumentos(argumentos: Mapping[str, Any]) -> str:
    return ", ".join(f"{nome}={valor!r}" for nome, valor in argumentos.items())


def _digitos(texto: str) -> str:
    return re.sub(r"\D", "", texto)
