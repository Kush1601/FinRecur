// Money is integer centavos everywhere on the wire; this is the only place that turns
// centavos into a printed BRL string.
export function formatBRL(centavos: number): string {
  const negative = centavos < 0;
  const abs = Math.abs(Math.trunc(centavos));
  const reais = Math.floor(abs / 100);
  const cents = String(abs % 100).padStart(2, "0");
  const withThousands = reais.toString().replace(/\B(?=(\d{3})+(?!\d))/g, ".");
  return `${negative ? "-" : ""}R$ ${withThousands},${cents}`;
}

// Percent inputs here are ratios (0.029 -> "2.9%"), matching how the policy stores rule
// thresholds (R4 = 0.029 etc). `digits` controls precision for tight bounds like 2.500%.
export function formatPercent(ratio: number, digits = 1): string {
  return `${(ratio * 100).toFixed(digits)}%`;
}
