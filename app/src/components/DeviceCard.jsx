function SensorCard({ agent, unitData }) {
  const uid     = agent.unit_id || agent.agent_id;
  const meta    = getAgentMeta(agent);
  const reading = getSensorReading(agent, unitData);
  return (
    <div style={{
      display: 'flex', alignItems: 'center', gap: 12, padding: '12px 14px',
      marginBottom: 8, borderRadius: 18,
      background: 'rgba(255,255,255,0.03)',
      border: '1px solid rgba(255,255,255,0.05)',
    }}>
      <div style={{ width: 42, height: 42, borderRadius: 13, flexShrink: 0,
        background: `${meta.color}18`, color: meta.color,
        display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <Icon name={meta.icon} size={19}/>
      </div>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: 14, fontWeight: 500, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {agent.name || uid}
        </div>
        <div style={{ fontSize: 11, color: 'rgba(255,255,255,0.28)', fontFamily: 'monospace', marginTop: 2 }}>
          {meta.label} · 只读
        </div>
      </div>
      <span style={{ fontSize: 14, fontWeight: 600, color: reading.color, fontFamily: 'monospace', flexShrink: 0 }}>
        {reading.value}
      </span>
      <div style={{ width: 8, height: 8, borderRadius: '50%', flexShrink: 0,
        background: agent._online ? LIME : 'rgba(255,255,255,0.2)',
        boxShadow: agent._online ? `0 0 6px ${LIME}` : 'none' }}/>
    </div>
  );
}

function ActuatorCard({ agent, unitData }) {
  const uid      = agent.unit_id || agent.agent_id;
  const meta     = getAgentMeta(agent);
  const active   = isAgentActive(agent, unitData);
  const n        = (agent.name || '').toLowerCase();
  const cmdTopic = agent.topics?.command;

  const sendCmd = (cmd, extra = {}) => {
    if (!cmdTopic) return;
    mqttBus.publish(cmdTopic, { cmd, ...extra });
  };

  const stateData = (unitData[uid] || {}).state || {};
  const ism = stateData.ism || 'OFF';

  const btnBase = {
    flex: 1, padding: '7px 4px', borderRadius: 999, fontSize: 12,
    cursor: 'pointer', fontFamily: 'inherit', border: 'none',
    transition: 'background 0.15s, color 0.15s',
  };
  const btnActive = (color) => ({
    ...btnBase,
    background: color, color: '#0B0B0E', fontWeight: 600,
    boxShadow: `0 0 10px ${color}60`,
  });
  const btnIdle = {
    ...btnBase,
    background: 'rgba(255,255,255,0.07)',
    border: '1px solid rgba(255,255,255,0.09)',
    color: 'rgba(255,255,255,0.55)',
  };

  let controls = null;
  if (n.includes('led') || n.includes('rgb') || n.includes('ws2812')) {
    const isOff = (ism === 'OFF');
    controls = (
      <div style={{ display: 'flex', gap: 6, marginTop: 10 }} onClick={e => e.stopPropagation()}>
        <button onClick={() => sendCmd('SET_STATE', { state: isOff ? 'BRIGHT' : 'OFF' })}
          style={isOff ? btnIdle : btnActive(meta.color)}>
          {isOff ? '开灯' : '关灯'}
        </button>
        <button onClick={() => sendCmd('SET_STATE', { state: 'DIM' })}
          style={ism === 'DIM' ? btnActive(meta.color) : btnIdle}>微光</button>
        <button onClick={() => sendCmd('BLINK', { r: 255, g: 180, b: 30, count: 3 })}
          style={ism === 'BLINK' ? btnActive(meta.color) : btnIdle}>闪烁</button>
      </div>
    );
  } else if (n.includes('buz')) {
    controls = (
      <div style={{ display: 'flex', gap: 6, marginTop: 10 }} onClick={e => e.stopPropagation()}>
        <button onClick={() => sendCmd('PLAY', { pattern: 'NOTIFY' })}
          style={ism === 'NOTIFY' ? btnActive(meta.color) : btnIdle}>通知音</button>
        <button onClick={() => sendCmd('PLAY', { pattern: 'ALERT' })}
          style={ism === 'ALERT' ? btnActive('#FF5252') : btnIdle}>警报</button>
        <button onClick={() => sendCmd('STOP')}
          style={ism === 'SILENT' ? btnActive(LIME) : btnIdle}>停止</button>
      </div>
    );
  }

  return (
    <div
      onClick={() => navigate('#/devices/' + (agent.slug || uid))}
      style={{
        padding: '12px 14px', marginBottom: 10, borderRadius: 18,
        background: active
          ? `linear-gradient(135deg, ${meta.color}15, rgba(255,255,255,0.03))`
          : 'rgba(255,255,255,0.04)',
        border: `1px solid ${active ? meta.color + '40' : 'rgba(255,255,255,0.07)'}`,
        cursor: 'pointer',
      }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
        <div style={{ width: 42, height: 42, borderRadius: 13, flexShrink: 0,
          background: active ? meta.color : 'rgba(255,255,255,0.06)',
          color: active ? '#0B0B0E' : meta.color,
          display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
          <Icon name={meta.icon} size={19}/>
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 14, fontWeight: 500, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {agent.name || uid}
          </div>
          <div style={{ fontSize: 11, color: active ? meta.color : 'rgba(255,255,255,0.3)', marginTop: 2, fontFamily: 'monospace' }}>
            {getStateLabel(agent, unitData)}
          </div>
        </div>
        <div style={{ flexShrink: 0, color: 'rgba(255,255,255,0.3)' }}>
          <Icon name="arrow" size={13} sw={2}/>
        </div>
        <div style={{ width: 8, height: 8, borderRadius: '50%', flexShrink: 0,
          background: agent._online ? LIME : 'rgba(255,255,255,0.2)',
          boxShadow: agent._online ? `0 0 6px ${LIME}` : 'none' }}/>
      </div>
      {controls}
    </div>
  );
}

window.SensorCard  = SensorCard;
window.ActuatorCard = ActuatorCard;
