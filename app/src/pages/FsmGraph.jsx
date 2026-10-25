/* FsmGraph — 当前状态的可用转换以连线呈现，点击连线即可执行。 */
function FsmGraph({ states = [], transitions = [], current = '', onFire, onSelectState, selectedState, color = '#C8FF3E', views = {}, layout = {}, showSafety = true }) {
  const { useMemo } = React;

  /* hex → "r,g,b" 供 rgba() 使用 */
  const rgb = useMemo(() => {
    const h = color.replace('#', '');
    const r = parseInt(h.slice(0, 2), 16);
    const g = parseInt(h.slice(2, 4), 16);
    const b = parseInt(h.slice(4, 6), 16);
    return `${r},${g},${b}`;
  }, [color]);

  const W = 240, H = 250;
  const CX = W / 2, CY = H / 2;
  const LAYOUT_R = 88;
  const NODE_R = 27;

  const pos = useMemo(() => {
    const map = {};
    // 默认沿圆周排布，避免中心节点挡住另外两个状态之间的连线。
    states.forEach((s, i) => {
      const a = (i * 2 * Math.PI) / states.length - Math.PI / 2;
      map[s] = states.length === 1
        ? { x: CX, y: CY }
        : { x: CX + LAYOUT_R * Math.cos(a), y: CY + LAYOUT_R * Math.sin(a) };
    });
    states.forEach(s => {
      const point = layout[s];
      if (Number.isFinite(point?.x) && Number.isFinite(point?.y)) {
        map[s] = { x: point.x * W, y: point.y * H };
      }
    });
    return map;
  }, [states, layout]);

  const outgoing  = transitions.filter(t => t.src === current && t.dst !== current && !t.safety);
  const safetyEdges = transitions.filter(t => t.src === current && t.safety && t.action);
  const reachable = new Set(outgoing.filter(t => t.action).map(t => t.dst));

  if (states.length === 0) return null;

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

  const fire = trigger => onFire && onFire(trigger);
  const onKey = action => event => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      action();
    }
  };

  return (
    <div className="fsm-graph-wrap">
    <svg
      className="fsm-graph"
      width={W} height={H}
      viewBox={`0 0 ${W} ${H}`}
      role="group"
      aria-label={`状态图，当前状态：${views[current]?.label || current || '未知'}`}
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
          <g key={`${t.trigger}-${t.dst}`}
            className={`fsm-graph__edge${t.action ? '' : ' is-automatic'}`}
            role={t.action ? 'button' : undefined} tabIndex={t.action ? 0 : undefined}
            aria-label={t.action ? `${views[current]?.label || current}，${t.label}，切换到${views[t.dst]?.label || t.dst}` : undefined}
            onClick={t.action ? () => fire(t.trigger) : undefined}
            onKeyDown={t.action ? onKey(() => fire(t.trigger)) : undefined}>
            <title>{t.label} → {views[t.dst]?.label || t.dst}</title>
            <path d={arc.d} fill="none" stroke="transparent" strokeWidth={18}/>
            <path d={arc.d} fill="none" stroke={color} strokeWidth={1.8}
              markerEnd="url(#fsm-arrow)" opacity={0.85}/>
            <text x={arc.lx} y={arc.ly}
              textAnchor="middle" dominantBaseline="middle"
              fontSize="10" fontFamily="var(--font-sans)" fill={color} opacity="0.95"
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
          ? outgoing.filter(tr => tr.dst === s && tr.action)
          : [];
        const trigger = singleArc.length === 1 ? singleArc[0].trigger : null;
        const label = views[s]?.label || s;
        const selectPreview = typeof onSelectState === 'function';
        const nodeAction = selectPreview
          ? () => { onSelectState(s); if (trigger) fire(trigger); }
          : trigger ? () => fire(trigger) : null;
        const isSelected = selectPreview && selectedState === s;

        return (
          <g key={s} className={`fsm-graph__node${trigger ? ' is-reachable' : ''}${selectPreview ? ' is-previewable' : ''}`}
            role={nodeAction ? 'button' : undefined} tabIndex={nodeAction ? 0 : undefined}
            aria-label={selectPreview
              ? `${label}，${trigger ? `${singleArc[0].label}并` : ''}查看预览`
              : trigger ? `${label}，${singleArc[0].label}` : label}
            aria-pressed={selectPreview ? isSelected : undefined}
            onClick={nodeAction || undefined}
            onKeyDown={nodeAction ? onKey(nodeAction) : undefined}>
            <title>{label}{isCurrent ? '（当前状态）' : ''}</title>
            {isSelected && (
              <circle cx={p.x} cy={p.y} r={NODE_R + 5}
                fill="none" stroke={color} strokeWidth="1" opacity="0.85"/>
            )}
            {isCurrent && (
              <circle cx={p.x} cy={p.y} r={NODE_R + 9}
                fill="none" stroke={color} strokeWidth="0.6" opacity="0.2"/>
            )}
            <circle cx={p.x} cy={p.y} r={NODE_R}
              fill={fill} stroke={stroke} strokeWidth={isCurrent ? 2 : 1}
              filter={isCurrent ? 'url(#fsm-glow)' : undefined}/>
            <text x={p.x} y={p.y + 1}
              textAnchor="middle" dominantBaseline="middle"
              fontSize="10" fontFamily="var(--font-sans)"
              fontWeight={isCurrent ? 'bold' : 'normal'}
              fill={textFill}
              style={{ pointerEvents: 'none', userSelect: 'none' }}>
              {label.length > 6 ? label.slice(0, 5) + '…' : label}
            </text>
          </g>
        );
      })}
    </svg>
    {onSelectState && <span className="fsm-graph__hint">点状态预览 · 可达状态执行</span>}
    {showSafety && safetyEdges.map(t => <button type="button" key={t.trigger} className="fsm-safety-edge"
      onClick={() => fire(t.trigger)} aria-label={`${t.label}，${views[current]?.label || current}到${views[t.dst]?.label || t.dst}`}>
      <span>{views[current]?.label || current}</span><b>── {t.label} ──▶</b><span>{views[t.dst]?.label || t.dst}</span>
    </button>)}
    </div>
  );
}
window.FsmGraph = FsmGraph;
