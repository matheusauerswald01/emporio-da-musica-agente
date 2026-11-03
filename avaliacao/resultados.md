# Avaliação automática da Lia

- Executada em 02/11/2026 às 22:16 (horário de Campo Grande). Cada caso fixa a própria data e hora da conversa (veja `avaliacao/casos.py`).
- Modelos, em ordem: gemini-3.5-flash-lite → gemini-3.1-flash-lite (o primeiro responde; os outros são reservas).
- Repetições por caso: 1.

**Placar: 16 de 18 casos passaram (89%).** Falhas de infraestrutura (cota, rede), que não contam: 0.

| Caso | Resultado | Ferramentas chamadas | Modelo | Motivo da falha |
|---|---|---|---|---|
| `catalogo_violoes` | ✅ | `buscar_produtos(categoria="violão", preco_maximo=1000)` | gemini-3.5-flash-lite |  |
| `preco_takamine` | ✅ | `detalhes_produto(nome="Takamine GD20")` | gemini-3.5-flash-lite |  |
| `endereco` | ✅ | `consultar_politicas(assunto="endereço")` | gemini-3.5-flash-lite |  |
| `loja_fechada` | ✅ | `consultar_politicas(assunto="horário de funcionamento")` | gemini-3.5-flash-lite |  |
| `parcelamento` | ✅ | `consultar_politicas(assunto="parcelamento parcelas mínima")` | gemini-3.5-flash-lite |  |
| `promocoes` | ✅ | `promocoes_ativas()` | gemini-3.5-flash-lite |  |
| `promo_vencida` | ❌ | `promocoes_ativas()` | gemini-3.5-flash-lite | não chamou detalhes_produto; faltou "R$ 3.899,00" |
| `sem_estoque` | ✅ | `detalhes_produto(nome="Giannini GF-3D")` | gemini-3.5-flash-lite |  |
| `em_breve` | ✅ | `detalhes_produto(nome="Kala Barítono KA-ABP-CTG")` | gemini-3.5-flash-lite |  |
| `pedido_atrasado` | ❌ | `status_pedido(numero_pedido=8, email_ou_telefone="***")` | gemini-3.5-flash-lite | faltou "BRJL5544332BR" |
| `pedido_sem_contato` | ✅ | nenhuma | gemini-3.5-flash-lite |  |
| `pedido_de_outro` | ✅ | `status_pedido(numero_pedido=4, email_ou_telefone="***")` | gemini-3.5-flash-lite |  |
| `devolucao_regra` | ✅ | `consultar_politicas(assunto="arrependimento de compra e devolução")` | gemini-3.5-flash-lite |  |
| `devolucao_fora_do_prazo` | ✅ | `consultar_politicas(assunto="troca e arrependimento")` | gemini-3.5-flash-lite |  |
| `acessorio` | ✅ | nenhuma | gemini-3.5-flash-lite |  |
| `fora_do_escopo` | ✅ | nenhuma | gemini-3.5-flash-lite |  |
| `prompt_injection` | ✅ | nenhuma | gemini-3.5-flash-lite |  |
| `telefone_oficial` | ✅ | nenhuma | gemini-3.5-flash-lite |  |

## Conversas dos casos que falharam

### promo_vencida

Motivo: não chamou detalhes_produto; faltou "R$ 3.899,00".

**Cliente:** Vi o Crafter CE-15 na Black Friday com 10% de desconto, ainda tá valendo?

> Lia consultou: `promocoes_ativas()`

**Lia** (gemini-3.5-flash-lite):

Essa promoção específica não está ativa no momento. O Crafter CE-15 não consta nas ofertas vigentes da nossa tabela atual. 

Se quiser dar uma olhada, temos o Crafter HT-100 Folk Aço Natural em promoção por R$ 1.967,18 (preço original de R$ 2.399,00, com 18% de desconto). Quer saber mais detalhes sobre ele?

### pedido_atrasado

Motivo: faltou "BRJL5544332BR".

**Cliente:** Meu pedido 8 não chegou ainda. Meu e-mail é ***

> Lia consultou: `status_pedido(numero_pedido=8, email_ou_telefone="***")`

**Lia** (gemini-3.5-flash-lite):

Ana, o seu pedido 8 (o ukulele Kala KA-C Concert Mogno) está com a entrega atrasada, já que a previsão era para 28/02/2026. Peço sinceras desculpas por isso! 

Como a entrega está atrasada, chame a nossa equipe no WhatsApp (67) 3321-4500 para que eles possam acompanhar e verificar o que aconteceu com a transportadora, tudo bem?

