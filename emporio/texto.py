"""Normalização de texto: o mesmo tratamento vale para o catálogo e para o que o cliente digita."""

from __future__ import annotations

import re
import unicodedata

CODIGO_DO_BRASIL = "55"

# Palavras que não ajudam a achar um produto ("quero um violão DE nylon" = "violão nylon").
PALAVRAS_IGNORADAS = frozenset(
    {
        "a", "o", "as", "os", "ao", "aos", "de", "da", "do", "das", "dos", "e", "ou",
        "em", "no", "na", "nos", "nas", "com", "para", "pra", "por", "um", "uma",
        "todo", "toda", "todos", "todas", "outro", "outra", "outros", "outras",
        "instrumento", "instrumentos", "musical", "musicais", "modelo", "modelos",
        "quero", "gostaria", "tem", "voces", "favor", "algum", "alguma",
    }
)

# Plurais comuns do português, do mais específico para o mais genérico.
PLURAIS = (("oes", "ao"), ("aes", "ao"), ("ais", "al"), ("res", "r"), ("zes", "z"))
SINGULARES_IRREGULARES = {"oboes": "oboe"}

# Jeitos diferentes de dizer a mesma coisa, inclusive termos em inglês dos nomes e das specs.
SINONIMOS = {
    "contrabaixo": "baixo",
    "bass": "baixo",
    "sax": "saxofone",
    "tuner": "afinador",
    "guitar": "guitarra",
    "keyboard": "teclado",
    "drum": "bateria",
    "electric": "eletrico",
    "eletrica": "eletrico",
    "eletronico": "eletrico",
    "eletronica": "eletrico",
    "captacao": "eletrico",
    "captador": "eletrico",
    "eletroacustica": "eletroacustico",
    "acoustic": "acustico",
    "acustica": "acustico",
    "classica": "classico",
    "concerto": "concert",
    "ukelele": "ukulele",
    "uquelele": "ukulele",
    "cedar": "cedro",
    "mahogany": "mogno",
    "maple": "bordo",
    "black": "preto",
    "preta": "preto",
    "red": "vermelho",
    "vermelha": "vermelho",
    "blue": "azul",
    "white": "branco",
    "branca": "branco",
    "quatro": "4",
    "cinco": "5",
    "seis": "6",
    "sete": "7",
    "oito": "8",
}


def normalizar_texto(texto: str) -> str:
    """Minúsculo, sem acento e sem pontuação: "Violões D-28" vira "violoes d 28"."""
    sem_acento = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", sem_acento.lower()).split())


def palavras_chave(texto: str) -> list[str]:
    """Palavras usadas nas buscas: normalizadas, no singular, com sinônimos unificados e sem
    palavras vazias. Letras e números são separados, para "GD20" e "GD-20" darem no mesmo:
    "Violões de Aço GD20" vira ["violao", "aco", "gd", "20"].
    """
    separado = re.sub(r"(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])", " ", normalizar_texto(texto))
    return [_forma_canonica(palavra) for palavra in separado.split() if palavra not in PALAVRAS_IGNORADAS]


def so_digitos(texto: str) -> str:
    """Mantém só os números: "(67) 99812-3456" vira "67998123456"."""
    return re.sub(r"\D", "", texto)


def normalizar_telefone(texto: str) -> str:
    """Só dígitos, sem zero inicial e sem o código do país: "+55 (67) 99812-3456" vira "67998123456".

    Vale para o cadastro e para o que o cliente digita, para os dois serem comparáveis.
    """
    digitos = so_digitos(texto).lstrip("0")
    if len(digitos) > 11 and digitos.startswith(CODIGO_DO_BRASIL):
        return digitos[len(CODIGO_DO_BRASIL):]
    return digitos


def _forma_canonica(palavra: str) -> str:
    # O sinônimo é conferido antes do singular porque "seis" no singular viraria "sei".
    if palavra in SINONIMOS:
        return SINONIMOS[palavra]
    singular = _singular(palavra)
    return SINONIMOS.get(singular, singular)


def _singular(palavra: str) -> str:
    """Reduz o plural mais comum ("violoes" vira "violao"), para "violão" achar "Violões"."""
    if palavra in SINGULARES_IRREGULARES:
        return SINGULARES_IRREGULARES[palavra]
    for plural, singular in PLURAIS:
        if palavra.endswith(plural):
            return palavra[: -len(plural)] + singular
    if palavra.endswith("s") and len(palavra) > 3:
        return palavra[:-1]
    return palavra
