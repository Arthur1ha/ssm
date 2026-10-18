/* FsmGraph — 面向用户的状态地图。
 * 节点负责解释「这是什么状态」，转换按钮负责表达「如何到达」。
 * 点击节点只预览，避免把探索状态机误当成执行命令。
 */
function FsmStateVisual({ state, view = {}, compact = false, active = false }) {
  const type = view.type || 'generic';
  const color = view.color || 'currentColor';
  const intensity = view.intensity ?? 0.7;

  if (type === 'light_ring') {
    const palette = view.palette || [color];
    return (
      <div className={`fsm-light ${compact ? 'is-compact' : ''} ${active ? 'is-live' : ''} motion-${view.motion || 'still'}`}
        style={{ '--visual-color': color, '--visual-intensity': intensity }} aria-hidden="true">
        {[0,1,2,3,4,5,6,7,8,9,10,11].map(n => (
          <i key={n} style={{ '--dot-index': n, '--dot-color': palette[n % palette.length] }}/>
        ))}
        <span/>
      </div>
    );
  }

  if (type === 'robot_pose') {
    return (
      <div className={`fsm-robot pose-${view.pose || 'standing'} ${compact ? 'is-compact' : ''} ${active ? 'is-live' : ''}`}
        style={{ '--visual-color': color }} aria-hidden="true">
        <span className="fsm-robot__head"/><span className="fsm-robot__body"/>
        <i/><i/><i/><i/>
        {view.pose === 'moving' && <b>››</b>}
      </div>
    );
  }

  return (
    <div className={`fsm-generic-visual ${compact ? 'is-compact' : ''}`} style={{ '--visual-color': color }} aria-hidden="true">
      <span>{view.symbol || String(state || '—').slice(0, 1).toUpperCase()}</span>
    </div>
  );
}

function FsmGraph({ states = [], transitions = [], current = '', onFire, color = '#C8FF3E', views = {} }) {
  const { useEffect, useMemo, useState } = React;
  const [selected, setSelected] = useState(current || states[0] || '');

  useEffect(() => {
    if (current) setSelected(current);
  }, [current]);

  const outgoing = useMemo(
    () => transitions.filter(t => t.src === current),
    [transitions, current],
  );
  const reachable = useMemo(() => new Set(outgoing.map(t => t.dst)), [outgoing]);
  const selectedTransition = outgoing.find(t => t.dst === selected);
  const view = views[selected] || {};
  const label = view.label || String(selected || '等待状态').replaceAll('_', ' ');
  const description = view.description || (selected === current
    ? '这是设备当前正在保持的状态。'
    : '选择状态可以查看含义；只有可达状态才能直接切换。');

  if (states.length === 0) return null;

  return (
    <div className="fsm-experience" style={{ '--fsm-color': color }}>
      <section className="fsm-preview" aria-live="polite">
        <div className="fsm-preview__visual">
          <FsmStateVisual state={selected} view={{ color, ...view }} active={selected === current}/>
        </div>
        <div className="fsm-preview__copy">
          <div className="fsm-preview__eyebrow">{selected === current ? '设备现在' : '状态预览'}</div>
          <strong>{label}</strong>
          <p>{description}</p>
          <code>{selected}</code>
        </div>
        <div className="fsm-preview__action">
          {selected === current ? (
            <span className="fsm-current-badge"><i/>当前状态</span>
          ) : selectedTransition ? (
            <button onClick={() => onFire && onFire(selectedTransition.trigger)}>
              {selectedTransition.label}<span>→</span>
            </button>
          ) : (
            <span className="fsm-unreachable">当前无法直接切换</span>
          )}
        </div>
      </section>

      <div className="fsm-map-title"><span>状态地图</span><small>点击卡片查看状态含义</small></div>
      <div className="fsm-state-map" role="list" aria-label="设备状态">
        {states.map(state => {
          const stateView = views[state] || {};
          const isCurrent = state === current;
          const isReachable = reachable.has(state);
          const isSelected = state === selected;
          return (
            <button key={state} role="listitem" onClick={() => setSelected(state)}
              className={`${isCurrent ? 'is-current ' : ''}${isReachable ? 'is-reachable ' : ''}${isSelected ? 'is-selected' : ''}`}
              aria-pressed={isSelected}>
              <FsmStateVisual state={state} view={{ color, ...stateView }} compact active={isCurrent}/>
              <span>{stateView.label || String(state).replaceAll('_', ' ')}</span>
              <code>{state}</code>
              {isCurrent && <i className="fsm-state-map__live"/>}
            </button>
          );
        })}
      </div>

      {outgoing.length > 0 && (
        <div className="fsm-routes" aria-label="当前可用转换">
          {outgoing.map(t => (
            <button key={`${t.trigger}-${t.dst}`}
              onClick={() => t.dst === current ? (onFire && onFire(t.trigger)) : setSelected(t.dst)}
              className={selected === t.dst ? 'is-selected' : ''}>
              <span>{views[t.dst]?.label || String(t.dst).replaceAll('_', ' ')}</span>
              <small>{t.label}<b>→</b></small>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

window.FsmGraph = FsmGraph;
