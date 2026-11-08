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

function getAgentOpeningLine(agent) {
  const id = typeof agent === 'string' ? agent : (agent?.agent_id || agent?.unit_id || '');
  const name = getAgentDisplayName(agent);
  if (id === 'orchestrator' || id === 'cloud_orchestrator') {
    return `我是${name}。你说要办什么，我来安排。`;
  }
  if (id === 'esp32_desk_led') {
    return `我是${name}。想怎么调光？我帮你弄得像样点。`;
  }
  if (id === 'go2' || id.startsWith('go2_')) {
    return `汪，我是${name}！已经准备好和你玩耍啦。`;
  }
  return `我是${name}。想做什么，告诉我吧。`;
}

function describeRunProgress(event, compact = false) {
  const searchStage = {
    search_observe: '正在搜索，观察当前位置', search_candidate: '发现疑似目标，正在复核',
    search_confirmed: '已确认目标', search_navigate: '正在前往下一观察点',
    search_approach: '正在靠近目标', search_finished: event.status === 'ok'
      ? event.mode === 'explore' ? '本轮探索已完成' : '已到目标附近并复核' : '搜索已结束，任务未完成',
  }[event.stage];
  if (searchStage) {
    const reason = {
      budget_exhausted: '搜索预算已用完', time_budget_exhausted: '搜索时间已用完',
      distance_budget_exhausted: '搜索路程预算已用完', step_budget_exhausted: '观察次数已达上限',
      coverage_complete: '可观察区域已检查，遮挡区域仍未确认', no_reachable_observation: '没有可达的观察点',
      calibration_unavailable: '已发现目标，尚缺相机实测标定，无法确认接近位置',
      search_scope_required: '请先设置允许搜索的范围', stop_unconfirmed: '停止指令尚未得到设备确认',
      search_scope_session_mismatch: '定位会话已变化，请重新确认搜索范围',
      no_reachable_observation_point: '没有可达的观察点，部分区域尚未检查',
      coverage_exhausted: '可观察区域已检查，遮挡区域仍未确认',
      cancelled: '用户已中断', authorization_expired: '本次操作授权已到期',
      disconnected: '机器狗连接已断开', map_session_changed: '定位会话已变化，需要重新观察',
    }[event.reason];
    return `${searchStage}${reason ? `：${reason}` : ''}`;
  }
  if (['planning', 'executing', 'summarizing', 'finished'].includes(event.stage)) {
    if (event.stage === 'planning') return compact ? '正在核对可用设备与当前状态'
      : `正在核对可用设备与当前状态，处理：${event.goal || '当前请求'}`;
    if (event.stage === 'executing') return compact ? '正在执行计划'
      : `已选定下一步，正在处理：${event.goal || '当前请求'}`;
    if (event.stage === 'summarizing') return `正在核对执行结果：${event.completed_count || 0} 项完成，${event.failed_count || 0} 项未完成`;
    if (event.stage === 'finished') {
      if (event.status === 'needs_clarification') return '等待补充信息，已执行步骤的结果已保留';
      const counts = `${event.completed_count || 0} 项完成，${event.failed_count || 0} 项未完成`;
      return ['error', 'timeout', 'blocked'].includes(event.status)
        ? `任务中断：${counts}` : `任务结束：${counts}`;
    }
    return event.stage || '处理中';
  }
  const targetName = event.target_unit_id ? getAgentDisplayName(event.target_unit_id) : '';
  const actionId = event.skill_id || event.tool || '操作';
  const action = ({ set_light_state: '调整灯光', set_led_state: '调整灯光',
    set_light_color: '调整灯光颜色', set_led_color: '调整灯光颜色', blink_led: '让灯光闪烁',
    go2_sport: '完成姿态动作', go2_move: '移动', go2_observe: '观察前方',
    go2_navigate_to_visible_target: '视觉定位并导航到目标附近', go2_find_object: '在指定范围内找物',
    go2_status: '检查连接状态', list_peers: '查找可协作的设备', list_agents: '查找可用设备',
    get_space_context: '查看空间信息', read_state: '查看传感器状态',
    await_result: '核对执行结果', request_peer_skill: '协同处理请求',
    go2_chat: '回应当前问题',
    start_activity: '安排空间活动', set_space_constraint: '记住空间里的共同要求',
    clear_space_constraints: '撤销空间里的共同要求' })[actionId] || '处理当前请求';
  // Raw light settings belong in the device detail view, not conversational progress.
  const argumentLabels = { duration: '时长', duration_s: '时长', speed: '速度' };
  const args = Object.entries(event.arguments || {}).filter(([key, value]) =>
    argumentLabels[key] && typeof value === 'number' && Number.isFinite(value))
    .map(([key, value]) => `${argumentLabels[key]} ${value}${key.startsWith('duration') ? ' 秒' : ''}`);
  const detailArgs = args.length ? `（${args.join('，')}）` : '';
  const state = event.arguments?.state;
  const cmd = event.arguments?.cmd;
  const sport = { StandDown: '坐下', StandUp: '站起来', Hello: '打招呼',
    StopMove: '停止移动', Stretch: '伸展' };
  const task = ['set_light_state', 'set_led_state'].includes(actionId) && (targetName || compact)
    ? `${compact ? '' : `请${targetName}`}${state === 'ON' ? '开灯' : state === 'OFF' ? '关灯' : `调整灯光${detailArgs}`}`
    : actionId === 'go2_chat' && (targetName || compact) && event.requested_text
      ? `${compact ? '' : `请${targetName}`}处理“${event.requested_text}”`
      : actionId === 'go2_sport' && sport[cmd]
        ? `${compact ? '' : `让${targetName || '机器狗'}`}${sport[cmd]}`
        : `${compact ? '' : targetName ? `请${targetName}` : '正在'}${action}${detailArgs}`;
  if (event.stage === 'decision') return compact ? `下一步：${task}` : `接下来${task}`;
  if (event.stage === 'dispatching') return `${task}，等待执行结果`;
  if (event.stage === 'considering') {
    const outcome = event.status === 'ok' ? '已确认完成'
      : event.status === 'accepted' || event.status === 'executing' || event.status === 'timeout'
        ? '完成情况未确认' : '未完成';
    return `${task}${outcome}，正在判断下一步`;
  }
  if (event.status === 'timeout' && event.reason === 'edge_result_unconfirmed') {
    return `${task}：等待设备执行回执超时，无法确认是否完成`;
  }
  const status = {
    ok: '已完成', accepted: '已受理，等待完成反馈', executing: '仍在执行',
    timeout: '等待结果超时，完成情况未确认', blocked: '未执行',
    rejected: '被拒绝', cancelled: '已取消', error: '执行失败',
  }[event.status] || '结果已更新';
  const stateLabels = { ON: '已开启', OFF: '已关闭', BLINKING: '正在闪烁', ERROR: '硬件异常',
    DIM: '柔光', BRIGHT: '亮光', COLOR: '彩光' };
  const detail = stateLabels[event.result_state] ? `，设备${stateLabels[event.result_state]}` : '';
  const reason = typeof event.reason === 'string' && /^[\u3400-\u9fff\u3000-\u303f\uff00-\uffef\s，。！？：；、（）“”·0-9]+$/.test(event.reason)
    ? `：${event.reason}` : '';
  return `${task}：${status}${detail}${reason}`;
}

function processEventText(event) {
  if (event.tool === 'start_activity') {
    return event.stage === 'decision' ? '正在邀请空间成员加入活动' : '';
  }
  if (event.stage === 'summarizing' || event.stage === 'considering') return '';
  return event.stage === 'narrated' && event.text
    ? event.text : describeRunProgress(event, true);
}

function isDeviceOnline(agent, unitData) {
  if (!(agent?._online ?? agent?.online)) return false;
  const connectionField = agent?.state_machine?.connection_field;
  if (!connectionField) return true;
  const state = unitData?.[agent.unit_id]?.state || agent.state || {};
  return state[connectionField] === true;
}

// 灯的运行状态与颜色参数分开显示。旧固件的 retained 状态只作迁移期展示。
function getLedStateInfo(state = {}) {
  const labels = {
    OFF: '熄灭', ON: '常亮', BLINKING: '闪烁', ERROR: '硬件异常',
    DIM: '柔光', BRIGHT: '亮光', COLOR: '彩光', BLINK: '闪烁',
  };
  const ism = state.ism || '';
  if (ism === 'ERROR') return { label: labels.ERROR, details: '', color: null };
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
