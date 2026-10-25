/* 首页设备入口：名称和当前状态只出现一次，整行可进入设备页。 */
function HomeDeviceRow({ agent, unitData, connected }) {
  const uid = agent.unit_id;
  const meta = getAgentMeta(agent);
  const online = connected && agent._online;
  const state = (unitData[uid] || {}).state || {};
  const stateLabel = agentKind(agent) === 'led'
    ? getLedStateInfo(state).label
    : getStateLabel(agent, unitData);
  const detail = !connected ? '状态待同步'
    : !agent._online ? '离线'
    : stateLabel === '状态待同步' ? '在线' : stateLabel;

  return <button type="button" className="home-device-row"
    onClick={() => navigate('#/devices/' + encodeURIComponent(uid))}
    aria-label={`查看${agent.name || uid}，${detail}`}>
    <span className="home-device-row__icon" style={{ color: meta.color, background: `${meta.color}16` }}>
      <Icon name={meta.icon} size={18}/>
    </span>
    <span className="home-device-row__text">
      <strong>{agent.name || uid}</strong>
      <small>{detail}</small>
    </span>
    <span className={`home-device-row__dot${online ? ' is-online' : ''}`} aria-hidden="true"/>
    <Icon name="arrow" size={14} sw={2}/>
  </button>;
}

window.HomeDeviceRow = HomeDeviceRow;
