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

const defaultAgentNames = {
  cloud_orchestrator: '总管家',
  esp32_desk_led: '皮皮灯',
  go2: '呆呆狗',
};
let currentAgentNames = { ...defaultAgentNames };

function setAgentDisplayNames(names) {
  currentAgentNames = { ...currentAgentNames, ...names };
  window.dispatchEvent(new Event('agent-names-changed'));
}

function getAgentDisplayName(agent) {
  const id = typeof agent === 'string' ? agent : (agent?.agent_id || agent?.unit_id || '');
  if (currentAgentNames[id]) return currentAgentNames[id];
  const names = {
    orchestrator: currentAgentNames.cloud_orchestrator,
  };
  if (names[id]) return names[id];
  if (id.startsWith('esp32_')) return '桌面智能体';
  if (id.startsWith('go2_')) return currentAgentNames.go2;
  return typeof agent === 'object' && agent?.name || '智能体';
}

// Fixed openings based on the three personas, revised after user feedback.
// Page visits reuse these lines; names follow the current profile.
function getAgentOpeningLine(agent) {
  const id = typeof agent === 'string' ? agent : (agent?.agent_id || agent?.unit_id || '');
  const name = getAgentDisplayName(agent);
  if (id === 'orchestrator' || id === 'cloud_orchestrator') {
    return `我是${name}。想做点什么，或只是想聊聊，都可以慢慢说。`;
  }
  if (id === 'esp32_desk_led') {
    return `我是${name}。灯光这事我有点讲究，有什么想法，尽管说。`;
  }
  if (id === 'go2' || id.startsWith('go2_')) {
    return `我是${name}！你想聊什么？我已经有点好奇啦。`;
  }
  return '';
}

// Capability labels complement the discovered Card names.
function agentCapabilityLabel(id) {
  return ({ set_light_state: '调整灯光', set_led_state: '调整灯光',
    set_light_color: '调整灯光颜色', set_led_color: '调整灯光颜色', blink_led: '让灯光闪烁',
    go2_sport: '完成姿态动作', go2_move: '移动', go2_observe: '观察前方',
    go2_navigate_to_visible_target: '视觉定位并导航到目标附近', go2_find_object: '在指定范围内找物',
    go2_status: '检查连接状态', list_peers: '查找可协作的设备', list_agents: '查找可用设备',
    get_space_context: '查看空间信息', read_state: '查看传感器状态',
    await_result: '核对执行结果', request_peer_skill: '协同处理请求',
    go2_chat: '回应当前问题',
    start_activity: '安排空间活动', set_space_constraint: '记住空间里的共同要求',
    clear_space_constraints: '撤销空间里的共同要求' })[id] || id || '处理请求';
}

function isDeviceOnline(agent, unitData) {
  if (!(agent?._online ?? agent?.online)) return false;
  const connectionField = agent?.state_machine?.connection_field;
  if (!connectionField) return true;
  const state = unitData?.[agent.unit_id]?.state || agent.state || {};
  return state[connectionField] === true;
}

// 灯的运行状态只使用契约中的三种状态，颜色参数独立显示。
function getLedStateInfo(state = {}) {
  const labels = {
    OFF: '熄灭', ON: '常亮', BLINKING: '闪烁',
  };
  const ism = state.ism || '';
  const channel = value => Number.isInteger(value) && value >= 0 && value <= 255;
  const hasRgb = channel(state.r) && channel(state.g) && channel(state.b);
  const hasBrightness = channel(state.brightness);
  const rgb = hasRgb ? `RGB ${state.r}, ${state.g}, ${state.b}` : '';
  const brightness = hasBrightness ? `亮度 ${state.brightness}/255` : '';
  const details = [rgb, brightness].filter(Boolean).join(' · ');
  const color = hasRgb ? `rgb(${state.r}, ${state.g}, ${state.b})` : null;
  return { label: labels[ism] || '状态待同步', details, color };
}

function getStateLabel(agent, unitData) {
  const uid  = agent.unit_id;
  const s    = (unitData[uid] || {}).state || {};
  if (agent.state_machine) return stateMachineSummary(agent.state_machine, s);
  switch (agentKind(agent)) {
    case 'robot':  return stateMachineSummary(agent.state_machine, s);
    case 'led': {
      const info = getLedStateInfo(s);
      return info.details && s.ism !== 'OFF' ? `${info.label} · ${info.details}` : info.label;
    }
    case 'buzzer': return ({ ON: '响铃中', OFF: '待命' })[s.ism] || '待命';
    case 'ir':     return s.presence !== undefined ? (s.presence ? '有人' : '无人') : '监测中';
    case 'sound':  return s.detected ? '检测到声音' : '静默';
    case 'light':  return ({ DARK: '暗', DIM: '微亮', NORMAL: '明亮', BRIGHT: '强光' })[s.level]
      || (s.lux !== undefined ? `照度 ${s.lux} 勒克斯` : '监测中');
    default:       return '状态待同步';
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
    case 'led': {
      const state = (unitData[agent.unit_id] || {}).state || {};
      return Boolean(state.ism && state.ism !== 'OFF');
    }
    default: return true;
  }
}
