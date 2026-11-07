/* Card-owned spatial layout. Target states control; edges explain transitions. */
function FsmGraph({ nodes, edges, states = [], transitions = [], current = '', views = {},
  selectedState, onSelectState, onInspectTransition, color = 'var(--color-accent)', pendingCommand, diagram }) {
  const { useState, useRef, useEffect } = React;
  const [camera, setCamera] = useState({ x:0, y:0, zoom:1 });
  const [allEdges, setAllEdges] = useState(true);
  const svgRef = useRef(null);
  const gesture = useRef({ points:new Map(), moved:false, blockClick:false });
  const marker = useRef('fsm-arrow-' + Math.random().toString(36).slice(2));
  const graphNodes = nodes || states.map(value => ({id:value,value,label:views[value]?.label || value,
    view:views[value],current:value === current}));
  const graphEdges = edges || transitions.filter(edge => !edge.safety && !edge.graph_hidden)
    .map((transition,index) => ({...transition,id:String(index),transition,active:transition.src === current}));
  const width = diagram?.width || 600, height = diagram?.height || 600;
  const cx = width/2, cy = height/2;
  const radius = graphNodes.length === 1 ? 0 : graphNodes.length < 6 ? 188 : 244;
  const points = Object.fromEntries(graphNodes.map((node,index) => {
    const angle = index * Math.PI * 2 / graphNodes.length - Math.PI / 2;
    return [node.id,{x:diagram && node.layout ? node.layout.x*width : cx + radius*Math.cos(angle),
      y:diagram && node.layout ? node.layout.y*height : cy + radius*Math.sin(angle),
      radius:node.radius || 39, node}];
  }));
  const bounds = Object.values(points);
  const left = Math.min(0,...bounds.map(p => p.x-p.radius-8));
  const right = Math.max(width,...bounds.map(p => p.x+p.radius+8));
  const top = Math.min(0,...bounds.map(p => p.y-p.radius-8));
  const bottom = Math.max(height,...bounds.map(p => p.y+p.radius+8));
  const fitZoom = Math.min(1,width/(right-left),height/(bottom-top));
  const fit = {zoom:fitZoom,x:(cx-(left+right)/2)*fitZoom,y:(cy-(top+bottom)/2)*fitZoom};
  const signature = JSON.stringify([width,height,...graphNodes.map(node => [node.id,points[node.id].x,points[node.id].y,points[node.id].radius])]);
  useEffect(() => { setCamera(fit); }, [signature]);
  const zoomAt = (factor, point = {x:cx,y:cy}) => setCamera(old => {
    const zoom = Math.max(.25, Math.min(3.5, old.zoom * factor));
    const ratio = zoom / old.zoom;
    return { zoom, x:point.x - cx - (point.x - cx - old.x) * ratio,
      y:point.y - cy - (point.y - cy - old.y) * ratio };
  });
  const localPoint = event => {
    const svg = svgRef.current;
    const point = svg.createSVGPoint();
    point.x = event.clientX; point.y = event.clientY;
    return point.matrixTransform(svg.getScreenCTM().inverse());
  };
  useEffect(() => {
    const svg = svgRef.current;
    if (!svg) return;
    const wheel = event => { event.preventDefault(); zoomAt(Math.exp(-event.deltaY * .002), localPoint(event)); };
    svg.addEventListener('wheel', wheel, {passive:false});
    return () => svg.removeEventListener('wheel', wheel);
  }, []);
  const pointerDown = event => {
    if (event.button != null && event.button !== 0) return;
    const g = gesture.current;
    if (!g.points.size) { g.moved = false; g.blockClick = false; }
    g.points.set(event.pointerId, {x:event.clientX,y:event.clientY});
    event.target.setPointerCapture?.(event.pointerId);
  };
  const pointerMove = event => {
    const g = gesture.current;
    const previous = g.points.get(event.pointerId);
    if (!previous) return;
    const next = {x:event.clientX,y:event.clientY};
    const dx = next.x - previous.x, dy = next.y - previous.y;
    if (!g.moved && Math.hypot(dx,dy) < 4) return;
    g.moved = true; g.blockClick = true;
    if (g.points.size === 2) {
      const other = [...g.points.entries()].find(([id]) => id !== event.pointerId)[1];
      const oldDistance = Math.hypot(previous.x-other.x,previous.y-other.y);
      const distance = Math.hypot(next.x-other.x,next.y-other.y);
      const center = localPoint({clientX:(next.x+other.x)/2,clientY:(next.y+other.y)/2});
      if (oldDistance > 0) zoomAt(distance/oldDistance,center);
    }
    const scale = 1 / Math.min(svgRef.current.clientWidth/width,svgRef.current.clientHeight/height);
    const divisor = g.points.size;
    setCamera(old => ({...old,x:old.x+dx*scale/divisor,y:old.y+dy*scale/divisor}));
    g.points.set(event.pointerId,next);
  };
  const pointerUp = event => { gesture.current.points.delete(event.pointerId); };
  const isMain = edge => points[edge.src]?.node.group && points[edge.src].radius > 40
    && points[edge.dst]?.radius > 40;
  const visibleEdges = graphEdges.filter(edge => {
    if (diagram && edge.src === edge.dst && !edge.active && selectedState !== edge.src) return false;
    if (diagram && !edge.transition.action && graphEdges.some(other => other.transition.action
      && other.src === edge.src && other.dst === edge.dst)) return false;
    const returnObservation = diagram && !edge.transition.action && points[edge.dst]?.node.current
      && points[edge.src]?.node.group === points[edge.dst]?.node.group;
    return allEdges || diagram && isMain(edge) || edge.active || returnObservation
      || selectedState && (edge.src === selectedState || edge.dst === selectedState);
  });
  const labelBounds = [];
  const loopCounts = new Map();
  const activate = callback => event => {
    if (event.type === 'keydown' && !['Enter',' '].includes(event.key)) return;
    if (event.type === 'keydown') event.preventDefault();
    if (!gesture.current.blockClick || event.type === 'keydown' || event.detail === 0) callback?.();
  };
  return <div className="fsm-whiteboard" style={{'--board-accent':color}}>
    <svg ref={svgRef} className="fsm-whiteboard__viewport" viewBox={`0 0 ${width} ${height}`}
      role="group" aria-label="设备状态机白板，可拖动和缩放"
      onPointerDown={pointerDown} onPointerMove={pointerMove} onPointerUp={pointerUp}
      onPointerCancel={pointerUp} onLostPointerCapture={pointerUp}>
      <defs><marker id={marker.current} markerWidth="8" markerHeight="8" refX="7" refY="4"
        orient="auto-start-reverse"><path d="M1 1L7 4L1 7" fill="none" stroke="currentColor" strokeWidth="1.3"/></marker></defs>
      <g transform={`translate(${cx+camera.x} ${cy+camera.y}) scale(${camera.zoom}) translate(${-cx} ${-cy})`}>
        {(diagram?.sections || []).map((section,index) => <g key={section.label} className="fsm-whiteboard__section" aria-label={section.label}>
          {index > 0 && <line x1="28" x2={width-28} y1={section.top-24} y2={section.top-24}/>}
          <text x="30" y={section.top}>{section.label}</text>
          {section.hint && <text x={width-30} y={section.top} textAnchor="end" className="fsm-whiteboard__hint">{section.hint}</text>}
        </g>)}
        {visibleEdges.map((edge,index) => {
          const source = points[edge.src], target = points[edge.dst];
          if (!source || !target) return null;
          const transition = edge.transition;
          const actionable = !!transition.action && edge.active;
          const disabled = !actionable || !!edge.reason;
          const running = pendingCommand && [transition.trigger,transition.action].includes(pendingCommand);
          const dx = target.x-source.x, dy = target.y-source.y, length = Math.hypot(dx,dy) || 1;
          const self = edge.src === edge.dst;
          const bend = diagram ? (Math.abs(dx)<20 ? 160 : 74) : (length < 190 ? 95 : 30) + (index % 3) * 12;
          const control = {x:(source.x+target.x)/2 - dy/length*bend,
            y:(source.y+target.y)/2 + dx/length*bend};
          const sx = source.x+dx/length*(source.radius+1), sy = source.y+dy/length*(source.radius+1);
          const endLength = Math.hypot(target.x-control.x,target.y-control.y) || 1;
          const ex = target.x-(target.x-control.x)/endLength*(target.radius+3);
          const ey = target.y-(target.y-control.y)/endLength*(target.radius+3);
          const loopLane = loopCounts.get(edge.src) || 0;
          if (self) loopCounts.set(edge.src,loopLane+1);
          const inwardLength = Math.hypot(cx-source.x,cy-source.y) || 1;
          const ux = (cx-source.x)/inwardLength;
          const uy = inwardLength > 1 ? (cy-source.y)/inwardLength : 1;
          const depth = 112 + loopLane*48;
          let path = self ? `M${source.x+ux*26-uy*30} ${source.y+uy*26+ux*30} C${source.x+ux*depth-uy*86} ${source.y+uy*depth+ux*86} ${source.x+ux*depth+uy*86} ${source.y+uy*depth-ux*86} ${source.x+ux*26+uy*30} ${source.y+uy*26-ux*30}`
            : `M${sx} ${sy}Q${control.x} ${control.y} ${ex} ${ey}`;
          const caption = `${transition.label}${running ? ' · 执行中' : ''}`;
          const labelWidth = Math.max(54,caption.length*16+16);
          // Choose a clear spot along the real curve, avoiding nodes and other
          // captions instead of stacking the whole fan of actions at its hub.
          const candidates = self ? [{x:source.x+ux*depth*.8,y:source.y+uy*depth*.8}]
            : [.74,.62,.50,.38,.86,.26].map(t => ({
              x:(1-t)**2*sx+2*(1-t)*t*control.x+t*t*ex,
              y:(1-t)**2*sy+2*(1-t)*t*control.y+t*t*ey}));
          const scored = candidates.map(point => {
            const box = {left:point.x-labelWidth/2,right:point.x+labelWidth/2,top:point.y-16,bottom:point.y+16};
            const hits = Object.values(points).filter(p => Math.hypot(
              p.x-Math.max(box.left,Math.min(p.x,box.right)),
              p.y-Math.max(box.top,Math.min(p.y,box.bottom))) < 47).length;
            const overlaps = labelBounds.filter(other => box.left<other.right+6 && box.right>other.left-6
              && box.top<other.bottom+6 && box.bottom>other.top-6).length;
            return {...point,box,score:hits*3+overlaps*2+(box.left<0 || box.right>width || box.top<0 || box.bottom>height ? 10 : 0)};
          });
          let spot = scored.reduce((best,item) => item.score<best.score ? item : best);
          const detail = diagram && !isMain(edge);
          const focused = selectedState && (edge.src === selectedState || edge.dst === selectedState);
          const minor = detail && !self && (source.y > 390 || target.y > 390);
          const captionVisible = !diagram || !minor || focused || running;
          if (diagram && self) {
            path = `M${source.x+source.radius*.7} ${source.y-23} C${source.x+source.radius+100} ${source.y-80} ${source.x+source.radius+100} ${source.y+80} ${source.x+source.radius*.7} ${source.y+23}`;
            spot = {x:source.x+source.radius+62,y:source.y};
          } else if (minor) {
            // Route through gaps between columns, including return paths,
            // so the lower rows never cut across another state circle.
            const upper = source.y < target.y ? source : target;
            const lower = source.y < target.y ? target : source;
            const lane = (upper.x === lower.x ? upper.x+90 : (upper.x+lower.x)/2)
              + (source.y < target.y ? -4 : 4);
            const startSide = lane>source.x ? 1 : -1, endSide = lane>target.x ? 1 : -1;
            path = `M${source.x+startSide*(source.radius+2)} ${source.y} H${lane} V${target.y} H${target.x+endSide*(target.radius+3)}`;
            spot = {x:lane,y:(source.y+target.y)/2};
          }
          if (captionVisible) labelBounds.push({left:spot.x-labelWidth/2,right:spot.x+labelWidth/2,
            top:spot.y-18,bottom:spot.y+18});
          const lx = spot.x, ly = spot.y;
          const title = edge.reason || (!edge.active && transition.action ? '当前状态不可执行' : transition.description || transition.label);
          const inspect = activate(() => onInspectTransition?.(edge));
          return <g key={edge.id} className={`fsm-whiteboard__edge${disabled ? ' is-disabled' : ' is-available'}${!transition.action ? ' is-automatic' : ''}${running ? ' is-running' : ''}${minor && !focused && !running ? ' is-context' : ''}`}
            data-action={transition.action || undefined} data-trigger={transition.trigger} data-label={transition.label}
            role="button" tabIndex={captionVisible ? 0 : -1}
            data-available={transition.action ? !disabled : undefined}
            aria-label={`${transition.label}，查看转换说明${edge.reason ? '，'+edge.reason : ''}`}
            onClick={inspect} onKeyDown={inspect}>
            <title>{title}</title>
            <path d={path} className="fsm-whiteboard__hit"/>
            <path d={path} className="fsm-whiteboard__line" markerEnd={`url(#${marker.current})`}/>
            {captionVisible && <>
              <rect x={lx-labelWidth/2} y={ly-18} width={labelWidth} height="36" rx="12" className="fsm-whiteboard__label-hit"/>
              <text x={lx} y={ly} className="fsm-whiteboard__edge-label">{caption}</text>
            </>}
          </g>;
        })}
        {graphNodes.map(node => {
          const p = points[node.id];
          const selected = selectedState === node.id;
          const nodeRadius = p.radius;
          const label = node.label;
          const targetAction = node.control?.transition;
          const nodeHint = node.current ? '当前状态，查看说明' : targetAction
            ? `点击${targetAction.label}` : node.control?.reason || '查看说明';
          const lines = label.length > 4 ? [label.slice(0,4),label.slice(4)] : [label];
          const select = activate(() => onSelectState?.(node.id));
          return <g key={node.id} className={`fsm-whiteboard__node${node.current ? ' is-current' : ''}${targetAction ? ' is-actionable' : ''}${node.retained ? ' is-retained' : ''}${selected ? ' is-selected' : ''}`}
            transform={`translate(${p.x} ${p.y})`} role="button" tabIndex="0"
            data-state={node.id} data-group={node.group} data-radius={nodeRadius}
            data-executable={!!targetAction} aria-label={`${label}，${nodeHint}`} aria-pressed={selected}
            onClick={select} onKeyDown={select}>
            <title>{`${label}：${nodeHint}`}</title>
            <circle r={nodeRadius+7} fill="transparent"/>
            {(node.current || selected) && <circle r={nodeRadius+7} className="fsm-whiteboard__halo"/>}
            <circle r={nodeRadius} className="fsm-whiteboard__circle"/>
            {lines.map((line,index) => <text key={index} y={(index-(lines.length-1)/2)*21} className="fsm-whiteboard__node-label">{line}</text>)}
            {node.current && <circle cx="0" cy={nodeRadius-11} r="2.5" className="fsm-whiteboard__current-dot"/>}
          </g>;
        })}
      </g>
    </svg>
    <div className="fsm-whiteboard__toolbar">
      <button type="button" aria-pressed={allEdges} onClick={() => setAllEdges(value => !value)}>{allEdges ? '聚焦当前' : '全部连线'}</button>
      <div><button type="button" aria-label="缩小白板" onClick={() => zoomAt(1/1.2)}>−</button>
        <button type="button" onClick={() => setCamera(fit)}>适应</button>
        <button type="button" aria-label="放大白板" onClick={() => zoomAt(1.2)}>＋</button></div>
    </div>
  </div>;
}
window.FsmGraph = FsmGraph;
