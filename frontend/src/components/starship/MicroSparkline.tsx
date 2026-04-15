import { useMemo } from 'react';

/** 微型折线：用价格波动幅度映射线条亮度（类 ATR 感知）；可选金色虚线 = 双向费后回本价 */
export function MicroSparkline({
  values,
  accentClass = 'stroke-[#2dd4bf]',
  breakEvenPrice,
}: {
  values: number[];
  accentClass?: string;
  /** 有持仓时：Taker进+Maker平 后的参考回本价（非裸开仓价） */
  breakEvenPrice?: number | null;
}) {
  const { d, intensity, beY } = useMemo(() => {
    if (!values.length) return { d: '', intensity: 0.15, beY: null as number | null };
    const series =
      breakEvenPrice != null && Number.isFinite(breakEvenPrice) && breakEvenPrice > 0
        ? [...values, breakEvenPrice]
        : values;
    const min = Math.min(...series);
    const max = Math.max(...series);
    const range = max - min || 1e-12;
    const w = 56;
    const h = 22;
    const pts = values.map((v, i) => {
      const x = (i / Math.max(values.length - 1, 1)) * w;
      const y = h - ((v - min) / range) * h;
      return `${x},${y}`;
    });
    const intensity = Math.min(1, (range / min) * 8);
    let beY: number | null = null;
    if (breakEvenPrice != null && Number.isFinite(breakEvenPrice) && breakEvenPrice > 0) {
      beY = h - ((breakEvenPrice - min) / range) * h;
    }
    return { d: `M ${pts.join(' L ')}`, intensity, beY };
  }, [values, breakEvenPrice]);

  if (!d) {
    return <div className="w-14 h-[22px] rounded bg-white/[0.03]" />;
  }

  return (
    <svg width="56" height="22" className="overflow-visible shrink-0" aria-hidden>
      {beY != null && (
        <line
          x1={0}
          x2={56}
          y1={beY}
          y2={beY}
          stroke="#e7b15a"
          strokeWidth={0.9}
          strokeDasharray="2 3"
          opacity={0.42}
        />
      )}
      <path
        d={d}
        fill="none"
        className={accentClass}
        strokeWidth="1.25"
        style={{ opacity: 0.35 + intensity * 0.55 }}
      />
    </svg>
  );
}
