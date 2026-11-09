/* One viewer for both live media types; the native dialog owns focus and Escape. */
function MediaWidget({ label, title, ready = true, emptyText, children, footer }) {
  const { useState, useRef, useEffect } = React;
  const [expanded, setExpanded] = useState(false);
  const triggerRef = useRef(null);
  const dialogRef = useRef(null);
  const closeRef = useRef(null);

  useEffect(() => {
    if (!expanded) return;
    const dialog = dialogRef.current;
    dialog.showModal();
    closeRef.current?.focus();
    return () => {
      dialog.close();
      if (triggerRef.current?.isConnected) triggerRef.current.focus();
    };
  }, [expanded]);

  return <>
    <button ref={triggerRef} type="button" className="device-media__thumb"
      onClick={() => setExpanded(true)} disabled={!ready}
      aria-label={ready ? `放大${label}` : emptyText} aria-haspopup="dialog" aria-expanded={expanded}>
      {!expanded && children}
      <span className="device-media__label">{label}</span>
      {ready && <span className="device-media__expand" aria-hidden="true">↗</span>}
    </button>
    {expanded && ReactDOM.createPortal(
      <dialog ref={dialogRef} className="device-media-viewer" role="dialog" aria-label={title} aria-modal="true"
        onCancel={event => { event.preventDefault(); setExpanded(false); }}
        onClose={() => setExpanded(false)}
        onClick={event => {
          if (event.target !== event.currentTarget) return;
          const rect = event.currentTarget.getBoundingClientRect();
          if (event.clientX < rect.left || event.clientX > rect.right
              || event.clientY < rect.top || event.clientY > rect.bottom) setExpanded(false);
        }}>
        <header className="device-media-viewer__header">
          <strong>{title}</strong>
          <button ref={closeRef} type="button" aria-label={`关闭${label}`} onClick={() => setExpanded(false)}>×</button>
        </header>
        <div className="device-media-viewer__image">{children}</div>
        {footer}
      </dialog>, document.body,
    )}
  </>;
}

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
    style: { width: "100%", height: "100%", display: "block", objectFit: "contain" },
  });
}

function MapWidget({ connected, endpoint }) {
  const { useEffect, useState } = React;
  const [imageUrl, setImageUrl] = useState('');

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

  return <MediaWidget label="地图" title="导航地图" ready={!!imageUrl} emptyText="等待地图数据"
    footer={<ul className="map-viewer__legend" aria-label="地图颜色说明">
            <li><span className="map-viewer__swatch map-viewer__swatch--robot" aria-hidden="true"/>机器狗</li>
            <li><span className="map-viewer__swatch map-viewer__swatch--free" aria-hidden="true"/>可通行</li>
            <li><span className="map-viewer__swatch map-viewer__swatch--obstacle" aria-hidden="true"/>障碍物</li>
            <li><span className="map-viewer__swatch map-viewer__swatch--clearance" aria-hidden="true"/>避让区</li>
            <li><span className="map-viewer__swatch map-viewer__swatch--route" aria-hidden="true"/>导航路线</li>
            <li><span className="map-viewer__swatch map-viewer__swatch--place" aria-hidden="true"/>已保存地点</li>
            <li><span className="map-viewer__swatch map-viewer__swatch--target" aria-hidden="true"/>目的地</li>
    </ul>}>
    {imageUrl ? <img className="device-media__map" src={imageUrl} alt="机器狗周围的导航地图"/>
      : <span className="device-media__empty">等待地图数据</span>}
  </MediaWidget>;
}

function VirtualJoystick({ onMove, onStop, disabled, label, radius = 62 }) {
  const { useRef, useState, useEffect, useCallback } = React;

  const R  = radius;
  const KR = Math.round(radius * 0.35);

  const padRef    = useRef(null);
  const activeRef = useRef(false);
  const pointerRef = useRef(null);
  const deltaRef  = useRef({ dx: 0, dy: 0 });
  const timerRef  = useRef(null);
  const [knob, setKnob] = useState({ x: 0, y: 0 });
  const [drag, setDrag] = useState(false);

  const release = useCallback(() => {
    activeRef.current = false;
    pointerRef.current = null;
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
  }, [R]);

  const onDown = useCallback((e) => {
    if (disabled || activeRef.current) return;
    e.currentTarget.setPointerCapture(e.pointerId);
    pointerRef.current = e.pointerId;
    activeRef.current = true;
    setDrag(true);
    applyPos(e.clientX, e.clientY);
    onMove(deltaRef.current.dx, deltaRef.current.dy);
    timerRef.current = setInterval(() => {
      if (activeRef.current) onMove(deltaRef.current.dx, deltaRef.current.dy);
    }, 120);
  }, [disabled, onMove, applyPos]);

  const onMove_ = useCallback((e) => {
    if (!activeRef.current || disabled || pointerRef.current !== e.pointerId) return;
    applyPos(e.clientX, e.clientY);
    onMove(deltaRef.current.dx, deltaRef.current.dy);
  }, [disabled, applyPos, onMove]);

  const onPointerEnd = useCallback(e => {
    if (activeRef.current && pointerRef.current === e.pointerId) release();
  }, [release]);

  const stopRef = useRef(onStop);
  stopRef.current = onStop;
  useEffect(() => {
    if (disabled && activeRef.current) release();
  }, [disabled, release]);
  useEffect(() => {
    const stop = () => {
      if (!activeRef.current) return;
      activeRef.current = false;
      pointerRef.current = null;
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
    border: `1px solid ${disabled ? "var(--color-border)" : "var(--color-accent-border)"}`,
    background: "var(--color-surface-1)",
    position: "relative", userSelect: "none", touchAction: "none",
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 7 }}>
      <div ref={padRef} className="device-joystick-pad" style={pad} role="group" tabIndex={disabled ? -1 : 0}
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
        onPointerUp={onPointerEnd}
        onPointerCancel={onPointerEnd}
        onLostPointerCapture={onPointerEnd}
      >
        {/* crosshairs */}
        <div style={{ position: "absolute", left: R - 0.5, top: 10, width: 1,
          height: R * 2 - 20, background: "var(--color-border-strong)",
          pointerEvents: "none" }} />
        <div style={{ position: "absolute", top: R - 0.5, left: 10, height: 1,
          width: R * 2 - 20, background: "var(--color-border-strong)",
          pointerEvents: "none" }} />
        {/* knob */}
        <div style={{
          position: "absolute",
          width: KR * 2, height: KR * 2, borderRadius: "50%",
          left: R - KR + knob.x, top: R - KR + knob.y,
          background: disabled
            ? "var(--color-surface-3)"
            : "var(--color-accent)",
          border: `1px solid ${disabled ? "var(--color-border-strong)" : "var(--color-accent)"}`,
          transition: drag ? "none" : "left 0.12s cubic-bezier(.2,.8,.4,1), top 0.12s cubic-bezier(.2,.8,.4,1)",
          pointerEvents: "none",
        }} />
      </div>
      <div style={{ fontSize: 12, color: "var(--color-text-muted)",
        fontFamily: 'var(--font-sans)' }}>{label}</div>
    </div>
  );
}

/* ─────────────────────────────────────────────────────────── */


window.VirtualJoystick = VirtualJoystick;
window.VideoCanvas = VideoCanvas;
window.MapWidget = MapWidget;
window.MediaWidget = MediaWidget;
