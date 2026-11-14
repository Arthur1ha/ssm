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

// The server emits bounded JPEG parts. Network chunks need not match part boundaries.
async function readMjpegFrames(response, acceptFrame) {
  const boundary = /boundary=(?:"([^"]+)"|([^;\s]+))/i.exec(response.headers.get('content-type') || '');
  if (!boundary || !response.body?.getReader) throw new Error('video stream unavailable');
  const marker = `--${boundary[1] || boundary[2]}`;
  const decoder = new TextDecoder('ascii');
  const reader = response.body.getReader();
  const MAX_FRAME_BYTES = 2 * 1024 * 1024;
  const MAX_HEADER_BYTES = 4096;
  let buffer = new Uint8Array(0);
  let frameLength = null;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      if (buffer.length + value.length > MAX_FRAME_BYTES * 2 + MAX_HEADER_BYTES) {
        throw new Error('video stream buffer limit');
      }
      const combined = new Uint8Array(buffer.length + value.length);
      combined.set(buffer); combined.set(value, buffer.length); buffer = combined;
      let latest = null;
      while (buffer.length) {
        if (frameLength === null) {
          let end = -1;
          for (let i = 0; i + 3 < Math.min(buffer.length, MAX_HEADER_BYTES); i++) {
            if (buffer[i] === 13 && buffer[i + 1] === 10 && buffer[i + 2] === 13 && buffer[i + 3] === 10) { end = i; break; }
          }
          if (end < 0) {
            if (buffer.length >= MAX_HEADER_BYTES) throw new Error('invalid video part header');
            break;
          }
          const header = decoder.decode(buffer.subarray(0, end)).trim();
          const length = /^content-length:\s*(\d+)\s*$/im.exec(header);
          if (!header.startsWith(marker + '\r\n') || !length) throw new Error('invalid video part');
          frameLength = Number(length[1]);
          if (!frameLength || frameLength > MAX_FRAME_BYTES) throw new Error('video frame limit');
          buffer = buffer.subarray(end + 4);
        }
        if (buffer.length < frameLength) break;
        latest = buffer.slice(0, frameLength);
        buffer = buffer.subarray(frameLength);
        frameLength = null;
      }
      // A delayed network read may contain many frames; paint the newest one.
      if (latest) acceptFrame(new Blob([latest], { type: 'image/jpeg' }));
    }
  } finally {
    reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}

function loadMediaImage(blob, signal) {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(blob);
    const image = new Image();
    const dispose = () => URL.revokeObjectURL(url);
    const decodeTimer = setTimeout(() => { cleanup(); dispose(); reject(new Error('media decode timed out')); }, 4000);
    const cleanup = () => {
      clearTimeout(decodeTimer);
      image.onload = null; image.onerror = null;
      signal.removeEventListener('abort', abort);
    };
    const abort = () => { cleanup(); dispose(); reject(new DOMException('Media cancelled', 'AbortError')); };
    image.onload = () => { cleanup(); resolve({ image, url, dispose }); };
    image.onerror = () => { cleanup(); dispose(); reject(new Error('media decode failed')); };
    if (signal.aborted) { abort(); return; }
    signal.addEventListener('abort', abort, { once: true });
    image.src = url;
  });
}

function VideoCanvas({ connected, endpoint = '/api/go2/video' }) {
  const { useRef, useEffect } = React;
  const canvasRef = useRef(null);

  useEffect(() => {
    if (!connected) return;
    // Effect-local ownership prevents an old fetch from reviving on reconnect.
    let active = true;
    let controller = null;
    let timer = null;
    let watchdog = null;
    let generation = 0;
    let snapshotUntil = 0;
    const clear = () => {
      const canvas = canvasRef.current;
      canvas?.getContext('2d').clearRect(0, 0, canvas.width, canvas.height);
    };
    const run = async () => {
      if (!active || document.hidden) return;
      const current = ++generation;
      controller = new AbortController();
      const requestController = controller;
      const signal = requestController.signal;
      const owned = () => active && current === generation && !document.hidden;
      const touch = () => {
        clearTimeout(watchdog);
        watchdog = setTimeout(() => requestController.abort(), 8000);
      };
      let pending = null;
      let paintPromise = null;
      const paint = async () => {
        try {
          while (pending && owned() && !signal.aborted) {
            const blob = pending; pending = null;
            const frame = await loadMediaImage(blob, signal);
            try {
              const canvas = canvasRef.current;
              if (canvas && owned() && !signal.aborted) {
                canvas.getContext('2d').drawImage(frame.image, 0, 0, canvas.width, canvas.height);
              }
            } finally { frame.dispose(); }
          }
        } catch (_) { requestController.abort(); }
        finally { paintPromise = null; }
      };
      const accept = blob => {
        if (!owned() || signal.aborted) return;
        pending = blob; touch();
        if (!paintPromise) paintPromise = paint();
      };
      touch();
      try {
        if (Date.now() < snapshotUntil) {
          const [path, query = ''] = endpoint.split('?');
          const response = await fetch(`${path}/snapshot?${query ? query + '&' : ''}preview=true&_=${Date.now()}`, {
            credentials: 'same-origin', cache: 'no-store', signal,
          });
          if (!response.ok) throw new Error('video unavailable');
          accept(await response.blob());
          await paintPromise;
        } else {
          const response = await fetch(endpoint, { credentials: 'same-origin', cache: 'no-store', signal });
          if (!response.ok) throw new Error('video unavailable');
          await readMjpegFrames(response, accept);
          throw new Error('video stream ended');
        }
      } catch (_) {
        if (owned()) {
          snapshotUntil = Date.now() + 15000;
          clear();
        }
      } finally {
        requestController.abort();
        if (current === generation) {
          clearTimeout(watchdog);
          if (owned()) timer = setTimeout(run, Date.now() < snapshotUntil ? 200 : 1000);
        }
      }
    };
    const stop = () => {
      clearTimeout(timer); clearTimeout(watchdog);
      generation++; controller?.abort();
    };
    const onVisibility = () => {
      stop(); clear();
      if (!document.hidden) { snapshotUntil = 0; run(); }
    };
    run();
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      active = false;
      stop(); clear();
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [connected, endpoint]);

  return React.createElement('canvas', {
    ref: canvasRef, width: 640, height: 360,
    style: { width: '100%', height: '100%', display: 'block', objectFit: 'contain' },
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
    let staleTimer = null;

    const expire = () => {
      if (!active) return;
      setImageUrl('');
      if (currentUrl) URL.revokeObjectURL(currentUrl);
      currentUrl = '';
    };
    const refresh = async () => {
      if (!active || inFlight || document.hidden) return;
      inFlight = true;
      controller = new AbortController();
      const requestController = controller;
      const timeout = setTimeout(() => requestController.abort(), 8000);
      let nextUrl = '';
      try {
        const separator = endpoint.includes('?') ? '&' : '?';
        const response = await fetch(`${endpoint}${separator}_=${Date.now()}`, {
          credentials: 'same-origin', cache: 'no-store', signal: requestController.signal,
        });
        if (!response.ok) throw new Error('map unavailable');
        const frame = await loadMediaImage(await response.blob(), requestController.signal);
        nextUrl = frame.url;
        if (!active || requestController.signal.aborted || document.hidden) return;
        const previousUrl = currentUrl;
        currentUrl = nextUrl;
        nextUrl = '';
        setImageUrl(currentUrl);
        clearTimeout(staleTimer);
        staleTimer = setTimeout(expire, 8000);
        if (previousUrl) setTimeout(() => URL.revokeObjectURL(previousUrl), 100);
      } catch (_) {
        // Keep a recent map across a brief error; its expiry still runs.
      } finally {
        clearTimeout(timeout);
        if (nextUrl) URL.revokeObjectURL(nextUrl);
        inFlight = false;
      }
    };

    const onVisibility = () => {
      if (document.hidden) controller?.abort();
      else refresh();
    };
    refresh();
    const timer = setInterval(refresh, 1000);
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      active = false;
      clearInterval(timer);
      clearTimeout(staleTimer);
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
