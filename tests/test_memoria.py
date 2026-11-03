"""Teste da memória em SQLite: a conversa sobrevive ao reinício da API e, apagada, não fica no disco."""

import asyncio

from langchain_core.messages import AIMessage, HumanMessage

from emporio.agente import criar_agente
from emporio.conversas import MemoriaDasConversas
from emporio.memoria import abrir_memoria
from tests.modelo_falso import ModeloRoteirizado

CONFIGURACAO = {"configurable": {"thread_id": "conversa"}}
EMAIL = b"ana.teste@exemplo.com"


def test_conversa_sobrevive_ao_reinicio_e_apagada_nao_deixa_resto_no_arquivo(tmp_path):
    arquivo = tmp_path / "conversas.db"

    def conteudo_do_banco() -> bytes:
        # O banco mais os arquivos auxiliares do SQLite (-wal, -shm), onde páginas antigas ficariam.
        return b"".join(caminho.read_bytes() for caminho in tmp_path.glob("conversas.db*"))

    async def cenario():
        async with abrir_memoria(arquivo) as memoria:
            agente = criar_agente(ModeloRoteirizado(roteiro=[AIMessage("Oi, Ana!")]), memoria)
            await agente.ainvoke({"messages": [HumanMessage(f"Sou a Ana, {EMAIL.decode()}")]}, CONFIGURACAO)
        assert EMAIL in conteudo_do_banco()

        # A API reiniciou: a conversa continua lá, e o cliente manda /apagar.
        async with abrir_memoria(arquivo) as memoria:
            conversas = MemoriaDasConversas(criar_agente(ModeloRoteirizado(roteiro=[]), memoria))
            assert [mensagem.text for mensagem in await conversas.mensagens("conversa")] == [
                f"Sou a Ana, {EMAIL.decode()}",
                "Oi, Ana!",
            ]
            await conversas.apagar("conversa")

    asyncio.run(cenario())
    assert EMAIL not in conteudo_do_banco()
