# Avaliação automática da Lia

- Executada em 02/11/2026 às 21:59 (horário de Campo Grande). Cada caso fixa a própria data e hora da conversa (veja `avaliacao/casos.py`).
- Modelos, em ordem: gemini-3.5-flash-lite → gemini-3.1-flash-lite (o primeiro responde; os outros são reservas).
- Repetições por caso: 1.

**Placar: 16 de 18 casos passaram (89%).** Falhas de infraestrutura (cota, rede), que não contam: 0.

| Caso | Resultado | Ferramentas chamadas | Modelo | Motivo da falha |
|---|---|---|---|---|
| `catalogo_violoes` | ✅ | `buscar_produtos(categoria="violão", preco_maximo=1000)` | gemini-3.5-flash-lite |  |
| `preco_takamine` | ✅ | `detalhes_produto(nome="Takamine GD20")` | gemini-3.5-flash-lite |  |
| `endereco` | ✅ | `consultar_politicas(assunto="endereço")` | gemini-3.5-flash-lite |  |
| `loja_fechada` | ✅ | `consultar_politicas(assunto="horário de funcionamento")` | gemini-3.5-flash-lite |  |
| `parcelamento` | ✅ | `consultar_politicas(assunto="parcelamento parcela mínima")` | gemini-3.5-flash-lite |  |
| `promocoes` | ✅ | `promocoes_ativas()` | gemini-3.5-flash-lite |  |
| `promo_vencida` | ❌ | `promocoes_ativas()` | gemini-3.5-flash-lite | não chamou detalhes_produto; faltou "R$ 3.899,00" |
| `sem_estoque` | ✅ | `detalhes_produto(nome="Giannini GF-3D")` | gemini-3.5-flash-lite |  |
| `em_breve` | ✅ | `detalhes_produto(nome="Kala Barítono KA-ABP-CTG")` | gemini-3.5-flash-lite |  |
| `pedido_atrasado` | ❌ | `status_pedido(numero_pedido=8, email_ou_telefone="***")` | gemini-3.5-flash-lite | faltou "BRJL5544332BR" |
| `pedido_sem_contato` | ✅ | nenhuma | gemini-3.5-flash-lite |  |
| `pedido_de_outro` | ✅ | `status_pedido(numero_pedido=4, email_ou_telefone="***")` | gemini-3.5-flash-lite |  |
| `devolucao_regra` | ✅ | `consultar_politicas(assunto="arrependimento devolução")` | gemini-3.5-flash-lite |  |
| `devolucao_fora_do_prazo` | ✅ | `consultar_politicas(assunto="prazo para devolução e arrependimento")` | gemini-3.5-flash-lite |  |
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

Aquela promoção da Black Friday com o Crafter CE-15 não está ativa no momento. 

Olhando as ofertas de agora, o Crafter HT-100 Folk Aço Natural está com 18% de desconto: de R$ 2.399,00 por R$ 1.967,18. 

Quer dar uma olhada nele ou prefere ver outros modelos?

### pedido_atrasado

Motivo: faltou "BRJL5544332BR".

**Cliente:** Meu pedido 8 não chegou ainda. Meu e-mail é ***

> Lia consultou: `status_pedido(numero_pedido=8, email_ou_telefone="***")`

**Lia** (gemini-3.5-flash-lite):

Ana, o seu pedido 8 (o Kala KA-C Concert Mogno) está com a entrega atrasada — a previsão era para o dia 28/02/2026. Peço desculpas por esse transtorno! 

Para que nossa equipe acompanhe a situação de perto com a transportadora, por favor, chame a gente no WhatsApp pelo número (67) 3321-4500.

