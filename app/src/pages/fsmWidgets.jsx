/* fsmWidgets — 按 card.widgets 声明组合当前状态的富控件。 */
function ConnectionWidget({ widget, unitId, actor, liveState, stateField, connectionField, onlineConnection }) {
  const { useEffect, useState, useCallback, useRef } = React;
  const statusEndpoint = widget.status_endpoint || widget.endpoint;
  const [status, setStatus] = useState({ connected: false, connection_state: '', error: '' });
  const [busy, setBusy] = useState(false);
  const [checked, setChecked] = useState(false);
  const connectRequestedRef = useRef(false);

  const refresh = useCallback(() => {
    if (!statusEndpoint?.startsWith('/api/')) return;
    fetch(statusEndpoint)
      .then(r => r.json())
      .then(d => {
        setStatus({
          connected: (connectionField ? d[connectionField] : d.connected) === true,
          connection_state: d.connection_state || '',
          error: d.error ? apiErrorText({ message: d.error }, '设备连接暂时不可用') : '',
        });
        setChecked(true);
      })
      .catch(() => {
        setStatus(prev => ({
          ...prev,
          connected: false,
          connection_state: 'offline',
          error: '设备连接暂时不可用，请稍后重试。',
        }));
        setChecked(true);
      });
  }, [statusEndpoint, stateField, connectionField]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const connect = useCallback(() => {
    if (busy) return;
    setBusy(true);
    commandBus.command(unitId, widget.connect_action || 'connect', {})
      .then(data => {
        if (data.result === 'error') throw new Error(data.error || '连接失败');
        setStatus(prev => ({ ...prev, connection_state: 'connecting', error: '' }));
        setTimeout(refresh, 700);
      })
      .catch(err => setStatus(prev => ({
        ...prev,
        error: apiErrorText(err, '连接没有完成，请稍后重试。'),
      })))
      .finally(() => setBusy(false));
  }, [busy, widget.connect_action, refresh]);

  const disconnect = () => {
    if (busy) return;
    setBusy(true);
    commandBus.command(unitId, widget.disconnect_action || 'disconnect', {})
      .then(() => setTimeout(refresh, 400))
      .catch(() => setStatus(prev => ({ ...prev, error: '断开请求失败。' })))
      .finally(() => setBusy(false));
  };

  const connected = connectionField
    ? (liveState?.[connectionField] ?? status.connected)
    : onlineConnection;
  const state = liveState?.connection_state || status.connection_state;
  const connecting = liveState?.connection_state === 'connecting' || state === 'connecting' || busy;

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
          flexShrink: 0,
        }}/>
        <div style={{
          flex: 1,
          color: 'var(--color-text-muted)',
          fontSize: 12,
          fontFamily: 'var(--font-sans)',
        }}>
          {({ connecting: '连接中', connected: '已连接', online: '已连接',
            disconnected: '未连接', offline: '未连接', error: '连接异常' })[state]
            || (connected ? '已连接' : '未连接')}
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
          {connecting ? '连接中...' : (connected ? (actor === 'admin' ? '断开' : '已连接') : '连接')}
        </button>
        {status.error && <small className="action-feedback action-feedback--error" role="alert" title={status.error}>{status.error}</small>}
        </div>
      </div>
    </div>
  );
}

function ColorSwatchesWidget({ unitId, widget, report, ledState }) {
  const { useState, useEffect, useRef } = React;
  const swatches = widget.swatches || [];
  const readDeviceColor = () => ({
    r: Number(ledState?.r ?? 255), g: Number(ledState?.g ?? 255),
    b: Number(ledState?.b ?? 255), brightness: Number(ledState?.brightness ?? 180),
  });
  const initial = () => {
    const color = readDeviceColor();
    const hsv = window.ssmColor.rgbToHsv(color.r, color.g, color.b);
    return { hue: hsv.h, saturation: hsv.s, brightness: color.brightness };
  };
  const [draft, setDraft] = useState(initial);
  const draftRef = useRef(draft);
  const wheelRef = useRef(null);
  const lastSent = useRef('');
  const queued = useRef(null);
  const sending = useRef(false);
  const sendTimer = useRef(null);
  const settleTimer = useRef(null);
  const awaitingState = useRef('');
  const retryAfter = useRef(0);
  const remoteState = useRef(readDeviceColor());
  const adjusting = useRef(false);
  const toDraft = color => {
    const hsv = window.ssmColor.rgbToHsv(color.r, color.g, color.b);
    return { hue: hsv.h, saturation: hsv.s, brightness: color.brightness };
  };

  useEffect(() => {
    if (!ledState || !Object.keys(ledState).length) return;
    const deviceColor = readDeviceColor();
    const hsv = window.ssmColor.rgbToHsv(deviceColor.r, deviceColor.g, deviceColor.b);
    const next = { hue: hsv.h, saturation: hsv.s, brightness: deviceColor.brightness };
    remoteState.current = deviceColor;
    if (adjusting.current || queued.current || sending.current) return;
    if (awaitingState.current && JSON.stringify(deviceColor) !== awaitingState.current) return;
    awaitingState.current = '';
    clearTimeout(settleTimer.current);
    draftRef.current = next;
    setDraft(next);
    lastSent.current = JSON.stringify(deviceColor);
  }, [ledState?.r, ledState?.g, ledState?.b, ledState?.brightness]);

  useEffect(() => () => {
    clearTimeout(sendTimer.current);
    clearTimeout(settleTimer.current);
  }, []);

  const disabledRef = React.useRef(!!widget._disabled);
  disabledRef.current = !!widget._disabled;
  React.useEffect(() => {
    if (!widget._disabled) return;
    queued.current = null;
    adjusting.current = false;
    clearTimeout(sendTimer.current);
  }, [widget._disabled]);

  const flushColor = async () => {
    if (disabledRef.current) { queued.current = null; return; }
    clearTimeout(sendTimer.current);
    sendTimer.current = null;
    if (sending.current || !queued.current) return;
    if (Date.now() < retryAfter.current) {
      sendTimer.current = setTimeout(flushColor, retryAfter.current - Date.now());
      return;
    }
    const next = queued.current;
    queued.current = null;
    const key = JSON.stringify(next);
    if (key === lastSent.current) return;
    sending.current = true;
    lastSent.current = key;
    const result = await commandBus.command(unitId, widget.action, next);
    sending.current = false;
    if (result?.result === 'error' || result?.result === 'timeout') {
      retryAfter.current = Date.now() + 1000;
      lastSent.current = '';
      awaitingState.current = '';
      clearTimeout(settleTimer.current);
      report(result);
    } else {
      // The HTTP 202 only confirms dispatch. Keep the slider local until the
      // matching ESP32 state arrives, so an older SSE update cannot pull it back.
      awaitingState.current = JSON.stringify(remoteState.current) === key ? '' : key;
      clearTimeout(settleTimer.current);
      if (awaitingState.current) {
        settleTimer.current = setTimeout(() => {
          awaitingState.current = '';
          if (!adjusting.current && !queued.current && !sending.current) {
            const next = toDraft(remoteState.current);
            draftRef.current = next;
            setDraft(next);
          }
        }, 5000);
      }
    }
    if (queued.current) flushColor();
  };

  const queueColor = (values, immediate = false) => {
    if (disabledRef.current) return;
    const nextDraft = {
      hue: ((Number(values.hue) % 360) + 360) % 360,
      saturation: Math.min(1, Math.max(0, Number(values.saturation))),
      brightness: Math.min(255, Math.max(1, Math.round(Number(values.brightness)))),
    };
    const next = {
      ...window.ssmColor.hsvToRgb(nextDraft.hue, nextDraft.saturation),
      brightness: nextDraft.brightness,
    };
    draftRef.current = nextDraft;
    setDraft(nextDraft);
    queued.current = next;
    if (immediate) flushColor();
    else if (!sendTimer.current) sendTimer.current = setTimeout(flushColor, 120);
  };

  const updateWheel = event => {
    const rect = wheelRef.current?.getBoundingClientRect();
    if (!rect) return;
    const radius = Math.min(rect.width, rect.height) / 2;
    const x = (event.clientX - rect.left - rect.width / 2) / radius;
    const y = (event.clientY - rect.top - rect.height / 2) / radius;
    const distance = Math.min(1, Math.hypot(x, y));
    const hue = (Math.atan2(y, x) * 180 / Math.PI + 450) % 360;
    queueColor({ ...draftRef.current, hue, saturation: distance });
  };
  const finishWheel = event => {
    if (wheelRef.current?.hasPointerCapture(event.pointerId)) {
      wheelRef.current.releasePointerCapture(event.pointerId);
    }
    adjusting.current = false;
    flushColor();
  };
  const adjustWithKey = event => {
    const next = { ...draftRef.current };
    if (event.key === 'ArrowRight') next.hue += 5;
    else if (event.key === 'ArrowLeft') next.hue -= 5;
    else if (event.key === 'ArrowUp') next.saturation += 0.05;
    else if (event.key === 'ArrowDown') next.saturation -= 0.05;
    else if (event.key === 'Home') next.saturation = 0;
    else if (event.key === 'End') next.saturation = 1;
    else return;
    event.preventDefault();
    queueColor(next);
  };
  const rgb = window.ssmColor.hsvToRgb(draft.hue, draft.saturation);
  const wheelAngle = draft.hue * Math.PI / 180;
  const markerX = 50 + Math.sin(wheelAngle) * draft.saturation * 43;
  const markerY = 50 - Math.cos(wheelAngle) * draft.saturation * 43;

  return <div className="device-led-controls">
    <div className="device-led-controls__layout">
    <div className="device-led-controls__picker">
      <div ref={wheelRef} className="device-led-controls__wheel" role="slider" tabIndex={widget._disabled ? -1 : 0} aria-disabled={!!widget._disabled}
        aria-label="选择灯光颜色" aria-valuemin="0" aria-valuemax="359"
        aria-valuenow={Math.round(draft.hue)}
        aria-valuetext={`色相 ${Math.round(draft.hue)} 度，饱和度 ${Math.round(draft.saturation * 100)}%`}
        style={{ '--wheel-x': `${markerX}%`, '--wheel-y': `${markerY}%`, '--wheel-color': `rgb(${rgb.r}, ${rgb.g}, ${rgb.b})` }}
        onPointerDown={event => {
          if (disabledRef.current) return;
          wheelRef.current.setPointerCapture(event.pointerId);
          adjusting.current = true;
          updateWheel(event);
        }}
        onPointerMove={event => {
          if (wheelRef.current?.hasPointerCapture(event.pointerId)) updateWheel(event);
        }}
        onPointerUp={finishWheel} onPointerCancel={finishWheel}
        onKeyDown={adjustWithKey} onKeyUp={() => flushColor()}
        onBlur={() => { adjusting.current = false; flushColor(); }}>
        <span className="device-led-controls__wheel-center" aria-hidden="true"/>
        <span className="device-led-controls__wheel-marker" aria-hidden="true"/>
      </div>
    </div>
    <div className="device-led-controls__side">
    <label className="device-led-controls__brightness">
      <span>亮度</span>
      <input type="range" min="1" max="255" step="1" value={draft.brightness}
        aria-label="亮度"
        onPointerDown={() => { adjusting.current = true; }}
        onChange={event => queueColor({ ...draftRef.current, brightness: Number(event.target.value) })}
        onPointerUp={() => { adjusting.current = false; flushColor(); }}
        onPointerCancel={() => { adjusting.current = false; flushColor(); }}
        onKeyUp={() => flushColor()}
        onBlur={() => { adjusting.current = false; flushColor(); }}/>
      <output>{Math.round(draft.brightness / 255 * 100)}%</output>
    </label>
    {swatches.length > 0 && <div className="device-led-controls__presets" aria-label="预设颜色">
      {swatches.map((swatch, idx) => <button key={(swatch.label || 'color') + idx}
        type="button" aria-pressed={Math.abs(rgb.r - swatch.r) <= 1 && Math.abs(rgb.g - swatch.g) <= 1 && Math.abs(rgb.b - swatch.b) <= 1}
        onClick={() => {
          const hsv = window.ssmColor.rgbToHsv(swatch.r, swatch.g, swatch.b);
          queueColor({ ...draftRef.current, hue: hsv.h, saturation: hsv.s }, true);
        }}>
        <i style={{ background: `rgb(${swatch.r}, ${swatch.g}, ${swatch.b})` }}/>
        {swatch.label}
      </button>)}
    </div>}
    </div>
    </div>
  </div>;
}

function JoystickControls({ unitId, widget, report, liveState, connected }) {
  const velocity = React.useRef({vx:0,vy:0,vyaw:0});
  const feedback = React.useRef(report);
  feedback.current = report;
  const action = widget.action;
  const limits = widget.params_schema?.properties;
  const maxX = limits?.x?.maximum ?? .5;
  const maxY = limits?.y?.maximum ?? .5;
  const maxYaw = limits?.z?.maximum ?? .8;
  const emit = React.useCallback(() => commandBus.velocity(unitId,action,{...velocity.current},result => {
    if (!['accepted','ok'].includes(result?.result)) feedback.current?.(result);
  }), [unitId,action]);
  const move = React.useCallback((dx,dy) => {
    velocity.current.vx = +(-dy*maxX).toFixed(2);
    velocity.current.vy = +(-dx*maxY).toFixed(2);
    emit();
  }, [emit,maxX,maxY]);
  const turn = React.useCallback(dx => { velocity.current.vyaw = +(-dx*maxYaw).toFixed(2); emit(); }, [emit,maxYaw]);
  const stopMove = React.useCallback(() => { velocity.current.vx=0; velocity.current.vy=0; emit(); }, [emit]);
  const stopTurn = React.useCallback(() => { velocity.current.vyaw=0; emit(); }, [emit]);
  const disabled = !connected || !stateWidgetEnabled(widget,liveState);
  const Joystick = window.VirtualJoystick;
  return <div className="device-joysticks">
    <Joystick label="移动" radius={36} disabled={disabled} onMove={move} onStop={stopMove}/>
    <Joystick label="转向" radius={36} disabled={disabled} onMove={turn} onStop={stopTurn}/>
  </div>;
}

function fsmWidget(unitId, state, widgets, report = () => {}, actor = 'visitor', liveState = null, stateField = '', connectionField = '', onlineConnection = false) {
  if (!widgets || !widgets.length) return null;

  const VirtualJoystick = window.VirtualJoystick;
  const VideoCanvas = window.VideoCanvas;
  const MapWidget = window.MapWidget;
  const MediaWidget = window.MediaWidget;

  // 只渲染当前状态匹配的 widget（states 为空=全程显示）
  const active = widgets.filter(w => !w.states || !w.states.length || w.states.includes(state));
  if (!active.length) return null;

  const nodes = active.map((w, idx) => {
    if (w.type === 'led_color' || w.type === 'color_swatches') {
      return <fieldset key={'color' + idx} className="device-state-control-fieldset" disabled={w._disabled}>
        <ColorSwatchesWidget unitId={unitId} widget={w} report={report} ledState={liveState}/></fieldset>;
    }
    if (w.type === 'connection') {
      return <ConnectionWidget key={'conn' + idx} widget={w} unitId={unitId} actor={actor} liveState={liveState} stateField={stateField} connectionField={connectionField} onlineConnection={onlineConnection}/>;
    }
    if (w.type === 'joystick' && VirtualJoystick) {
      return <JoystickControls key={'joy' + idx} unitId={unitId} widget={w} report={report}
        liveState={liveState} connected={onlineConnection}/>;
    }
    if (w.type === 'video' && VideoCanvas && MediaWidget && w.endpoint?.startsWith('/api/')) {
      return (
        <MediaWidget key={'cam' + idx} label="视频" title="实时视频" ready={onlineConnection} emptyText="等待视频连接">
          <VideoCanvas connected={onlineConnection} endpoint={w.endpoint}/>
        </MediaWidget>
      );
    }
    if (w.type === 'map' && MapWidget && w.endpoint?.startsWith('/api/')) {
      return <MapWidget key={'map' + idx} connected={onlineConnection} endpoint={w.endpoint}/>;
    }
    return <span key={'unsupported' + idx} className="device-widget-unsupported">控件不可用：{w.type}</span>;
  }).filter(Boolean);

  if (!nodes.length) return null;
  return nodes;
}
window.fsmWidget = fsmWidget;
