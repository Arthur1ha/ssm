/* Agent Card 的 views[type] 决定状态预设图的渲染器。 */
function RobotPoseVisual({ pose, label }) {
  const variants = {
    offline:     { y: 68, angle: 0,  feet: [[52, 117], [92, 117], [143, 117], [181, 117]] },
    lying:       { y: 80, angle: 0,  feet: [[53, 116], [91, 116], [145, 116], [184, 116]] },
    standing:    { y: 48, angle: 0,  feet: [[52, 116], [91, 116], [145, 116], [184, 116]] },
    moving:      { y: 48, angle: 0,  feet: [[38, 116], [106, 108], [130, 108], [198, 116]] },
    greeting:    { y: 48, angle: 0,  feet: [[52, 116], [91, 116], [145, 116], [205, 24]] },
    stretching:  { y: 67, angle: -8, feet: [[48, 116], [87, 116], [153, 116], [199, 116]] },
    'dance-left':  { y: 48, angle: -13, feet: [[48, 116], [86, 107], [150, 116], [194, 72]] },
    'dance-right': { y: 48, angle: 13, feet: [[41, 78], [90, 116], [150, 107], [190, 116]] },
  };
  const cfg = variants[pose] || variants.standing;
  const roots = [68, 96, 145, 170];
  const muted = pose === 'offline';

  return (
    <svg className="device-pose" viewBox="0 0 240 140" role="img" aria-label={`${label}动作预设图`}>
      <path d="M18 118 H222" stroke="currentColor" strokeOpacity=".22" strokeWidth="1"/>
      <path d="M35 125 H76 M164 125 H204" stroke="currentColor" strokeOpacity=".1" strokeWidth="1"/>
      <g opacity={muted ? .35 : 1} transform={`rotate(${cfg.angle} 122 75)`}>
        {cfg.feet.map(([fx, fy], index) => (
          <g key={index}>
            <path d={`M${roots[index]} ${cfg.y + 27} L${fx} ${fy}`}
              stroke="currentColor" strokeWidth="9" strokeLinecap="round" opacity={index % 2 ? .65 : .9}/>
            <circle cx={fx} cy={fy} r="4" fill="currentColor"/>
          </g>
        ))}
        <rect x="55" y={cfg.y} width="125" height="39" rx="11"
          fill="currentColor" fillOpacity=".13" stroke="currentColor" strokeWidth="2"/>
        <path d={`M67 ${cfg.y + 9} H166`} stroke="currentColor" strokeOpacity=".35" strokeWidth="1"/>
        <path d={`M180 ${cfg.y + 4} l25 4 v24 l-25 4 z`}
          fill="currentColor" fillOpacity=".2" stroke="currentColor" strokeWidth="2"/>
        <circle cx="194" cy={cfg.y + 17} r="3" fill="currentColor"/>
        <path d={`M57 ${cfg.y + 10} l-12 -4`} stroke="currentColor" strokeWidth="5" strokeLinecap="round"/>
      </g>
      {pose === 'moving' && <path d="M15 63 H42 M20 72 H36" stroke="currentColor" strokeOpacity=".55" strokeWidth="2" strokeLinecap="round"/>}
      {(pose === 'dance-left' || pose === 'dance-right') && <path d="M42 34 l6 -10 6 10 M201 27 l5 -8 5 8" stroke="currentColor" strokeOpacity=".5" fill="none"/>}
    </svg>
  );
}

function StateVisual({ view, state, color }) {
  if (!view) return null;
  const label = view.label || state;
  let visual = null;
  if (view.type === 'robot_pose') visual = <RobotPoseVisual pose={view.pose} label={label}/>;
  if (view.type === 'light_ring') visual = <div className="device-light-preview" role="img" aria-label={`${label}灯光预览`}>
    <span style={{ '--light-color': view.color || '#fff', opacity: view.intensity ?? 1 }}/>
  </div>;
  const imageSrc = typeof view.src === 'string' && view.src.startsWith('/')
    && !view.src.startsWith('//') && !view.src.includes('\\')
    && !view.src.includes('#') && !view.src.split('?')[0].split('/').includes('..');
  if (view.type === 'image' && imageSrc) {
    visual = <img className="device-pose" src={view.src} alt={`${label}预设图`}/>;
  }
  if (!visual) visual = <div className="device-state-visual__description">{view.description || label}</div>;
  return (
    <div className="device-state-visual" style={{ color }}>
      <div className="device-state-visual__eyebrow">动作预设 · {label}</div>
      {visual}
      {view.description && <div className="device-state-visual__description">{view.description}</div>}
    </div>
  );
}
window.StateVisual = StateVisual;
