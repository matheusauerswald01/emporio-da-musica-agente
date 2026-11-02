import type { Metadata, Viewport } from "next";
import { Atkinson_Hyperlegible_Next, Young_Serif } from "next/font/google";
import type { ReactNode } from "react";

import "./globals.css";

// Young Serif só na marca e no slogan; Atkinson Hyperlegible Next, feita para leitura fácil, no resto.
const fonteDaMarca = Young_Serif({ weight: "400", subsets: ["latin"], variable: "--fonte-marca" });
// O Next não tem as métricas desta fonte para gerar o fallback ajustado; usa o do sistema.
const fonteDoTexto = Atkinson_Hyperlegible_Next({
  subsets: ["latin"],
  variable: "--fonte-texto",
  adjustFontFallback: false,
  fallback: ["system-ui", "sans-serif"],
});

export const metadata: Metadata = {
  title: "Empório da Música · Atendimento",
  description:
    "Fale com a Lia, atendente virtual da Empório da Música: preços, estoque, pedidos e políticas da loja.",
};

export const viewport: Viewport = { themeColor: "#3b2318" };

export default function Layout({ children }: { children: ReactNode }) {
  return (
    <html lang="pt-BR" className={`${fonteDaMarca.variable} ${fonteDoTexto.variable}`}>
      <body>{children}</body>
    </html>
  );
}
