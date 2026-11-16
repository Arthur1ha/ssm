/* A whole silhouette changes shape; its independent face stays legible. */
function ShapeFace({agent, unitId, shape = 'idle', expression = 'neutral', motion = 'none',
  animationKey = null, size = 64, label, className = ''}) {
  const profile = resolveAvatarProfile(agent || unitId);
  const body = AVATAR_SHAPES[shape] || AVATAR_SHAPES.idle;
  const face = AVATAR_EXPRESSIONS[expression] || AVATAR_EXPRESSIONS.neutral;
  const motionName = animationKey != null && AVATAR_MOTIONS[motion] ? motion : 'none';
  const eyes = face.eyes;
  return <svg className={`shape-face ${className}`} viewBox="0 0 100 100" width={size} height={size}
    role={label ? 'img' : undefined} aria-label={label || undefined} aria-hidden={label ? undefined : true}
    focusable="false" data-shape={AVATAR_SHAPES[shape] ? shape : 'idle'}
    style={{'--avatar-color':profile.color, '--avatar-ink':profile.ink}}>
    <g className="shape-face__form" key={animationKey || 'still'} data-motion={motionName}>
      <path className="shape-face__body" d={body[profile.silhouette]}/>
      {body.detail && <path className="shape-face__features" d={body.detail}/>}
      <g className="shape-face__features" transform={`translate(0 ${body.faceY})`}>
        {eyes === 'closed' || eyes === 'smile'
          ? <><path d={eyes === 'smile' ? 'M 33 -2 Q 37 -8 41 -2' : 'M 33 -2 L 41 -2'}/>
            <path d={eyes === 'smile' ? 'M 59 -2 Q 63 -8 67 -2' : 'M 59 -2 L 67 -2'}/></>
          : <><ellipse className="shape-face__eye" cx="37" cy={eyes === 'curious' ? '-4' : '-2'} rx="3" ry="4.5"/>
            <ellipse className="shape-face__eye" cx="63" cy="-2" rx="3" ry={eyes === 'curious' ? '5.5' : '4.5'}/></>}
        <path d={face.mouth} fill={expression === 'happy' ? 'var(--avatar-ink)' : 'none'}/>
      </g>
    </g>
  </svg>;
}
window.ShapeFace = ShapeFace;
