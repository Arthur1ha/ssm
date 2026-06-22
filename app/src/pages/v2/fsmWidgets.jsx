/* fsmWidgets — 按 card.widgets 声明渲染态内富控件（type 枚举：connection/joystick/video/map）。 */
function ConnectionWidget({ widget }) {
  const { useEffect, useState, useCallback, useRef } = React;
  const endpoint = widget.endpoint || '/api/go2/connection';
  const statusEndpoint = widget.status_endpoint || endpoint;
  const [status, setStatus] = useState({ connected: false, fsm_state: 'offline', error: '' });
  const [busy, setBusy] = useState(false);
  const [checked, setChecked] = useState(false);
  const connectRequestedRef = useRef(false);

  const refresh = useCallback(() => {
    fetch(statusEndpoint)
      .then(r => r.json())
      .then(d => {
        setStatus({
          connected: d.connected === true,
          fsm_state: d.fsm_state || (d.connected ? 'standing' : 'offline'),
          error: d.error || '',
        });
        setChecked(true);
      })
      .catch(() => {
        setStatus(prev => ({
          ...prev,
          connected: false,
          fsm_state: 'offline',
          error: '连接入口不可达，请确认 API 服务正在运行。',
        }));
        setChecked(true);
      });
  }, [statusEndpoint]);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 1500);
    return () => clearInterval(id);
  }, [refresh]);

  const connect = useCallback(() => {
    if (busy) return;
    setBusy(true);
    fetch(endpoint, { method: 'POST' })
      .then(async r => {
        const data = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(data.detail || String(r.status));
        setStatus(prev => ({ ...prev, fsm_state: 'connecting', error: '' }));
        setTimeout(refresh, 700);
      })
      .catch(err => setStatus(prev => ({
        ...prev,
        error: err?.message || '连接请求没有送达，请检查 API 端口和 Go2 配置。',
      })))
      .finally(() => setBusy(false));
  }, [busy, endpoint, refresh]);

  const disconnect = () => {
    if (busy) return;
    setBusy(true);
    fetch(endpoint, { method: 'DELETE' })
      .then(() => setTimeout(refresh, 400))
      .catch(() => setStatus(prev => ({ ...prev, error: '断开请求失败。' })))
      .finally(() => setBusy(false));
  };

  const connected = status.connected === true;
  const connecting = status.fsm_state === 'connecting' || busy;

  useEffect(() => {
    if (!checked || !widget.auto_connect || connected || connecting || connectRequestedRef.current) return;
    connectRequestedRef.current = true;
    connect();
  }, [checked, widget.auto_connect, connected, connecting, connect]);

  if (widget.visible === false) return null;

  return (
    <div style={{
      marginTop: 12,
      padding: '10px 12px',
      border: '1px solid var(--color-border)',
      borderRadius: 'var(--radius-card)',
      background: 'var(--color-surface-1)',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <span style={{
          width: 7, height: 7, borderRadius: '50%',
          background: connected ? 'var(--color-online)' : 'var(--color-offline)',
          boxShadow: connected ? '0 0 6px var(--color-online-glow)' : 'none',
          flexShrink: 0,
        }}/>
        <div style={{
          flex: 1,
          color: 'var(--color-text-muted)',
          fontSize: 11,
          letterSpacing: '0.08em',
          fontFamily: 'var(--font-mono)',
        }}>
          {(status.fsm_state || 'offline').toUpperCase()}
        </div>
        <button
          className="btn"
          onClick={connected ? disconnect : connect}
          disabled={connecting}
          style={{
            padding: '7px 12px',
            background: connected ? 'var(--color-surface-2)' : 'var(--color-accent)',
            color: connected ? 'var(--color-text-muted)' : 'var(--color-bg)',
            borderColor: connected ? 'var(--color-border)' : 'var(--color-accent-border)',
            cursor: connecting ? 'default' : 'pointer',
          }}
        >
          {connecting ? '连接中...' : (connected ? '断开' : '连接机器狗')}
        </button>
      </div>
      {status.error && (
        <div style={{
          marginTop: 8,
          color: 'var(--color-danger)',
          fontSize: 11,
          lineHeight: 1.45,
        }}>
          {status.error}
        </div>
      )}
    </div>
  );
}

function publishMqttTask(unitId, action, params) {
  const task_id = 'widget_' + Date.now();
  mqttBus.publish(`ssm/task/${unitId}/${task_id}`, {
    task_id,
    session_id: task_id,
    action,
    params,
    ts: Date.now(),
  });
}

function ColorSwatchesWidget({ unitId, widget }) {
  const swatches = widget.swatches || [];
  if (!swatches.length) return null;

  const sendColor = (swatch) => {
    publishMqttTask(unitId, widget.action || 'SET_COLOR', {
      r: Number(swatch.r) || 0,
      g: Number(swatch.g) || 0,
      b: Number(swatch.b) || 0,
      brightness: Number(swatch.brightness ?? 180),
    });
  };

  return (
    <div style={{
      padding: '8px 12px 0',
      display: 'flex',
      gap: 8,
      overflowX: 'auto',
      scrollbarWidth: 'none',
    }}>
      {swatches.map((swatch, idx) => {
        const rgb = `rgb(${swatch.r}, ${swatch.g}, ${swatch.b})`;
        const isLight = Number(swatch.r) + Number(swatch.g) + Number(swatch.b) > 690;
        return (
          <button
            key={(swatch.label || 'color') + idx}
            className="btn"
            onClick={() => sendColor(swatch)}
            title={swatch.label}
            aria-label={swatch.label}
            style={{
              flexShrink: 0,
              minWidth: 64,
              padding: '7px 10px',
              display: 'inline-flex',
              alignItems: 'center',
              justifyContent: 'center',
              gap: 6,
              background: 'var(--color-surface-1)',
              color: 'var(--color-text-muted)',
              borderColor: 'var(--color-border)',
              borderRadius: 'var(--radius-sm)',
              fontSize: 11,
              whiteSpace: 'nowrap',
              cursor: 'pointer',
            }}
          >
            <span style={{
              width: 12,
              height: 12,
              borderRadius: '50%',
              background: rgb,
              border: isLight ? '1px solid var(--color-border-strong)' : '1px solid transparent',
              boxShadow: `0 0 8px ${rgb}`,
              flexShrink: 0,
            }}/>
            <span>{swatch.label}</span>
          </button>
        );
      })}
    </div>
  );
}

function fsmWidget(unitId, state, widgets) {
  if (!widgets || !widgets.length) return null;

  const VirtualJoystick = window.VirtualJoystick;
  const VideoCanvas = window.VideoCanvas;

  // 只渲染当前状态匹配的 widget（states 为空=全程显示）
  const active = widgets.filter(w => !w.states || !w.states.length || w.states.includes(state));
  if (!active.length) return null;

  const sendVelocity = (endpoint, vx, vy, vyaw) => fetch(endpoint, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ vx, vy, vyaw }),
  }).catch(e => console.error('[fsmWidget] velocity failed:', e));

  const nodes = active.map((w, idx) => {
    if (w.type === 'color_swatches') {
      return <ColorSwatchesWidget key={'color' + idx} unitId={unitId} widget={w}/>;
    }
    if (w.type === 'connection') {
      return <ConnectionWidget key={'conn' + idx} widget={w}/>;
    }
    if (w.type === 'joystick' && VirtualJoystick) {
      const ep = w.endpoint || '/api/go2/velocity';
      return (
        <div key={'joy' + idx} style={{ marginTop: 14, display: 'flex', justifyContent: 'space-between' }}>
          <VirtualJoystick label="MOVE" disabled={false}
            onMove={(dx, dy) => sendVelocity(ep, +(-dy * 0.5).toFixed(2), +(-dx * 0.5).toFixed(2), 0)}
            onStop={() => sendVelocity(ep, 0, 0, 0)}/>
          <VirtualJoystick label="ROTATE" disabled={false}
            onMove={(dx) => sendVelocity(ep, 0, 0, +(-dx * 0.8).toFixed(2))}
            onStop={() => sendVelocity(ep, 0, 0, 0)}/>
        </div>
      );
    }
    if (w.type === 'video' && VideoCanvas) {
      // w.endpoint 字段（如 /api/go2/video）暂未消费：
      // VideoCanvas 内部硬编码抓帧路径（/api/go2/video/snapshot），不接受外部 url prop。
      return (
        <div key={'cam' + idx} style={{ marginTop: 14, aspectRatio: '16/9', background: '#0a0c14',
          border: '1px solid var(--color-accent-border)', borderRadius: 'var(--radius-sm)',
          overflow: 'hidden' }}>
          <VideoCanvas connected={true}/>
        </div>
      );
    }
    return null;
  }).filter(Boolean);

  if (!nodes.length) return null;
  return nodes;
}
window.fsmWidget = fsmWidget;
