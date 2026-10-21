// 设备类别：tags 优先（来自 manifest），name 仅兜底。决定图标/状态文案/活跃判定。
const LIME = '#C8FF3E';

function agentKind(agent) {
  const tags = agent.tags || [];
  if (agent.agent_type === 'robot') return 'robot';
  if (tags.includes('lighting') || tags.includes('ambiance')) return 'led';
  if (tags.includes('light_level')) return 'light';
  if (tags.includes('presence'))    return 'ir';
  if (tags.includes('sound'))       return 'sound';
  const n = (agent.name || '').toLowerCase();
  if (n.includes('led') || n.includes('rgb') || n.includes('ws2812') || n.includes('ring') || n.includes('灯')) return 'led';
  if (n.includes('buz'))                        return 'buzzer';
  if (n.includes('ir'))                         return 'ir';
  if (n.includes('sound') || n.includes('mic')) return 'sound';
  if (n.includes('light') || n.includes('lux')) return 'light';
  return agent.agent_type || 'device';
}

function getAgentMeta(agent) {
  switch (agentKind(agent)) {
    case 'robot':  return { icon: 'zap',      color: LIME,      label: '机器人' };
    case 'led':    return { icon: 'bulb',     color: '#FF9A5A', label: 'LED 灯' };
    case 'buzzer': return { icon: 'volume',   color: '#E26BFF', label: '蜂鸣器' };
    case 'ir':     return { icon: 'zap',      color: '#6B6CFF', label: 'IR 传感器' };
    case 'sound':  return { icon: 'mic',      color: '#E26BFF', label: '声音传感器' };
    case 'light':  return { icon: 'sun',      color: LIME,      label: '光线传感器' };
    case 'actuator': return { icon: 'settings', color: '#FF9A5A', label: '执行器' };
    default:       return { icon: 'wifi',     color: '#7EE8A2', label: agent.agent_type === 'sensor' ? '传感器' : '设备' };
  }
}

function getAgentBubbleColor(agent) {
  const id = typeof agent === 'string' ? agent : (agent?.agent_id || agent?.unit_id || '');
  if (id === 'orchestrator' || id === 'cloud_orchestrator') return LIME;
  if (id === 'go2' || id.startsWith('go2_')) return '#4BD4FF';
  if (id.startsWith('esp32_')) return '#FF9A5A';
  return '#7EE8A2';
}

function getAgentDisplayName(agent) {
  const id = typeof agent === 'string' ? agent : (agent?.agent_id || agent?.unit_id || '');
  const names = {
    orchestrator: 'SSM 管家',
    cloud_orchestrator: 'SSM 管家',
    go2: '机器狗',
  };
  if (names[id]) return names[id];
  if (id.startsWith('esp32_')) return '桌面智能体';
  if (id.startsWith('go2_')) return '机器狗';
  return id || '智能体';
}

// 灯的运行状态与颜色参数分开显示。旧固件的 retained 状态只作迁移期展示。
function getLedStateInfo(state = {}) {
  const labels = {
    OFF: '已关闭', ON: '常亮', BLINKING: '闪烁中',
    DIM: '微亮', BRIGHT: '亮灯', COLOR: '彩色', BLINK: '闪烁中',
  };
  const ism = state.ism || '';
  const channel = value => Number.isInteger(value) && value >= 0 && value <= 255;
  const hasRgb = channel(state.r) && channel(state.g) && channel(state.b);
  const hasBrightness = channel(state.brightness);
  const rgb = hasRgb ? `RGB ${state.r}, ${state.g}, ${state.b}` : '';
  const brightness = hasBrightness ? `亮度 ${state.brightness}/255` : '';
  const details = [rgb, brightness].filter(Boolean).join(' · ');
  const color = hasRgb ? `rgb(${state.r}, ${state.g}, ${state.b})` : null;
  return { label: labels[ism] || '在线', details, color };
}

function getStateLabel(agent, unitData) {
  const uid  = agent.unit_id;
  const s    = (unitData[uid] || {}).state || {};
  switch (agentKind(agent)) {
    case 'robot':  return s.ism || '待命';
    case 'led': {
      const info = getLedStateInfo(s);
      return info.details && s.ism !== 'OFF' ? `${info.label} · ${info.details}` : info.label;
    }
    case 'buzzer': return s.ism || '待命';
    case 'ir':     return s.presence !== undefined ? (s.presence ? '有人' : '无人') : '监测中';
    case 'sound':  return s.detected ? '检测到声音' : '静默';
    case 'light':  return s.level || (s.lux !== undefined ? `${s.lux} lux` : '监测中');
    default:       return '在线';
  }
}

function getSensorReading(agent, unitData) {
  const uid = agent.unit_id;
  const d   = unitData[uid] || {};
  const s   = d.state || d.event || {};
  switch (agentKind(agent)) {
    case 'light': {
      const map = { DARK: ['暗', '#6B6CFF'], DIM: ['微亮', '#7EE8A2'], NORMAL: ['明亮', '#C8FF3E'], BRIGHT: ['强光', '#FFD060'] };
      const [label, color] = map[s.level] || ['...', 'rgba(255,255,255,0.25)'];
      return { value: label, color };
    }
    case 'ir':
      if (s.presence === undefined) return { value: '...', color: 'rgba(255,255,255,0.25)' };
      return s.presence ? { value: '有人', color: '#C8FF3E' } : { value: '无人', color: 'rgba(255,255,255,0.35)' };
    case 'sound': {
      const ev = d.event || {};
      const recentDetect = ev.detected && (Date.now() / 1000 - (ev.ts || 0) < 5);
      return recentDetect
        ? { value: '检测到声音', color: '#E26BFF' }
        : { value: '暂无声音',     color: 'rgba(226,107,255,0.55)' };
    }
    default:
      return { value: '...', color: 'rgba(255,255,255,0.25)' };
  }
}

function isAgentActive(agent, unitData) {
  switch (agentKind(agent)) {
    default: return true;
  }
}
