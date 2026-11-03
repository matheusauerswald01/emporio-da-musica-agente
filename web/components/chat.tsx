"use client";

import { useChat } from "@ai-sdk/react";
import { DefaultChatTransport } from "ai";
import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";

import { BracoDoViolao } from "./braco-do-violao";
import { Balao, Mensagem, type MensagemDoChat } from "./mensagem";

// O mesmo limite que a API aceita.
const TAMANHO_MAXIMO = 2000;
const WHATSAPP = "(67) 3321-4500";
// Quão perto do fim (em px) a conversa precisa estar para acompanhar a resposta chegando.
const DISTANCIA_PARA_ACOMPANHAR = 80;

// A API avisa no stream qual ferramenta a Lia chamou; aqui vira o que o cliente lê.
const CONSULTAS: Record<string, string> = {
  buscar_produtos: "Procurando no catálogo",
  detalhes_produto: "Conferindo o produto",
  promocoes_ativas: "Vendo as promoções",
  status_pedido: "Consultando o pedido",
  consultar_politicas: "Lendo as políticas da loja",
};

const SUGESTOES = [
  "Quais violões vocês têm até R$ 1.000?",
  "Quanto custa o Takamine GD20?",
  "Tem alguma promoção agora?",
  "Onde fica a loja?",
  "Me arrependi da compra. Posso devolver?",
  "Onde está o meu pedido?",
];

const transporte = new DefaultChatTransport<MensagemDoChat>({
  api: "/api/chat",
  // Só a última mensagem vai para a API: o histórico fica na memória do agente, no servidor.
  prepareSendMessagesRequest: ({ id, messages }) => ({ body: { id, message: messages.at(-1) } }),
});

// O id da conversa fica guardado no navegador, para o cliente continuar de onde parou.
const CHAVE_DO_ID_NO_NAVEGADOR = "emporio-da-musica:conversa";
const FORMATO_UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

// UUID v4 da conversa. O crypto.randomUUID só existe em contexto seguro (HTTPS ou localhost); aberta
// pela rede local (no celular, por exemplo), a página monta o UUID com getRandomValues.
function novoIdDeConversa(): string {
  if (typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40; // versão 4
  bytes[8] = (bytes[8] & 0x3f) | 0x80; // variante RFC 4122
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

// Sem acesso ao armazenamento do navegador (bloqueado, ou na renderização do servidor), a conversa
// vale só enquanto a página está aberta.
function idDaConversaGuardado(): string {
  try {
    const guardado = localStorage.getItem(CHAVE_DO_ID_NO_NAVEGADOR);
    if (guardado && FORMATO_UUID.test(guardado)) {
      return guardado;
    }
    const novo = novoIdDeConversa();
    localStorage.setItem(CHAVE_DO_ID_NO_NAVEGADOR, novo);
    return novo;
  } catch {
    return novoIdDeConversa();
  }
}

export function Chat() {
  const [idDaConversa] = useState(idDaConversaGuardado);
  const [rascunho, setRascunho] = useState("");
  const [consulta, setConsulta] = useState<string | null>(null);
  // Aviso da API antes de um erro, com o texto para o cliente e se vale tentar de novo: a API sabe o que
  // aconteceu. O banner usa só o aviso, e não o texto do erro, que numa falha de rede vem do navegador.
  const [avisoDeErro, setAvisoDeErro] = useState<{ texto: string; podeTentarDeNovo: boolean } | null>(null);
  const conversa = useRef<HTMLElement>(null);
  const acompanhandoOFim = useRef(true);
  const historicoApagado = useRef(false);

  const { messages, setMessages, sendMessage, status, error, stop, regenerate, clearError } = useChat<MensagemDoChat>({
    id: idDaConversa,
    transport: transporte,
    // Redesenha no máximo a cada 50 ms enquanto o texto chega, em vez de a cada pedaço.
    throttle: 50,
    onData: (parte) => {
      if (parte.type === "data-ferramenta") {
        setConsulta(CONSULTAS[parte.data.nome] ?? "Consultando");
      }
      if (parte.type === "data-historico-apagado") {
        historicoApagado.current = true;
      }
      if (parte.type === "data-erro") {
        setAvisoDeErro(parte.data);
      }
    },
    onFinish: ({ message }) => {
      // Depois do /apagar, a tela também esquece a conversa: fica só a confirmação da Lia.
      if (historicoApagado.current) {
        historicoApagado.current = false;
        setMessages([message]);
      }
    },
  });

  const respondendo = status === "submitted" || status === "streaming";
  const ultimaParte = messages.at(-1)?.role === "assistant" ? messages.at(-1)?.parts.at(-1) : undefined;
  const escrevendo = ultimaParte?.type === "text" && ultimaParte.state === "streaming";

  // Acompanha a resposta chegando, a menos que o cliente tenha rolado para cima para reler algo.
  useEffect(() => {
    const area = conversa.current;
    if (area && messages.length > 0 && acompanhandoOFim.current) {
      area.scrollTop = area.scrollHeight;
    }
  }, [messages, status]);

  // Ao abrir a página, mostra o histórico guardado no servidor (se o cliente ainda não começou a escrever).
  useEffect(() => {
    let paginaAberta = true;
    fetch(`/api/chat/${idDaConversa}`)
      .then((resposta) => (resposta.ok ? (resposta.json() as Promise<{ messages: MensagemDoChat[] }>) : null))
      .then((historico) => {
        if (paginaAberta && historico?.messages.length) {
          setMessages((atuais) => (atuais.length === 0 ? historico.messages : atuais));
        }
      })
      .catch(() => {
        // Sem histórico disponível, a conversa só começa do zero.
      });
    return () => {
      paginaAberta = false;
    };
  }, [idDaConversa, setMessages]);

  function aoRolar() {
    const area = conversa.current;
    if (area) {
      acompanhandoOFim.current = area.scrollHeight - area.scrollTop - area.clientHeight < DISTANCIA_PARA_ACOMPANHAR;
    }
  }

  function enviar(texto: string) {
    const pergunta = texto.trim();
    if (!pergunta || respondendo) {
      return;
    }
    acompanhandoOFim.current = true;
    setConsulta(null);
    setAvisoDeErro(null);
    setRascunho("");
    void sendMessage({ text: pergunta });
  }

  function aoEnviarFormulario(evento: FormEvent<HTMLFormElement>) {
    evento.preventDefault();
    enviar(rascunho);
  }

  function aoDigitar(evento: KeyboardEvent<HTMLTextAreaElement>) {
    // Enter envia; Shift+Enter quebra a linha.
    if (evento.key === "Enter" && !evento.shiftKey && !evento.nativeEvent.isComposing) {
      evento.preventDefault();
      enviar(rascunho);
    }
  }

  function tentarDeNovo() {
    clearError();
    setAvisoDeErro(null);
    void regenerate();
  }

  async function parar() {
    await stop();
    // O servidor desfaz a resposta interrompida, com a pergunta. A tela faz o mesmo e devolve a
    // pergunta ao campo, para o cliente reenviar ou mudar.
    const indiceDaPergunta = messages.findLastIndex((mensagem) => mensagem.role === "user");
    if (indiceDaPergunta === -1) {
      return;
    }
    const pergunta = messages[indiceDaPergunta].parts.flatMap((parte) => (parte.type === "text" ? [parte.text] : []));
    setMessages((atuais) => atuais.slice(0, indiceDaPergunta));
    setRascunho(pergunta.join("\n"));
  }

  return (
    <div className="pagina">
      <header className="cabecalho">
        <div className="cabecalho__conteudo coluna">
          <div>
            <h1 className="marca">Empório da Música</h1>
            <p className="cabecalho__atendente">Lia · atendente virtual</p>
          </div>
        </div>
        <BracoDoViolao tocando={respondendo} />
      </header>

      <main className="conversa" ref={conversa} onScroll={aoRolar}>
        <div className="coluna">
          {messages.length === 0 && <p className="slogan">Sua música começa aqui.</p>}

          <div role="log" aria-label="Conversa com a Lia" aria-busy={respondendo}>
            <ol className="mensagens">
              <Balao autor="lia">
                <p>
                  Oi! Eu sou a Lia, da Empório da Música. Posso ver preço e estoque dos instrumentos,
                  acompanhar o seu pedido e tirar dúvidas sobre trocas, pagamento e entrega. Como posso ajudar?
                </p>
              </Balao>
              {messages.map((mensagem) => (
                <Mensagem key={mensagem.id} mensagem={mensagem} />
              ))}
            </ol>
          </div>

          {messages.length === 0 && (
            <section className="sugestoes" aria-labelledby="titulo-sugestoes">
              <h2 id="titulo-sugestoes" className="sugestoes__titulo">
                Perguntas comuns
              </h2>
              <ul className="sugestoes__lista">
                {SUGESTOES.map((sugestao) => (
                  <li key={sugestao}>
                    <button type="button" className="sugestao" onClick={() => enviar(sugestao)}>
                      {sugestao}
                    </button>
                  </li>
                ))}
              </ul>
            </section>
          )}

          {/* Sempre montada: leitores de tela só anunciam o que muda dentro de uma região que já existe. */}
          <p className="consultando" role="status">
            {respondendo && !escrevendo ? `${consulta ?? "Lia está digitando"}…` : ""}
          </p>

          {error && (
            <div className="erro" role="alert">
              {/* Sem o aviso da API, a falha foi no caminho (rede, API fora do ar). */}
              <p>
                {avisoDeErro?.texto ??
                  `A Lia não conseguiu responder agora. Tente de novo; se continuar, chame a gente no WhatsApp ${WHATSAPP}.`}
              </p>
              {(avisoDeErro?.podeTentarDeNovo ?? true) && (
                <button type="button" className="erro__botao" onClick={tentarDeNovo}>
                  Tentar de novo
                </button>
              )}
            </div>
          )}
        </div>
      </main>

      <footer className="compositor">
        <form className="compositor__formulario coluna" onSubmit={aoEnviarFormulario}>
          <label htmlFor="pergunta" className="so-leitor-de-tela">
            Sua mensagem para a Lia
          </label>
          <textarea
            id="pergunta"
            className="compositor__campo"
            rows={1}
            maxLength={TAMANHO_MAXIMO}
            placeholder="Escreva sua pergunta"
            value={rascunho}
            onChange={(evento) => setRascunho(evento.target.value)}
            onKeyDown={aoDigitar}
          />
          {respondendo ? (
            <button type="button" className="compositor__botao" onClick={() => void parar()}>
              Parar
            </button>
          ) : (
            <button type="submit" className="compositor__botao" disabled={!rascunho.trim()}>
              Enviar
            </button>
          )}
        </form>
        {/* O prazo é o PRAZO_DE_GUARDA de emporio/conversas.py. */}
        <p className="aviso-de-privacidade coluna">
          Não envie CPF, dados de cartão nem senhas. As mensagens são processadas pelo Gemini, do Google. O
          histórico fica guardado por até 30 dias para você continuar depois; para apagá-lo agora, envie{" "}
          <strong>/apagar</strong>.
        </p>
      </footer>
    </div>
  );
}
