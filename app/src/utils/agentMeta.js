function getAgentMeta(agent) {
  const n = (agent.name || '').toLowerCase();
  // agent_type 优先，防止名字误匹配（如 "Go2 Air" 含 "ir"）
  if (agent.agent_type === 'robot')    return { icon: 'zap',      color: LIME,      label: '机器人' };
  if (n.includes('led') || n.includes('rgb') || n.includes('ws2812') || n.includes('ring')) return { icon: 'bulb',     color: '#FF9A5A', label: 'LED 灯' };
  if (n.includes('buz'))                                                                      return { icon: 'volume',   color: '#E26BFF', label: '蜂鸣器' };
  if (n.includes('ir'))                                                                       return { icon: 'zap',      color: '#6B6CFF', label: 'IR 传感器' };
  if (n.includes('sound') || n.includes('mic'))                                               return { icon: 'mic',      color: '#E26BFF', label: '声音传感器' };
  if (n.includes('light') || n.includes('lux'))                                               return { icon: 'sun',      color: LIME,      label: '光线传感器' };
  if (agent.agent_type === 'sensor')                                                          return { icon: 'wifi',     color: '#7EE8A2', label: '传感器' };
  if (agent.agent_type === 'actuator')                                                        return { icon: 'settings', color: '#FF9A5A', label: '执行器' };
  return { icon: 'wifi', color: '#7EE8A2', label: '设备' };
}

function getStateLabel(agent, unitData) {
  const uid = agent.unit_id || agent.agent_id;
  const s   = (unitData[uid] || {}).state || {};
  const n   = (agent.name || '').toLowerCase();
  if (agent.agent_type === 'robot') return '点击控制';
  if (n.includes('led') || n.includes('rgb')) return s.ism || (s.state === 'OFF' ? '已关闭' : '待命');
  if (n.includes('buz'))   return s.ism || '待命';
  if (n.includes('ir'))    return s.presence !== undefined ? (s.presence ? '有人' : '无人') : '监测中';
  if (n.includes('sound')) return s.detected ? '检测到声音' : '静默';
  if (n.includes('light')) return s.level || (s.lux !== undefined ? `${s.lux} lux` : '监测中');
  return '在线';
}

function getSensorReading(agent, unitData) {
  const uid = agent.unit_id || agent.agent_id;
  const d   = unitData[uid] || {};
  const s   = d.state || d.event || {};
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
    const ev = d.event || {};
    const recentDetect = ev.detected && (Date.now() / 1000 - (ev.ts || 0) < 5);
    return recentDetect
      ? { value: '检测到声音', color: '#E26BFF' }
      : { value: '静默',       color: 'rgba(255,255,255,0.25)' };
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
