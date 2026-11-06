function VideoCanvas({ connected, endpoint = '/api/go2/video' }) {
  const { useRef, useEffect } = React;
  const canvasRef  = useRef(null);
  const activeRef  = useRef(false);

  useEffect(() => {
    if (!connected) return;
    activeRef.current = true;

    const tick = async () => {
      if (!activeRef.current) return;
      try {
        const res = await fetch(`${endpoint}/snapshot?_=${Date.now()}`);
        if (res.ok) {
          const blob = await res.blob();
          const url  = URL.createObjectURL(blob);
          const img  = new Image();
          img.onload = () => {
            const c = canvasRef.current;
            if (c) c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
            URL.revokeObjectURL(url);
            if (activeRef.current) requestAnimationFrame(tick);
          };
          img.onerror = () => { URL.revokeObjectURL(url); if (activeRef.current) setTimeout(tick, 200); };
          img.src = url;
          return;
        }
      } catch (_) {}
      if (activeRef.current) setTimeout(tick, 200);
    };

    tick();
    return () => { activeRef.current = false; };
  }, [connected, endpoint]);

  return React.createElement("canvas", {
    ref: canvasRef, width: 640, height: 360,
    style: { width: "100%", height: "100%", display: "block" },
  });
}

function MapWidget({ connected, endpoint }) {
  const { useEffect, useRef, useState } = React;
  const [imageUrl, setImageUrl] = useState('');
  const [expanded, setExpanded] = useState(false);
  const triggerRef = useRef(null);
  const closeRef = useRef(null);

  useEffect(() => {
    if (!connected || !endpoint?.startsWith('/api/')) {
      setImageUrl('');
      return;
    }
    let active = true;
    let inFlight = false;
    let currentUrl = '';
    let controller = null;

    const refresh = async () => {
      if (!active || inFlight || document.hidden) return;
      inFlight = true;
      controller = new AbortController();
      let nextUrl = '';
      try {
        const separator = endpoint.includes('?') ? '&' : '?';
        const response = await fetch(`${endpoint}${separator}_=${Date.now()}`, {
          credentials: 'same-origin', cache: 'no-store', signal: controller.signal,
        });
        if (!response.ok) throw new Error('map unavailable');
        nextUrl = URL.createObjectURL(await response.blob());
        const image = new Image();
        await new Promise((resolve, reject) => {
          image.onload = resolve;
          image.onerror = reject;
          image.src = nextUrl;
        });
        if (!active) return;
        const previousUrl = currentUrl;
        currentUrl = nextUrl;
        nextUrl = '';
        setImageUrl(currentUrl);
        if (previousUrl) setTimeout(() => URL.revokeObjectURL(previousUrl), 100);
      } catch (_) {
        if (active && !document.hidden) {
          setImageUrl('');
          if (currentUrl) URL.revokeObjectURL(currentUrl);
          currentUrl = '';
        }
      } finally {
        if (nextUrl) URL.revokeObjectURL(nextUrl);
        inFlight = false;
      }
    };

    const onVisibility = () => {
      if (document.hidden) controller?.abort();
      else refresh();
    };
    refresh();
    const timer = setInterval(refresh, 2000);
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      active = false;
      clearInterval(timer);
      controller?.abort();
      document.removeEventListener('visibilitychange', onVisibility);
      if (currentUrl) URL.revokeObjectURL(currentUrl);
    };
  }, [connected, endpoint]);

  useEffect(() => {
    if (!expanded) return;
    closeRef.current?.focus();
    const onKeyDown = event => {
      if (event.key === 'Escape') { event.stopPropagation(); setExpanded(false); }
    };
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('keydown', onKeyDown);
      triggerRef.current?.focus();
    };
  }, [expanded]);

  return <>
    <button ref={triggerRef} type="button" className="map-widget__thumb"
      onClick={() => setExpanded(true)} disabled={!imageUrl}
      aria-label={imageUrl ? '展开导航地图' : '等待地图数据'}>
      {imageUrl ? <img src={imageUrl} alt=""/> : <span className="map-widget__empty">等待地图</span>}
      <span className="map-widget__label">局部地图</span>
      {imageUrl && <span className="map-widget__expand" aria-hidden="true">↗</span>}
    </button>
    {expanded && ReactDOM.createPortal(
        <section className="map-viewer" role="dialog" aria-modal="false" aria-label="导航地图">
          <header className="map-viewer__header">
            <strong>局部导航地图 · 机器狗居中</strong>
            <button ref={closeRef} type="button" onClick={() => setExpanded(false)} aria-label="关闭地图">×</button>
          </header>
          <div className="map-viewer__image">
            {imageUrl ? <img src={imageUrl} alt="机器狗周围的导航地图"/>
              : <span>等待地图数据</span>}
          </div>
          <ul className="map-viewer__legend" aria-label="地图颜色说明">
            <li><span className="map-viewer__swatch map-viewer__swatch--robot" aria-hidden="true"/>机器狗</li>
            <li><span className="map-viewer__swatch map-viewer__swatch--free" aria-hidden="true"/>可通行</li>
            <li><span className="map-viewer__swatch map-viewer__swatch--obstacle" aria-hidden="true"/>障碍物</li>
            <li><span className="map-viewer__swatch map-viewer__swatch--clearance" aria-hidden="true"/>避让区</li>
            <li><span className="map-viewer__swatch map-viewer__swatch--unknown" aria-hidden="true"/>未探索</li>
            <li><span className="map-viewer__swatch map-viewer__swatch--route" aria-hidden="true"/>导航路线</li>
            <li><span className="map-viewer__swatch map-viewer__swatch--place" aria-hidden="true"/>已保存地点</li>
            <li><span className="map-viewer__swatch map-viewer__swatch--target" aria-hidden="true"/>目的地</li>
          </ul>
        </section>
      , document.body,
    )}
  </>;
}

function VirtualJoystick({ onMove, onStop, disabled, label, radius = 62 }) {
  const { useRef, useState, useEffect, useCallback } = React;

  const R  = radius;
  const KR = Math.round(radius * 0.35);

  const padRef    = useRef(null);
  const activeRef = useRef(false);
  const deltaRef  = useRef({ dx: 0, dy: 0 });
  const timerRef  = useRef(null);
  const [knob, setKnob] = useState({ x: 0, y: 0 });
  const [drag, setDrag] = useState(false);

  const release = useCallback(() => {
    activeRef.current = false;
    deltaRef.current  = { dx: 0, dy: 0 };
    clearInterval(timerRef.current);
    timerRef.current = null;
    setDrag(false);
    setKnob({ x: 0, y: 0 });
    onStop();
  }, [onStop]);

  const applyPos = useCallback((clientX, clientY) => {
    const rect = padRef.current.getBoundingClientRect();
    const cx = rect.left + rect.width  / 2;
    const cy = rect.top  + rect.height / 2;
    let dx = clientX - cx;
    let dy = clientY - cy;
    const d = Math.sqrt(dx * dx + dy * dy);
    if (d > R) { dx = dx / d * R; dy = dy / d * R; }
    deltaRef.current = { dx: dx / R, dy: dy / R };
    setKnob({ x: dx, y: dy });
  }, []);

  const onDown = useCallback((e) => {
    if (disabled || activeRef.current) return;
    e.currentTarget.setPointerCapture(e.pointerId);
    activeRef.current = true;
    setDrag(true);
    applyPos(e.clientX, e.clientY);
    onMove(deltaRef.current.dx, deltaRef.current.dy);
    timerRef.current = setInterval(() => {
      if (activeRef.current) onMove(deltaRef.current.dx, deltaRef.current.dy);
    }, 120);
  }, [disabled, onMove, applyPos]);

  const onMove_ = useCallback((e) => {
    if (!activeRef.current || disabled) return;
    applyPos(e.clientX, e.clientY);
  }, [disabled, applyPos]);

  const stopRef = useRef(onStop);
  stopRef.current = onStop;
  useEffect(() => {
    if (disabled && activeRef.current) release();
  }, [disabled, release]);
  useEffect(() => {
    const stop = () => {
      if (!activeRef.current) return;
      activeRef.current = false;
      clearInterval(timerRef.current);
      deltaRef.current = { dx: 0, dy: 0 };
      setDrag(false);
      setKnob({ x: 0, y: 0 });
      stopRef.current();
    };
    const hidden = () => { if (document.hidden) stop(); };
    window.addEventListener('blur', stop);
    document.addEventListener('visibilitychange', hidden);
    return () => {
      stop();
      clearInterval(timerRef.current);
      window.removeEventListener('blur', stop);
      document.removeEventListener('visibilitychange', hidden);
    };
  }, []);

  const pad = {
    width: R * 2, height: R * 2, borderRadius: "50%",
    border: `1.5px solid ${disabled ? "#181818" : "rgba(200,255,62,0.2)"}`,
    background: disabled
      ? "rgba(0,0,0,0.15)"
      : "radial-gradient(circle at 50% 50%, rgba(200,255,62,0.06) 0%, rgba(200,255,62,0.02) 60%, transparent 100%)",
    position: "relative", userSelect: "none", touchAction: "none",
    boxShadow: disabled ? "none" : "0 0 0 1px rgba(200,255,62,0.05) inset",
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 7 }}>
      <div ref={padRef} style={pad} role="group" tabIndex={disabled ? -1 : 0}
        aria-disabled={disabled} aria-label={`${label}摇杆，方向键控制，松开停止`}
        onKeyDown={event => {
          if (disabled) return;
          const directions = {ArrowUp:[0,-1],ArrowDown:[0,1],ArrowLeft:[-1,0],ArrowRight:[1,0]};
          if (event.key === 'Escape' || event.key === ' ') {
            event.preventDefault(); if (activeRef.current) release(); return;
          }
          const delta = directions[event.key];
          if (!delta) return;
          event.preventDefault();
          if (activeRef.current) return;
          activeRef.current = true;
          setDrag(true);
          deltaRef.current = {dx:delta[0],dy:delta[1]};
          setKnob({x:delta[0]*R,y:delta[1]*R});
          onMove(delta[0], delta[1]);
          timerRef.current = setInterval(() => {
            if (activeRef.current) onMove(deltaRef.current.dx, deltaRef.current.dy);
          }, 120);
        }}
        onKeyUp={event => {
          if (event.key.startsWith('Arrow') && activeRef.current) { event.preventDefault(); release(); }
        }}
        onBlur={() => { if (activeRef.current) release(); }}
        onPointerDown={onDown} onPointerMove={onMove_}
        onPointerUp={() => { if (activeRef.current) release(); }}
        onPointerCancel={() => { if (activeRef.current) release(); }}
        onLostPointerCapture={() => { if (activeRef.current) release(); }}
      >
        {/* crosshairs */}
        <div style={{ position: "absolute", left: R - 0.5, top: 10, width: 1,
          height: R * 2 - 20, background: disabled ? "#0f0f0f" : "rgba(200,255,62,0.07)",
          pointerEvents: "none" }} />
        <div style={{ position: "absolute", top: R - 0.5, left: 10, height: 1,
          width: R * 2 - 20, background: disabled ? "#0f0f0f" : "rgba(200,255,62,0.07)",
          pointerEvents: "none" }} />
        {/* knob */}
        <div style={{
          position: "absolute",
          width: KR * 2, height: KR * 2, borderRadius: "50%",
          left: R - KR + knob.x, top: R - KR + knob.y,
          background: disabled
            ? "#1c1c1c"
            : `radial-gradient(circle at 35% 35%, var(--color-accent), rgba(140,210,20,0.85))`,
          border: `1px solid ${disabled ? "#242424" : "rgba(200,255,62,0.55)"}`,
          boxShadow: disabled ? "none" : "0 0 18px rgba(200,255,62,0.45), 0 2px 6px rgba(0,0,0,0.6)",
          transition: drag ? "none" : "left 0.12s cubic-bezier(.2,.8,.4,1), top 0.12s cubic-bezier(.2,.8,.4,1)",
          pointerEvents: "none",
        }} />
      </div>
      <div style={{ fontSize: 9, color: "var(--color-text-dim)", letterSpacing: "0.22em",
        fontFamily: 'var(--font-mono)' }}>{label}</div>
    </div>
  );
}

/* ─────────────────────────────────────────────────────────── */


window.VirtualJoystick = VirtualJoystick;
window.VideoCanvas = VideoCanvas;
window.MapWidget = MapWidget;
