"""Expediente da loja: se ela está aberta agora e, se não está, quando volta a abrir.

O cálculo fica em Python, e não com o modelo: a resposta depende do dia da semana, da hora e dos
feriados, e o manual (seção 7.1) pede que, fora do expediente, o atendimento informe o horário
de retorno. Os horários repetem a tabela da seção 2 do manual; um teste confere que continuam
iguais. Feriados considerados: os nacionais, o estadual de MS (11/10) e o aniversário de Campo
Grande (26/08). Outros feriados municipais e pontos facultativos ficam de fora (ver README).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

FUSO_DA_LOJA = ZoneInfo("America/Campo_Grande")
DIAS_DA_SEMANA = (
    "segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo",
)

# Abertura e fechamento por dia da semana (0 = segunda). Domingo não abre. Manual, seção 2.
HORARIOS = {
    **{dia: (time(9), time(18)) for dia in range(5)},
    5: (time(9), time(13)),
}

FERIADOS_FIXOS = {
    (1, 1): "Confraternização Universal",
    (4, 21): "Tiradentes",
    (5, 1): "Dia do Trabalho",
    (8, 26): "Aniversário de Campo Grande",
    (9, 7): "Independência do Brasil",
    (10, 11): "Criação do Estado de Mato Grosso do Sul",
    (10, 12): "Nossa Senhora Aparecida",
    (11, 2): "Finados",
    (11, 15): "Proclamação da República",
    (11, 20): "Dia da Consciência Negra",
    (12, 25): "Natal",
}

# Uma semana basta para achar a próxima abertura, mesmo com um feriado no caminho.
DIAS_PARA_PROCURAR_ABERTURA = 8


def agora_na_loja() -> datetime:
    """Data e hora atuais no fuso da loja."""
    return datetime.now(FUSO_DA_LOJA)


def situacao_da_loja(agora: datetime) -> str:
    """Frase pronta para as instruções do atendente: aberta até quando, ou fechada até quando."""
    horario = _horario_do_dia(agora.date())
    if horario and horario[0] <= agora.time() < horario[1]:
        return f"A loja está aberta agora, até as {horario[1]:%H:%M}."
    nome_do_feriado = feriado(agora.date())
    motivo = f" (feriado: {nome_do_feriado})" if nome_do_feriado else ""
    abertura = proxima_abertura(agora)
    return (
        f"A loja está fechada agora{motivo}. Volta a abrir {DIAS_DA_SEMANA[abertura.weekday()]}, "
        f"{abertura:%d/%m/%Y}, às {abertura:%H:%M}."
    )


def proxima_abertura(agora: datetime) -> datetime:
    """Próximo momento em que a loja abre, a partir de agora."""
    for dias in range(DIAS_PARA_PROCURAR_ABERTURA):
        dia = agora.date() + timedelta(days=dias)
        horario = _horario_do_dia(dia)
        if horario:
            abertura = datetime.combine(dia, horario[0], tzinfo=agora.tzinfo)
            if abertura > agora:
                return abertura
    raise ValueError("A loja não abre em nenhum dia da próxima semana; confira HORARIOS e FERIADOS_FIXOS.")


def feriado(dia: date) -> str | None:
    """Nome do feriado nessa data, ou None."""
    if (dia.month, dia.day) in FERIADOS_FIXOS:
        return FERIADOS_FIXOS[(dia.month, dia.day)]
    if dia == _domingo_de_pascoa(dia.year) - timedelta(days=2):
        return "Sexta-feira Santa"
    return None


def _horario_do_dia(dia: date) -> tuple[time, time] | None:
    """Abertura e fechamento do dia, ou None se a loja não abre (domingo ou feriado)."""
    if feriado(dia):
        return None
    return HORARIOS.get(dia.weekday())


def _domingo_de_pascoa(ano: int) -> date:
    """Domingo de Páscoa no calendário gregoriano (algoritmo de Meeus/Jones/Butcher)."""
    a, b, c = ano % 19, ano // 100, ano % 100
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    mes, dia = divmod(h + l - 7 * m + 114, 31)
    return date(ano, mes, dia + 1)
