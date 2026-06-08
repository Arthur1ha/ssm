/* SSM PWA — App 根组件：路由、MQTT 引导、聊天历史状态 */
const { useState, useEffect, useRef } = React;

/* ── Hash 路由 ──────────────────────────────────────────────────── */
function useHash() {
  const [hash, setHash] = React.useState(window.location.hash);
  React.useEffect(() => {
    const handler = () => setHash(window.location.hash);
    window.addEventListener('hashchange', handler);
    return () => window.removeEventListener('hashchange', handler);
  }, []);
  return hash;
}

/* ── App ─────────────────────────────────────────────────────────── */
const EXCL_TYPES = new Set(['decision', 'supervisor']);
const EXCL_PLAT  = new Set(['pc', 'pwa']);

const GO2_STATIC_DEVICE = {
  unit_id:      "go2",
  agent_id:     "go2",
  slug:         "go2",
  name:         "Go2 Air",
  agent_type:   "robot",
  capabilities: ["MOVE", "STAND_UP", "SIT_DOWN", "HELLO", "STRETCH", "DANCE"],
};

function App() {
  const [tab, setTab]                         = useState('discover');
  const [sheetOpen, setSheetOpen]             = useState(false);
  const [connected, setConnected]             = useState(false);
  const [agents, setAgents]                   = useState([GO2_STATIC_DEVICE]);
  const [unitData, setUnitData]               = useState({});
  const [phoneLoc, setPhoneLoc]               = useState(null);
  const [locError, setLocError]               = useState(null);
  const [discoveryDevice, setDiscoveryDevice] = useState(null);
  const [chatHistories, setChatHistories]     = useState({
    main: [{ role: 'assistant', agent: 'orchestrator', agentName: 'SSM助手', text: '你好，需要我做什么？', actions: [] }],
    go2:  [{ role: 'assistant', agent: 'go2', agentName: 'Go2', text: '需要 Go2 做什么？', actions: [] }],
  });

  const appendMessage = (context, msg) => {
    setChatHistories(h => ({ ...h, [context]: [...(h[context] ?? []), msg] }));
  };

  const currentHash      = useHash();
  const seenPopupDevices = useRef(new Set());
  const agentsRef        = useRef([]);
  const phoneLocRef      = useRef(null);
  const onlineIdsRef     = useRef(new Set());

  useEffect(() => { agentsRef.current  = agents;   }, [agents]);
  useEffect(() => { phoneLocRef.current = phoneLoc; }, [phoneLoc]);

  useEffect(() => {
    const loc = phoneLocRef.current;
    const currentIds = new Set(agents.map(a => a.unit_id || a.agent_id));
    currentIds.forEach(uid => {
      if (!onlineIdsRef.current.has(uid)) seenPopupDevices.current.delete(uid);
    });
    onlineIdsRef.current = currentIds;
    if (!loc) return;
    agents.forEach(agent => {
      if (agent._lat == null || agent._lng == null) return;
      const uid = agent.unit_id || agent.agent_id;
      if (seenPopupDevices.current.has(uid)) return;
      const dist = haversine(loc.lat, loc.lng, agent._lat, agent._lng);
      if (dist < POPUP_RADIUS_M) {
        seenPopupDevices.current.add(uid);
        setDiscoveryDevice(prev => prev || agent);
      }
    });
  }, [agents]);

  useEffect(() => {
    if (!navigator.geolocation) { setLocError('浏览器不支持定位'); return; }
    const watchId = navigator.geolocation.watchPosition(
      pos => {
        const loc = { lat: pos.coords.latitude, lng: pos.coords.longitude };
        setPhoneLoc(loc);
        console.log('[GPS] phone loc:', loc, 'agents:', agentsRef.current.length);
        agentsRef.current.forEach(agent => {
          const uid = agent.unit_id || agent.agent_id;
          if (agent._lat == null || agent._lng == null) { console.log('[GPS] skip (no loc):', uid); return; }
          const dist = Math.round(haversine(loc.lat, loc.lng, agent._lat, agent._lng));
          console.log('[GPS]', uid, 'dist:', dist, 'm / threshold:', POPUP_RADIUS_M, 'm | seen:', seenPopupDevices.current.has(uid));
          if (seenPopupDevices.current.has(uid)) return;
          if (dist < POPUP_RADIUS_M) {
            seenPopupDevices.current.add(uid);
            setDiscoveryDevice(prev => prev || agent);
          }
        });
      },
      err => { console.warn('[GPS] error:', err.code, err.message); setLocError(err.code === 1 ? '位置权限被拒绝' : '定位失败'); },
      { enableHighAccuracy: true, timeout: 10000 }
    );
    return () => navigator.geolocation.clearWatch(watchId);
  }, []);

  useEffect(() => {
    const registry   = new AgentRegistry(mqttBus);
    const ismTracker = new ISMTracker(mqttBus);

    registry.addEventListener('change', () => {
      const mqttAgents = registry.getAll().filter(a =>
        a.agent_type && !EXCL_TYPES.has(a.agent_type) && !EXCL_PLAT.has(a.hw_platform) && a._online === true
      );
      setAgents([GO2_STATIC_DEVICE, ...mqttAgents]);
    });

    registry.addEventListener('reconnect', ({ detail }) => {
      agentsRef.current.forEach(agent => {
        if (agent.parent_id === detail.parentId) {
          const uid = agent.unit_id || agent.agent_id;
          seenPopupDevices.current.delete(uid);
          onlineIdsRef.current.delete(uid);
        }
      });
    });

    let pendingUnitData = false;
    ismTracker.addEventListener('update', () => {
      if (pendingUnitData) return;
      pendingUnitData = true;
      requestAnimationFrame(() => {
        pendingUnitData = false;
        const snap = {};
        ismTracker.unitIds().forEach(uid => {
          snap[uid] = {
            state:  ismTracker.get(uid, 'state'),
            event:  ismTracker.get(uid, 'event'),
            report: ismTracker.get(uid, 'report'),
          };
        });
        setUnitData({ ...snap });
      });
    });

    mqttBus.onConnect(() => {
      setConnected(true);
      mqttBus.publish('ssm/agents/phone_ui/manifest', {
        unit_id: 'phone_ui', agent_type: 'supervisor',
        name: 'human_supervisor', hw_platform: 'pwa',
        ts: Math.floor(Date.now() / 1000),
      }, { retain: true });
      mqttBus.subscribe('ssm/agents/desk/speech');
    });
    mqttBus.addEventListener('disconnect', () => setConnected(false));
    mqttBus.addEventListener('reconnect',  () => setConnected(false));

    mqttBus.connect(BROKER_URL, null, { username: BROKER_USER, password: BROKER_PASS });

    mqttBus.subscribe('ssm/agents/go2/thought');
    const go2ThoughtListener = (e) => {
      const ev = e.detail;
      if (!ev || !ev.type) return;
      if (ev.type === 'think') {
        appendMessage('main', { role: 'assistant', agent: 'go2', agentName: 'Go2', text: ev.text });
      } else if (ev.type === 'act') {
        appendMessage('main', { role: 'step', agent: 'go2', text: ev.text });
      }
    };
    mqttBus.addEventListener('topic:ssm/agents/go2/thought', go2ThoughtListener);
    return () => mqttBus.removeEventListener('topic:ssm/agents/go2/thought', go2ThoughtListener);
  }, []);

  useEffect(() => {
    const handleSpeech = (e) => {
      const { audio } = e.detail || {};
      if (audio) playAudioB64(audio);
    };
    mqttBus.addEventListener('topic:ssm/agents/desk/speech', handleSpeech);
    return () => mqttBus.removeEventListener('topic:ssm/agents/desk/speech', handleSpeech);
  }, []);

  const hashMatch = currentHash.match(/^#\/devices\/([^/]+)$/);
  if (hashMatch) {
    const slug = hashMatch[1];
    if (slug === "go2") {
      return <Go2DevicePage
        onBack={() => navigate('#')}
        messages={chatHistories.go2}
        onAppend={msg => appendMessage('go2', msg)}
      />;
    }
    const device = agents.find(a => a.slug === slug || (a.unit_id || a.agent_id) === slug);
    if (!device) { navigate('#'); return null; }
    const devCtx = device.unit_id || slug;
    return (
      <DeviceDetailPage
        slug={slug}
        device={device}
        unitData={unitData}
        onBack={() => navigate('#')}
        messages={chatHistories[devCtx] ?? [{ role: 'assistant', agent: devCtx, agentName: device.name, text: `你好，我是 ${device.name}，有什么可以帮你？` }]}
        onAppend={msg => appendMessage(devCtx, msg)}
      />
    );
  }

  return (
    <div style={{
      position: 'fixed', inset: 0, overflow: 'hidden',
      background: '#0B0B0E', color: '#fff',
      fontFamily: '-apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue", sans-serif',
      paddingTop: 'env(safe-area-inset-top, 0px)',
    }}>
      <div style={{ position: 'relative', height: '100%' }}>
        {tab === 'discover' && (
          <DiscoverScreen agents={agents} connected={connected} phoneLoc={phoneLoc} locError={locError}/>
        )}
        {tab === 'devices' && (
          <DevicesScreen agents={agents} unitData={unitData}/>
        )}
        {tab === 'rules' && <RulesScreen />}
        <PersistentInputBar onOpen={() => setSheetOpen(true)}/>
        <TabBar tab={tab} setTab={setTab} deviceBadge={agents.length}/>
        {discoveryDevice && (
          <div style={{ position: 'absolute', inset: 0, zIndex: 40,
            background: 'rgba(0,0,0,0.45)', backdropFilter: 'blur(3px)' }}
            onClick={() => setDiscoveryDevice(null)}>
            <div onClick={e => e.stopPropagation()}>
              <DeviceDiscoveryCard
                agent={discoveryDevice}
                unitData={unitData}
                onDismiss={() => setDiscoveryDevice(null)}
                onGo={() => { setTab('devices'); setDiscoveryDevice(null); }}
              />
            </div>
          </div>
        )}
      </div>
      <ChatSheet
        open={sheetOpen}
        onClose={() => setSheetOpen(false)}
        agents={agents}
        unitData={unitData}
        messages={chatHistories.main}
        onAppend={msg => appendMessage('main', msg)}
      />
    </div>
  );
}

ReactDOM.createRoot(document.getElementById('root')).render(<App/>);
