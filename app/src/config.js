const BROKER_URL  = (location.protocol === 'https:')
  ? `wss://${location.host}/mqtt`
  : 'ws://47.116.137.202:9001';
const BROKER_USER = 'ssm_user';
const BROKER_PASS = 'Wl4sErQrlrpEbm7r';
const LIME            = '#C8FF3E';
const NEARBY_RADIUS_M = 300;
const POPUP_RADIUS_M  = 5000;

/* ── 智能体气泡颜色注册表（新增智能体在此追加） ── */
var AGENT_BUBBLE_COLORS = {
  orchestrator:   '#7C6DFF',
  go2:            '#00D4FF',
  esp32_desk_led: '#FF9A5A',
};
var AGENT_DISPLAY_NAMES = {
  orchestrator:   '管家',
  go2:            'GO2',
  esp32_desk_led: '智能灯',
};

function getAgentBubbleColor(agentId) {
  if (!agentId) return AGENT_BUBBLE_COLORS.orchestrator;
  if (AGENT_BUBBLE_COLORS[agentId]) return AGENT_BUBBLE_COLORS[agentId];
  var n = agentId.toLowerCase();
  if (n.includes('led') || n.includes('rgb') || n.includes('ws2812') || n.includes('ring')) return '#FF9A5A';
  if (n.includes('sound') || n.includes('mic'))  return '#E26BFF';
  if (n.includes('light') || n.includes('lux'))  return '#C8FF3E';
  if (n.includes('ir'))                          return '#6B6CFF';
  return AGENT_BUBBLE_COLORS.orchestrator;
}

function getAgentDisplayName(agentId) {
  if (!agentId) return AGENT_DISPLAY_NAMES.orchestrator;
  if (AGENT_DISPLAY_NAMES[agentId]) return AGENT_DISPLAY_NAMES[agentId];
  var n = agentId.toLowerCase();
  if (n.includes('led') || n.includes('rgb') || n.includes('ring')) return '智能灯';
  if (n.includes('sound') || n.includes('mic'))  return '声音传感器';
  if (n.includes('light') || n.includes('lux'))  return '光线传感器';
  if (n.includes('ir'))                          return 'IR 传感器';
  return agentId;
}

function navigate(hash) {
  window.location.hash = hash;
}

/* ── UI 版本开关：?ui=v2 > localStorage > 默认 v1 ── */
const UI_VERSION = (() => {
  const p = new URLSearchParams(location.search).get('ui');
  if (p) { try { localStorage.setItem('ssm_ui', p); } catch (e) {} return p; }
  try { return localStorage.getItem('ssm_ui') || 'v1'; } catch (e) { return 'v1'; }
})();
