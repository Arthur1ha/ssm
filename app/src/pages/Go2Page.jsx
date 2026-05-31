/* Go2DevicePage — Tactical Command Interface
   通信：MJPEG + SSE + REST   无 WebRTC 前端代码
*/
function Go2DevicePage({ onBack }) {
  const { useState, useEffect, useRef, useCallback } = React;

  const LIME   = "#C8FF3E";
  const DIM    = "rgba(200,255,62,0.18)";
  const BG     = "#06080F";
  const PANEL  = "#0C0F1A";
  const BORDER = "rgba(200,255,62,0.15)";

  const [status,    setStatus]  = useState("idle");
  const [robotState,setRobot]   = useState(null);
  const [error,     setError]   = useState("");
  const [moving,    setMoving]  = useState(null);
  const [mode,      setMode]    = useState("normal");
  const moveRef  = useRef(null);
  const esRef    = useRef(null);
  const pollRef  = useRef(null);

  /* ── 状态轮询 ───────────────────────────────────────────────── */
  const pollStatus = useCallback(async () => {
    try {
      const d = await fetch("/api/go2/status").then(r => r.json());
      if (d.connected && status !== "connected") { setStatus("connected"); setError(""); }
      else if (!d.connected && status === "connected") { setStatus("idle"); setRobot(null); }
      else if (!d.connected && status === "connecting" && d.error) { setStatus("error"); setError(d.error); }
    } catch (_) {}
  }, [status]);

  useEffect(() => { pollRef.current = setInterval(pollStatus, 3000); return () => clearInterval(pollRef.current); }, [pollStatus]);

  /* ── SSE ─────────────────────────────────────────────────────── */
  useEffect(() => {
    if (status !== "connected") return;
    const es = new EventSource("/api/go2/state");
    esRef.current = es;
    es.onmessage = e => { try { const d = JSON.parse(e.data); if (d.mode !== undefined) setRobot(d); } catch (_) {} };
    return () => { es.close(); esRef.current = null; };
  }, [status]);

  useEffect(() => () => {
    if (moveRef.current) clearInterval(moveRef.current);
    if (esRef.current) esRef.current.close();
  }, []);

  /* ── 连接 / 断开 ──────────────────────────────────────────── */
  const connect = async () => {
    setError(""); setStatus("connecting");
    try {
      const r = await fetch("/api/go2/connect", { method: "POST" });
      if (!r.ok) throw new Error((await r.json()).detail || "连接失败");
    } catch (e) { setStatus("error"); setError(String(e)); }
  };

  const disconnect = async () => {
    if (moveRef.current) { clearInterval(moveRef.current); moveRef.current = null; }
    if (esRef.current)   { esRef.current.close(); esRef.current = null; }
    await fetch("/api/go2/disconnect", { method: "POST" });
    setStatus("idle"); setRobot(null); setMoving(null);
  };

  /* ── 指令 ─────────────────────────────────────────────────── */
  const sendCmd = (cmd, params) => fetch("/api/go2/command", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ cmd, params: params || {} }),
  });

  const switchMode = async m => {
    await fetch("/api/go2/mode", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode: m }),
    });
    setMode(m);
  };

  /* ── 移动 ────────────────────────────────────────────────── */
  const startMove = (x, y, z, label) => {
    if (moveRef.current) clearInterval(moveRef.current);
    setMoving(label);
    const fire = () => sendCmd("Move", { x, y, z });
    fire();
    moveRef.current = setInterval(fire, 500);
  };
  const stopMove = () => {
    if (moveRef.current) { clearInterval(moveRef.current); moveRef.current = null; }
    setMoving(null); sendCmd("StopMove");
  };

  const connected = status === "connected";
  const busy      = status === "connecting";

  /* ── 样式工厂 ──────────────────────────────────────────────── */
  const statusColor = { idle: "#555", connecting: "#f0a500", connected: LIME, error: "#ff4455" }[status];
  const statusLabel = { idle: "OFFLINE", connecting: "CONNECTING", connected: "ONLINE", error: "ERROR" }[status];

  const actionBtn = (disabled) => ({
    background: disabled ? "rgba(255,255,255,0.03)" : "rgba(200,255,62,0.07)",
    color: disabled ? "#333" : "rgba(200,255,62,0.8)",
    border: `1px solid ${disabled ? "#1a1a1a" : "rgba(200,255,62,0.2)"}`,
    borderRadius: 6, padding: "10px 0", fontSize: 12, fontWeight: 700,
    cursor: disabled ? "not-allowed" : "pointer", letterSpacing: "0.06em",
    fontFamily: "inherit", transition: "all 0.1s",
    WebkitTapHighlightColor: "transparent",
  });

  const dpadBtn = (disabled) => ({
    background: disabled ? "rgba(255,255,255,0.02)" : PANEL,
    color: disabled ? "#2a2a2a" : "rgba(200,255,62,0.7)",
    border: `1px solid ${disabled ? "#111" : "rgba(200,255,62,0.18)"}`,
    borderRadius: 8, display: "flex", alignItems: "center", justifyContent: "center",
    cursor: disabled ? "not-allowed" : "pointer", fontSize: 18,
    width: 54, height: 54,
    WebkitTapHighlightColor: "transparent",
    userSelect: "none",
    boxShadow: disabled ? "none" : "0 4px 8px rgba(0,0,0,0.5), inset 0 1px 0 rgba(255,255,255,0.04)",
  });

  const modeBtn = (m) => ({
    flex: 1, padding: "8px 4px", background: mode === m ? DIM : "transparent",
    color: mode === m ? LIME : "#444",
    border: `1px solid ${mode === m ? "rgba(200,255,62,0.4)" : "#1c1c1c"}`,
    borderRadius: 4, fontSize: 11, fontWeight: 700, cursor: connected ? "pointer" : "not-allowed",
    letterSpacing: "0.1em", fontFamily: "inherit",
    WebkitTapHighlightColor: "transparent",
    transition: "all 0.15s",
  });

  /* ── HUD bracket style (corner marks around video) ─────── */
  const cornerSize = 14;
  const cornerStyle = (top, right, bottom, left) => ({
    position: "absolute", width: cornerSize, height: cornerSize,
    borderColor: `rgba(200,255,62,0.6)`,
    borderStyle: "solid", borderWidth: 0,
    ...(top    !== undefined && { top:    2, borderTopWidth:    1.5 }),
    ...(right  !== undefined && { right:  2, borderRightWidth:  1.5 }),
    ...(bottom !== undefined && { bottom: 2, borderBottomWidth: 1.5 }),
    ...(left   !== undefined && { left:   2, borderLeftWidth:   1.5 }),
  });

  return (
    <div style={{ background: BG, color: "#ccc", minHeight: "100vh",
      fontFamily: "'Share Tech Mono', 'Courier New', monospace",
      display: "flex", flexDirection: "column" }}>

      {/* 字体加载 + 动画 */}
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=Share+Tech+Mono&display=swap');
        @keyframes scanline {
          0%   { transform: translateY(-100%); }
          100% { transform: translateY(100vh); }
        }
        @keyframes blink {
          0%,49% { opacity: 1; } 50%,100% { opacity: 0; }
        }
        @keyframes fadeIn {
          from { opacity:0; transform: translateY(6px); }
          to   { opacity:1; transform: translateY(0); }
        }
        .go2-action:active { transform: scale(0.94); filter: brightness(1.3); }
        .go2-dpad:active   { transform: scale(0.9);  filter: brightness(1.4); }
      `}</style>

      {/* ── Header ───────────────────────────────────────────── */}
      <div style={{ display: "flex", alignItems: "center", gap: 10,
        padding: "12px 16px", borderBottom: `1px solid ${BORDER}`,
        background: PANEL, flexShrink: 0 }}>
        <button onClick={onBack} style={{ background: "none", border: "none",
          color: "#555", cursor: "pointer", fontSize: 18, padding: "0 4px",
          fontFamily: "inherit", lineHeight: 1 }}>←</button>

        <div style={{ fontSize: 13, fontWeight: 700, letterSpacing: "0.15em",
          color: LIME, textShadow: `0 0 12px ${LIME}60` }}>
          GO2 AIR
        </div>

        <div style={{ fontSize: 9, color: "#333", letterSpacing: "0.2em", marginLeft: -4 }}>
          UNITREE
        </div>

        <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 8 }}>
          {/* 状态指示点 */}
          <div style={{ width: 7, height: 7, borderRadius: "50%",
            background: statusColor,
            boxShadow: connected ? `0 0 8px ${LIME}` : "none",
            animation: connected ? "none" : busy ? "blink 1s infinite" : "none" }} />
          <span style={{ fontSize: 10, color: statusColor, letterSpacing: "0.12em" }}>
            {statusLabel}
          </span>
          {/* 连接/断开按钮 */}
          {!connected
            ? <button onClick={connect} disabled={busy}
                style={{ background: busy ? "transparent" : DIM,
                  color: busy ? "#555" : LIME,
                  border: `1px solid ${busy ? "#2a2a2a" : "rgba(200,255,62,0.4)"}`,
                  borderRadius: 4, padding: "5px 12px", fontSize: 10,
                  cursor: busy ? "not-allowed" : "pointer",
                  letterSpacing: "0.1em", fontFamily: "inherit",
                  WebkitTapHighlightColor: "transparent" }}>
                {busy ? "CONNECTING" : "CONNECT"}
              </button>
            : <button onClick={disconnect}
                style={{ background: "rgba(255,68,85,0.12)", color: "#ff4455",
                  border: "1px solid rgba(255,68,85,0.3)",
                  borderRadius: 4, padding: "5px 12px", fontSize: 10,
                  cursor: "pointer", letterSpacing: "0.1em", fontFamily: "inherit",
                  WebkitTapHighlightColor: "transparent" }}>
                DISCONNECT
              </button>
          }
        </div>
      </div>

      {/* ── 错误提示 ─────────────────────────────────────────── */}
      {error && (
        <div style={{ background: "rgba(255,68,85,0.08)", borderBottom: "1px solid rgba(255,68,85,0.2)",
          padding: "8px 16px", fontSize: 11, color: "#ff6677",
          animation: "fadeIn 0.2s ease", flexShrink: 0 }}>
          ⚠ {error}
        </div>
      )}

      {/* 可滚动主体 */}
      <div style={{ flex: 1, overflowY: "auto", paddingBottom: 24 }}>

        {/* ── 视频区 ───────────────────────────────────────── */}
        <div style={{ position: "relative", width: "100%", aspectRatio: "16/9",
          background: "#000", overflow: "hidden" }}>

          {connected
            ? <img src="/api/go2/video" alt="Go2 视频"
                style={{ width: "100%", height: "100%", objectFit: "cover", display: "block" }} />
            : <div style={{ width: "100%", height: "100%", display: "flex",
                alignItems: "center", justifyContent: "center",
                flexDirection: "column", gap: 8 }}>
                <div style={{ fontSize: 11, color: "#1e2a1e", letterSpacing: "0.2em" }}>
                  NO SIGNAL
                </div>
                <div style={{ width: 40, height: 1, background: "#1a2a1a" }} />
                <div style={{ fontSize: 9, color: "#162016", letterSpacing: "0.15em" }}>
                  CONNECT TO ENABLE
                </div>
              </div>
          }

          {/* 扫描线动画 */}
          {connected && (
            <div style={{ position: "absolute", inset: 0, pointerEvents: "none",
              background: "linear-gradient(transparent 50%, rgba(0,0,0,0.04) 50%)",
              backgroundSize: "100% 3px", opacity: 0.4 }} />
          )}

          {/* HUD 角标 */}
          <div style={cornerStyle("top", undefined, undefined, "left")} />
          <div style={cornerStyle("top", "right", undefined, undefined)} />
          <div style={cornerStyle(undefined, undefined, "bottom", "left")} />
          <div style={cornerStyle(undefined, "right", "bottom", undefined)} />

          {/* 视频叠加信息 */}
          {connected && robotState && (
            <div style={{ position: "absolute", bottom: 8, left: 10, right: 10,
              display: "flex", justifyContent: "space-between",
              fontSize: 9, color: `${LIME}99`, letterSpacing: "0.1em",
              pointerEvents: "none" }}>
              <span>MODE {robotState.mode ?? "--"}</span>
              <span>H {typeof robotState.body_height === "number"
                ? robotState.body_height.toFixed(3) : "--"}m</span>
              <span>VX {Array.isArray(robotState.velocity)
                ? (robotState.velocity[0] ?? 0).toFixed(2) : "--"}</span>
              <span style={{ animation: "blink 1.2s infinite" }}>● REC</span>
            </div>
          )}

          {/* 移动状态叠加 */}
          {moving && (
            <div style={{ position: "absolute", top: 8, left: "50%",
              transform: "translateX(-50%)",
              background: "rgba(6,8,15,0.8)", border: `1px solid ${LIME}44`,
              borderRadius: 3, padding: "3px 10px",
              fontSize: 9, color: LIME, letterSpacing: "0.15em",
              pointerEvents: "none" }}>
              ▶ {moving.toUpperCase()}
            </div>
          )}
        </div>

        {/* ── 模式切换 ─────────────────────────────────────── */}
        <div style={{ padding: "10px 14px 8px", borderBottom: `1px solid ${BORDER}` }}>
          <div style={{ fontSize: 9, color: "#333", letterSpacing: "0.2em", marginBottom: 7 }}>
            MOTION MODE
          </div>
          <div style={{ display: "flex", gap: 6 }}>
            {["normal", "ai", "mcf"].map(m => (
              <button key={m} onClick={() => connected && switchMode(m)} style={modeBtn(m)}
                className="">
                {m.toUpperCase()}
              </button>
            ))}
          </div>
        </div>

        {/* ── 动作 + 方向控制双栏 ───────────────────────────── */}
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr",
          gap: 0, borderBottom: `1px solid ${BORDER}` }}>

          {/* 动作按钮 */}
          <div style={{ padding: "12px 14px", borderRight: `1px solid ${BORDER}` }}>
            <div style={{ fontSize: 9, color: "#333", letterSpacing: "0.2em", marginBottom: 10 }}>
              ACTIONS
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 6 }}>
              {[
                ["站起",  "StandUp"],
                ["坐下",  "StandDown"],
                ["挥手",  "Hello"],
                ["伸展",  "Stretch"],
                ["舞蹈1", "Dance1"],
                ["舞蹈2", "Dance2"],
              ].map(([label, cmd]) => (
                <button key={cmd} disabled={!connected}
                  onClick={() => connected && sendCmd(cmd)}
                  className="go2-action"
                  style={actionBtn(!connected)}>
                  {label}
                </button>
              ))}
            </div>
          </div>

          {/* 方向控制 */}
          <div style={{ padding: "12px 14px" }}>
            <div style={{ fontSize: 9, color: "#333", letterSpacing: "0.2em", marginBottom: 10 }}>
              MOVEMENT
            </div>

            {/* D-pad 十字 */}
            <div style={{ display: "flex", flexDirection: "column",
              alignItems: "center", gap: 5 }}>

              {/* 前进 */}
              <button className="go2-dpad" disabled={!connected} style={dpadBtn(!connected)}
                onPointerDown={() => connected && startMove(0.3, 0, 0, "前进")}
                onPointerUp={stopMove} onPointerLeave={stopMove} onPointerCancel={stopMove}>
                ▲
              </button>

              {/* 中排：左 / 停 / 右 */}
              <div style={{ display: "flex", gap: 5 }}>
                <button className="go2-dpad" disabled={!connected} style={dpadBtn(!connected)}
                  onPointerDown={() => connected && startMove(0, 0.3, 0, "左移")}
                  onPointerUp={stopMove} onPointerLeave={stopMove} onPointerCancel={stopMove}>
                  ◀
                </button>
                <button className="go2-dpad" disabled={!connected}
                  style={{ ...dpadBtn(!connected),
                    background: connected ? "rgba(255,68,85,0.12)" : "rgba(255,255,255,0.02)",
                    border: `1px solid ${connected ? "rgba(255,68,85,0.3)" : "#111"}`,
                    color: connected ? "#ff4455" : "#1a1a1a", fontSize: 14 }}
                  onClick={stopMove}>
                  ■
                </button>
                <button className="go2-dpad" disabled={!connected} style={dpadBtn(!connected)}
                  onPointerDown={() => connected && startMove(0, -0.3, 0, "右移")}
                  onPointerUp={stopMove} onPointerLeave={stopMove} onPointerCancel={stopMove}>
                  ▶
                </button>
              </div>

              {/* 后退 */}
              <button className="go2-dpad" disabled={!connected} style={dpadBtn(!connected)}
                onPointerDown={() => connected && startMove(-0.3, 0, 0, "后退")}
                onPointerUp={stopMove} onPointerLeave={stopMove} onPointerCancel={stopMove}>
                ▼
              </button>

              {/* 旋转 */}
              <div style={{ display: "flex", gap: 5, marginTop: 2 }}>
                <button className="go2-dpad" disabled={!connected}
                  style={{ ...dpadBtn(!connected), width: 54, fontSize: 14, letterSpacing: 0 }}
                  onPointerDown={() => connected && startMove(0, 0, 0.5, "左转")}
                  onPointerUp={stopMove} onPointerLeave={stopMove} onPointerCancel={stopMove}>
                  ↺
                </button>
                <button className="go2-dpad" disabled={!connected}
                  style={{ ...dpadBtn(!connected), width: 54, fontSize: 14, letterSpacing: 0 }}
                  onPointerDown={() => connected && startMove(0, 0, -0.5, "右转")}
                  onPointerUp={stopMove} onPointerLeave={stopMove} onPointerCancel={stopMove}>
                  ↻
                </button>
              </div>
            </div>
          </div>
        </div>

        {/* ── 底部状态栏 ──────────────────────────────────── */}
        {connected && robotState && (
          <div style={{ padding: "10px 14px",
            display: "grid", gridTemplateColumns: "repeat(3,1fr)", gap: 8,
            animation: "fadeIn 0.3s ease" }}>
            {[
              { k: "MODE",  v: robotState.mode ?? "--" },
              { k: "HEIGHT", v: typeof robotState.body_height === "number"
                  ? robotState.body_height.toFixed(3) + "m" : "--" },
              { k: "VX",    v: Array.isArray(robotState.velocity)
                  ? (robotState.velocity[0] ?? 0).toFixed(2) : "--" },
            ].map(({ k, v }) => (
              <div key={k} style={{ textAlign: "center",
                background: PANEL, border: `1px solid ${BORDER}`,
                borderRadius: 6, padding: "8px 4px" }}>
                <div style={{ fontSize: 8, color: "#333", letterSpacing: "0.2em", marginBottom: 4 }}>
                  {k}
                </div>
                <div style={{ fontSize: 15, fontWeight: 700, color: LIME,
                  textShadow: `0 0 8px ${LIME}60` }}>
                  {v}
                </div>
              </div>
            ))}
          </div>
        )}

      </div>
    </div>
  );
}

window.Go2DevicePage = Go2DevicePage;
