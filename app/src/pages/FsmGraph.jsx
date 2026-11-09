/* Card-owned spatial layout. State clicks delegate actions; edges inspect rules. */
function FsmGraph({ nodes:graphNodes, edges:graphEdges,
  selectedState, selectedLink, selectedDirection, onSelectState, onInspectTransition, onDismiss, color = 'var(--color-accent)', pendingCommand, diagram }) {
  const { useState, useRef, useEffect } = React;
  const [camera, setCamera] = useState({ x:0, y:0, zoom:1 });
  const svgRef = useRef(null);
  const gesture = useRef({ points:new Map(), moved:false, blockClick:false });
  const marker = useRef('fsm-arrow-' + Math.random().toString(36).slice(2));
  const {width,height} = stateGraphDimensions(graphNodes.length, diagram);
  const cx = width/2, cy = height/2;
  const radius = graphNodes.length === 1 ? 0 : graphNodes.length < 6 ? 188 : 244;
  const points = Object.fromEntries(graphNodes.map((node,index) => {
    const angle = index * Math.PI * 2 / graphNodes.length - (graphNodes.length === 2 ? Math.PI : Math.PI / 2);
    return [node.id,{x:diagram && node.layout ? node.layout.x*width : cx + radius*Math.cos(angle),
      y:diagram && node.layout ? node.layout.y*height : cy + Math.min(radius, height/2-56)*Math.sin(angle),
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
    const wheel = event => { event.preventDefault(); onDismiss?.(); zoomAt(Math.exp(-event.deltaY * .002), localPoint(event)); };
    svg.addEventListener('wheel', wheel, {passive:false});
    return () => svg.removeEventListener('wheel', wheel);
  }, [onDismiss]);
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
    if (!g.moved) onDismiss?.();
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
  const visibleEdges = stateGraphLinks(graphEdges);
  // Geometry stays fixed when switching direction; only the arrow changes.
  const routes = new Map(visibleEdges.map(edge => {
    const source = points[edge.src], target = points[edge.dst];
    if (!source || !target) return [edge.id,''];
    const dx = target.x-source.x, dy = target.y-source.y, length = Math.hypot(dx,dy) || 1;
    // Radial links keep separate corridors when straight. Bend only to avoid
    // a node; a mandatory bend can make adjacent spokes cross near their hub.
    const base = Math.min(28,length*.12);
    const candidates = [0,base,-base,base+32,-base-32,base+64,-base-64,base+104,-base-104,base+152,-base-152];
    const obstacles = bounds.filter(point => point !== source && point !== target);
    const route = candidates.map(bend => {
      const control = {x:(source.x+target.x)/2-dy/length*bend,
        y:(source.y+target.y)/2+dx/length*bend};
      const startLength = Math.hypot(control.x-source.x,control.y-source.y) || 1;
      const endLength = Math.hypot(target.x-control.x,target.y-control.y) || 1;
      const start = {x:source.x+(control.x-source.x)/startLength*(source.radius+2),
        y:source.y+(control.y-source.y)/startLength*(source.radius+2)};
      const end = {x:target.x-(target.x-control.x)/endLength*(target.radius+5),
        y:target.y-(target.y-control.y)/endLength*(target.radius+5)};
      let score = Math.abs(bend)*.04;
      for (let step=1; step<16; step++) {
        const t=step/16, u=1-t;
        const x=u*u*start.x+2*u*t*control.x+t*t*end.x;
        const y=u*u*start.y+2*u*t*control.y+t*t*end.y;
        for (const point of obstacles) score += Math.max(0,point.radius+10-Math.hypot(x-point.x,y-point.y))**2;
        if (x<8 || x>width-8 || y<8 || y>height-8) score += 1000;
      }
      return {bend,score,path:`M${start.x} ${start.y}Q${control.x} ${control.y} ${end.x} ${end.y}`};
    }).reduce((best,candidate) => candidate.score < best.score ? candidate : best);
    return [edge.id,route.path];
  }));
  const activate = callback => event => {
    if (event.type === 'keydown' && !['Enter',' '].includes(event.key)) return;
    if (event.type === 'keydown') event.preventDefault();
    if (!gesture.current.blockClick || event.type === 'keydown' || event.detail === 0) callback?.(event);
  };
  const anchorAt = event => {
    const element = event.currentTarget;
    if (!element?.getScreenCTM) return null;
    const keyboard = event.type === 'keydown' || event.detail === 0;
    const path = element.querySelector('.fsm-whiteboard__line');
    let point = path ? path.getPointAtLength(path.getTotalLength()/2) : {x:0,y:0};
    if (!keyboard) {
      const screen = svgRef.current.createSVGPoint();
      screen.x = event.clientX; screen.y = event.clientY;
      point = screen.matrixTransform(element.getScreenCTM().inverse());
    }
    return {element, point:{x:point.x,y:point.y}, keyboard};
  };
  const inspectedLink = visibleEdges.find(edge => edge.id === selectedLink);
  return <div className={`fsm-whiteboard${inspectedLink ? ' is-inspecting' : ''}`} style={{'--board-accent':color}}>
    <svg ref={svgRef} className="fsm-whiteboard__viewport" viewBox={`0 0 ${width} ${height}`}
      role="group" aria-label="设备状态机白板，可拖动和缩放"
      onClick={event => { if (!event.target.closest('[role="button"]') && !gesture.current.blockClick) onDismiss?.(); }}
      onPointerDown={pointerDown} onPointerMove={pointerMove} onPointerUp={pointerUp}
      onPointerCancel={pointerUp} onLostPointerCapture={pointerUp}>
      <defs><marker id={marker.current} markerWidth="8" markerHeight="8" refX="7" refY="4"
        orient="auto-start-reverse"><path d="M1 1L7 4L1 7" fill="none" stroke="currentColor" strokeWidth="1.3"/></marker></defs>
      <g transform={`translate(${cx+camera.x} ${cy+camera.y}) scale(${camera.zoom}) translate(${-cx} ${-cy})`}>
        {visibleEdges.map(edge => {
          const source = points[edge.src], target = points[edge.dst];
          if (!source || !target) return null;
          const selected = selectedLink === edge.id;
          const direction = stateLinkDirection(edge,graphNodes,selected ? selectedDirection : null);
          const rules = direction.edges;
          const representative = rules.find(rule => rule.transition.action) || rules[0];
          const transition = representative.transition;
          const disabled = !rules.some(rule => rule.active && !rule.reason);
          const automatic = rules.every(rule => !rule.transition.action);
          const running = pendingCommand && rules.some(rule => [rule.transition.trigger,rule.transition.action].includes(pendingCommand));
          const path = routes.get(edge.id);
          const focused = selectedState && (edge.src === selectedState || edge.dst === selectedState);
          const emphasized = selected || focused || (!selectedState && !selectedLink && points[direction.src]?.node.current && !disabled);
          const title = edge.directions.length > 1 ? `${source.node.label} ↔ ${target.node.label}`
            : `${points[direction.src].node.label} → ${points[direction.dst].node.label}`;
          const inspect = activate(event => onInspectTransition?.(edge,direction,anchorAt(event)));
          const reverse = direction.src !== edge.src;
          return <g key={edge.id} className={`fsm-whiteboard__edge${disabled ? ' is-disabled' : ' is-available'}${automatic ? ' is-automatic' : ''}${running ? ' is-running' : ''}${emphasized ? ' is-emphasized' : ''}${selected ? ' is-selected' : ''}`}
            data-action={transition.action || undefined} data-trigger={transition.trigger} data-label={transition.label}
            data-source={direction.src} data-target={direction.dst} data-link={edge.id} data-direction={direction.id}
            data-rule-count={rules.length}
            role="button" tabIndex="0" aria-pressed={selected}
            data-available={transition.action ? rules.some(rule => rule.transition.action && rule.active && !rule.reason) : !disabled}
            aria-label={`${title}，查看转换规则`}
            onClick={inspect} onKeyDown={inspect}>
            <title>{title}</title>
            <path d={path} className="fsm-whiteboard__hit"/>
            <path d={path} className="fsm-whiteboard__line"
              markerStart={reverse ? `url(#${marker.current})` : undefined}
              markerEnd={reverse ? undefined : `url(#${marker.current})`}/>
          </g>;
        })}
        {graphNodes.map(node => {
          const p = points[node.id];
          const selected = selectedState === node.id;
          const nodeRadius = p.radius;
          const label = node.label;
          const targetAction = node.control?.transition;
          const actionable = !!targetAction || node.control?.choices?.some(edge => !edge.reason);
          const reachable = node.control?.reachable || actionable;
          const related = inspectedLink && [inspectedLink.src,inspectedLink.dst].includes(node.id);
          const nodeHint = targetAction ? '点击执行' : node.current
            ? '当前状态，查看说明' : reachable ? '查看转换条件' : node.control?.reason || '查看说明';
          const lines = label.length > 4 ? [label.slice(0,4),label.slice(4)] : [label];
          const select = activate(event => onSelectState?.(node.id, anchorAt(event)));
          return <g key={node.id} className={`fsm-whiteboard__node${node.current ? ' is-current' : ''}${actionable ? ' is-actionable' : ''}${reachable ? ' is-reachable' : ''}${related ? ' is-related' : ''}${node.retained ? ' is-retained' : ''}${selected ? ' is-selected' : ''}`}
            transform={`translate(${p.x} ${p.y})`} role="button" tabIndex="0"
            data-state={node.id} data-group={node.group} data-radius={nodeRadius}
            data-executable={!!targetAction} data-reachable={!!reachable} aria-label={`${label}，${nodeHint}`} aria-pressed={selected}
            onClick={select} onKeyDown={select}>
            <title>{label}</title>
            <circle r={nodeRadius+7} fill="transparent"/>
            {(node.current || selected) && <circle r={nodeRadius+7} className="fsm-whiteboard__halo"/>}
            <circle r={nodeRadius} className="fsm-whiteboard__circle"/>
            {lines.map((line,index) => <text key={index} y={(index-(lines.length-1)/2)*21} className="fsm-whiteboard__node-label">{line}</text>)}
          </g>;
        })}
      </g>
    </svg>
  </div>;
}
window.FsmGraph = FsmGraph;
