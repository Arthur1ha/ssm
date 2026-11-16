/* Preset expressions share one small SVG rig; animation is presentation only. */
function ShapeFace({agent, unitId, shape = 'idle', expression, emotion = 'neutral', behavior = 'idle',
  motion, animationKey = null, animated = false, size = 64, label, className = ''}) {
  const profile = resolveAvatarProfile(agent || unitId);
  const body = AVATAR_SHAPES[shape] || AVATAR_SHAPES.idle;
  const mood = normalizeAvatarEmotion(expression || emotion);
  const face = AVATAR_EXPRESSIONS[mood];
  const ref = React.useRef(null);
  const [visible, setVisible] = React.useState(true);
  const [awake, setAwake] = React.useState(!document.hidden);
  const [blinkPeriod] = React.useState(() => 3 + Math.random() * 4);
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
  const playing = animated && visible && awake;
  const motionName = animationKey != null && AVATAR_MOTIONS[motion || face.motion] ? (motion || face.motion) : 'none';
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
    setEntrance({id:animationKey, motion:motionName});
  }, [animationKey, animated]);
  React.useLayoutEffect(() => {
    if (!entrance || !form.current) return;
    // Restart the entrance without remounting the face layers: their opacity
    // transitions must survive a new reply or a game gesture.
    form.current.style.animation = 'none';
    form.current.getBoundingClientRect();
    form.current.style.animation = '';
  }, [entrance]);
  return <svg ref={ref} className={`shape-face ${className}`} viewBox="0 0 100 100" width={size} height={size}
    role={label ? 'img' : undefined} aria-label={label || undefined} aria-hidden={label ? undefined : true}
    focusable="false" data-shape={AVATAR_SHAPES[shape] ? shape : 'idle'} data-emotion={mood}
    data-behavior={behavior} data-animated={animated ? 'true' : 'false'} data-paused={playing ? undefined : 'true'}
    style={{'--avatar-color':profile.color, '--avatar-ink':profile.ink, '--avatar-blink-period':`${blinkPeriod}s`}}>
    <g className="shape-face__ambient"><g ref={form} className="shape-face__form" data-motion={entrance?.motion || 'none'}>
      <g className="shape-face__pose">
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
      </g>
    </g></g>
  </svg>;
}

/* Activity events have a browser-only receive marker set by the existing SSE
 * handler. REST history never gets it. One hook per activity, no subscriptions. */
function useAvatarPresentation({events = [], activityId, active, connected}) {
  const [views, setViews] = React.useState({});
  const seen = React.useRef(new Set(events.map(event => event.event_id)));
  const started = React.useRef(Date.now());
  const latest = React.useRef({});
  const timers = React.useRef(new Set());
  const connectionStart = typeof apiClient !== 'undefined' ? apiClient.eventsOpenedAt || 0 : 0;
  React.useEffect(() => {
    started.current = Date.now();
    latest.current = {};
    seen.current = new Set(events.map(event => event.event_id));
    setViews({});
    return () => {timers.current.forEach(clearTimeout); timers.current.clear();};
  }, [activityId, active, connected, connectionStart]);
  React.useEffect(() => {
    if (!active || !connected) return;
    const now = Date.now();
    const incoming = [];
    for (const event of events) {
      if (!event.event_id || seen.current.has(event.event_id)) continue;
      seen.current.add(event.event_id);
      if (seen.current.size > 640) seen.current.delete(seen.current.values().next().value);
      if (event.activity_id !== activityId || event.avatarReceivedAt < started.current || !event.avatarReceivedAt) continue;
      if (event.ts * 1000 < Math.max(started.current, connectionStart)) continue;
      if (!Number.isFinite(event.ts) || event.ts * 1000 < now - 10000 || event.ts * 1000 > now + 5000) continue;
      incoming.push(event);
    }
    if (!incoming.length) return;
    {
      const next = {...views};
      incoming.forEach(event => {
        const unit = event.unit_id || event.agent_id;
        const order = event.ts;
        const update = (id, field, value, duration) => {
          const key = `${id}:${field}`;
          if (order < (latest.current[key] || 0)) return;
          latest.current[key] = order;
          next[id] = {...next[id], [field]:{...value, eventId:event.event_id, until:now + duration}};
          const timer = setTimeout(() => {
            timers.current.delete(timer);
            setViews(current => current[id]?.[field]?.eventId === event.event_id
              ? {...current, [id]:{...current[id], [field]:null}} : current);
          }, duration);
          timers.current.add(timer);
        };
        if (unit && ['message','question'].includes(event.event_type) && 'emotion' in event)
          update(unit, 'emotion', {value:normalizeAvatarEmotion(event.emotion)}, AVATAR_TIMING.emotion);
        if (event.stage === 'rps_result') Object.keys(event.game_round?.choices || {}).forEach(id => {
          const gesture = avatarEventPresentation(event, id);
          if (gesture) update(id, 'gesture', {...gesture,
            expression:next[id]?.emotion?.value || 'neutral'}, AVATAR_TIMING.gesture);
        });
      });
      setViews(next);
    }
  }, [events, activityId, active, connected, connectionStart]);
  return active && connected ? views : {};
}
window.ShapeFace = ShapeFace;
window.useAvatarPresentation = useAvatarPresentation;
