/* FsmGraph — SVG 状态机拓扑图。
 * color prop 由父组件从 getAgentMeta 传入，使每类设备有各自的强调色。
 */
function FsmGraph({ states = [], transitions = [], current = '', onFire, color = '#C8FF3E' }) {
  const { useMemo } = React;

  if (states.length === 0) return null;

  /* hex → "r,g,b" 供 rgba() 使用 */
  const rgb = useMemo(() => {
    const h = color.replace('#', '');
    const r = parseInt(h.slice(0, 2), 16);
    const g = parseInt(h.slice(2, 4), 16);
    const b = parseInt(h.slice(4, 6), 16);
    return `${r},${g},${b}`;
  }, [color]);

  const W = 320, H = 280;
  const CX = W / 2, CY = H / 2;
  const LAYOUT_R = Math.min(CX, CY) - 42;
  const NODE_R = 26;

  const pos = useMemo(() => {
    const map = {};
    states.forEach((s, i) => {
      const a = (i * 2 * Math.PI) / states.length - Math.PI / 2;
      map[s] = { x: CX + LAYOUT_R * Math.cos(a), y: CY + LAYOUT_R * Math.sin(a) };
    });
    return map;
  }, [states]);

  const outgoing  = transitions.filter(t => t.src === current);
  const reachable = new Set(outgoing.map(t => t.dst));

  const makeArc = (srcName, dstName) => {
    const s = pos[srcName], d = pos[dstName];
    if (!s || !d) return null;
    const dx = d.x - s.x, dy = d.y - s.y;
    const len = Math.sqrt(dx * dx + dy * dy) || 1;
    const sx = s.x + (dx / len) * NODE_R;
    const sy = s.y + (dy / len) * NODE_R;
    const mx = (s.x + d.x) / 2, my = (s.y + d.y) / 2;
    const cpx = mx + (-dy / len) * 22;
    const cpy = my + (dx / len) * 22;
    const edx = d.x - cpx, edy = d.y - cpy;
    const elen = Math.sqrt(edx * edx + edy * edy) || 1;
    const ex = d.x - (edx / elen) * NODE_R;
    const ey = d.y - (edy / elen) * NODE_R;
    const lx = 0.25 * sx + 0.5 * cpx + 0.25 * ex;
    const ly = 0.25 * sy + 0.5 * cpy + 0.25 * ey;
    return { d: `M${sx},${sy} Q${cpx},${cpy} ${ex},${ey}`, lx, ly };
  };

  return (
    <svg
      width={W} height={H}
      viewBox={`0 0 ${W} ${H}`}
      style={{ display: 'block', width: '100%', height: 'auto', maxHeight: H }}
    >
      <defs>
        <marker id="fsm-arrow" markerWidth="7" markerHeight="7"
          refX="6" refY="3.5" orient="auto">
          <path d="M0,0 L7,3.5 L0,7 Z" fill={color} opacity="0.9"/>
        </marker>
        <filter id="fsm-glow" x="-50%" y="-50%" width="200%" height="200%">
          <feGaussianBlur stdDeviation="4" result="blur"/>
          <feMerge>
            <feMergeNode in="blur"/>
            <feMergeNode in="SourceGraphic"/>
          </feMerge>
        </filter>
      </defs>

      {/* ── 转移弧（仅当前态出发） ── */}
      {outgoing.map(t => {
        const arc = makeArc(t.src, t.dst);
        if (!arc) return null;
        return (
          <g key={t.trigger}
            onClick={() => onFire && onFire(t.trigger)}
            style={{ cursor: 'pointer' }}>
            <path d={arc.d} fill="none" stroke="transparent" strokeWidth={18}/>
            <path d={arc.d} fill="none" stroke={color} strokeWidth={1.8}
              markerEnd="url(#fsm-arrow)" opacity={0.85}/>
            <text x={arc.lx} y={arc.ly}
              textAnchor="middle" dominantBaseline="middle"
              fontSize="6.5" fontFamily="var(--font-mono)" fill={color} opacity="0.9"
              style={{ pointerEvents: 'none', userSelect: 'none' }}>
              {t.label}
            </text>
          </g>
        );
      })}

      {/* ── 状态节点 ── */}
      {states.map(s => {
        const p = pos[s];
        if (!p) return null;
        const isCurrent   = s === current;
        const isReachable = reachable.has(s);

        const stroke = isCurrent
          ? color
          : isReachable ? `rgba(${rgb},0.55)`
          : 'rgba(255,255,255,0.14)';
        const fill = isCurrent
          ? `rgba(${rgb},0.13)`
          : 'rgba(255,255,255,0.04)';
        const textFill = isCurrent
          ? color
          : isReachable ? `rgba(${rgb},0.85)`
          : 'rgba(255,255,255,0.28)';

        const singleArc = isReachable
          ? outgoing.filter(tr => tr.dst === s)
          : [];
        const handleClick = singleArc.length === 1
          ? () => onFire && onFire(singleArc[0].trigger)
          : undefined;

        return (
          <g key={s} onClick={handleClick}
            style={{ cursor: handleClick ? 'pointer' : 'default' }}>
            {isCurrent && (
              <circle cx={p.x} cy={p.y} r={NODE_R + 9}
                fill="none" stroke={color} strokeWidth="0.6" opacity="0.2"/>
            )}
            <circle cx={p.x} cy={p.y} r={NODE_R}
              fill={fill} stroke={stroke} strokeWidth={isCurrent ? 2 : 1}
              filter={isCurrent ? 'url(#fsm-glow)' : undefined}/>
            <text x={p.x} y={p.y + 1}
              textAnchor="middle" dominantBaseline="middle"
              fontSize="7.5" fontFamily="var(--font-mono)"
              fontWeight={isCurrent ? 'bold' : 'normal'}
              fill={textFill}
              style={{ pointerEvents: 'none', userSelect: 'none' }}>
              {s.length > 8 ? s.slice(0, 7) + '…' : s}
            </text>
          </g>
        );
      })}
    </svg>
  );
}
window.FsmGraph = FsmGraph;
