"""Testes do expediente da loja: aberta ou fechada, feriados, próxima abertura e relógio fixado."""

from datetime import date, datetime, timedelta

import pytest

from emporio.expediente import (
    FUSO_DA_LOJA,
    HORARIOS,
    VARIAVEL_DO_RELOGIO,
    agora_na_loja,
    feriado,
    relogio_fixado,
    situacao_da_loja,
)
from emporio.politicas import ler_secoes


def _em_campo_grande(*data_e_hora: int) -> datetime:
    return datetime(*data_e_hora, tzinfo=FUSO_DA_LOJA)


def test_relogio_fixado_vale_no_horario_de_campo_grande(monkeypatch):
    monkeypatch.setenv(VARIAVEL_DO_RELOGIO, "2026-11-08T15:00")  # domingo
    assert agora_na_loja() == _em_campo_grande(2026, 11, 8, 15, 0)
    assert situacao_da_loja(agora_na_loja()).startswith("A loja está fechada agora.")

    monkeypatch.setenv(VARIAVEL_DO_RELOGIO, "2026-11-03T10:00")  # terça-feira
    assert situacao_da_loja(agora_na_loja()) == "A loja está aberta agora, até as 18:00."


def test_sem_relogio_fixado_vale_a_hora_atual(monkeypatch):
    monkeypatch.delenv(VARIAVEL_DO_RELOGIO, raising=False)

    assert abs(agora_na_loja() - datetime.now(FUSO_DA_LOJA)) < timedelta(seconds=5)
    assert agora_na_loja().tzinfo == FUSO_DA_LOJA


def test_relogio_fixado_so_dentro_do_bloco(monkeypatch):
    monkeypatch.setenv(VARIAVEL_DO_RELOGIO, "2026-11-03T10:00")

    with relogio_fixado("2026-11-08T15:00"):
        assert agora_na_loja() == _em_campo_grande(2026, 11, 8, 15, 0)
    assert agora_na_loja() == _em_campo_grande(2026, 11, 3, 10, 0)


def test_relogio_fixado_em_formato_errado_avisa_o_formato_certo(monkeypatch):
    monkeypatch.setenv(VARIAVEL_DO_RELOGIO, "03/11/2026 10:30")

    with pytest.raises(ValueError, match="2026-11-03T10:30"):
        agora_na_loja()


def test_feriado_fecha_a_loja_e_informa_quando_ela_volta_a_abrir():
    # 02/11/2026 é uma segunda-feira, mas é Finados.
    assert situacao_da_loja(_em_campo_grande(2026, 11, 2, 14, 30)) == (
        "A loja está fechada agora (feriado: Finados). Volta a abrir terça-feira, 03/11/2026, às 09:00."
    )


def test_sabado_so_abre_de_manha():
    assert situacao_da_loja(_em_campo_grande(2026, 11, 7, 10, 0)) == "A loja está aberta agora, até as 13:00."
    assert situacao_da_loja(_em_campo_grande(2026, 11, 7, 13, 0)) == (
        "A loja está fechada agora. Volta a abrir segunda-feira, 09/11/2026, às 09:00."
    )


def test_sexta_feira_santa_muda_de_data_a_cada_ano():
    assert feriado(date(2026, 4, 3)) == "Sexta-feira Santa"
    assert feriado(date(2027, 3, 26)) == "Sexta-feira Santa"
    assert feriado(date(2027, 4, 2)) is None


def test_horarios_sao_os_mesmos_do_manual():
    manual = next(secao for secao in ler_secoes() if secao.titulo == "2. Horário de Funcionamento").texto
    semana, sabado = HORARIOS[0], HORARIOS[5]

    assert f"Segunda a Sexta-feira | {semana[0]:%H:%M} às {semana[1]:%H:%M}" in manual
    assert f"Sábado | {sabado[0]:%H:%M} às {sabado[1]:%H:%M}" in manual
    assert "Domingo e Feriados | Fechado" in manual and 6 not in HORARIOS
