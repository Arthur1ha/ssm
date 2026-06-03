/* Go2DevicePage — Mobile 双摇杆界面
   通信：MJPEG + SSE + REST   无 WebRTC 前端代码
*/

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

function Go2DevicePage({ onBack }) {
  const { useState, useEffect, useRef, useCallback } = React;

  const LIME   = "#C8FF3E";
  const DIM    = "rgba(200,255,62,0.13)";
  const BG     = "#06080F";
  const PANEL  = "#0C0F1A";
  const BORDER = "rgba(200,255,62,0.1)";

  const [status,      setStatus]   = useState("idle");
  const [robotState,  setRobot]    = useState(null);
  const [error,       setError]    = useState("");
  const [mode,        setMode]     = useState("normal");
  const [chatMsg,     setChatMsg]  = useState("");
  const [chatResp,    setChatResp] = useState("");
  const [chatLoading, setChatLoad] = useState(false);
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

  const switchMode = async m => {
    await fetch("/api/go2/mode", {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode: m }),
    });
    setMode(m);
  };

  /* ── 摇杆回调 ── */
  // 左摇杆：dx右正→y负(右移)，dy下正→x负(后退)
  const handleLeftMove = useCallback((dx, dy) => {
    sendCmd("Move", {
      x: parseFloat((-dy * 0.5).toFixed(2)),
      y: parseFloat((-dx * 0.5).toFixed(2)),
      z: 0,
    });
  }, []);

  // 右摇杆：仅旋转，dx右正→z负(右转)
  const handleRightMove = useCallback((dx, _dy) => {
    sendCmd("Move", { x: 0, y: 0, z: parseFloat((-dx * 0.8).toFixed(2)) });
  }, []);

  const handleStop = useCallback(() => sendCmd("StopMove"), []);

  /* ── AI 对话 ── */
  const sendChat = useCallback(async () => {
    if (!chatMsg.trim() || !connected || chatLoading) return;
    const msg = chatMsg.trim();
    setChatMsg("");
    setChatLoad(true);
    setChatResp("");
    try {
      const r = await fetch("/api/go2/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: msg }),
      });
      const d = await r.json();
      setChatResp(d.response || "");
    } catch (_) {
      setChatResp("请求失败，请检查连接");
    } finally {
      setChatLoad(false);
    }
  }, [chatMsg, connected, chatLoading]);

  const connected = status === "connected";
  const busy      = status === "connecting";

  const statusColor = { idle: "#444", connecting: "#f0a500", connected: LIME, error: "#ff4455" }[status];
  const statusLabel = { idle: "OFFLINE", connecting: "CONNECTING", connected: "ONLINE", error: "ERROR" }[status];

  const MODES = [
    { key: "normal", label: "标准" },
    { key: "ai",     label: "AI 避障" },
    { key: "mcf",    label: "MCF" },
  ];

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
    <div style={{ background: BG, color: "#ccc", minHeight: "100vh",
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

        {/* 视频 */}
        <div style={{ position: "relative", width: "100%", aspectRatio: "16/9",
          background: "#000", overflow: "hidden", flexShrink: 0 }}>
          {connected
            ? <img src="/api/go2/video" alt="Go2 视频"
                style={{ width: "100%", height: "100%", objectFit: "cover", display: "block" }} />
            : <div style={{ width: "100%", height: "100%", display: "flex",
                alignItems: "center", justifyContent: "center",
                flexDirection: "column", gap: 8 }}>
                <div style={{ fontSize: 11, color: "#172017", letterSpacing: "0.2em" }}>NO SIGNAL</div>
                <div style={{ width: 28, height: 1, background: "#121812" }} />
                <div style={{ fontSize: 9, color: "#0f170f", letterSpacing: "0.15em" }}>CONNECT TO ENABLE</div>
              </div>
          }

          {/* 扫描线 */}
          {connected && (
            <div style={{ position: "absolute", inset: 0, pointerEvents: "none",
              background: "linear-gradient(transparent 50%, rgba(0,0,0,0.04) 50%)",
              backgroundSize: "100% 3px", opacity: 0.3 }} />
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
              <span>MODE {robotState.mode ?? "--"}</span>
              <span>H {typeof robotState.body_height === "number"
                ? robotState.body_height.toFixed(3) : "--"}m</span>
              <span>VX {Array.isArray(robotState.velocity)
                ? (robotState.velocity[0] ?? 0).toFixed(2) : "--"}</span>
              <span style={{ animation: "blink 1.2s infinite" }}>● REC</span>
            </div>
          )}
        </div>

        {/* 模式切换 */}
        <div style={{ display: "flex", gap: 6, padding: "8px 14px",
          borderBottom: `1px solid ${BORDER}` }}>
          {MODES.map(({ key, label }) => (
            <button key={key} onClick={() => connected && switchMode(key)} style={{
              flex: 1, padding: "7px 4px",
              background: mode === key ? DIM : "transparent",
              color: mode === key ? LIME : "#303030",
              border: `1px solid ${mode === key ? "rgba(200,255,62,0.3)" : "#181818"}`,
              borderRadius: 4, fontSize: 11, fontWeight: 700,
              cursor: connected ? "pointer" : "not-allowed",
              letterSpacing: "0.07em", fontFamily: "inherit",
              WebkitTapHighlightColor: "transparent",
              transition: "all 0.15s",
            }}>{label}</button>
          ))}
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
            onClick={() => connected && sendCmd("StopMove")}
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

        {/* ── AI 对话区 ── */}
        <div style={{ padding: "10px 14px 4px",
          borderTop: `1px solid ${BORDER}`, marginTop: 10 }}>
          <div style={{ fontSize: 9, color: "#252525", letterSpacing: "0.2em",
            marginBottom: 7 }}>AI AGENT</div>
          <div style={{ display: "flex", gap: 6 }}>
            <input
              value={chatMsg}
              onChange={e => setChatMsg(e.target.value)}
              onKeyDown={e => e.key === "Enter" && sendChat()}
              placeholder={connected ? "对话控制，如：站起来跳个舞" : "未连接"}
              disabled={!connected || chatLoading}
              style={{
                flex: 1,
                background: "rgba(200,255,62,0.025)",
                border: `1px solid ${connected ? "rgba(200,255,62,0.1)" : "#181818"}`,
                borderRadius: 5, padding: "9px 11px",
                color: connected ? "#bbb" : "#2a2a2a",
                fontSize: 11, fontFamily: "inherit",
                outline: "none",
                WebkitTapHighlightColor: "transparent",
              }}
            />
            <button
              onClick={sendChat}
              disabled={!connected || chatLoading || !chatMsg.trim()}
              style={{
                background: connected && chatMsg.trim() && !chatLoading ? DIM : "transparent",
                color: connected && chatMsg.trim() && !chatLoading ? LIME : "#222",
                border: `1px solid ${connected && chatMsg.trim() ? "rgba(200,255,62,0.25)" : "#181818"}`,
                borderRadius: 5, padding: "9px 14px",
                fontSize: 11, fontFamily: "inherit",
                cursor: connected && chatMsg.trim() && !chatLoading ? "pointer" : "not-allowed",
                letterSpacing: "0.08em",
                WebkitTapHighlightColor: "transparent",
                transition: "all 0.1s",
              }}
            >{chatLoading ? "···" : "发送"}</button>
          </div>
          {chatResp && (
            <div style={{
              marginTop: 8, padding: "9px 11px",
              background: "rgba(200,255,62,0.02)",
              border: "1px solid rgba(200,255,62,0.07)",
              borderRadius: 5, fontSize: 11,
              color: "rgba(200,255,62,0.55)",
              letterSpacing: "0.03em", lineHeight: 1.5,
              animation: "fadeIn 0.2s ease",
            }}>{chatResp}</div>
          )}
        </div>

      </div>
    </div>
  );
}

window.Go2DevicePage = Go2DevicePage;
