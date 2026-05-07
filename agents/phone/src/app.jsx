/* SSM PWA — Nearby device discovery + control via MQTT */
const { useState, useEffect, useRef, useCallback } = React;

const BROKER_URL  = (location.protocol === 'https:')
  ? `wss://${location.host}/mqtt`
  : 'ws://47.116.137.202:9001';
const BROKER_USER = 'ssm_user';
const BROKER_PASS = 'Wl4sErQrlrpEbm7r';
const LIME             = '#C8FF3E';
const NEARBY_RADIUS_M  = 300;   // only show devices within this distance

const ICONS = {
  search:   "M21 21l-6-6m2-5a7 7 0 1 1-14 0 7 7 0 0 1 14 0",
  home:     "M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2zM9 22V12h6v10",
  arrow:    "M5 12h14M12 5l7 7-7 7",
  check:    "M20 6L9 17l-5-5",
  settings: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06-.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z",
  bulb:     "M9 18h6M10 22h4M12 2a7 7 0 0 1 7 7c0 2.8-1.6 5.2-4 6.4V17a1 1 0 0 1-1 1h-4a1 1 0 0 1-1-1v-1.6C5.6 14.2 4 11.8 4 9a7 7 0 0 1 8-7z",
  volume:   "M11 5L6 9H2v6h4l5 4V5zM19.07 4.93a10 10 0 0 1 0 14.14M15.54 8.46a5 5 0 0 1 0 7.07",
  zap:      "M13 2L3 14h9l-1 8 10-12h-9l1-8z",
  mic:      "M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3zM19 10v2a7 7 0 0 1-14 0v-2M12 19v4M8 23h8",
  sun:      "M12 17A5 5 0 1 0 12 7a5 5 0 0 0 0 10zM12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42",
  wifi:     "M5 12.55a11 11 0 0 1 14.08 0M1.42 9a16 16 0 0 1 21.16 0M8.53 16.11a6 6 0 0 1 6.95 0M12 20h.01",
  x:        "M18 6L6 18M6 6l12 12",
  list:     "M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01",
};

function Icon({ name, size = 18, sw = 1.75, color = "currentColor" }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none"
      stroke={color} strokeWidth={sw} strokeLinecap="round" strokeLinejoin="round"
      style={{ display: 'block', flexShrink: 0 }}>
      <path d={ICONS[name] || ICONS.wifi}/>
    </svg>
  );
}

/* ── Location helpers ───────────────────────────────────────────── */
function haversine(lat1, lng1, lat2, lng2) {
  const R = 6371000;
  const f1 = lat1 * Math.PI / 180, f2 = lat2 * Math.PI / 180;
  const df = (lat2 - lat1) * Math.PI / 180;
  const dl = (lng2 - lng1) * Math.PI / 180;
  const a = Math.sin(df/2)**2 + Math.cos(f1)*Math.cos(f2)*Math.sin(dl/2)**2;
  return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1-a));
}

function formatDist(m) {
  if (m == null) return null;
  return m < 1000 ? `${Math.round(m)}m` : `${(m/1000).toFixed(1)}km`;
}

/* ── Agent metadata helpers ─────────────────────────────────────── */
function getAgentMeta(agent) {
  const n = (agent.name || '').toLowerCase();
  if (n.includes('led') || n.includes('rgb') || n.includes('ws2812') || n.includes('ring')) return { icon: 'bulb', color: '#FF9A5A', label: 'LED 灯' };
  if (n.includes('buz'))                       return { icon: 'volume',   color: '#E26BFF', label: '蜂鸣器' };
  if (n.includes('ir'))                        return { icon: 'zap',      color: '#6B6CFF', label: 'IR 传感器' };
  if (n.includes('sound') || n.includes('mic'))return { icon: 'mic',      color: '#E26BFF', label: '声音传感器' };
  if (n.includes('light') || n.includes('lux'))return { icon: 'sun',      color: LIME,      label: '光线传感器' };
  if (agent.agent_type === 'sensor')           return { icon: 'wifi',     color: '#7EE8A2', label: '传感器' };
  if (agent.agent_type === 'actuator')         return { icon: 'settings', color: '#FF9A5A', label: '执行器' };
  return { icon: 'wifi', color: '#7EE8A2', label: '设备' };
}

function getStateLabel(agent, unitData) {
  const uid = agent.unit_id || agent.agent_id;
  const s   = (unitData[uid] || {}).state || {};
  const n   = (agent.name || '').toLowerCase();
  if (n.includes('led') || n.includes('rgb')) return s.ism || (s.state === 'OFF' ? '已关闭' : '待命');
  if (n.includes('buz'))   return s.ism || '待命';
  if (n.includes('ir'))    return s.presence !== undefined ? (s.presence ? '有人' : '无人') : '监测中';
  if (n.includes('sound')) return s.detected ? '检测到声音' : '静默';
  if (n.includes('light')) return s.level || (s.lux !== undefined ? `${s.lux} lux` : '监测中');
  return '在线';
}

function getSensorReading(agent, unitData) {
  const uid = agent.unit_id || agent.agent_id;
  const s   = (unitData[uid] || {}).state || (unitData[uid] || {}).event || {};
  const n   = (agent.name || '').toLowerCase();
  if (n.includes('light') || n.includes('lux')) {
    const map = { DARK: ['暗', '#6B6CFF'], DIM: ['微亮', '#7EE8A2'], NORMAL: ['正常', '#C8FF3E'], BRIGHT: ['强光', '#FFD060'] };
    const [label, color] = map[s.level] || ['...', 'rgba(255,255,255,0.25)'];
    return { value: label, color };
  }
  if (n.includes('ir')) {
    if (s.presence === undefined) return { value: '...', color: 'rgba(255,255,255,0.25)' };
    return s.presence ? { value: '有人', color: '#C8FF3E' } : { value: '无人', color: 'rgba(255,255,255,0.35)' };
  }
  if (n.includes('sound') || n.includes('mic')) {
    return { value: '监测中', color: 'rgba(255,255,255,0.25)' };
  }
  return { value: '...', color: 'rgba(255,255,255,0.25)' };
}

function isAgentActive(agent, unitData) {
  const uid = agent.unit_id || agent.agent_id;
  const s   = (unitData[uid] || {}).state || {};
  const n   = (agent.name || '').toLowerCase();
  if (n.includes('led') || n.includes('rgb')) return s.ism && s.ism !== 'OFF' && s.ism !== 'IDLE';
  if (n.includes('ir'))    return !!s.detected;
  if (n.includes('sound')) return !!s.detected;
  return true;
}

/* ── useLongPress ───────────────────────────────────────────────── */
function useLongPress(onLongPress, ms = 500) {
  const timer = useRef(null);
  const start = useCallback(() => {
    timer.current = setTimeout(onLongPress, ms);
  }, [onLongPress, ms]);
  const cancel = useCallback(() => { clearTimeout(timer.current); }, []);
  return { onTouchStart: start, onTouchEnd: cancel, onTouchMove: cancel,
           onMouseDown: start, onMouseUp: cancel, onMouseLeave: cancel };
}

/* ── Radar ──────────────────────────────────────────────────────── */
function RadarScan({ agents }) {
  const [sweep, setSweep] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setSweep(s => (s + 1.5) % 360), 16);
    return () => clearInterval(id);
  }, []);
  return (
    <div style={{ position: 'relative', width: 240, height: 240, margin: '0 auto' }}>
      {[1, 0.7, 0.44].map((s, i) => (
        <div key={i} style={{ position: 'absolute', inset: `${(1-s)*50}%`,
          borderRadius: '50%', border: '1px solid rgba(200,255,62,0.12)' }}/>
      ))}
      <div style={{ position: 'absolute', top: '50%', left: 0, right: 0, borderTop: '1px dashed rgba(200,255,62,0.07)' }}/>
      <div style={{ position: 'absolute', left: '50%', top: 0, bottom: 0, borderLeft: '1px dashed rgba(200,255,62,0.07)' }}/>
      <div style={{ position: 'absolute', inset: 0, transform: `rotate(${sweep}deg)`, pointerEvents: 'none' }}>
        <div style={{ position: 'absolute', left: '50%', top: '50%', width: '50%', height: 2,
          background: 'linear-gradient(to right, rgba(200,255,62,0.85), rgba(200,255,62,0))',
          transformOrigin: '0 50%', boxShadow: '0 0 10px rgba(200,255,62,0.5)' }}/>
        <div style={{ position: 'absolute', left: '50%', top: '50%', width: '50%', height: 120,
          background: 'conic-gradient(from -8deg, rgba(200,255,62,0.14), rgba(200,255,62,0) 35deg)',
          transformOrigin: '0 0', transform: 'translateY(-60px)' }}/>
      </div>
      {agents.map((a, i) => {
        const meta  = getAgentMeta(a);
        const angle = (i * 73 + 30) % 360;
        const r     = 42 + (i % 3) * 28;
        const cx    = 120 + r * Math.cos(angle * Math.PI / 180);
        const cy    = 120 + r * Math.sin(angle * Math.PI / 180);
        return (
          <div key={a.unit_id || a.agent_id} style={{
            position: 'absolute', left: cx - 12, top: cy - 12,
            width: 24, height: 24, borderRadius: '50%',
            background: meta.color, border: '2px solid #0B0B0E',
            boxShadow: `0 0 12px ${meta.color}99`,
            display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#0B0B0E',
          }}>
            <Icon name={meta.icon} size={11} sw={2}/>
          </div>
        );
      })}
      <div style={{ position: 'absolute', left: '50%', top: '50%', transform: 'translate(-50%,-50%)',
        width: 14, height: 14, borderRadius: '50%', background: '#fff', border: '3px solid #0B0B0E',
        boxShadow: '0 0 14px rgba(255,255,255,0.4)' }}/>
    </div>
  );
}

/* ── AgentCard — long-press to subscribe ────────────────────────── */
function AgentCard({ agent, subscribed, onToggle, phoneLoc }) {
  const uid   = agent.unit_id || agent.agent_id;
  const meta  = getAgentMeta(agent);
  const isSub = subscribed.includes(uid);
  const dist  = (phoneLoc && agent._lat != null)
    ? formatDist(haversine(phoneLoc.lat, phoneLoc.lng, agent._lat, agent._lng))
    : null;
  const lp = useLongPress(useCallback(() => onToggle(uid), [uid, onToggle]));
  return (
    <div {...lp} style={{
      display: 'flex', alignItems: 'center', gap: 12, padding: '11px 14px',
      marginBottom: 8, borderRadius: 18,
      background: isSub ? 'rgba(200,255,62,0.06)' : 'rgba(255,255,255,0.04)',
      border: `1px solid ${isSub ? 'rgba(200,255,62,0.28)' : 'rgba(255,255,255,0.07)'}`,
      userSelect: 'none', WebkitUserSelect: 'none',
    }}>
      <div style={{ width: 40, height: 40, borderRadius: 12, flexShrink: 0,
        background: `${meta.color}22`, color: meta.color,
        display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <Icon name={meta.icon} size={18}/>
      </div>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: 14, fontWeight: 500, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {agent.name || uid}
        </div>
        <div style={{ fontSize: 11, color: 'rgba(255,255,255,0.32)', fontFamily: 'monospace', marginTop: 2 }}>
          {meta.label}{dist && <> · <span style={{ color: LIME }}>{dist}</span></>}
        </div>
      </div>
      {isSub
        ? <div style={{ width: 26, height: 26, borderRadius: '50%', flexShrink: 0,
            background: LIME, color: '#0B0B0E',
            display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
            <Icon name="check" size={12} sw={2.5}/>
          </div>
        : <span style={{ fontSize: 10, color: 'rgba(255,255,255,0.2)', flexShrink: 0, letterSpacing: '0.02em' }}>
            长按订阅
          </span>
      }
    </div>
  );
}

/* ── SensorCard — read-only, shows live sensor value ────────────── */
function SensorCard({ agent, unitData }) {
  const meta    = getAgentMeta(agent);
  const reading = getSensorReading(agent, unitData);
  return (
    <div style={{
      display: 'flex', alignItems: 'center', gap: 10,
      padding: '8px 12px', marginBottom: 6, borderRadius: 12,
      background: 'rgba(255,255,255,0.025)',
      border: '1px solid rgba(255,255,255,0.04)',
    }}>
      <div style={{
        width: 28, height: 28, borderRadius: 8, flexShrink: 0,
        background: `${meta.color}18`, color: meta.color,
        display: 'flex', alignItems: 'center', justifyContent: 'center',
      }}>
        <Icon name={meta.icon} size={13} sw={2}/>
      </div>
      <span style={{ flex: 1, fontSize: 12, color: 'rgba(255,255,255,0.4)' }}>
        {meta.label}
      </span>
      <span style={{ fontSize: 12, fontWeight: 600, color: reading.color, fontFamily: 'monospace' }}>
        {reading.value}
      </span>
    </div>
  );
}

/* ── DiscoverScreen ─────────────────────────────────────────────── */
function DiscoverScreen({ agents, connected, subscribed, toggleSub, phoneLoc, locError, unitData }) {
  const sorted = [...agents].sort((a, b) => {
    const da = (phoneLoc && a._lat != null) ? haversine(phoneLoc.lat, phoneLoc.lng, a._lat, a._lng) : Infinity;
    const db = (phoneLoc && b._lat != null) ? haversine(phoneLoc.lat, phoneLoc.lng, b._lat, b._lng) : Infinity;
    return da - db;
  });

  // filter by range — if phone location available, only show devices within NEARBY_RADIUS_M
  // devices without location data are always shown (can't filter without coordinates)
  const visible = phoneLoc
    ? sorted.filter(a => a._lat == null || haversine(phoneLoc.lat, phoneLoc.lng, a._lat, a._lng) <= NEARBY_RADIUS_M)
    : sorted;

  const groups = {};
  visible.forEach(a => {
    const key = a.hw_platform || 'other';
    if (!groups[key]) groups[key] = { actuators: [], sensors: [] };
    (a.agent_type === 'actuator' ? groups[key].actuators : groups[key].sensors).push(a);
  });
  return (
    <div style={{ position: 'absolute', inset: 0, display: 'flex', flexDirection: 'column' }}>
      <div style={{ position: 'absolute', inset: 0, background: 'radial-gradient(60% 40% at 30% 5%, rgba(255,154,90,0.2), transparent 70%)', pointerEvents: 'none' }}/>
      <div style={{ padding: '14px 22px 8px', display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexShrink: 0, position: 'relative' }}>
        <span style={{ fontSize: 16, fontWeight: 700, letterSpacing: '0.06em' }}>SSM</span>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <div style={{ width: 7, height: 7, borderRadius: '50%',
            background: connected ? LIME : '#FF5252',
            boxShadow: connected ? `0 0 8px ${LIME}` : 'none' }}/>
          <span style={{ fontSize: 11, color: connected ? LIME : '#FF5252' }}>
            {connected ? '已连接' : '连接中'}
          </span>
        </div>
      </div>
      <div style={{ padding: '6px 22px 0', flexShrink: 0, position: 'relative' }}>
        <div style={{ fontSize: 32, fontWeight: 300, letterSpacing: '-0.02em', lineHeight: 1.1 }}>附近设备</div>
        <div style={{ fontSize: 13, color: 'rgba(255,255,255,0.5)', marginTop: 6 }}>
          {agents.length === 0
            ? '扫描周边设备中...'
            : phoneLoc
              ? `${NEARBY_RADIUS_M}m 内发现 ${visible.length} 个 · 长按订阅`
              : `发现 ${visible.length} 个（位置未知）· 长按订阅`}
        </div>
        <div style={{ marginTop: 8, display: 'flex', alignItems: 'center', gap: 6,
          padding: '5px 10px', borderRadius: 999, width: 'fit-content',
          background: phoneLoc ? 'rgba(200,255,62,0.08)' : 'rgba(255,255,255,0.05)',
          border: `1px solid ${phoneLoc ? 'rgba(200,255,62,0.2)' : 'rgba(255,255,255,0.08)'}` }}>
          <span style={{ fontSize: 12 }}>{phoneLoc ? '📍' : '🔍'}</span>
          <span style={{ fontSize: 11, color: phoneLoc ? LIME : 'rgba(255,255,255,0.4)' }}>
            {phoneLoc
              ? `${phoneLoc.lat.toFixed(4)}, ${phoneLoc.lng.toFixed(4)}`
              : locError || '获取位置中…'}
          </span>
        </div>
      </div>
      <div style={{ padding: '16px 0 8px', flexShrink: 0, position: 'relative' }}>
        <RadarScan agents={agents}/>
      </div>
      <div style={{ flex: 1, overflowY: 'auto', padding: '0 14px',
        paddingBottom: 'calc(158px + env(safe-area-inset-bottom, 0px))', position: 'relative' }}>
        {agents.length === 0 ? (
          <div style={{ padding: '20px 0', textAlign: 'center', color: 'rgba(255,255,255,0.3)', fontSize: 13 }}>
            等待 ESP32 上线...
          </div>
        ) : visible.length === 0 ? (
          <div style={{ padding: '20px 0', textAlign: 'center', color: 'rgba(255,255,255,0.3)', fontSize: 13 }}>
            附近 {NEARBY_RADIUS_M}m 内暂无设备
          </div>
        ) : (
          <>
            <div style={{ padding: '0 6px 8px', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <span style={{ fontSize: 11, textTransform: 'uppercase', letterSpacing: '0.08em', color: 'rgba(255,255,255,0.3)', fontWeight: 500 }}>已发现</span>
              <span style={{ fontSize: 11, color: LIME, fontWeight: 500 }}>{subscribed.length} 已订阅</span>
            </div>
            {Object.entries(groups).map(([plat, { actuators, sensors }]) => {
              const allIds = [...actuators, ...sensors].map(a => a.unit_id || a.agent_id);
              let prefix = allIds[0] || plat;
              for (const id of allIds) {
                while (prefix && !id.startsWith(prefix)) prefix = prefix.slice(0, prefix.lastIndexOf('_'));
              }
              return (
                <div key={plat} style={{ marginBottom: 16 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '4px 6px 10px' }}>
                    <div style={{ width: 6, height: 6, borderRadius: '50%', background: LIME, boxShadow: `0 0 6px ${LIME}`, flexShrink: 0 }}/>
                    <span style={{ fontSize: 13, fontWeight: 600, color: 'rgba(255,255,255,0.9)', fontFamily: 'monospace' }}>{prefix || plat}</span>
                    <span style={{ fontSize: 10, color: 'rgba(255,255,255,0.25)', fontFamily: 'monospace' }}>{plat}</span>
                  </div>
                  {actuators.length > 0 && <>
                    <div style={{ padding: '0 6px 6px', fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.08em', color: '#FF9A5A', fontWeight: 600 }}>执行器</div>
                    {actuators.map(a => <AgentCard key={a.unit_id||a.agent_id} agent={a} subscribed={subscribed} onToggle={toggleSub} phoneLoc={phoneLoc}/>)}
                  </>}
                  {sensors.length > 0 && <>
                    <div style={{ padding: actuators.length ? '8px 6px 6px' : '0 6px 6px', display: 'flex', alignItems: 'center', gap: 6 }}>
                      <span style={{ fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.08em', color: 'rgba(255,255,255,0.25)', fontWeight: 600 }}>传感器</span>
                      <span style={{ fontSize: 9, color: 'rgba(255,255,255,0.18)', border: '1px solid rgba(255,255,255,0.1)', borderRadius: 4, padding: '1px 5px' }}>只读</span>
                    </div>
                    {sensors.map(a => <SensorCard key={a.unit_id||a.agent_id} agent={a} unitData={unitData}/>)}
                  </>}
                </div>
              );
            })}
          </>
        )}
      </div>
    </div>
  );
}

/* ── DevicesScreen ──────────────────────────────────────────────── */
function DevicesScreen({ agents, subscribed, unitData }) {
  const subs = agents.filter(a => subscribed.includes(a.unit_id || a.agent_id) && a.agent_type === 'actuator');
  return (
    <div style={{ position: 'absolute', inset: 0, display: 'flex', flexDirection: 'column' }}>
      <div style={{ position: 'absolute', inset: 0, background: 'radial-gradient(50% 30% at 80% 5%, rgba(226,107,255,0.18), transparent 70%)', pointerEvents: 'none' }}/>
      <div style={{ padding: '14px 22px 8px', flexShrink: 0, position: 'relative' }}>
        <span style={{ fontSize: 16, fontWeight: 700, letterSpacing: '0.06em' }}>SSM</span>
      </div>
      <div style={{ padding: '6px 22px 14px', flexShrink: 0, position: 'relative' }}>
        <div style={{ fontSize: 32, fontWeight: 300, letterSpacing: '-0.02em', lineHeight: 1.1 }}>我的设备</div>
        <div style={{ fontSize: 13, color: 'rgba(255,255,255,0.5)', marginTop: 6 }}>
          {subs.length} 个已订阅 · {subs.filter(a => isAgentActive(a, unitData)).length} 个活跃
        </div>
      </div>
      <div style={{ flex: 1, overflowY: 'auto', padding: '0 14px',
        paddingBottom: 'calc(158px + env(safe-area-inset-bottom, 0px))', position: 'relative' }}>
        {subs.length === 0 ? (
          <div style={{ padding: '60px 20px', textAlign: 'center' }}>
            <div style={{ width: 60, height: 60, borderRadius: '50%', background: 'rgba(255,255,255,0.04)', margin: '0 auto 16px', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
              <Icon name="wifi" size={24} color="rgba(255,255,255,0.25)"/>
            </div>
            <div style={{ fontSize: 20, fontWeight: 300, marginBottom: 8 }}>暂无订阅设备</div>
            <div style={{ fontSize: 13, color: 'rgba(255,255,255,0.4)', lineHeight: 1.6, maxWidth: 220, margin: '0 auto' }}>
              前往「附近」长按设备卡片即可订阅
            </div>
          </div>
        ) : (
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
            {subs.map(a => {
              const uid    = a.unit_id || a.agent_id;
              const meta   = getAgentMeta(a);
              const active = isAgentActive(a, unitData);
              const label  = getStateLabel(a, unitData);
              return (
                <div key={uid} style={{
                  background: active ? `linear-gradient(135deg, ${meta.color}1a, rgba(255,255,255,0.03))` : 'rgba(255,255,255,0.04)',
                  border: `1px solid ${active ? meta.color + '44' : 'rgba(255,255,255,0.07)'}`,
                  borderRadius: 20, padding: 14, height: 128,
                  display: 'flex', flexDirection: 'column', justifyContent: 'space-between',
                }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
                    <div style={{ width: 34, height: 34, borderRadius: 10,
                      background: active ? meta.color : 'rgba(255,255,255,0.06)',
                      color: active ? '#0B0B0E' : 'rgba(255,255,255,0.35)',
                      display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                      <Icon name={meta.icon} size={16}/>
                    </div>
                    <div style={{ width: 8, height: 8, borderRadius: '50%', marginTop: 6,
                      background: active ? meta.color : 'rgba(255,255,255,0.1)',
                      boxShadow: active ? `0 0 8px ${meta.color}` : 'none' }}/>
                  </div>
                  <div>
                    <div style={{ fontSize: 13, fontWeight: 500, lineHeight: 1.2, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {a.name || uid}
                    </div>
                    <div style={{ fontSize: 11, color: active ? meta.color : 'rgba(255,255,255,0.3)', marginTop: 3, fontFamily: 'monospace' }}>
                      {label}
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}

/* ── ChatSheet — bottom sheet, context persists across open/close ─ */
const SUGGESTIONS = ['开灯，暖白', '红色 LED', '播放通知音', '关闭 LED'];

function ChatSheet({ open, onClose, subscribed, agents, unitData }) {
  const subsRef  = useRef([]);
  subsRef.current = agents.filter(a => subscribed.includes(a.unit_id || a.agent_id) && a.agent_type === 'actuator');
  const subs = subsRef.current;

  const [messages, setMessages]     = useState([
    { role: 'assistant', text: '需要控制什么设备？', actions: [] }
  ]);
  const [input, setInput]           = useState('');
  const [thinking, setThinking]     = useState(false);
  const [thinkingText, setThinkingText] = useState('');
  const [kbOffset, setKbOffset]     = useState(0);
  const [pendingRule, setPendingRule] = useState(null);
  const [savingRule, setSavingRule]   = useState(false);
  const endRef   = useRef(null);
  const inputRef = useRef(null);

  useEffect(() => {
    if (open) setTimeout(() => inputRef.current?.focus(), 360);
    if (!open) setKbOffset(0);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const vv = window.visualViewport;
    if (!vv) return;
    const update = () => setKbOffset(Math.max(0, window.innerHeight - vv.height));
    vv.addEventListener('resize', update);
    vv.addEventListener('scroll', update);
    return () => { vv.removeEventListener('resize', update); vv.removeEventListener('scroll', update); };
  }, [open]);

  useEffect(() => { endRef.current?.scrollIntoView({ behavior: 'smooth' }); }, [messages, thinking, kbOffset]);

  const handleConfirmRule = async () => {
    if (!pendingRule) return;
    setSavingRule(true);
    try {
      await fetch('/api/rules', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(pendingRule),
      });
      const saved = pendingRule;
      setPendingRule(null);
      setMessages(m => [...m, { role: 'assistant', text: `规则「${saved.name}」已保存，条件触发时自动执行。`, actions: [] }]);
    } catch {
      setMessages(m => [...m, { role: 'assistant', text: '规则保存失败，请重试。', actions: [] }]);
    }
    setSavingRule(false);
  };

  const handleCancelRule = () => {
    setPendingRule(null);
    setMessages(m => [...m, { role: 'assistant', text: '已取消，规则未保存。', actions: [] }]);
  };

  const send = async (text) => {
    const t = (text || input).trim();
    if (!t) return;
    setInput('');
    setPendingRule(null);
    setMessages(m => [...m, { role: 'user', text: t }]);

    const actuators = agents.filter(a => a.agent_type === 'actuator');
    if (actuators.length === 0) {
      setMessages(m => [...m, { role: 'assistant', text: '附近没有发现可控设备，请确认设备已上线。', actions: [] }]);
      return;
    }
    if (subs.length === 0) {
      setMessages(m => [...m, { role: 'assistant', text: '请先在「附近」页面订阅设备，才能通过对话控制。', actions: [] }]);
      return;
    }

    setThinking(true);
    setThinkingText('解析意图...');

    try {
      // ① NLU parse — 同时识别 intent_type
      const res = await fetch('/api/nlu', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: t, devices: subs }),
      });
      if (!res.ok) throw new Error('nlu_failed');
      const nluData = await res.json();
      const { session_id, nlu_feedback, intent_type, requirements, rule } = nluData;

      setMessages(m => [...m, { role: 'assistant', text: nlu_feedback, actions: [] }]);
      setThinking(false);
      setThinkingText('');

      // ② 按 intent_type 分流
      if (intent_type === 'define_rule' && rule) {
        setPendingRule(rule);
        return;
      }

      // ③ execute 流程：订阅 feedback → 发布 intent
      setThinking(true);
      setThinkingText('正在规划...');
      const feedbackTopic = `ssm/feedback/${session_id}`;
      mqttBus.subscribe(feedbackTopic);

      let timeoutId = setTimeout(() => {
        mqttBus.removeEventListener('topic:' + feedbackTopic, handleFeedback);
        setThinking(false);
        setThinkingText('');
        setMessages(m => [...m, { role: 'assistant', text: '操作超时，设备可能无响应', actions: [] }]);
      }, 30000);

      function handleFeedback(e) {
        const { stage, text } = e.detail || {};
        if (!stage) return;
        if (stage === 'planning' || stage === 'executing') {
          clearTimeout(timeoutId);
          timeoutId = setTimeout(() => {
            mqttBus.removeEventListener('topic:' + feedbackTopic, handleFeedback);
            setThinking(false);
            setThinkingText('');
            setMessages(m => [...m, { role: 'assistant', text: '操作超时，设备可能无响应', actions: [] }]);
          }, 20000);
          setThinkingText(stage === 'planning' ? '正在规划...' : '正在执行...');
        } else if (stage === 'done' || stage === 'partial' || stage === 'failed') {
          clearTimeout(timeoutId);
          mqttBus.removeEventListener('topic:' + feedbackTopic, handleFeedback);
          setThinking(false);
          setThinkingText('');
          setMessages(m => [...m, { role: 'assistant', text, actions: [] }]);
        }
      }
      mqttBus.addEventListener('topic:' + feedbackTopic, handleFeedback);

      mqttBus.publish(`ssm/intent/${session_id}`, {
        session_id,
        user_msg: t,
        requirements,
        priority: 5,
        ts: Math.floor(Date.now() / 1000),
      });

    } catch (e) {
      setThinking(false);
      setThinkingText('');
      setMessages(m => [...m, { role: 'assistant', text: '服务暂时无响应，请稍后重试。', actions: [] }]);
    }
  };

  return (
    <>
      <div onClick={onClose} style={{
        position: 'fixed', inset: 0, zIndex: 200,
        background: 'rgba(0,0,0,0.55)',
        opacity: open ? 1 : 0,
        transition: 'opacity 0.3s',
        pointerEvents: open ? 'auto' : 'none',
      }}/>
      <div style={{
        position: 'fixed', left: 0, right: 0,
        bottom: kbOffset,
        height: kbOffset > 0 ? `calc(100vh - ${kbOffset}px - 20px)` : '82vh',
        zIndex: 201,
        background: '#0F0F14',
        borderRadius: '22px 22px 0 0',
        border: '1px solid rgba(255,255,255,0.08)',
        boxShadow: '0 -20px 60px rgba(0,0,0,0.5)',
        transform: open ? 'translateY(0)' : 'translateY(100%)',
        transition: 'transform 0.38s cubic-bezier(0.32, 0.72, 0, 1), bottom 0.22s ease, height 0.22s ease',
        display: 'flex', flexDirection: 'column', overflow: 'hidden',
      }}>
        {/* handle */}
        <div style={{ padding: '10px 0 2px', display: 'flex', justifyContent: 'center', flexShrink: 0 }}>
          <div style={{ width: 36, height: 4, borderRadius: 2, background: 'rgba(255,255,255,0.12)' }}/>
        </div>
        {/* header */}
        <div style={{ padding: '6px 20px 10px', display: 'flex', alignItems: 'center', gap: 10,
          flexShrink: 0, borderBottom: '1px solid rgba(255,255,255,0.05)' }}>
          <div style={{ width: 30, height: 30, borderRadius: 10, background: LIME, color: '#0B0B0E',
            display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 16, flexShrink: 0 }}>◐</div>
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: 14, fontWeight: 500 }}>SSM 助手</div>
            <div style={{ fontSize: 11, color: LIME, fontFamily: 'monospace' }}>{subs.length} 个设备</div>
          </div>
          <button onClick={onClose} style={{
            width: 30, height: 30, borderRadius: '50%', border: '1px solid rgba(255,255,255,0.09)',
            background: 'rgba(255,255,255,0.06)', color: 'rgba(255,255,255,0.6)',
            cursor: 'pointer', display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 0,
          }}>
            <Icon name="x" size={14}/>
          </button>
        </div>
        {/* messages */}
        <div style={{ flex: 1, overflowY: 'auto', padding: '12px 16px', display: 'flex', flexDirection: 'column', gap: 10 }}>
          {messages.map((m, i) => (
            <div key={i} style={{ maxWidth: '82%', alignSelf: m.role==='user'?'flex-end':'flex-start', display:'flex', flexDirection:'column', gap:6 }}>
              <div style={{
                padding: '10px 14px', fontSize: 14, lineHeight: 1.5,
                borderRadius: m.role==='user' ? '18px 18px 4px 18px' : '4px 18px 18px 18px',
                background: m.role==='user' ? LIME : 'rgba(255,255,255,0.06)',
                color: m.role==='user' ? '#0B0B0E' : '#fff',
                border: m.role==='user' ? 'none' : '1px solid rgba(255,255,255,0.07)',
              }}>{m.text}</div>
              {m.actions?.map((ac, ai) => (
                <div key={ai} style={{ display:'flex', alignItems:'center', gap:8, padding:'7px 11px',
                  background:'rgba(200,255,62,0.06)', border:'1px solid rgba(200,255,62,0.2)', borderRadius:12 }}>
                  <div style={{ width:22, height:22, borderRadius:7, background:LIME, color:'#0B0B0E',
                    display:'flex', alignItems:'center', justifyContent:'center' }}>
                    <Icon name="check" size={11} sw={2.5}/>
                  </div>
                  <span style={{ fontSize:12, color:'rgba(255,255,255,0.55)' }}>{ac.name}</span>
                  <span style={{ fontSize:12, color:LIME, fontFamily:'monospace', marginLeft:'auto' }}>{ac.action}</span>
                </div>
              ))}
            </div>
          ))}
          {thinking && (
            <div style={{ alignSelf:'flex-start', padding:'10px 14px', borderRadius:'4px 18px 18px 18px',
              background:'rgba(255,255,255,0.06)', border:'1px solid rgba(255,255,255,0.07)',
              display:'flex', flexDirection:'column', gap:6 }}>
              <div style={{ display:'flex', gap:4, alignItems:'center' }}>
                <span className="typing-dot"/><span className="typing-dot" style={{ animationDelay:'.14s' }}/><span className="typing-dot" style={{ animationDelay:'.28s' }}/>
              </div>
              {thinkingText && (
                <span style={{ fontSize:11, color:'rgba(255,255,255,0.32)', fontFamily:'monospace' }}>
                  {thinkingText}
                </span>
              )}
            </div>
          )}
          <div ref={endRef}/>
        </div>
        {/* rule preview card */}
        {pendingRule && (
          <div style={{ padding: '0 16px 10px', flexShrink: 0 }}>
            <div style={{ background: 'rgba(200,255,62,0.07)', border: '1px solid rgba(200,255,62,0.22)',
              borderRadius: 18, padding: '14px 16px' }}>
              <div style={{ fontSize: 12, color: LIME, fontWeight: 600, marginBottom: 8 }}>规则预览 · 确认保存？</div>
              <div style={{ fontSize: 14, fontWeight: 500, marginBottom: 4 }}>{pendingRule.name}</div>
              <div style={{ fontSize: 11, color: 'rgba(255,255,255,0.4)', fontFamily: 'monospace', marginBottom: 12 }}>
                当 {pendingRule.trigger?.agent_tag}.{pendingRule.trigger?.event}
                {' → '}
                {pendingRule.action?.resource_tag} / {pendingRule.action?.cmd}
              </div>
              <div style={{ display: 'flex', gap: 8 }}>
                <button onClick={handleCancelRule}
                  style={{ flex: 1, padding: '9px 0', borderRadius: 999, fontSize: 13,
                    background: 'rgba(255,255,255,0.06)', border: '1px solid rgba(255,255,255,0.09)',
                    color: 'rgba(255,255,255,0.5)', cursor: 'pointer', fontFamily: 'inherit' }}>
                  取消
                </button>
                <button onClick={handleConfirmRule} disabled={savingRule}
                  style={{ flex: 2, padding: '9px 0', borderRadius: 999, fontSize: 13, fontWeight: 600,
                    background: LIME, border: 'none', color: '#0B0B0E',
                    cursor: 'pointer', fontFamily: 'inherit',
                    boxShadow: '0 0 16px rgba(200,255,62,0.3)' }}>
                  {savingRule ? '保存中...' : '确认保存'}
                </button>
              </div>
            </div>
          </div>
        )}
        {/* suggestions */}
        {messages.length <= 1 && (
          <div style={{ padding:'0 12px 8px', display:'flex', gap:6, overflowX:'auto', scrollbarWidth:'none', flexShrink:0 }}>
            {SUGGESTIONS.map(s => (
              <button key={s} onClick={() => send(s)} style={{
                padding:'6px 12px', borderRadius:999, whiteSpace:'nowrap',
                background:'rgba(255,255,255,0.06)', border:'1px solid rgba(255,255,255,0.09)',
                color:'rgba(255,255,255,0.6)', fontSize:12, cursor:'pointer', fontFamily:'inherit',
              }}>{s}</button>
            ))}
          </div>
        )}
        {/* input */}
        <div style={{ padding:'8px 12px', paddingBottom:'calc(12px + env(safe-area-inset-bottom, 0px))',
          borderTop:'1px solid rgba(255,255,255,0.05)', flexShrink:0 }}>
          <div style={{ display:'flex', alignItems:'center', gap:8, padding:'6px 6px 6px 16px',
            background:'rgba(30,29,38,0.95)', border:'1px solid rgba(255,255,255,0.09)', borderRadius:999 }}>
            <input ref={inputRef} value={input} onChange={e => setInput(e.target.value)}
              onKeyDown={e => e.key==='Enter' && send()}
              placeholder="告诉设备要做什么…"
              style={{ flex:1, background:'transparent', border:'none', color:'#fff', fontSize:14, fontFamily:'inherit', outline:'none' }}/>
            <button onClick={() => send()} disabled={!input.trim()} style={{
              width:38, height:38, borderRadius:999,
              background: input.trim() ? LIME : 'rgba(255,255,255,0.08)',
              color: input.trim() ? '#0B0B0E' : 'rgba(255,255,255,0.25)',
              border:'none', display:'flex', alignItems:'center', justifyContent:'center',
              cursor: input.trim() ? 'pointer' : 'default',
              boxShadow: input.trim() ? '0 0 18px rgba(200,255,62,0.35)' : 'none', flexShrink:0,
            }}>
              <Icon name="arrow" size={16} sw={2.2}/>
            </button>
          </div>
        </div>
      </div>
    </>
  );
}

/* ── PersistentInputBar ─────────────────────────────────────────── */
function PersistentInputBar({ onOpen }) {
  return (
    <div onClick={onOpen} style={{
      position: 'absolute', left: 12, right: 12,
      bottom: 'calc(86px + env(safe-area-inset-bottom, 0px))',
      display: 'flex', alignItems: 'center', gap: 10,
      padding: '10px 10px 10px 16px',
      background: 'rgba(22,21,28,0.94)',
      backdropFilter: 'blur(28px) saturate(140%)',
      border: '1px solid rgba(255,255,255,0.09)',
      borderRadius: 999,
      cursor: 'text',
      boxShadow: '0 4px 24px rgba(0,0,0,0.35)',
      zIndex: 10,
    }}>
      <span style={{ fontSize: 17, lineHeight: 1 }}>◐</span>
      <span style={{ flex: 1, fontSize: 14, color: 'rgba(255,255,255,0.3)', userSelect: 'none' }}>
        告诉我想要什么…
      </span>
      <div style={{ width: 34, height: 34, borderRadius: 999, flexShrink: 0,
        background: `${LIME}15`, border: `1px solid ${LIME}33`,
        display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <Icon name="arrow" size={14} color={LIME}/>
      </div>
    </div>
  );
}

/* ── RulesScreen ─────────────────────────────────────────────────── */
function RulesScreen() {
  const [rules, setRules] = useState([]);

  const load = async () => {
    try {
      const r = await fetch('/api/rules');
      setRules(await r.json());
    } catch {}
  };

  useEffect(() => { load(); }, []);

  const handleDelete = async (rule_id) => {
    await fetch(`/api/rules/${rule_id}`, { method: 'DELETE' });
    load();
  };

  const handleToggle = async (rule_id, enabled) => {
    await fetch(`/api/rules/${rule_id}/toggle?enabled=${!enabled}`, { method: 'PATCH' });
    load();
  };

  return (
    <div style={{ position: 'absolute', inset: 0, display: 'flex', flexDirection: 'column' }}>
      <div style={{ position: 'absolute', inset: 0, pointerEvents: 'none',
        background: 'radial-gradient(50% 30% at 50% 5%, rgba(200,255,62,0.1), transparent 70%)' }}/>
      <div style={{ padding: '14px 22px 4px', flexShrink: 0, position: 'relative' }}>
        <span style={{ fontSize: 13, color: 'rgba(255,255,255,0.4)', fontWeight: 500, letterSpacing: '0.06em' }}>SSM</span>
      </div>
      <div style={{ padding: '4px 22px 16px', flexShrink: 0, position: 'relative' }}>
        <div style={{ fontSize: 32, fontWeight: 300, letterSpacing: '-0.02em', lineHeight: 1.1 }}>自动规则</div>
        <div style={{ fontSize: 13, color: 'rgba(255,255,255,0.4)', marginTop: 6 }}>
          {rules.filter(r => r.enabled).length} 条启用 · {rules.length} 条总计 · 在对话中定义新规则
        </div>
      </div>
      <div style={{ flex: 1, overflowY: 'auto', padding: '0 14px',
        paddingBottom: 'calc(158px + env(safe-area-inset-bottom, 0px))', position: 'relative' }}>
        {rules.length === 0 ? (
          <div style={{ padding: '60px 20px', textAlign: 'center', color: 'rgba(255,255,255,0.25)', fontSize: 13, lineHeight: 2 }}>
            还没有规则<br/>
            <span style={{ fontSize: 12 }}>在对话框里说"检测到人就开灯"来创建</span>
          </div>
        ) : rules.map(rule => (
          <div key={rule.rule_id} style={{
            display: 'flex', alignItems: 'center', gap: 12,
            padding: '13px 14px', marginBottom: 8, borderRadius: 18,
            background: rule.enabled ? 'rgba(200,255,62,0.05)' : 'rgba(255,255,255,0.03)',
            border: `1px solid ${rule.enabled ? 'rgba(200,255,62,0.16)' : 'rgba(255,255,255,0.06)'}`,
          }}>
            <div onClick={() => handleToggle(rule.rule_id, rule.enabled)}
              style={{ width: 38, height: 22, borderRadius: 11, flexShrink: 0, cursor: 'pointer',
                background: rule.enabled ? LIME : 'rgba(255,255,255,0.12)', position: 'relative',
                transition: 'background 0.2s' }}>
              <div style={{ position: 'absolute', top: 3, left: rule.enabled ? 18 : 3,
                width: 16, height: 16, borderRadius: '50%',
                background: '#0B0B0E', transition: 'left 0.2s' }}/>
            </div>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontSize: 14, fontWeight: 500,
                color: rule.enabled ? '#fff' : 'rgba(255,255,255,0.4)',
                overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {rule.name}
              </div>
              <div style={{ fontSize: 11, color: 'rgba(255,255,255,0.28)', fontFamily: 'monospace', marginTop: 2 }}>
                {rule.trigger?.agent_tag}.{rule.trigger?.event} → {rule.action?.resource_tag}
              </div>
            </div>
            <button onClick={() => handleDelete(rule.rule_id)}
              style={{ width: 28, height: 28, borderRadius: '50%', flexShrink: 0, padding: 0,
                background: 'rgba(255,82,82,0.1)', border: '1px solid rgba(255,82,82,0.2)',
                color: 'rgba(255,82,82,0.7)', cursor: 'pointer',
                display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
              <Icon name="x" size={12}/>
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}

/* ── TabBar — 3 tabs ────────────────────────────────────────────── */
function TabBar({ tab, setTab, badge }) {
  const tabs = [
    { id: 'discover', icon: 'search', label: '附近' },
    { id: 'devices',  icon: 'home',   label: '设备', badge },
    { id: 'rules',    icon: 'list',   label: '规则' },
  ];
  return (
    <div style={{
      position: 'absolute', left: 12, right: 12,
      bottom: 'calc(10px + env(safe-area-inset-bottom, 0px))',
      background: 'rgba(18,17,22,0.88)', backdropFilter: 'blur(24px) saturate(140%)',
      border: '1px solid rgba(255,255,255,0.09)', borderRadius: 24,
      padding: 8, display: 'flex', gap: 4,
      boxShadow: '0 16px 40px rgba(0,0,0,0.5)',
    }}>
      {tabs.map(t => {
        const active = tab === t.id;
        return (
          <button key={t.id} onClick={() => setTab(t.id)} style={{
            flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 3,
            padding: '8px 4px', borderRadius: 16,
            background: active ? 'rgba(255,255,255,0.07)' : 'transparent',
            color: active ? '#fff' : 'rgba(255,255,255,0.38)',
            border: 'none', cursor: 'pointer', position: 'relative', fontFamily: 'inherit',
          }}>
            <Icon name={t.icon} size={18}/>
            <span style={{ fontSize: 10, fontWeight: 500 }}>{t.label}</span>
            {t.badge > 0 && !active && (
              <span style={{ position:'absolute', top:6, right:'26%',
                minWidth:16, height:16, padding:'0 4px', borderRadius:999,
                background:LIME, color:'#0B0B0E', fontSize:10, fontWeight:600,
                display:'flex', alignItems:'center', justifyContent:'center' }}>{t.badge}</span>
            )}
          </button>
        );
      })}
    </div>
  );
}

/* ── App ────────────────────────────────────────────────────────── */
const EXCL_TYPES = new Set(['decision', 'supervisor']);
const EXCL_PLAT  = new Set(['pc', 'pwa']);

function App() {
  const [tab, setTab]               = useState('discover');
  const [sheetOpen, setSheetOpen]   = useState(false);
  const [connected, setConnected]   = useState(false);
  const [agents, setAgents]         = useState([]);
  const [unitData, setUnitData]     = useState({});
  const [phoneLoc, setPhoneLoc]     = useState(null);
  const [locError, setLocError]     = useState(null);
  const [subscribed, setSubscribed] = useState(() => {
    try { return JSON.parse(localStorage.getItem('ssm_sub') || '[]'); }
    catch { return []; }
  });

  useEffect(() => {
    if (!navigator.geolocation) { setLocError('浏览器不支持定位'); return; }
    navigator.geolocation.getCurrentPosition(
      pos => setPhoneLoc({ lat: pos.coords.latitude, lng: pos.coords.longitude }),
      err => setLocError(err.code === 1 ? '位置权限被拒绝' : '定位失败'),
      { enableHighAccuracy: true, timeout: 10000 }
    );
  }, []);

  useEffect(() => {
    const registry   = new AgentRegistry(mqttBus);
    const ismTracker = new ISMTracker(mqttBus);

    registry.addEventListener('change', () => {
      setAgents(registry.getAll().filter(a =>
        !EXCL_TYPES.has(a.agent_type) && !EXCL_PLAT.has(a.hw_platform) && a._online === true
      ));
    });

    ismTracker.addEventListener('update', () => {
      const snap = {};
      ismTracker.unitIds().forEach(uid => {
        snap[uid] = {
          state:  ismTracker.get(uid, 'state'),
          event:  ismTracker.get(uid, 'event'),
          report: ismTracker.get(uid, 'report'),
        };
      });
      setUnitData({ ...snap });
    });

    mqttBus.onConnect(() => {
      setConnected(true);
      mqttBus.publish('ssm/agents/phone_ui/manifest', {
        unit_id: 'phone_ui', agent_type: 'supervisor',
        name: 'human_supervisor', hw_platform: 'pwa',
        ts: Math.floor(Date.now() / 1000),
      }, { retain: true });
    });
    mqttBus.addEventListener('disconnect', () => setConnected(false));
    mqttBus.addEventListener('reconnect',  () => setConnected(false));

    mqttBus.connect(BROKER_URL, null, { username: BROKER_USER, password: BROKER_PASS });
  }, []);

  const toggleSub = useCallback((uid) => {
    setSubscribed(prev => {
      const next = prev.includes(uid) ? prev.filter(x => x !== uid) : [...prev, uid];
      localStorage.setItem('ssm_sub', JSON.stringify(next));
      return next;
    });
  }, []);

  return (
    <div style={{
      position: 'fixed', inset: 0, overflow: 'hidden',
      background: '#0B0B0E', color: '#fff',
      fontFamily: '-apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue", sans-serif',
      paddingTop: 'env(safe-area-inset-top, 0px)',
    }}>
      <div style={{ position: 'relative', height: '100%' }}>
        {tab === 'discover' && (
          <DiscoverScreen agents={agents} connected={connected}
            subscribed={subscribed} toggleSub={toggleSub}
            phoneLoc={phoneLoc} locError={locError} unitData={unitData}/>
        )}
        {tab === 'devices' && (
          <DevicesScreen agents={agents} subscribed={subscribed} unitData={unitData}/>
        )}
        {tab === 'rules' && <RulesScreen />}
        <PersistentInputBar onOpen={() => setSheetOpen(true)}/>
        <TabBar tab={tab} setTab={setTab} badge={agents.filter(a => subscribed.includes(a.unit_id || a.agent_id) && a.agent_type === 'actuator').length}/>
      </div>
      <ChatSheet
        open={sheetOpen}
        onClose={() => setSheetOpen(false)}
        subscribed={subscribed}
        agents={agents}
        unitData={unitData}
      />
    </div>
  );
}

ReactDOM.createRoot(document.getElementById('root')).render(<App/>);
