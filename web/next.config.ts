import type { NextConfig } from "next";

// A API Python (FastAPI) roda à parte. O Next repassa /api/* para ela, então o navegador fala com
// uma origem só e a API não precisa liberar CORS. API_URL é lida quando o Next monta o app: no
// build de produção, o endereço fica fixo no `next build`.
const API_URL = process.env.API_URL ?? "http://127.0.0.1:8000";

const nextConfig: NextConfig = {
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${API_URL}/api/:path*` }];
  },
};

export default nextConfig;
