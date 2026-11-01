"""Converte as planilhas CSV da loja em documentos JSON validados, um arquivo por entidade.

Os CSV em data/ são a fonte original; os JSON em data/json/ são o que o agente lê. Para mudar
um dado, edite o CSV e rode de novo (não edite os JSON à mão):
    python scripts/csv_para_json.py

Cada registro vira um documento completo, que nunca é quebrado em pedaços:
- números, booleanos e datas (ISO) com o tipo certo, e null no lugar de célula vazia;
- as especificações viram objeto de textos, e não JSON dentro de uma célula;
- os registros filhos ficam dentro do pai (promoções no produto, itens no pedido),
  e o nome da categoria fica no produto.

Esta é a porta de entrada dos dados: célula obrigatória vazia, valor inválido, id repetido ou
referência a id inexistente interrompem a conversão com tabela, linha e coluna do problema.

Dos clientes, só vai o necessário para conferir o dono de um pedido: primeiro nome, e-mail e
telefone (LGPD, princípio da necessidade). Clientes sem pedido continuam no arquivo porque um
contato cadastrado para dois clientes não pode servir de prova de identidade.
"""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import date
from pathlib import Path

PASTA_CSV = Path(__file__).resolve().parent.parent / "data"
PASTA_JSON = PASTA_CSV / "json"
PREFIXO_CSV = "desafio_tecnico_ai_eng - "

STATUS_PRODUTO = {"active", "discontinued", "coming_soon"}
STATUS_PEDIDO = {"pending", "confirmed", "shipped", "delivered", "cancelled"}
BOOLEANOS = {"1": True, "0": False}

ROTULOS_ESPECIFICACOES = {
    "top": "tampo",
    "back_sides": "fundo e laterais",
    "neck": "braço",
    "body": "corpo",
    "strings": "cordas",
    "scale": "escala",
    "electronics": "eletrônica",
    "color": "cor",
    "shells": "cascos",
    "pieces": "peças",
    "hardware": "ferragens",
    "keys": "teclas",
    "type": "tipo",
    "polyphony": "polifonia",
}

Linha = dict[str, str]


def converter(pasta_csv: Path = PASTA_CSV, pasta_json: Path = PASTA_JSON) -> None:
    """Lê os CSV de `pasta_csv`, valida e grava os documentos JSON em `pasta_json`."""
    categorias = _ler(pasta_csv, "categories", _categoria)
    produtos = _ler(pasta_csv, "products", _produto)
    promocoes = _ler(pasta_csv, "promotions", _promocao)
    pedidos = _ler(pasta_csv, "orders", _pedido)
    itens = _ler(pasta_csv, "order_items", _item)
    clientes = _ler(pasta_csv, "customers", _cliente)

    for nome, registros, chave in (
        ("categoria", categorias, "id_categoria"),
        ("produto", produtos, "id_produto"),
        ("promoção", promocoes, "id_promocao"),
        ("pedido", pedidos, "id_pedido"),
        ("cliente", clientes, "id_cliente"),
    ):
        _conferir_unicos(nome, registros, chave)
    _conferir_referencias("produto", produtos, "id_categoria", categorias)
    _conferir_referencias("promoção", promocoes, "id_produto", produtos)
    _conferir_referencias("item de pedido", itens, "id_produto", produtos)
    _conferir_referencias("item de pedido", itens, "id_pedido", pedidos)
    _conferir_referencias("pedido", pedidos, "id_cliente", clientes)

    nomes_categorias = {categoria["id_categoria"]: categoria["categoria"] for categoria in categorias}
    for produto in produtos:
        produto["categoria"] = nomes_categorias[produto["id_categoria"]]
    _aninhar(produtos, "promocoes", promocoes, chave="id_produto")
    _aninhar(pedidos, "itens", itens, chave="id_pedido")

    pasta_json.mkdir(parents=True, exist_ok=True)
    _gravar(pasta_json, "categorias", categorias, "id_categoria")
    _gravar(pasta_json, "produtos", produtos, "id_produto")
    _gravar(pasta_json, "pedidos", pedidos, "id_pedido")
    _gravar(pasta_json, "clientes", clientes, "id_cliente")


def _ler(pasta: Path, tabela: str, converter_linha: Callable[[Linha], dict]) -> list[dict]:
    """Converte cada linha do CSV; um erro aponta tabela e linha (a 1 é o cabeçalho)."""
    with open(pasta / f"{PREFIXO_CSV}{tabela}.csv", encoding="utf-8", newline="") as arquivo:
        linhas = list(csv.DictReader(arquivo))
    registros = []
    for numero, linha in enumerate(linhas, start=2):
        try:
            registros.append(converter_linha(linha))
        except ValueError as erro:
            raise ValueError(f"{tabela}.csv, linha {numero}: {erro}") from erro
    return registros


def _categoria(linha: Linha) -> dict:
    return {
        "id_categoria": _inteiro(linha, "category_id"),
        "categoria": _texto(linha, "name"),
        "descricao": _texto_opcional(linha, "description"),
    }


def _produto(linha: Linha) -> dict:
    return {
        "id_produto": _inteiro(linha, "product_id"),
        "nome": _texto(linha, "name"),
        "id_categoria": _inteiro(linha, "category_id"),
        "categoria": None,  # nome, preenchido a partir da tabela de categorias
        "preco_tabela": _decimal(linha, "price_brl", minimo=0),
        "estoque": _inteiro(linha, "stock_quantity", minimo=0),
        "status": _um_de(linha, "status", STATUS_PRODUTO),
        "descricao": _texto_opcional(linha, "description"),
        "especificacoes": _especificacoes(linha, "specs"),
        "criado_em": _data_opcional(linha, "created_at"),
    }


def _promocao(linha: Linha) -> dict:
    desconto = _decimal(linha, "discount_percent", minimo=0)
    if desconto > 100:
        raise ValueError(f"coluna discount_percent={desconto!r} passa de 100%")
    return {
        "id_promocao": _inteiro(linha, "promotion_id"),
        "id_produto": _inteiro(linha, "product_id"),
        "desconto_percentual": int(desconto) if desconto.is_integer() else desconto,
        "campanha": _texto_opcional(linha, "description"),
        "ativa": BOOLEANOS[_um_de(linha, "is_active", set(BOOLEANOS))],
    }


def _pedido(linha: Linha) -> dict:
    return {
        "id_pedido": _inteiro(linha, "order_id"),
        "id_cliente": _inteiro(linha, "customer_id"),
        "data_pedido": _data(linha, "order_date"),
        "status": _um_de(linha, "status", STATUS_PEDIDO),
        "total": _decimal(linha, "total_brl", minimo=0),
        "forma_pagamento": _texto(linha, "payment_method"),
        "codigo_rastreio": _texto_opcional(linha, "tracking_code"),
        "previsao_entrega": _data_opcional(linha, "estimated_delivery"),
        "observacao": _texto_opcional(linha, "notes"),
    }


def _item(linha: Linha) -> dict:
    return {
        "id_pedido": _inteiro(linha, "order_id"),
        "id_produto": _inteiro(linha, "product_id"),
        "quantidade": _inteiro(linha, "quantity", minimo=1),
    }


def _cliente(linha: Linha) -> dict:
    return {
        "id_cliente": _inteiro(linha, "customer_id"),
        "primeiro_nome": _texto(linha, "name").split()[0],
        "email": _texto_opcional(linha, "email"),
        "telefone": _texto_opcional(linha, "phone"),
    }


def _texto_opcional(linha: Linha, coluna: str) -> str | None:
    """Texto sem espaços nas pontas; célula vazia vira null."""
    return linha[coluna].strip() or None


def _texto(linha: Linha, coluna: str) -> str:
    texto = _texto_opcional(linha, coluna)
    if texto is None:
        raise ValueError(f"coluna {coluna} é obrigatória e está vazia")
    return texto


def _inteiro(linha: Linha, coluna: str, minimo: int | None = None) -> int:
    valor = _texto(linha, coluna)
    try:
        numero = int(valor)
    except ValueError:
        raise ValueError(f"coluna {coluna}={valor!r} não é um número inteiro") from None
    return _no_minimo(coluna, numero, minimo)


def _decimal(linha: Linha, coluna: str, minimo: float | None = None) -> float:
    valor = _texto(linha, coluna)
    try:
        numero = float(valor)
    except ValueError:
        raise ValueError(f"coluna {coluna}={valor!r} não é um número (use ponto como separador decimal)") from None
    return _no_minimo(coluna, numero, minimo)


def _no_minimo(coluna: str, numero: float, minimo: float | None) -> float:
    if minimo is not None and numero < minimo:
        raise ValueError(f"coluna {coluna}={numero!r} é menor que {minimo}")
    return numero


def _um_de(linha: Linha, coluna: str, permitidos: set[str]) -> str:
    valor = _texto(linha, coluna)
    if valor not in permitidos:
        raise ValueError(f"coluna {coluna}={valor!r} não é um destes valores: {sorted(permitidos)}")
    return valor


def _data_opcional(linha: Linha, coluna: str) -> str | None:
    """Data no formato ISO (AAAA-MM-DD), conferida; célula vazia vira null."""
    valor = _texto_opcional(linha, coluna)
    if valor is None:
        return None
    try:
        return date.fromisoformat(valor).isoformat()
    except ValueError:
        raise ValueError(f"coluna {coluna}={valor!r} não é uma data AAAA-MM-DD") from None


def _data(linha: Linha, coluna: str) -> str:
    _texto(linha, coluna)
    return _data_opcional(linha, coluna)


def _especificacoes(linha: Linha, coluna: str) -> dict[str, str]:
    """O JSON da célula vira objeto de textos (como na planilha), com rótulos em português."""
    valor = _texto_opcional(linha, coluna)
    if valor is None:
        return {}
    try:
        especificacoes = json.loads(valor)
    except json.JSONDecodeError as erro:
        raise ValueError(f"coluna {coluna} não é um JSON válido: {erro}") from None
    if not isinstance(especificacoes, dict):
        raise ValueError(f"coluna {coluna} deveria ser um objeto JSON, mas é {type(especificacoes).__name__}")
    return {ROTULOS_ESPECIFICACOES.get(chave, chave): str(conteudo) for chave, conteudo in especificacoes.items()}


def _conferir_unicos(nome: str, registros: list[dict], chave: str) -> None:
    """Interrompe a conversão se dois registros têm o mesmo id."""
    repetidos = sorted(valor for valor, vezes in Counter(registro[chave] for registro in registros).items() if vezes > 1)
    if repetidos:
        raise ValueError(f"{nome} com {chave} repetido: {repetidos}")


def _conferir_referencias(nome: str, registros: list[dict], chave: str, destino: list[dict]) -> None:
    """Interrompe a conversão se algum registro aponta para um id que não existe."""
    existentes = {registro[chave] for registro in destino}
    orfaos = sorted({registro[chave] for registro in registros} - existentes)
    if orfaos:
        raise ValueError(f"{nome} aponta para {chave} inexistente: {orfaos}")


def _aninhar(pais: list[dict], campo: str, filhos: list[dict], chave: str) -> None:
    """Coloca cada filho dentro do pai com a mesma chave, sem repetir a chave no filho."""
    por_pai = defaultdict(list)
    for filho in filhos:
        por_pai[filho[chave]].append({nome: valor for nome, valor in filho.items() if nome != chave})
    for pai in pais:
        pai[campo] = por_pai[pai[chave]]


def _gravar(pasta: Path, entidade: str, registros: list[dict], chave: str) -> None:
    caminho = pasta / f"{entidade}.json"
    ordenados = sorted(registros, key=lambda registro: registro[chave])
    with open(caminho, "w", encoding="utf-8", newline="\n") as arquivo:
        json.dump(ordenados, arquivo, ensure_ascii=False, indent=2)
        arquivo.write("\n")
    print(f"{entidade}.json: {len(ordenados)} registros")


if __name__ == "__main__":
    converter()
