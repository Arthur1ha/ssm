/* fsmWidgets — 按 card.widgets 声明组合当前状态的富控件。 */
function ConnectionWidget({ widget, unitId, actor }) {
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
    commandBus.command(unitId, 'connect', {})
      .then(data => {
        if (data.result === 'error') throw new Error(data.error || '连接失败');
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
    commandBus.command(unitId, 'disconnect', {})
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
        <div className="feedback-button"><button
          className="btn"
          onClick={connected ? disconnect : connect}
          disabled={connecting || (connected && actor !== 'admin')}
          style={{
            padding: '7px 12px',
            background: connected ? 'var(--color-surface-2)' : 'var(--color-accent)',
            color: connected ? 'var(--color-text-muted)' : 'var(--color-bg)',
            borderColor: connected ? 'var(--color-border)' : 'var(--color-accent-border)',
            cursor: connecting || (connected && actor !== 'admin') ? 'default' : 'pointer',
          }}
        >
          {connecting ? '连接中...' : (connected ? (actor === 'admin' ? '断开' : '已连接') : '连接机器狗')}
        </button>
        {status.error && <small className="action-feedback action-feedback--error" role="status" title={status.error}>{status.error}</small>}
        </div>
      </div>
    </div>
  );
}

function ColorSwatchesWidget({ unitId, widget, report, ledState }) {
  const { useState, useEffect, useRef } = React;
  const swatches = widget.swatches || [];
  const channelLabels = { r: '红色', g: '绿色', b: '蓝色', brightness: '亮度' };
  const initial = () => ({
    r: ledState?.r ?? 255, g: ledState?.g ?? 255,
    b: ledState?.b ?? 255, brightness: ledState?.brightness ?? 180,
  });
  const [draft, setDraft] = useState(initial);
  const lastSent = useRef('');

  useEffect(() => {
    if (!['ON', 'BRIGHT', 'DIM', 'COLOR'].includes(ledState?.ism)) return;
    const next = initial();
    setDraft(next);
    lastSent.current = JSON.stringify(next);
  }, [ledState?.r, ledState?.g, ledState?.b, ledState?.brightness, ledState?.ism]);

  const sendColor = values => {
    const next = {
      r: Number(values.r), g: Number(values.g), b: Number(values.b),
      brightness: Number(values.brightness),
    };
    const key = JSON.stringify(next);
    if (key === lastSent.current) return;
    lastSent.current = key;
    setDraft(next);
    report({ result: 'executing' });
    commandBus.mqtt(unitId, widget.action || 'SET_COLOR', next)
      .then(report).catch(error => report({ result: 'error', error: error.message }));
  };

  return <div className="device-led-controls">
    <div className="device-led-controls__sliders">
      {(widget.channels || ['r', 'g', 'b', 'brightness']).map(channel => <label key={channel} className="device-led-controls__slider">
        <span>{channelLabels[channel] || channel}</span>
        <input type="range" min={channel === 'brightness' ? 1 : 0} max="255" step="1"
          value={draft[channel]} aria-label={channelLabels[channel] || channel}
          onChange={event => setDraft(prev => ({ ...prev, [channel]: Number(event.target.value) }))}
          onPointerUp={event => sendColor({ ...draft, [channel]: Number(event.currentTarget.value) })}
          onKeyUp={event => sendColor({ ...draft, [channel]: Number(event.currentTarget.value) })}
          onBlur={event => sendColor({ ...draft, [channel]: Number(event.currentTarget.value) })}/>
        <output>{draft[channel]}</output>
      </label>)}
    </div>
    {swatches.length > 0 && <div className="device-led-controls__presets" aria-label="预设颜色">
      {swatches.map((swatch, idx) => <button key={(swatch.label || 'color') + idx}
        type="button" onClick={() => sendColor(swatch)}>
        <i style={{ background: `rgb(${swatch.r}, ${swatch.g}, ${swatch.b})` }}/>
        {swatch.label}
      </button>)}
    </div>}
  </div>;
}

function fsmWidget(unitId, state, widgets, report = () => {}, actor = 'visitor', ledState = null) {
  if (!widgets || !widgets.length) return null;

  const VirtualJoystick = window.VirtualJoystick;
  const VideoCanvas = window.VideoCanvas;

  // 只渲染当前状态匹配的 widget（states 为空=全程显示）
  const active = widgets.filter(w => !w.states || !w.states.length || w.states.includes(state));
  if (!active.length) return null;

  const sendVelocity = (_endpoint, vx, vy, vyaw) => commandBus.velocity(
    unitId, { vx, vy, vyaw }, result => {
      if (result?.result !== 'accepted' && result?.result !== 'ok') report(result);
    },
  );

  const nodes = active.map((w, idx) => {
    if (w.type === 'led_color' || w.type === 'color_swatches') {
      return <ColorSwatchesWidget key={'color' + idx} unitId={unitId} widget={w} report={report} ledState={ledState}/>;
    }
    if (w.type === 'connection') {
      return <ConnectionWidget key={'conn' + idx} widget={w} unitId={unitId} actor={actor}/>;
    }
    if (w.type === 'joystick' && VirtualJoystick) {
      const ep = w.endpoint || '/api/go2/velocity';
      return (
        <div key={'joy' + idx} className="device-joysticks">
          <VirtualJoystick label="移动" radius={36} disabled={false}
            onMove={(dx, dy) => sendVelocity(ep, +(-dy * 0.5).toFixed(2), +(-dx * 0.5).toFixed(2), 0)}
            onStop={() => sendVelocity(ep, 0, 0, 0)}/>
          <VirtualJoystick label="转向" radius={36} disabled={false}
            onMove={(dx) => sendVelocity(ep, 0, 0, +(-dx * 0.8).toFixed(2))}
            onStop={() => sendVelocity(ep, 0, 0, 0)}/>
        </div>
      );
    }
    if (w.type === 'video' && VideoCanvas) {
      return (
        <div key={'cam' + idx} className="device-state-video" style={{ aspectRatio: '16/9', background: '#0a0c14',
          border: '1px solid var(--color-accent-border)', borderRadius: 'var(--radius-sm)',
          overflow: 'hidden' }}>
          <VideoCanvas connected={true} endpoint={w.endpoint}/>
        </div>
      );
    }
    return null;
  }).filter(Boolean);

  if (!nodes.length) return null;
  return nodes;
}
window.fsmWidget = fsmWidget;
