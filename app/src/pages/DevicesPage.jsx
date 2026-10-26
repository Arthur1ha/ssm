/* 首页把设备入口、环境读数和最近事件收在同一个空间概览里。 */
function DeviceEmptyState({ isAdmin, onAddDevice }) {
  return (
    <div className="device-empty">
      <span className="device-empty__icon"><Icon name="addDevice" size={26}/></span>
      <strong>空间里还没有设备</strong>
      <p>{isAdmin
        ? '添加第一台设备，空间就能开始感知和响应。'
        : '管理员添加设备后，设备状态会自动出现在这里。'}</p>
      {isAdmin && <button type="button" className="btn btn--accent" onClick={onAddDevice}>添加设备</button>}
    </div>
  );
}

function DevicesScreen({ agents, unitData, connected, recentEvents = [], spaceName }) {
  const { useState } = React;
  const [sensorsOpen, setSensorsOpen] = useState(false);
  const devices = agents.filter(a => a.agent_type === 'robot' || a.agent_type === 'actuator');
  const sensors = agents.filter(a => a.agent_type !== 'robot' && a.agent_type !== 'actuator');
  const onlineCount = agents.filter(agent => isDeviceOnline(agent, unitData)).length;
  const visibleSensors = sensorsOpen ? sensors : sensors.slice(0, 3);
  const latestEvent = recentEvents[0];

  if (!agents.length) return null;

  return <section className="home-space" aria-label={`${spaceName || '当前空间'}概览`}>
    <header className="home-space__heading">
      <h2>空间现在</h2>
      <span>{connected ? `${onlineCount}/${agents.length} 在线` : '状态待同步'}</span>
    </header>

    {devices.length > 0 && <div className="home-space__devices">
      {devices.map(agent => <HomeDeviceRow key={agent.unit_id} agent={agent}
        unitData={unitData} connected={connected}/>)}
    </div>}

    {sensors.length > 0 && <div className="home-space__sensors">
      <div className="home-space__sensors-heading">
        <span>环境感知</span>
        {sensors.length > 3 && <button type="button" aria-expanded={sensorsOpen}
          onClick={() => setSensorsOpen(open => !open)}>
          {sensorsOpen ? '收起' : `查看全部 ${sensors.length}`}
        </button>}
      </div>
      <div className={`home-space__readings${sensorsOpen ? ' is-expanded' : ''}`}>
        {visibleSensors.map(agent => {
          const reading = connected && agent._online ? getSensorReading(agent, unitData) : null;
          return <span className="home-space__reading" key={agent.unit_id}>
            <span>{agent.name || getAgentMeta(agent).label}</span>
            <strong style={{ color: reading?.color || 'var(--color-text-dim)' }}>
              {reading?.value || (connected ? '离线' : '—')}
            </strong>
          </span>;
        })}
      </div>
    </div>}

    {latestEvent && <div className="home-space__recent">
      <span>最近</span>
      <strong>{latestEvent.description}</strong>
      <time>{new Date(latestEvent.receivedAt).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}</time>
    </div>}
  </section>;
}

window.DeviceEmptyState = DeviceEmptyState;
window.DevicesScreen = DevicesScreen;
