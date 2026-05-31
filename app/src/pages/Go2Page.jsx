/* Go2DevicePage — Unitree Go2 Air 控制页面
   通信：MJPEG (<img>) + SSE (EventSource) + REST (fetch)
   无 WebRTC 代码，无外部依赖。
*/
function Go2DevicePage({ onBack }) {
  const { useState, useEffect, useRef, useCallback } = React;

  const [status, setStatus]    = useState("idle"); // idle|connecting|connected|error
  const [robotState, setRobot] = useState(null);
  const [error, setError]      = useState("");
  const [moving, setMoving]    = useState(null);
  const moveIntervalRef        = useRef(null);
  const esRef                  = useRef(null);
  const pollRef                = useRef(null);

  const LIME = "#C8FF3E";

  // ── 状态轮询（每 3s，检测连接状态变化）───────────────────────────
  const pollStatus = useCallback(async () => {
    try {
      const r = await fetch("/api/go2/status");
      const data = await r.json();
      if (data.connected && status !== "connected") {
        setStatus("connected");
      } else if (!data.connected && status === "connected") {
        setStatus("idle");
        setRobot(null);
      }
    } catch (_) {}
  }, [status]);

  useEffect(() => {
    pollRef.current = setInterval(pollStatus, 3000);
    return () => clearInterval(pollRef.current);
  }, [pollStatus]);

  // ── SSE 状态订阅（已连接时启动）─────────────────────────────────
  useEffect(() => {
    if (status !== "connected") return;
    const es = new EventSource("/api/go2/state");
    esRef.current = es;
    es.onmessage = (e) => {
      try {
        const d = JSON.parse(e.data);
        if (d.mode !== undefined) setRobot(d);
      } catch (_) {}
    };
    return () => { es.close(); esRef.current = null; };
  }, [status]);

  // ── 清理 ─────────────────────────────────────────────────────────
  useEffect(() => () => {
    if (moveIntervalRef.current) clearInterval(moveIntervalRef.current);
    if (esRef.current) esRef.current.close();
  }, []);

  // ── 连接 / 断开 ──────────────────────────────────────────────────
  const connect = async () => {
    setError("");
    setStatus("connecting");
    try {
      const r = await fetch("/api/go2/connect", { method: "POST" });
      if (!r.ok) throw new Error((await r.json()).detail || await r.text());
    } catch (e) {
      setStatus("error");
      setError(String(e));
    }
  };

  const disconnect = async () => {
    if (moveIntervalRef.current) { clearInterval(moveIntervalRef.current); moveIntervalRef.current = null; }
    if (esRef.current) { esRef.current.close(); esRef.current = null; }
    await fetch("/api/go2/disconnect", { method: "POST" });
    setStatus("idle");
    setRobot(null);
    setMoving(null);
  };

  // ── 指令发送 ─────────────────────────────────────────────────────
  const sendCmd = (cmd, params) => {
    fetch("/api/go2/command", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cmd, params: params || {} }),
    });
  };

  // ── 持续移动 ─────────────────────────────────────────────────────
  const startMove = (x, y, z, label) => {
    if (moveIntervalRef.current) clearInterval(moveIntervalRef.current);
    setMoving(label);
    const fire = () => sendCmd("Move", { x, y, z });
    fire();
    moveIntervalRef.current = setInterval(fire, 500);
  };

  const stopMove = () => {
    if (moveIntervalRef.current) { clearInterval(moveIntervalRef.current); moveIntervalRef.current = null; }
    setMoving(null);
    sendCmd("StopMove");
  };

  // ── UI ────────────────────────────────────────────────────────────
  const connected = status === "connected";
  const busy      = status === "connecting";

  const statusLabel = { idle: "未连接", connecting: "连接中…", connected: "已连接 ✓", error: "连接失败" }[status];
  const statusColor = { idle: "#666", connecting: "#f0a500", connected: LIME, error: "#ff4444" }[status];

  const btn = (color = "#2a2a2a", disabled = false) => ({
    background: disabled ? "#222" : color,
    color: disabled ? "#555" : color === LIME ? "#000" : "#fff",
    border: "none", borderRadius: 8, padding: "10px 16px",
    fontWeight: 600, fontSize: 14, cursor: disabled ? "not-allowed" : "pointer",
    WebkitTapHighlightColor: "transparent",
  });

  return (
    <div style={{ background: "#0B0B0E", color: "#fff", minHeight: "100vh", fontFamily: "system-ui, sans-serif" }}>

      {/* Header */}
      <div style={{ display: "flex", alignItems: "center", gap: 12, padding: "16px 20px", borderBottom: "1px solid #1a1a1a" }}>
        <button onClick={onBack} style={{ background: "none", border: "none", color: "#888", cursor: "pointer", fontSize: 20, padding: 0 }}>←</button>
        <span style={{ fontSize: 17, fontWeight: 700 }}>Go2 Air</span>
        <span style={{ marginLeft: "auto", fontSize: 13, color: statusColor, fontWeight: 600 }}>{statusLabel}</span>
      </div>

      <div style={{ padding: "16px 20px", display: "flex", flexDirection: "column", gap: 14 }}>

        {/* 连接 / 断开 */}
        {!connected
          ? <button onClick={connect} disabled={busy} style={btn(LIME, busy)}>{busy ? "连接中…" : "连接 Go2"}</button>
          : <button onClick={disconnect} style={btn("#ff4444")}>断开连接</button>
        }

        {error && (
          <div style={{ background: "#1a0000", border: "1px solid #ff4444", borderRadius: 8, padding: "10px 14px", fontSize: 13, color: "#ff6666" }}>
            {error}
          </div>
        )}

        {/* 视频流 */}
        <div style={{ borderRadius: 12, overflow: "hidden", background: "#000", aspectRatio: "16/9", display: "flex", alignItems: "center", justifyContent: "center" }}>
          {connected
            ? <img src="/api/go2/video" alt="Go2 视频流"
                style={{ width: "100%", height: "100%", objectFit: "cover", display: "block" }} />
            : <span style={{ color: "#333", fontSize: 13 }}>视频流（连接后显示）</span>
          }
        </div>

        {/* 状态面板 */}
        {robotState && (
          <div style={{ background: "#141414", borderRadius: 12, padding: "12px 16px", display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 8 }}>
            {[
              { label: "模式", value: robotState.mode ?? "—" },
              { label: "体高 m", value: typeof robotState.body_height === "number" ? robotState.body_height.toFixed(3) : "—" },
              { label: "速度", value: Array.isArray(robotState.velocity) ? robotState.velocity[0]?.toFixed(2) : "—" },
            ].map(({ label, value }) => (
              <div key={label} style={{ textAlign: "center" }}>
                <div style={{ fontSize: 11, color: "#555", marginBottom: 3 }}>{label}</div>
                <div style={{ fontSize: 16, fontWeight: 700, color: LIME }}>{value}</div>
              </div>
            ))}
          </div>
        )}

        {/* 动作按钮 */}
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
          {[
            ["站起",  "StandUp"],
            ["坐下",  "StandDown"],
            ["挥手",  "Hello"],
            ["伸展",  "Stretch"],
            ["舞蹈1", "Dance1"],
            ["舞蹈2", "Dance2"],
          ].map(([label, cmd]) => (
            <button key={cmd} disabled={!connected} onClick={() => sendCmd(cmd)} style={btn("#1e1e1e", !connected)}>
              {label}
            </button>
          ))}
        </div>

        {/* 移动控制 */}
        <div style={{ background: "#141414", borderRadius: 12, padding: 16 }}>
          <div style={{ fontSize: 12, color: "#555", textAlign: "center", marginBottom: 10 }}>
            移动控制{moving && <span style={{ color: LIME }}>（{moving}）</span>}
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 8, maxWidth: 180, margin: "0 auto" }}>
            <div />
            <button disabled={!connected} style={btn("#1e1e1e", !connected)}
              onPointerDown={() => startMove(0.3, 0, 0, "前进")} onPointerUp={stopMove} onPointerLeave={stopMove}>▲</button>
            <div />
            <button disabled={!connected} style={btn("#1e1e1e", !connected)}
              onPointerDown={() => startMove(0, 0.3, 0, "左移")} onPointerUp={stopMove} onPointerLeave={stopMove}>◀</button>
            <button disabled={!connected} style={btn("#ff4444", !connected)} onClick={stopMove}>■</button>
            <button disabled={!connected} style={btn("#1e1e1e", !connected)}
              onPointerDown={() => startMove(0, -0.3, 0, "右移")} onPointerUp={stopMove} onPointerLeave={stopMove}>▶</button>
            <div />
            <button disabled={!connected} style={btn("#1e1e1e", !connected)}
              onPointerDown={() => startMove(-0.3, 0, 0, "后退")} onPointerUp={stopMove} onPointerLeave={stopMove}>▼</button>
            <div />
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8, maxWidth: 180, margin: "10px auto 0" }}>
            <button disabled={!connected} style={btn("#1e1e1e", !connected)}
              onPointerDown={() => startMove(0, 0, 0.5, "左转")} onPointerUp={stopMove} onPointerLeave={stopMove}>↺ 左转</button>
            <button disabled={!connected} style={btn("#1e1e1e", !connected)}
              onPointerDown={() => startMove(0, 0, -0.5, "右转")} onPointerUp={stopMove} onPointerLeave={stopMove}>↻ 右转</button>
          </div>
        </div>

      </div>
    </div>
  );
}
