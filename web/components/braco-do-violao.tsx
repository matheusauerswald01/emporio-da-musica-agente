import type { CSSProperties } from "react";

// Trecho do braço de um violão, da pestana até a 12ª casa, que fica no pé do cabeçalho.
// As cordas vibram enquanto a Lia responde.

const CASAS = 12;

// Os trastes ficam onde ficam num instrumento de verdade: na escala temperada, a casa n está a
// 1 - 2^(-n/12) do comprimento da corda, por isso as casas encurtam rumo ao corpo do violão.
function posicaoDoTraste(casa: number): number {
  return (1 - 2 ** (-casa / 12)) / (1 - 2 ** (-CASAS / 12));
}

const TRASTES = Array.from({ length: CASAS }, (_, indice) => posicaoDoTraste(indice + 1));

// Casas com marcação no braço; a 12ª (a oitava da corda solta) tem marcação dupla.
const MARCACOES = [3, 5, 7, 9, 12];

// Da mi grave (bronze, revestida) à mi aguda (aço liso). Corda mais grossa vibra mais devagar.
const CORDAS = [
  { espessura: 2.6, material: "bronze", periodo: 0.24 },
  { espessura: 2.1, material: "bronze", periodo: 0.2 },
  { espessura: 1.7, material: "bronze", periodo: 0.17 },
  { espessura: 1.3, material: "bronze", periodo: 0.14 },
  { espessura: 1, material: "aco", periodo: 0.11 },
  { espessura: 0.8, material: "aco", periodo: 0.09 },
];

function meioDaCasa(casa: number): number {
  return (posicaoDoTraste(casa - 1) + posicaoDoTraste(casa)) / 2;
}

export function BracoDoViolao({ tocando }: { tocando: boolean }) {
  return (
    <div className="braco" data-tocando={tocando} aria-hidden="true">
      {TRASTES.map((posicao) => (
        <span key={posicao} className="braco__traste" style={{ left: `${posicao * 100}%` }} />
      ))}
      {MARCACOES.map((casa) => (
        <span
          key={casa}
          className={casa === CASAS ? "braco__marcacao braco__marcacao--dupla" : "braco__marcacao"}
          style={{ left: `${meioDaCasa(casa) * 100}%` }}
        />
      ))}
      {CORDAS.map((corda, indice) => (
        <span
          key={indice}
          className={`braco__corda braco__corda--${corda.material}`}
          style={
            {
              top: `${14 + indice * 14.4}%`,
              height: `${corda.espessura}px`,
              "--periodo": `${corda.periodo}s`,
            } as CSSProperties
          }
        />
      ))}
    </div>
  );
}
