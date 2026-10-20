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
    if (disabled) return;
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
    const stop = () => {
      if (!activeRef.current) return;
      activeRef.current = false;
      clearInterval(timerRef.current);
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
      <div ref={padRef} style={pad}
        onPointerDown={onDown} onPointerMove={onMove_}
        onPointerUp={release} onPointerCancel={release}
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
