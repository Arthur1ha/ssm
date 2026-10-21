/* AdminSpaceDrawer — current space settings, device membership, and available assets.
 * This component is mounted by App for space managers.
 */
function AdminSpaceDrawer({ open, onClose, onInvite, onDevicesChanged }) {
  const { useCallback, useEffect, useState } = React;
  const [space, setSpace] = useState(null);
  const [spaceName, setSpaceName] = useState('');
  const [deviceCandidates, setDeviceCandidates] = useState([]);
  const [locations, setLocations] = useState([]);
  const [personality, setPersonality] = useState('');
  const [people, setPeople] = useState([]);
  const [memory, setMemory] = useState({ taught: [], today: '' });
  const [locationName, setLocationName] = useState('');
  const [personName, setPersonName] = useState('');
  const [personRole, setPersonRole] = useState('known');
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState('');
  const [message, setMessage] = useState(null);

  const loadAssets = useCallback(async () => {
    setLoading(true);
    setMessage(null);
    const requests = [
      apiClient.request('/api/admin/space'),
      apiClient.request('/api/admin/device-candidates'),
      apiClient.request('/api/go2/navigation/locations'),
      apiClient.request('/api/go2/personality'),
      apiClient.request('/api/go2/people'),
      apiClient.request('/api/go2/memory'),
    ];
    const [spaceResult, candidateResult, locationResult, personalityResult, peopleResult, memoryResult] =
      await Promise.allSettled(requests);

    if (spaceResult.status === 'fulfilled') {
      setSpace(spaceResult.value);
      setSpaceName(spaceResult.value.name || '');
    }
    if (candidateResult.status === 'fulfilled') setDeviceCandidates(candidateResult.value || []);
    if (locationResult.status === 'fulfilled') {
      setLocations(Array.isArray(locationResult.value) ? locationResult.value : []);
    }
    if (personalityResult.status === 'fulfilled') {
      setPersonality(personalityResult.value?.prompt || '');
    }
    if (peopleResult.status === 'fulfilled') {
      setPeople(Array.isArray(peopleResult.value) ? peopleResult.value : []);
    }
    if (memoryResult.status === 'fulfilled') {
      setMemory({
        taught: Array.isArray(memoryResult.value?.taught) ? memoryResult.value.taught : [],
        today: memoryResult.value?.today || '（今天暂无记录）',
      });
    }

    const failures = [spaceResult, candidateResult, locationResult, personalityResult, peopleResult, memoryResult]
      .filter(result => result.status === 'rejected');
    if (failures.length) {
      setMessage({
        tone: 'error',
        text: failures.length === requests.length
          ? apiErrorText(failures[0].reason, '长期资产加载失败')
          : `有 ${failures.length} 组资产暂时无法读取，可稍后刷新`,
      });
    }
    setLoading(false);
  }, []);

  useEffect(() => { if (open) loadAssets(); }, [open, loadAssets]);
  useEffect(() => {
    if (!open) return undefined;
    const closeOnEscape = event => { if (event.key === 'Escape') onClose(); };
    window.addEventListener('keydown', closeOnEscape);
    return () => window.removeEventListener('keydown', closeOnEscape);
  }, [open, onClose]);

  const saveSpaceName = async event => {
    event.preventDefault();
    const name = spaceName.trim();
    if (!name || name === space?.name) return;
    setBusy('space'); setMessage(null);
    try {
      const updated = await apiClient.request('/api/admin/space', {
        method: 'PATCH', body: { name },
      });
      setSpace(updated); setSpaceName(updated.name || name);
      setMessage({ tone: 'ok', text: '空间名称已保存' });
    } catch (error) {
      setMessage({ tone: 'error', text: apiErrorText(error, '空间名称保存失败') });
    } finally { setBusy(''); }
  };

  const toggleDevice = async deviceId => {
    if (!space || busy) return;
    const current = new Set(space.device_ids || []);
    if (!current.has(deviceId)) return;
    current.delete(deviceId);
    setBusy(`device-${deviceId}`); setMessage(null);
    try {
      const updated = await apiClient.request('/api/admin/space', {
        method: 'PATCH', body: { device_ids: [...current] },
      });
      setSpace(updated);
      setDeviceCandidates(rows => rows.map(row => ({ ...row, added: current.has(row.device_id) })));
      onDevicesChanged?.();
      setMessage({ tone: 'ok', text: '设备已从当前空间移除' });
    } catch (error) {
      setMessage({ tone: 'error', text: apiErrorText(error, '设备配置保存失败') });
    } finally { setBusy(''); }
  };

  const runGo2Action = async (key, action, params, successText) => {
    if (busy) return false;
    setBusy(key); setMessage(null);
    const result = await commandBus.command('go2', action, params);
    if (result?.result === 'error' || result?.result === 'blocked' || result?.error) {
      setMessage({ tone: 'error', text: result.error || commandBus.describe(result) });
      setBusy('');
      return false;
    }
    setMessage({ tone: 'ok', text: successText });
    setBusy('');
    await loadAssets();
    setMessage({ tone: 'ok', text: successText });
    return true;
  };

  const tagLocation = async event => {
    event.preventDefault();
    const name = locationName.trim();
    if (!name) return;
    if (await runGo2Action('tag-location', 'tag_location', { name }, `已保存当前位置为「${name}」`)) {
      setLocationName('');
    }
  };

  const savePersonality = async event => {
    event.preventDefault();
    const prompt = personality.trim();
    if (!prompt) return;
    await runGo2Action('personality', 'set_personality', { prompt }, 'Go2 人设已更新');
  };

  const rememberPerson = async event => {
    event.preventDefault();
    const name = personName.trim();
    if (!name) return;
    if (await runGo2Action('remember-person', 'remember_person', {
      name, role: personRole,
    }, `已登记「${name}」`)) setPersonName('');
  };

  const roleLabel = role => ({ owner: '主人', family: '家人', friend: '朋友', known: '熟人' }[role] || role || '熟人');
  const formatCoordinate = value => Number.isFinite(Number(value)) ? Number(value).toFixed(2) : '—';
  const taughtLabel = rule => {
    const value = rule.behavior || rule.name || rule.action || rule.trigger;
    if (typeof value === 'string') return value;
    if (value && typeof value === 'object') return JSON.stringify(value);
    return '未命名习惯';
  };

  return (
    <>
      <div className={`drawer-mask ${open ? 'is-open' : ''}`} onClick={onClose}/>
      <aside className={`space-drawer ${open ? 'is-open' : ''}`} aria-hidden={!open}
        role="dialog" aria-modal="true" aria-labelledby="space-drawer-title">
        <div className="space-drawer__handle"/>
        <header className="space-drawer__header">
          <div>
            <small>SPACE MANAGEMENT</small>
            <h2 id="space-drawer-title">空间建设</h2>
          </div>
          <div className="space-drawer__header-actions">
            <button className="btn" onClick={loadAssets} disabled={loading}>
              {loading ? '同步中…' : '刷新资产'}
            </button>
            <button className="btn space-drawer__close" onClick={onClose} aria-label="关闭空间建设">
              <Icon name="x" size={14}/>
            </button>
          </div>
        </header>

        {message && (
          <div className={`space-notice space-notice--${message.tone}`} role="status">{message.text}</div>
        )}

        <div className="space-drawer__body" aria-busy={loading}>
          <section className="asset-section asset-section--space">
            <div className="asset-section__heading">
              <div><small>SPACE ID · {space?.id || '—'}</small><h3>{space?.name || '当前空间'}</h3></div>
              <span>{space?.device_ids?.length || 0} 台设备</span>
            </div>
            <form className="asset-inline-form" onSubmit={saveSpaceName}>
              <label htmlFor="space-name">空间名称</label>
              <div><input id="space-name" maxLength="80" value={spaceName}
                onChange={event => setSpaceName(event.target.value)} placeholder="给这个空间命名"/>
                <button className="btn btn--accent" disabled={busy === 'space' || !spaceName.trim() || spaceName.trim() === space?.name}>
                  {busy === 'space' ? '保存中…' : '保存名称'}
                </button></div>
            </form>
            <div className="space-members">
              {(space?.device_ids || []).map(deviceId => <code key={deviceId}>{deviceId}</code>)}
            </div>
          </section>

          <section className="asset-section asset-section--devices">
            <div className="asset-section__heading">
              <div><small>SPACE DEVICES</small><h3>空间设备</h3></div>
              <span>{space?.device_ids?.length || 0} 台已加入</span>
            </div>
            <p className="asset-section__intro">在首页右上角扫码添加设备；这里可移除已加入的设备并管理标签。</p>
            <div className="space-device-options">
              {deviceCandidates.map(device => (
                <div key={device.device_id} className={`space-device-option ${device.added ? 'is-added' : ''}`}>
                  <span className="space-device-option__icon"><Icon name="wifi" size={17}/></span>
                  <span className="space-device-option__copy"><strong>{device.name}</strong><small>{device.description}</small></span>
                  <span className={`space-device-option__state ${device.online ? 'is-online' : ''}`}>
                    <i/>{device.online ? '在线' : '离线'} · {device.added ? '已加入' : '未加入'}
                  </span>
                  <div className="space-device-option__actions">
                    <DeviceLabelButton device={device}/>
                    {device.added && <button type="button" className="btn btn--danger" onClick={() => toggleDevice(device.device_id)} disabled={loading || Boolean(busy)}>移除</button>}
                  </div>
                </div>
              ))}
            </div>
          </section>

          <section className="asset-section">
            <div className="asset-section__heading">
              <div><small>SPATIAL LEDGER</small><h3>命名地点</h3></div>
              <span>{locations.length} 个坐标</span>
            </div>
            <div className="location-ledger">
              {locations.length ? locations.map(location => (
                <article className="location-row" key={location.name}>
                  <div className="location-row__mark" aria-hidden="true"><i/><i/></div>
                  <div className="location-row__name"><strong>{location.name}</strong>
                    <small>H {formatCoordinate(location.heading)}</small></div>
                  <code>X {formatCoordinate(location.x)}<br/>Y {formatCoordinate(location.y)}</code>
                  <button className="asset-icon-action" title={`删除${location.name}`}
                    disabled={busy === `delete-location-${location.name}`}
                    onClick={() => {
                      if (window.confirm(`删除地点「${location.name}」？`)) {
                        runGo2Action(`delete-location-${location.name}`, 'delete_location',
                          { name: location.name }, `已删除地点「${location.name}」`);
                      }
                    }}><Icon name="x" size={12}/></button>
                </article>
              )) : <p className="asset-empty">还没有命名地点。让 Go2 站在目标位置后保存。</p>}
            </div>
            <form className="asset-action-row" onSubmit={tagLocation}>
              <input value={locationName} onChange={event => setLocationName(event.target.value)}
                maxLength="48" placeholder="当前位置名称，如：窗边"/>
              <button className="btn btn--accent" disabled={busy === 'tag-location' || !locationName.trim()}>保存当前位置</button>
            </form>
            <button className="btn asset-wide-action" disabled={busy === 'set-home'}
              onClick={() => runGo2Action('set-home', 'set_home', {}, '当前位置已设为 home')}>
              <Icon name="home" size={13}/> 将当前位置设为 home
            </button>
          </section>

          <section className="asset-section">
            <div className="asset-section__heading">
              <div><small>SOUL PROFILE</small><h3>Go2 人设</h3></div>
              <span>长期生效</span>
            </div>
            <form className="personality-editor" onSubmit={savePersonality}>
              <textarea value={personality} onChange={event => setPersonality(event.target.value)}
                rows="5" maxLength="4000" placeholder="描述 Go2 的性格、表达方式和行为偏好…"/>
              <div><small>{personality.length} / 4000</small>
                <button className="btn btn--accent" disabled={busy === 'personality' || !personality.trim()}>
                  {busy === 'personality' ? '更新中…' : '更新人设'}
                </button></div>
            </form>
          </section>

          <section className="asset-section">
            <div className="asset-section__heading">
              <div><small>KNOWN PEOPLE</small><h3>人员库</h3></div>
              <span>{people.length} 人</span>
            </div>
            <div className="people-list">
              {people.length ? people.map(person => (
                <article className="person-row" key={person.person_id}>
                  <div className="person-row__avatar">{(person.name || '?').slice(0, 1)}</div>
                  <div><strong>{person.name}</strong><small>{roleLabel(person.role)} · {person.face_count || 0} 张人脸</small></div>
                  <button className="btn" disabled={busy === `face-${person.person_id}`}
                    onClick={() => runGo2Action(`face-${person.person_id}`, 'add_person_face',
                      { person_id: person.person_id }, `已为「${person.name}」补录当前人脸`)}>补录人脸</button>
                  <button className="asset-icon-action" title={`忘记${person.name}`}
                    disabled={busy === `delete-person-${person.person_id}`}
                    onClick={() => {
                      if (window.confirm(`从人员库删除「${person.name}」？`)) {
                        runGo2Action(`delete-person-${person.person_id}`, 'delete_person',
                          { person_id: person.person_id }, `已从人员库删除「${person.name}」`);
                      }
                    }}><Icon name="x" size={12}/></button>
                </article>
              )) : <p className="asset-empty">人员库为空。请让待登记的人单独站在 Go2 镜头前。</p>}
            </div>
            <form className="person-enrol" onSubmit={rememberPerson}>
              <input value={personName} onChange={event => setPersonName(event.target.value)}
                maxLength="48" placeholder="镜头前的人叫什么？"/>
              <select value={personRole} onChange={event => setPersonRole(event.target.value)} aria-label="人员关系">
                <option value="owner">主人</option><option value="family">家人</option>
                <option value="friend">朋友</option><option value="known">熟人</option>
              </select>
              <button className="btn btn--accent" disabled={busy === 'remember-person' || !personName.trim()}>登记当前人脸</button>
            </form>
          </section>

          <section className="asset-section asset-section--memory">
            <div className="asset-section__heading">
              <div><small>LONG MEMORY</small><h3>长期记忆摘要</h3></div>
              <span>{memory.taught.length} 条习惯</span>
            </div>
            <div className="memory-block">
              <h4>今日事件</h4><pre>{memory.today || '（今天暂无记录）'}</pre>
            </div>
            <div className="memory-block">
              <h4>已教习惯</h4>
              {memory.taught.length ? <ul>{memory.taught.map((rule, index) => (
                <li key={rule.id || index}><span>{taughtLabel(rule)}</span>
                  <code>{rule.hit_count || 0} 次触发</code></li>
              ))}</ul> : <p className="asset-empty">还没有保存的调教习惯。</p>}
            </div>
          </section>
        </div>
        <footer className="space-drawer__footer">
          <span>{space?.name || '当前空间'} · {space?.device_ids?.length || 0} 台设备</span>
          <button className="btn btn--accent" onClick={onInvite} disabled={!space?.device_ids?.length}>
            生成访客二维码 <Icon name="arrow" size={14}/>
          </button>
        </footer>
      </aside>
    </>
  );
}

window.AdminSpaceDrawer = AdminSpaceDrawer;

function DeviceLabelButton({ device }) {
  const { useEffect, useRef, useState } = React;
  const [label, setLabel] = useState(null);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const qrRef = useRef(null);

  const load = async () => {
    setOpen(true); setBusy(true); setError('');
    try { setLabel(await apiClient.request(`/api/admin/device-codes/${encodeURIComponent(device.device_id)}`)); }
    catch (requestError) { setError(apiErrorText(requestError, '设备标签暂时不可用')); }
    finally { setBusy(false); }
  };
  useEffect(() => {
    if (!open || !label?.code || !qrRef.current) return;
    let active = true;
    qrRef.current.innerHTML = '';
    loadQrLibrary('generate').then(QRCode => {
      if (!active || !qrRef.current) return;
      new QRCode(qrRef.current, { text: label.code, width: 176, height: 176,
        colorDark: '#0c0c15', colorLight: '#ffffff', correctLevel: QRCode.CorrectLevel.M });
    }).catch(() => { if (active) setError('二维码生成失败，请重试'); });
    return () => { active = false; };
  }, [open, label?.code]);

  const rotate = async () => {
    if (!window.confirm(`轮换「${device.name}」设备标签？旧标签将立即失效。`)) return;
    setBusy(true); setError('');
    try { setLabel(await apiClient.request(`/api/admin/device-codes/${encodeURIComponent(device.device_id)}/rotate`, { method: 'POST', body: {} })); }
    catch (requestError) { setError(apiErrorText(requestError, '标签轮换失败')); }
    finally { setBusy(false); }
  };
  const save = () => {
    const canvas = qrRef.current?.querySelector('canvas');
    const image = qrRef.current?.querySelector('img');
    const href = canvas?.toDataURL('image/png') || image?.src;
    if (!href) { setError('二维码尚未生成，请稍后重试'); return; }
    const link = document.createElement('a');
    link.href = href;
    link.download = `ssm-${device.device_id}-label.png`;
    link.click();
  };

  return <>
    <button type="button" className="btn" onClick={load}>设备标签</button>
    {open && ReactDOM.createPortal(<div className="device-label-backdrop" onClick={() => setOpen(false)}>
      <section className="device-label-dialog" role="dialog" aria-modal="true" aria-label={`${device.name}设备标签`} onClick={event => event.stopPropagation()}>
        <header><div><small>PHYSICAL DEVICE · {device.device_id}</small><h3>{device.name}设备标签</h3></div><button className="btn" onClick={() => setOpen(false)} aria-label="关闭设备标签"><Icon name="x" size={14}/></button></header>
        <p>保存或打印后贴在设备上。扫码时，设备仍需先发布能力卡。</p>
        {busy && !label && <p role="status">正在读取设备标签…</p>}
        {label && <div className="device-label-qr" ref={qrRef}/>}
        {error && <p className="device-scan-error" role="alert">{error}</p>}
        <div className="device-label-actions">
          <button className="btn btn--accent" onClick={save} disabled={!label || busy}>保存二维码</button>
          <button className="btn" onClick={() => window.print()} disabled={!label || busy}>打印</button>
          <button className="btn btn--danger" onClick={rotate} disabled={busy}>轮换标签</button>
        </div>
      </section>
    </div>, document.body)}
  </>;
}
