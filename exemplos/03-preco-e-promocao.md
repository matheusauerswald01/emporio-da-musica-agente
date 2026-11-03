# Exemplo 3: Preço, promoções e uma promoção que já acabou

> Gerado automaticamente com o horário fixado em 03/11/2026 às 10:30 (Campo Grande), com o gemini-3.5-flash-lite. As respostas não foram editadas.

**O que este exemplo mostra:** consulta de preço de um modelo específico, as promoções ativas com o preço original, o desconto e o preço final, e a resposta honesta para uma promoção que não está mais valendo.

**Cliente:** Quanto custa o Takamine GD20?

> 🔧 Lia consultou: `detalhes_produto(nome="Takamine GD20")`

**Lia:** O Takamine GD20 Dreadnought Natural custa R$ 2.199,00 e temos ele à pronta entrega na loja. 

Quer saber mais detalhes sobre ele ou posso ajudar com outra coisa?

**Cliente:** Tem algum violão em promoção?

> 🔧 Lia consultou: `promocoes_ativas()`

**Lia:** Temos dois violões com desconto rolando agora:

- Crafter HT-100 Folk Aço Natural: de R$ 2.399,00 por R$ 1.967,18 com 18% de desconto
- Taylor 110e Dreadnought Natural: de R$ 5.999,00 por R$ 5.519,08 com 8% de desconto

**Cliente:** Vi o Crafter CE-15 na Black Friday com 10% de desconto, ainda vale?

> 🔧 Lia consultou: `promocoes_ativas()`
> 🔧 Lia consultou: `detalhes_produto(nome="Crafter CE-15")`

**Lia:** Essa promoção da Black Friday não está mais ativa. O Crafter CE-15 Elétrico Nylon Natural está custando o preço normal de R$ 3.899,00.
