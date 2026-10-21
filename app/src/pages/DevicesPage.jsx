/* 空间首页：操作入口保持显眼，环境读数默认只占一行。 */
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

function DevicesScreen({ agents, unitData }) {
  const { useState: useStateD } = React;
  const [sensorsOpen, setSensorsOpen] = useStateD(false);
  const devices = agents.filter(a => a.agent_type === 'robot' || a.agent_type === 'actuator');
  const sensors = agents.filter(a => a.agent_type !== 'robot' && a.agent_type !== 'actuator');
  const visibleSensors = sensors.slice(0, 3);

  if (!agents.length) return null;

  return (
    <section className="home-devices" aria-label="空间设备">
      {devices.length > 0 && <div className="home-devices__group">
        <h2 className="home-devices__heading">可操作设备 <span>{devices.length}</span></h2>
        <div className="home-devices__list">
          {devices.map(agent => <ActuatorCard key={agent.unit_id} agent={agent} unitData={unitData}/>)}
        </div>
      </div>}
      {sensors.length > 0 && <div className="home-devices__group">
        <div className="home-devices__heading-row">
          <h2 className="home-devices__heading">环境感知 <span>{sensors.length}</span></h2>
          <button type="button" className="home-devices__expand" aria-expanded={sensorsOpen}
            onClick={() => setSensorsOpen(open => !open)}>
            {sensorsOpen ? '收起' : '查看全部'}
            <Icon name="arrow" size={12} sw={2}/>
          </button>
        </div>
        {!sensorsOpen && <div className="home-sensor-summary">
          {visibleSensors.map(agent => {
            const reading = agent._online ? getSensorReading(agent, unitData).value : '离线';
            return <div className="home-sensor-summary__item" key={agent.unit_id}>
              <span>{agent.name || getAgentMeta(agent).label}</span>
              <strong>{reading}</strong>
            </div>;
          })}
          {sensors.length > visibleSensors.length && <span className="home-sensor-summary__more">另有 {sensors.length - visibleSensors.length} 个</span>}
        </div>}
        {sensorsOpen && <div className="home-devices__list">
          {sensors.map(agent => <SensorCard key={agent.unit_id} agent={agent} unitData={unitData}/>)}
        </div>}
      </div>}
    </section>
  );
}

window.DeviceEmptyState = DeviceEmptyState;
window.DevicesScreen = DevicesScreen;
