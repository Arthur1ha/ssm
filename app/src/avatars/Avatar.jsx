/* Preset expressions share one small SVG rig; animation is presentation only. */
function ShapeFace({agent, unitId, shape = 'idle', expression, emotion = 'neutral', behavior = 'idle',
  motion, animationKey = null, animationStartedAt, motionDurationMs, motionEpoch,
  lightingState, lightingColor, lightingBrightness,
  animated = false, online, size = 64, label, className = ''}) {
  // undefined: decorative/history avatar; null: connection is being synced.
  animated = animated && online !== false && online !== null;
  const profile = resolveAvatarProfile(agent || unitId);
  const body = AVATAR_SHAPES[shape] || AVATAR_SHAPES.idle;
  const mood = normalizeAvatarEmotion(expression || emotion);
  const face = AVATAR_EXPRESSIONS[mood];
  const ref = React.useRef(null);
  const [visible, setVisible] = React.useState(true);
  const [awake, setAwake] = React.useState(!document.hidden);
  const identity = unitId || (typeof agent === 'string' ? agent : agent?.unit_id || agent?.agent_id);
  const blinkPeriod = React.useMemo(() => {
    const hash = Array.from(identity || 'companion').reduce((value, char) => (value * 31 + char.charCodeAt(0)) >>> 0, 0);
    return 3.8 + hash % 25 / 10;
  }, [identity]);
  const [renderedSize, setRenderedSize] = React.useState(size);
  React.useLayoutEffect(() => {
    const update = () => {
      const width = ref.current?.getBoundingClientRect().width;
      if (width > 0) setRenderedSize(previous => Math.abs(previous - width) > .2 ? width : previous);
    };
    update();
    const observer = typeof ResizeObserver === 'function' ? new ResizeObserver(update) : null;
    if (ref.current) observer?.observe(ref.current);
    return () => observer?.disconnect();
  }, [size]);
  const playing = animated && visible && awake;
  const phase = React.useMemo(() => {
    const elapsed = Number.isFinite(motionEpoch) ? Math.max(0, Date.now() - motionEpoch) : 0;
    const ambientPeriod = behavior === 'rest' ? 6000 : 4000;
    return {ambient:-(elapsed % ambientPeriod), blink:-(elapsed % (blinkPeriod * 1000)), light:-(elapsed % 1200),
      behavior:-(elapsed % (behavior === 'working' ? 1800 : behavior === 'play' ? 3000 : 4000))};
  }, [motionEpoch, behavior, blinkPeriod]);
  React.useLayoutEffect(() => {
    if (!playing || !Number.isFinite(motionEpoch) || !ref.current) return;
    // Offscreen/hidden loops pause for efficiency, then rejoin the shared clock.
    // Only continuous layers seek here; event entrances keep their own timestamp.
    const loops = new Set(['avatar-breathe','avatar-blink','avatar-look','avatar-scan',
      'avatar-explore','avatar-work','avatar-engaged','avatar-light']);
    for (const animation of ref.current.getAnimations({subtree:true})) {
      if (!loops.has(animation.animationName)) continue;
      const delay = Number(animation.effect?.getTiming().delay) || 0;
      animation.currentTime = Math.max(0, Date.now() - motionEpoch + delay);
    }
  }, [playing, motionEpoch, behavior, mood]);
  const previousPresence = React.useRef({identity, online});
  React.useEffect(() => {
    const previous = previousPresence.current;
    previousPresence.current = {identity, online};
    if (previous.identity !== identity || previous.online !== false || online !== true
        || !animated || !visible || document.hidden
        || window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    const transition = ref.current?.animate([
      {transform:'scale(.96)'}, {transform:'translateY(-2px) scale(1.03)', offset:.45}, {transform:'none'},
    ], {duration:300, easing:'ease-out'});
    return () => transition?.cancel();
  }, [online, identity]);
  React.useEffect(() => {
    if (!animated) return;
    const media = window.matchMedia('(prefers-reduced-motion: reduce)');
    const update = () => setAwake(!document.hidden && !media.matches);
    update();
    document.addEventListener('visibilitychange', update);
    media.addEventListener('change', update);
    const observer = typeof IntersectionObserver === 'function'
      ? new IntersectionObserver(entries => setVisible(entries[0].isIntersecting)) : null;
    if (ref.current) observer?.observe(ref.current);
    return () => {observer?.disconnect(); document.removeEventListener('visibilitychange', update); media.removeEventListener('change', update);};
  }, [animated]);
  const motionName = animationKey != null && AVATAR_MOTIONS[motion || face.motion] ? (motion || face.motion) : 'none';
  const duration = Number.isFinite(motionDurationMs) && motionDurationMs > 0 ? motionDurationMs
    : (AVATAR_TIMING.motions[motionName] || 0);
  const resting = behavior === 'rest';
  const played = React.useRef(new Set());
  const form = React.useRef(null);
  const [entrance, setEntrance] = React.useState(null);
  React.useEffect(() => {
    if (!animated || animationKey == null || played.current.has(animationKey)) {
      setEntrance(null); return;
    }
    played.current.add(animationKey);
    if (played.current.size > 64) played.current.delete(played.current.values().next().value);
    const startedAt = Number.isFinite(animationStartedAt) ? animationStartedAt : Date.now();
    if (!duration || Date.now() - startedAt >= duration) { setEntrance(null); return; }
    setEntrance({id:animationKey, motion:motionName, startedAt, duration});
    const timer = setTimeout(() => setEntrance(current => current?.id === animationKey ? null : current),
      Math.max(0, duration - (Date.now() - startedAt)));
    return () => clearTimeout(timer);
  }, [animationKey, animated, animationStartedAt, duration, motionName]);
  React.useLayoutEffect(() => {
    if (!entrance || !form.current) return;
    // Restart the entrance without remounting the face layers: their opacity
    // transitions must survive a new reply or a game gesture.
    form.current.style.animation = 'none';
    form.current.style.setProperty('--avatar-motion-delay', `${-Math.max(0, Date.now() - entrance.startedAt)}ms`);
    form.current.style.setProperty('--avatar-motion-duration', `${entrance.duration}ms`);
    form.current.getBoundingClientRect();
    form.current.style.animation = '';
  }, [entrance, playing]);
  return <svg ref={ref} className={`shape-face ${className}`} viewBox="0 0 100 100" width={size} height={size}
    role={label ? 'img' : undefined} aria-label={label || undefined} aria-hidden={label ? undefined : true}
    focusable="false" data-shape={AVATAR_SHAPES[shape] ? shape : 'idle'} data-emotion={mood}
    data-online={typeof online === 'boolean' ? String(online) : undefined}
    data-lighting={lightingState || undefined}
    data-behavior={behavior} data-animated={animated ? 'true' : 'false'} data-paused={playing ? undefined : 'true'}
    style={{'--avatar-color':profile.color, '--avatar-ink':profile.ink, '--avatar-blink-period':`${blinkPeriod}s`,
      '--avatar-motion-scale':Math.max(.85, Math.min(1.85, 64 / renderedSize)),
      '--avatar-light-color':lightingColor || 'transparent',
      '--avatar-light-opacity':Number.isFinite(lightingBrightness) ? Math.max(0, Math.min(255, lightingBrightness)) / 255 * .3 : .22,
      '--avatar-ambient-delay':`${phase.ambient}ms`, '--avatar-blink-delay':`${phase.blink}ms`,
      '--avatar-behavior-delay':`${phase.behavior}ms`, '--avatar-light-delay':`${phase.light}ms`}}>
    {lightingColor && ['ON','BLINKING'].includes(lightingState) && online !== false && online !== null
      && <ellipse className="shape-face__light" cx="50" cy="85" rx="35" ry="7"/>}
    <g className="shape-face__ambient"><g ref={form} className="shape-face__form" data-motion={entrance?.motion || 'none'}>
      <g className="shape-face__behavior"><g className="shape-face__pose">
        <path className="shape-face__body" d={body[profile.silhouette]}/>
        {body.detail && <path className="shape-face__features" d={body.detail}/>}
        <g className="shape-face__features" transform={`translate(0 ${body.faceY})`}>
          {Object.entries(AVATAR_EXPRESSIONS).map(([name, preset]) => {
            const eyes = resting ? 'closed' : preset.eyes;
            const height = eyes === 'wide' ? 7 : ['narrow','low','soft'].includes(eyes) ? 2.7 : 4.5;
            return <g key={name} className="shape-face__expression" data-current={name === mood ? 'true' : 'false'}>
              {preset.brows && !resting && <path className="shape-face__brows" d={preset.brows}/>}
              <g className="shape-face__eyes" data-closed={resting || eyes === 'smile' ? 'true' : undefined}>
                {eyes === 'closed' || eyes === 'smile' ? <>
                  <path d={eyes === 'smile' ? 'M 32 -2 Q 37 -10 42 -2' : 'M 32 -2 Q 37 1 42 -2'}/>
                  <path d={eyes === 'smile' ? 'M 58 -2 Q 63 -10 68 -2' : 'M 58 -2 Q 63 1 68 -2'}/>
                </> : <g className="shape-face__gaze">
                  <ellipse className="shape-face__eye" cx="37" cy={eyes === 'low' ? '1' : '-2'} rx="3" ry={height}/>
                  <ellipse className="shape-face__eye" cx="63" cy={eyes === 'curious' ? '-5' : '-2'} rx={eyes === 'uneven' ? '2' : '3'} ry={eyes === 'curious' ? '6' : height}/>
                </g>}
              </g>
              <path className="shape-face__mouth" d={resting ? AVATAR_EXPRESSIONS.relaxed.mouth : preset.mouth}
                fill={!resting && ['happy','surprised'].includes(name) ? 'var(--avatar-ink)' : 'none'}/>
              {name === 'happy' && !resting && <g className="shape-face__cheeks"><path d="M 25 8 L 29 9 M 71 9 L 75 8"/></g>}
            </g>;
          })}
        </g>
      </g></g>
    </g></g>
  </svg>;
}

window.ShapeFace = ShapeFace;
