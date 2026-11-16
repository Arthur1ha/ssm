/* 首页设备入口：名称和当前状态只出现一次，整行可进入设备页。 */
function HomeDeviceRow({ agent, unitData, connected, presentation }) {
  const uid = agent.unit_id;
  const online = connected && isDeviceOnline(agent, unitData);
  const stateLabel = getStateLabel(agent, unitData);
  const available = Boolean(online && stateLabel !== '状态待同步');
  const gesture = available && presentation?.gesture;
  const emotion = available && presentation?.emotion;
  const detail = !connected ? '状态待同步'
    : !online ? '离线'
    : stateLabel === '状态待同步' ? '在线' : stateLabel;

  return <button type="button" className="home-device-row"
    onClick={() => navigate('#/devices/' + encodeURIComponent(uid))}
    aria-label={`查看${getAgentDisplayName(agent)}，${detail}`}>
    <span className="home-device-row__icon">
      <ShapeFace agent={agent} size={36} animated={available}
        online={!connected || apiClient.devicesSynced === false ? null : Boolean(online)}
        shape={gesture?.shape || 'idle'} emotion={gesture?.expression || emotion?.value || 'neutral'}
        motion={gesture?.motion} animationKey={gesture?.eventId || emotion?.eventId}/>
    </span>
    <span className="home-device-row__text">
      <strong>{getAgentDisplayName(agent)}</strong>
      <small>{detail}</small>
    </span>
    <span className={`home-device-row__dot${online ? ' is-online' : ''}`} aria-hidden="true"/>
    <Icon name="arrow" size={14} sw={2}/>
  </button>;
}

window.HomeDeviceRow = HomeDeviceRow;
