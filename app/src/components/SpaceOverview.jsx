/* Compact, read-only picture of the current space. */
function SpaceOverview({ spaceName, agents, unitData, connected, recentEvents = [] }) {
  if (!agents.length) return null;

  const onlineCount = agents.filter(agent => agent._online).length;
  const actors = agents.filter(agent => agent.agent_type === 'robot' || agent.agent_type === 'actuator').slice(0, 3);
  const sensors = agents.filter(agent => agent.agent_type === 'sensor' && agent._online)
    .map(agent => {
      const kind = agentKind(agent);
      const live = unitData[agent.unit_id] || {};
      const state = live.state || {};
      if (kind === 'light') {
        const levels = { DARK: '暗', DIM: '微亮', NORMAL: '正常', BRIGHT: '强光' };
        return levels[state.level] ? { id: agent.unit_id, name: agent.name || '光线', value: levels[state.level] } : null;
      }
      if (kind === 'ir' && typeof state.presence === 'boolean') {
        return { id: agent.unit_id, name: agent.name || '人员感应', value: state.presence ? '有人' : '无人' };
      }
      if (kind === 'sound' && state.detected === false) {
        return { id: agent.unit_id, name: agent.name || '声音', value: '静默' };
      }
      return null;
    }).filter(Boolean).slice(0, 3);

  const actorStatus = agent => {
    if (!agent._online) return '离线';
    const state = unitData[agent.unit_id]?.state || {};
    if (agentKind(agent) === 'led') {
      const info = getLedStateInfo(state);
      return info.details && state.ism !== 'OFF' ? `${info.label} · ${info.details}` : info.label;
    }
    if (typeof state.ism === 'string' && state.ism) return state.ism;
    return '在线';
  };

  return <section className="space-overview" aria-label={`${spaceName || '当前空间'}概览`}>
    <div className="space-overview__heading">
      <span>空间现在</span>
      <span className="space-overview__count">{connected ? `${onlineCount}/${agents.length} 在线` : '连接中断，状态待同步'}</span>
    </div>
    {actors.length > 0 && <div className="space-overview__actors">
      {actors.map(agent => <div key={agent.unit_id} className="space-overview__actor">
        <span className={`space-overview__dot ${agent._online && connected ? 'is-online' : ''}`}/>
        <span className="space-overview__actor-name">{agent.name || getAgentMeta(agent).label}</span>
        <span className="space-overview__actor-state">{connected ? actorStatus(agent) : '待同步'}</span>
      </div>)}
    </div>}
    {connected && sensors.length > 0 && <div className="space-overview__sensors">
      {sensors.map(sensor => <span key={sensor.id}>{sensor.name} <strong>{sensor.value}</strong></span>)}
    </div>}
    <div className="space-overview__recent">
      <div className="space-overview__recent-title">最近发生</div>
      {recentEvents.length ? <ul>{recentEvents.slice(0, 3).map(item => <li key={item.id}>
        <span>{item.description}</span>
        <small>{item.name} · {new Date(item.receivedAt).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}</small>
      </li>)}</ul> : <p>本次进入后暂无设备事件</p>}
    </div>
  </section>;
}
window.SpaceOverview = SpaceOverview;
