import type { UIMessage } from "ai";
import { memo, type ReactNode } from "react";
import Markdown from "react-markdown";

// Mensagem do useChat. A API manda avisos passageiros, que não ficam salvos na mensagem: o que a
// Lia está consultando ("data-ferramenta"), a confirmação do /apagar ("data-historico-apagado") e,
// antes de um erro, o texto dele e se vale tentar de novo ("data-erro").
export type MensagemDoChat = UIMessage<
  never,
  {
    ferramenta: { nome: string };
    "historico-apagado": Record<string, never>;
    erro: { texto: string; podeTentarDeNovo: boolean };
  }
>;

type Autor = "lia" | "cliente";

// Só a formatação que a Lia usa. Imagens e links ficam de fora: uma resposta induzida a trazer
// uma imagem externa faria o navegador mandar dados da conversa para outro site, sem clique.
const ELEMENTOS_PERMITIDOS = ["p", "strong", "em", "ul", "ol", "li", "br"];

export function Balao({ autor, children }: { autor: Autor; children: ReactNode }) {
  return (
    <li className={`balao balao--${autor}`}>
      <span className="so-leitor-de-tela">{autor === "lia" ? "Lia:" : "Você:"}</span>
      {children}
    </li>
  );
}

// memo: enquanto a resposta chega, só a última mensagem muda; as anteriores não são redesenhadas.
export const Mensagem = memo(function Mensagem({ mensagem }: { mensagem: MensagemDoChat }) {
  const textos = mensagem.parts.flatMap((parte) => (parte.type === "text" && parte.text ? [parte.text] : []));
  if (textos.length === 0) {
    return null;
  }
  if (mensagem.role === "user") {
    return (
      <Balao autor="cliente">
        <p>{textos.join("\n")}</p>
      </Balao>
    );
  }
  // O react-markdown não renderiza HTML cru; o que não está na lista vira texto simples.
  return (
    <Balao autor="lia">
      {textos.map((texto, indice) => (
        <Markdown key={indice} allowedElements={ELEMENTOS_PERMITIDOS} unwrapDisallowed>
          {texto}
        </Markdown>
      ))}
    </Balao>
  );
});
