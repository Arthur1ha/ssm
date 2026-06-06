/* Go2DevicePage — Mobile 双摇杆界面
   通信：Snapshot 轮询 + SSE + REST   兼容 iOS Safari
*/

function VideoCanvas({ connected }) {
  const { useRef, useEffect } = React;
  const canvasRef  = useRef(null);
  const activeRef  = useRef(false);

  useEffect(() => {
    if (!connected) return;
    activeRef.current = true;

    const tick = async () => {
      if (!activeRef.current) return;
      try {
        const res = await fetch(`/api/go2/video/snapshot?_=${Date.now()}`);
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
  }, [connected]);

  return React.createElement("canvas", {
    ref: canvasRef, width: 640, height: 360,
    style: { width: "100%", height: "100%", display: "block" },
  });
}

function MapCanvas({ connected }) {
  const { useRef, useEffect } = React;
  const imgRef    = useRef(null);
  const activeRef = useRef(false);

  useEffect(() => {
    if (!connected) return;
    activeRef.current = true;

    const tick = () => {
      if (!activeRef.current) return;
      const el = imgRef.current;
      if (el) el.src = `/api/go2/map.png?_=${Date.now()}`;
    };

    tick();
    const id = setInterval(tick, 1000);
    return () => { activeRef.current = false; clearInterval(id); };
  }, [connected]);

  return React.createElement("img", {
    ref: imgRef,
    style: { width: "100%", height: "100%", display: "block", objectFit: "contain" },
    alt: "occupancy map",
  });
}

function VirtualJoystick({ onMove, onStop, disabled, label }) {
  const { useRef, useState, useEffect, useCallback } = React;

  const R  = 62;  // pad radius px
  const KR = 22;  // knob radius px
  const LIME = "#C8FF3E";

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
    }, 80);
  }, [disabled, onMove, applyPos]);

  const onMove_ = useCallback((e) => {
    if (!activeRef.current || disabled) return;
    applyPos(e.clientX, e.clientY);
  }, [disabled, applyPos]);

  useEffect(() => () => clearInterval(timerRef.current), []);

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
            : `radial-gradient(circle at 35% 35%, ${LIME}, rgba(140,210,20,0.85))`,
          border: `1px solid ${disabled ? "#242424" : "rgba(200,255,62,0.55)"}`,
          boxShadow: disabled ? "none" : "0 0 18px rgba(200,255,62,0.45), 0 2px 6px rgba(0,0,0,0.6)",
          transition: drag ? "none" : "left 0.12s cubic-bezier(.2,.8,.4,1), top 0.12s cubic-bezier(.2,.8,.4,1)",
          pointerEvents: "none",
        }} />
      </div>
      <div style={{ fontSize: 9, color: "#2e2e2e", letterSpacing: "0.22em",
        fontFamily: "'Share Tech Mono','Courier New',monospace" }}>{label}</div>
    </div>
  );
}

/* ─────────────────────────────────────────────────────────── */

function WaypointMap({ locations, LIME, BORDER }) {
  const W = 280, H = 130, PAD = 22;

  if (locations.length === 0) {
    return (
      <svg width="100%" viewBox={`0 0 ${W} ${H}`}
        style={{ border: `1px solid ${BORDER}`, borderRadius: 4, display: "block" }}>
        <text x={W / 2} y={H / 2} textAnchor="middle" dominantBaseline="middle"
          fill="#1e1e1e" fontSize="10" fontFamily="'Share Tech Mono',monospace"
          letterSpacing="2">NO DATA</text>
      </svg>
    );
  }

  const xs = locations.map(l => l.x);
  const ys = locations.map(l => l.y);
  const minX = Math.min(...xs), maxX = Math.max(...xs);
  const minY = Math.min(...ys), maxY = Math.max(...ys);
  const rangeX = maxX - minX || 1, rangeY = maxY - minY || 1;
  const scale = Math.min((W - PAD * 2) / rangeX, (H - PAD * 2) / rangeY, 55);
  const offX = (W - PAD * 2 - rangeX * scale) / 2;
  const offY = (H - PAD * 2 - rangeY * scale) / 2;
  const toSvg = (x, y) => ({
    sx: PAD + offX + (x - minX) * scale,
    sy: H - PAD - offY - (y - minY) * scale,
  });

  return (
    <svg width="100%" viewBox={`0 0 ${W} ${H}`}
      style={{ border: `1px solid ${BORDER}`, borderRadius: 4, display: "block",
        background: "rgba(200,255,62,0.012)" }}>
      <defs>
        <filter id="wp-glow">
          <feGaussianBlur stdDeviation="1.8" result="blur" />
          <feMerge><feMergeNode in="blur" /><feMergeNode in="SourceGraphic" /></feMerge>
        </filter>
      </defs>
      <line x1={W / 2} y1={0} x2={W / 2} y2={H} stroke="rgba(200,255,62,0.04)" strokeWidth="0.5" />
      <line x1={0} y1={H / 2} x2={W} y2={H / 2} stroke="rgba(200,255,62,0.04)" strokeWidth="0.5" />
      {locations.length > 1 && locations.map((loc, i) => {
        if (i === 0) return null;
        const { sx: x1, sy: y1 } = toSvg(locations[i - 1].x, locations[i - 1].y);
        const { sx: x2, sy: y2 } = toSvg(loc.x, loc.y);
        return <line key={`ln${i}`} x1={x1} y1={y1} x2={x2} y2={y2}
          stroke="rgba(200,255,62,0.1)" strokeWidth="0.8" strokeDasharray="3,3" />;
      })}
      {locations.map((loc, i) => {
        const { sx, sy } = toSvg(loc.x, loc.y);
        return (
          <g key={loc.name}>
            <circle cx={sx} cy={sy} r={3.5} fill={LIME} fillOpacity="0.85"
              filter="url(#wp-glow)" />
            <text x={sx + 6} y={sy} fontSize="7.5" dominantBaseline="middle"
              fontFamily="'Share Tech Mono',monospace"
              fill="rgba(200,255,62,0.5)" letterSpacing="0.3">{loc.name}</text>
          </g>
        );
      })}
    </svg>
  );
}

/* ─────────────────────────────────────────────────────────── */

function Go2DevicePage({ onBack, messages, onAppend }) {
  const { useState, useEffect, useRef, useCallback } = React;

  const LIME   = "#C8FF3E";
  const DIM    = "rgba(200,255,62,0.13)";
  const BG     = "#06080F";
  const PANEL  = "#0C0F1A";
  const BORDER = "rgba(200,255,62,0.1)";

  const [status,        setStatus]     = useState("idle");
  const [robotState,    setRobot]      = useState(null);
  const [error,         setError]      = useState("");
  const [autonomyMode,  setAutonomyMode] = useState("manual");
  const [chatThinking,  setChatThink]  = useState(false);
  const [wpOpen,       setWpOpen]    = useState(false);
  const [locations,    setLocations] = useState([]);
  const [tagName,      setTagName]   = useState("");
  const [wpLoading,    setWpLoading] = useState(false);
  const [navigating,   setNavigating]= useState(null);
  const [homing,       setHoming]    = useState(false);
  const [settingHome,  setSettingHome]= useState(false);
  const esRef   = useRef(null);
  const pollRef = useRef(null);

  /* ── 状态轮询 ── */
  const pollStatus = useCallback(async () => {
    try {
      const d = await fetch("/api/go2/connection").then(r => r.json());
      if (d.connected && status !== "connected") { setStatus("connected"); setError(""); }
      else if (!d.connected && status === "connected") { setStatus("idle"); setRobot(null); }
      else if (!d.connected && status === "connecting" && d.error) { setStatus("error"); setError(d.error); }
    } catch (_) {}
  }, [status]);

  useEffect(() => {
    pollRef.current = setInterval(pollStatus, 3000);
    return () => clearInterval(pollRef.current);
  }, [pollStatus]);

  /* ── SSE ── */
  useEffect(() => {
    if (status !== "connected") return;
    const es = new EventSource("/api/go2/connection/stream");
    esRef.current = es;
    es.onmessage = e => {
      try { const d = JSON.parse(e.data); if (d.mode !== undefined) setRobot(d); } catch (_) {}
    };
    return () => { es.close(); esRef.current = null; };
  }, [status]);

  /* ── 连接 ── */
  const connect = useCallback(async () => {
    setError(""); setStatus("connecting");
    try {
      const r = await fetch("/api/go2/connection", { method: "POST" });
      if (!r.ok) throw new Error((await r.json()).detail || "连接失败");
    } catch (e) { setStatus("error"); setError(String(e)); }
  }, []);

  useEffect(() => {
    fetch("/api/go2/connection")
      .then(r => r.json())
      .then(d => { if (d.connected) { setStatus("connected"); setError(""); } else connect(); })
      .catch(() => connect());
  }, []);

  useEffect(() => () => { if (esRef.current) esRef.current.close(); }, []);

  const disconnect = async () => {
    if (esRef.current) { esRef.current.close(); esRef.current = null; }
    await fetch("/api/go2/connection", { method: "DELETE" });
    setStatus("idle"); setRobot(null);
  };

  /* ── 指令 ── */
  const sendCmd = (cmd, params) => fetch("/api/go2/commands", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ cmd, params: params || {} }),
  });

  const switchAutonomy = async m => {
    try {
      await fetch("/api/go2/autonomy", {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode: m }),
      });
      setAutonomyMode(m);
    } catch (_) {}
  };

  useEffect(() => {
    if (status === "connected") {
      fetch("/api/go2/autonomy").then(r => r.json()).then(d => setAutonomyMode(d.mode)).catch(() => {});
    } else {
      setAutonomyMode("manual");
    }
  }, [status]);

  /* ── 摇杆回调 ── */
  // 左摇杆：dx右正→y负(右移)，dy下正→x负(后退)
  const sendVelocity = (vx, vy, vyaw) => fetch("/api/go2/velocity", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ vx, vy, vyaw }),
  });

  const handleLeftMove = useCallback((dx, dy) => {
    sendVelocity(
      parseFloat((-dy * 0.5).toFixed(2)),
      parseFloat((-dx * 0.5).toFixed(2)),
      0,
    );
  }, []);

  const handleRightMove = useCallback((dx, _dy) => {
    sendVelocity(0, 0, parseFloat((-dx * 0.8).toFixed(2)));
  }, []);

  const handleStop = useCallback(async () => {
    await fetch("/api/go2/stop", { method: "POST" });
    setAutonomyMode("manual");
  }, []);

  /* ── 地点管理 ── */
  const fetchLocations = useCallback(async () => {
    try {
      const data = await fetch("/api/go2/navigation/locations").then(r => r.json());
      setLocations(Array.isArray(data) ? data : []);
    } catch (_) {}
  }, []);

  const tagLocation = useCallback(async () => {
    if (!tagName.trim()) return;
    setWpLoading(true);
    try {
      const r = await fetch("/api/go2/navigation/locations", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: tagName.trim() }),
      });
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        setError(d.detail || `标记失败 (${r.status})`);
        return;
      }
      setTagName("");
      setError("");
      await fetchLocations();
    } catch (_) {} finally { setWpLoading(false); }
  }, [tagName, fetchLocations]);

  const deleteLocation = useCallback(async (name) => {
    try {
      await fetch(`/api/go2/navigation/locations/${encodeURIComponent(name)}`, { method: "DELETE" });
      await fetchLocations();
    } catch (_) {}
  }, [fetchLocations]);

  const navigateTo = useCallback(async (name) => {
    setNavigating(name);
    try {
      await fetch("/api/go2/navigation/go", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
    } catch (_) {} finally { setNavigating(null); }
  }, []);

  useEffect(() => { if (wpOpen) fetchLocations(); }, [wpOpen, fetchLocations]);
  useEffect(() => { if (status === "connected") fetchLocations(); }, [status, fetchLocations]);

  const connected = status === "connected";
  const busy      = status === "connecting";

  const goHome = useCallback(async () => {
    if (!connected || homing) return;
    setHoming(true);
    try {
      await fetch("/api/go2/navigation/home/go", { method: "POST" });
    } catch (_) {} finally { setHoming(false); }
  }, [connected, homing]);

  const setHome = useCallback(async () => {
    if (!connected || settingHome) return;
    setSettingHome(true);
    try {
      await fetch("/api/go2/navigation/home", { method: "PUT" });
      await fetchLocations();
    } catch (_) {} finally { setSettingHome(false); }
  }, [connected, settingHome, fetchLocations]);

  /* ── AI 对话 ── */
  const sendChat = useCallback(async (text) => {
    if (!connected || chatThinking) return;
    onAppend({ role: 'user', text });
    setChatThink(true);
    try {
      const r = await fetch("/api/go2/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text }),
      });
      const d = await r.json();
      onAppend({ role: 'assistant', text: d.response || "已处理", actions: [] });
    } catch (_) {
      onAppend({ role: 'assistant', text: "请求失败，请检查连接", actions: [] });
    } finally {
      setChatThink(false);
    }
  }, [connected, chatThinking, onAppend]);

  const statusColor = { idle: "#444", connecting: "#f0a500", connected: LIME, error: "#ff4455" }[status];
  const statusLabel = { idle: "OFFLINE", connecting: "CONNECTING", connected: "ONLINE", error: "ERROR" }[status];

  const ACTIONS = [
    ["站起", "StandUp"],  ["挥手",  "Hello"],   ["舞蹈1", "Dance1"],
    ["坐下", "StandDown"],["伸展",  "Stretch"],  ["舞蹈2", "Dance2"],
  ];

  /* ── HUD 角标 ── */
  const corner = (t, r, b, l) => ({
    position: "absolute", width: 11, height: 11,
    borderColor: "rgba(200,255,62,0.5)", borderStyle: "solid", borderWidth: 0,
    ...(t !== undefined && { top: 2,    borderTopWidth:    1.5 }),
    ...(r !== undefined && { right: 2,  borderRightWidth:  1.5 }),
    ...(b !== undefined && { bottom: 2, borderBottomWidth: 1.5 }),
    ...(l !== undefined && { left: 2,   borderLeftWidth:   1.5 }),
  });

  /* ── 动作按钮样式 ── */
  const actStyle = {
    background: connected ? "rgba(200,255,62,0.055)" : "rgba(255,255,255,0.015)",
    color: connected ? "rgba(200,255,62,0.72)" : "#222",
    border: `1px solid ${connected ? "rgba(200,255,62,0.16)" : "#151515"}`,
    borderRadius: 5, fontSize: 11, fontWeight: 700,
    cursor: connected ? "pointer" : "not-allowed",
    padding: "9px 0", fontFamily: "inherit",
    letterSpacing: "0.05em",
    WebkitTapHighlightColor: "transparent",
    transition: "all 0.1s",
  };

  return (
    <div style={{ background: BG, color: "#ccc",
      position: "fixed", inset: 0,
      paddingTop: "env(safe-area-inset-top, 0px)",
      fontFamily: "'Share Tech Mono','Courier New',monospace",
      display: "flex", flexDirection: "column" }}>

      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=Share+Tech+Mono&display=swap');
        @keyframes blink  { 0%,49%{opacity:1} 50%,100%{opacity:0} }
        @keyframes fadeIn { from{opacity:0;transform:translateY(4px)} to{opacity:1;transform:translateY(0)} }
        .g2-act:active { filter:brightness(1.6); transform:scale(0.91) !important; }
        .g2-stop:active { filter:brightness(1.4); transform:scale(0.98) !important; }
      `}</style>

      {/* ── Header ── */}
      <div style={{ display: "flex", alignItems: "center", gap: 10,
        padding: "11px 16px", borderBottom: `1px solid ${BORDER}`,
        background: PANEL, flexShrink: 0 }}>
        <button onClick={onBack} style={{ background: "none", border: "none",
          color: "#444", cursor: "pointer", fontSize: 18, padding: "0 4px",
          fontFamily: "inherit", lineHeight: 1,
          WebkitTapHighlightColor: "transparent" }}>←</button>

        <div style={{ fontSize: 13, fontWeight: 700, letterSpacing: "0.15em",
          color: LIME, textShadow: `0 0 14px ${LIME}45` }}>GO2 AIR</div>
        <div style={{ fontSize: 9, color: "#222", letterSpacing: "0.2em", marginLeft: -4 }}>UNITREE</div>

        <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 8 }}>
          <div style={{ width: 7, height: 7, borderRadius: "50%",
            background: statusColor,
            boxShadow: connected ? `0 0 8px ${LIME}` : "none",
            animation: busy ? "blink 1s infinite" : "none" }} />
          <span style={{ fontSize: 10, color: statusColor, letterSpacing: "0.1em" }}>
            {statusLabel}
          </span>
          {!connected
            ? <button onClick={connect} disabled={busy} style={{
                background: busy ? "transparent" : DIM,
                color: busy ? "#3a3a3a" : LIME,
                border: `1px solid ${busy ? "#222" : "rgba(200,255,62,0.38)"}`,
                borderRadius: 4, padding: "5px 12px", fontSize: 10,
                cursor: busy ? "not-allowed" : "pointer",
                letterSpacing: "0.1em", fontFamily: "inherit",
                WebkitTapHighlightColor: "transparent" }}>
                {busy ? "..." : "CONNECT"}
              </button>
            : <button onClick={disconnect} style={{
                background: "rgba(255,68,85,0.09)", color: "#ff4455",
                border: "1px solid rgba(255,68,85,0.22)",
                borderRadius: 4, padding: "5px 10px", fontSize: 10,
                cursor: "pointer", letterSpacing: "0.1em", fontFamily: "inherit",
                WebkitTapHighlightColor: "transparent" }}>
                DISC
              </button>
          }
        </div>
      </div>

      {/* ── 错误条 ── */}
      {error && (
        <div style={{ background: "rgba(255,68,85,0.07)",
          borderBottom: "1px solid rgba(255,68,85,0.15)",
          padding: "7px 16px", fontSize: 11, color: "#ff6677",
          animation: "fadeIn 0.2s ease", flexShrink: 0 }}>
          ⚠ {error}
        </div>
      )}

      {/* ── 可滚动主体 ── */}
      <div style={{ flex: 1, overflowY: "auto", paddingBottom: 24 }}>

        {/* 主显示区：视频画面 */}
        <div style={{ position: "relative", width: "100%", aspectRatio: "16/9",
          background: "#0a0c14", overflow: "hidden", flexShrink: 0 }}>
          {connected
            ? <VideoCanvas connected={connected} />
            : <div style={{ width: "100%", height: "100%", display: "flex",
                alignItems: "center", justifyContent: "center",
                flexDirection: "column", gap: 8 }}>
                <div style={{ fontSize: 11, color: "#172017", letterSpacing: "0.2em" }}>NO SIGNAL</div>
                <div style={{ width: 28, height: 1, background: "#121812" }} />
                <div style={{ fontSize: 9, color: "#0f170f", letterSpacing: "0.15em" }}>CONNECT TO ENABLE</div>
              </div>
          }

          {/* 地图画中画：左上角 */}
          {connected && (
            <div style={{ position: "absolute", top: 8, left: 8,
              width: "28%", aspectRatio: "1/1",
              background: "#0a0c14", border: "1px solid rgba(200,255,62,0.2)",
              borderRadius: 3, overflow: "hidden", zIndex: 10 }}>
              <MapCanvas connected={connected} />
            </div>
          )}

          {/* 角标 */}
          <div style={corner("t", undefined, undefined, "l")} />
          <div style={corner("t", "r", undefined, undefined)} />
          <div style={corner(undefined, undefined, "b", "l")} />
          <div style={corner(undefined, "r", "b", undefined)} />

          {/* HUD 叠加 */}
          {connected && robotState && (
            <div style={{ position: "absolute", bottom: 8, left: 10, right: 10,
              display: "flex", justifyContent: "space-between",
              fontSize: 9, color: `${LIME}75`, letterSpacing: "0.1em",
              pointerEvents: "none" }}>
              <span>H {typeof robotState.body_height === "number"
                ? robotState.body_height.toFixed(3) : "--"}m</span>
              <span>VX {Array.isArray(robotState.velocity)
                ? (robotState.velocity[0] ?? 0).toFixed(2) : "--"}</span>
              <span>LIVE CAM</span>
            </div>
          )}
        </div>

        {/* 自主模式切换 */}
        <div style={{ display: "flex", gap: 6, padding: "0 14px 8px",
          borderBottom: `1px solid ${BORDER}` }}>
          {[
            { key: "manual",       label: "完全遥控", icon: "◎" },
            { key: "reactive",     label: "自主反应", icon: "◉" },
            { key: "free_explore", label: "自由探索", icon: "◈" },
          ].map(({ key, label, icon }) => {
            const active = autonomyMode === key;
            const accent = key === "free_explore" ? "#f0a500"
                         : key === "reactive"     ? "#00d4ff"
                         : LIME;
            return (
              <button key={key} onClick={() => connected && switchAutonomy(key)} style={{
                flex: 1, padding: "8px 4px",
                background: active ? `${accent}18` : "transparent",
                color: active ? accent : "#2a2a2a",
                border: `1px solid ${active ? `${accent}40` : "#181818"}`,
                borderRadius: 4, fontSize: 11, fontWeight: 700,
                cursor: connected ? "pointer" : "not-allowed",
                letterSpacing: "0.07em", fontFamily: "inherit",
                WebkitTapHighlightColor: "transparent",
                transition: "all 0.15s",
                boxShadow: active ? `0 0 10px ${accent}20` : "none",
              }}>{icon} {label}</button>
            );
          })}
        </div>

        {/* 摇杆区 */}
        <div style={{ padding: "18px 16px 12px",
          display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <VirtualJoystick
            onMove={handleLeftMove} onStop={handleStop}
            disabled={!connected} label="MOVE" />
          <VirtualJoystick
            onMove={handleRightMove} onStop={handleStop}
            disabled={!connected} label="ROTATE" />
        </div>

        {/* 动作按钮 3×2 */}
        <div style={{ padding: "0 14px",
          display: "grid", gridTemplateColumns: "repeat(3,1fr)", gap: 6 }}>
          {ACTIONS.map(([lbl, cmd]) => (
            <button key={cmd} disabled={!connected}
              onClick={() => connected && sendCmd(cmd)}
              className="g2-act" style={actStyle}>
              {lbl}
            </button>
          ))}
        </div>

        {/* STOP 按钮 */}
        <div style={{ padding: "8px 14px 0" }}>
          <button disabled={!connected}
            onClick={() => connected && handleStop()}
            className="g2-stop"
            style={{
              width: "100%", padding: "11px 0",
              background: connected ? "rgba(255,68,85,0.1)" : "rgba(255,255,255,0.02)",
              color: connected ? "#ff4455" : "#1e1e1e",
              border: `1px solid ${connected ? "rgba(255,68,85,0.26)" : "#141414"}`,
              borderRadius: 5, fontSize: 12, fontWeight: 700,
              cursor: connected ? "pointer" : "not-allowed",
              fontFamily: "inherit", letterSpacing: "0.12em",
              WebkitTapHighlightColor: "transparent",
              transition: "all 0.1s",
            }}>
            ■ STOP
          </button>
        </div>

        {/* ── GO HOME ── */}
        <div style={{ padding: "8px 14px 0" }}>
          <button disabled={!connected || homing} onClick={goHome}
            className="g2-act"
            style={{
              width: "100%", padding: "11px 0",
              background: connected ? "rgba(200,255,62,0.07)" : "rgba(255,255,255,0.02)",
              color: connected ? LIME : "#1e1e1e",
              border: `1px solid ${connected ? "rgba(200,255,62,0.28)" : "#141414"}`,
              borderRadius: 5, fontSize: 12, fontWeight: 700,
              cursor: connected ? "pointer" : "not-allowed",
              fontFamily: "inherit", letterSpacing: "0.12em",
              WebkitTapHighlightColor: "transparent", transition: "all 0.1s",
            }}>
            {homing ? "···" : "⌂ HOME"}
          </button>
        </div>

        {/* ── WAYPOINTS ── */}
        <div style={{ borderTop: `1px solid ${BORDER}`, marginTop: 8 }}>
          <button onClick={() => setWpOpen(o => !o)} style={{
            width: "100%", background: "none", border: "none",
            display: "flex", alignItems: "center", justifyContent: "space-between",
            padding: "10px 14px", cursor: "pointer",
            fontFamily: "inherit", WebkitTapHighlightColor: "transparent",
          }}>
            <span style={{ fontSize: 9, color: "#252525", letterSpacing: "0.2em" }}>WAYPOINTS</span>
            <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
              {locations.length > 0 && (
                <span style={{ fontSize: 9, color: "rgba(200,255,62,0.35)",
                  letterSpacing: "0.1em" }}>{locations.length} PTS</span>
              )}
              <span style={{ fontSize: 10, color: "#2a2a2a", display: "inline-block",
                transform: wpOpen ? "rotate(180deg)" : "rotate(0deg)",
                transition: "transform 0.2s" }}>▾</span>
            </span>
          </button>

          {wpOpen && (
            <div style={{ padding: "0 14px 14px", animation: "fadeIn 0.15s ease" }}>
              <WaypointMap locations={locations} LIME={LIME} BORDER={BORDER} />

              <div style={{ marginTop: 8, display: "flex", flexDirection: "column", gap: 4 }}>
                {locations.length === 0 && (
                  <div style={{ fontSize: 10, color: "#1e1e1e", textAlign: "center",
                    padding: "10px 0", letterSpacing: "0.15em" }}>— NO WAYPOINTS —</div>
                )}
                {locations.map(loc => (
                  <div key={loc.name} style={{
                    display: "flex", alignItems: "center", gap: 6,
                    background: "rgba(200,255,62,0.025)",
                    border: `1px solid ${BORDER}`,
                    borderRadius: 4, padding: "7px 10px",
                  }}>
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ fontSize: 11, color: "rgba(200,255,62,0.7)",
                        letterSpacing: "0.05em", overflow: "hidden",
                        textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{loc.name}</div>
                      <div style={{ fontSize: 9, color: "#2e2e2e", letterSpacing: "0.07em", marginTop: 2 }}>
                        ({loc.x.toFixed(2)}, {loc.y.toFixed(2)})&nbsp;
                        {(loc.heading * 180 / Math.PI).toFixed(0)}°
                      </div>
                    </div>
                    <button onClick={() => navigateTo(loc.name)}
                      disabled={!connected || navigating === loc.name}
                      style={{
                        background: connected ? "rgba(200,255,62,0.07)" : "transparent",
                        color: connected ? "rgba(200,255,62,0.6)" : "#1e1e1e",
                        border: `1px solid ${connected ? "rgba(200,255,62,0.2)" : "#181818"}`,
                        borderRadius: 3, fontSize: 9, padding: "4px 8px",
                        cursor: connected ? "pointer" : "not-allowed",
                        fontFamily: "inherit", letterSpacing: "0.1em",
                        WebkitTapHighlightColor: "transparent",
                      }}>
                      {navigating === loc.name ? "···" : "GO"}
                    </button>
                    <button onClick={() => deleteLocation(loc.name)} style={{
                      background: "rgba(255,68,85,0.06)", color: "#ff4455",
                      border: "1px solid rgba(255,68,85,0.15)",
                      borderRadius: 3, fontSize: 11, padding: "3px 7px",
                      cursor: "pointer", fontFamily: "inherit", lineHeight: 1,
                      WebkitTapHighlightColor: "transparent",
                    }}>×</button>
                  </div>
                ))}
              </div>

              <div style={{ display: "flex", gap: 6, marginTop: 8 }}>
                <input value={tagName} onChange={e => setTagName(e.target.value)}
                  onKeyDown={e => e.key === "Enter" && connected && tagLocation()}
                  placeholder="标记当前位置名称..."
                  disabled={!connected}
                  style={{
                    flex: 1, background: "rgba(200,255,62,0.03)",
                    border: `1px solid ${BORDER}`, borderRadius: 4,
                    padding: "7px 10px", fontSize: 11,
                    color: connected ? "#aaa" : "#2a2a2a",
                    fontFamily: "inherit", outline: "none", letterSpacing: "0.05em",
                  }} />
                <button onClick={tagLocation}
                  disabled={!connected || !tagName.trim() || wpLoading}
                  style={{
                    background: connected && tagName.trim() ? DIM : "transparent",
                    color: connected && tagName.trim() ? LIME : "#252525",
                    border: `1px solid ${connected && tagName.trim() ? "rgba(200,255,62,0.3)" : "#1a1a1a"}`,
                    borderRadius: 4, fontSize: 10, padding: "7px 12px",
                    cursor: connected && tagName.trim() ? "pointer" : "not-allowed",
                    fontFamily: "inherit", letterSpacing: "0.1em",
                    WebkitTapHighlightColor: "transparent", whiteSpace: "nowrap",
                  }}>
                  {wpLoading ? "···" : "TAG"}
                </button>
              </div>

              <div style={{ marginTop: 6 }}>
                <button disabled={!connected || settingHome} onClick={setHome}
                  style={{
                    width: "100%", padding: "7px 0",
                    background: connected ? "rgba(200,255,62,0.03)" : "transparent",
                    color: connected ? "rgba(200,255,62,0.45)" : "#1e1e1e",
                    border: `1px solid ${connected ? "rgba(200,255,62,0.14)" : "#141414"}`,
                    borderRadius: 4, fontSize: 10, fontWeight: 700,
                    cursor: connected ? "pointer" : "not-allowed",
                    fontFamily: "inherit", letterSpacing: "0.1em",
                    WebkitTapHighlightColor: "transparent", whiteSpace: "nowrap",
                  }}>
                  {settingHome ? "···" : "SET HOME"}
                </button>
              </div>
            </div>
          )}
        </div>

        {/* ── AI 对话区 ── */}
        <div style={{ borderTop: `1px solid ${BORDER}`, marginTop: 10,
          display: "flex", flexDirection: "column", minHeight: 200, flex: 1 }}>
          <div style={{ fontSize: 9, color: "#252525", letterSpacing: "0.2em",
            padding: "10px 14px 0" }}>AI AGENT</div>
          <ChatPanel
            messages={messages}
            thinking={chatThinking}
            onSend={sendChat}
            placeholder={connected ? "对话控制，如：站起来跳个舞" : "未连接"}
            variant="inline"
            disabled={!connected}
          />
        </div>

      </div>
    </div>
  );
}

window.Go2DevicePage = Go2DevicePage;
