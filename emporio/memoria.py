"""Memória das conversas em SQLite (conversas.db), que sobrevive a reinícios da API.

O arquivo guarda conversas de clientes (LGPD): fica só nesta máquina e fora do Git (.gitignore).
Apagar uma conversa remove o conteúdo de verdade. Por padrão, o SQLite só marca o espaço como
livre e o texto continua no arquivo; aqui ele é sobrescrito com zeros (secure_delete), e o
registro de transações (WAL), que ainda teria as páginas antigas, é esvaziado logo em seguida.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

ARQUIVO_DAS_CONVERSAS = Path(__file__).resolve().parent.parent / "conversas.db"


class MemoriaEmSqlite(AsyncSqliteSaver):
    """Checkpointer do LangGraph em SQLite que não deixa resto da conversa apagada no disco."""

    async def adelete_thread(self, thread_id: str) -> None:
        await super().adelete_thread(thread_id)
        async with self.lock:
            await self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


@asynccontextmanager
async def abrir_memoria(arquivo: Path = ARQUIVO_DAS_CONVERSAS) -> AsyncIterator[MemoriaEmSqlite]:
    """Abre (ou cria) o banco das conversas, já configurado para apagar de verdade."""
    async with MemoriaEmSqlite.from_conn_string(str(arquivo)) as memoria:
        await memoria.conn.execute("PRAGMA secure_delete = ON")
        yield memoria
