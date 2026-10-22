/* AdminSpaceDrawer — current space settings and device membership.
 * This component is mounted by App for space managers.
 */
function AdminSpaceDrawer({ open, onClose, onInvite, onDevicesChanged, onSpaceRenamed }) {
  const { useCallback, useEffect, useState } = React;
  const [space, setSpace] = useState(null);
  const [spaceName, setSpaceName] = useState('');
  const [deviceCandidates, setDeviceCandidates] = useState([]);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState('');
  const [message, setMessage] = useState(null);

  const loadAssets = useCallback(async () => {
    setLoading(true);
    setMessage(null);
    const [spaceResult, candidateResult] = await Promise.allSettled([
      apiClient.request('/api/admin/space'),
      apiClient.request('/api/admin/device-candidates'),
    ]);
    if (spaceResult.status === 'fulfilled') {
      setSpace(spaceResult.value);
      setSpaceName(spaceResult.value.name || '');
    }
    if (candidateResult.status === 'fulfilled') setDeviceCandidates(candidateResult.value || []);
    const failures = [spaceResult, candidateResult].filter(result => result.status === 'rejected');
    if (failures.length) {
      setMessage({
        key: 'refresh',
        tone: 'error',
        text: failures.length === 2
          ? apiErrorText(failures[0].reason, '空间设置加载失败')
          : '部分空间设置暂时无法读取，可稍后刷新',
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
      onSpaceRenamed?.();
      setMessage({ key: 'space', tone: 'ok', text: '空间名称已保存' });
    } catch (error) {
      setMessage({ key: 'space', tone: 'error', text: apiErrorText(error, '空间名称保存失败') });
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
      setMessage({ key: `device-${deviceId}`, tone: 'ok', text: '设备已从当前空间移除' });
    } catch (error) {
      setMessage({ key: `device-${deviceId}`, tone: 'error', text: apiErrorText(error, '设备配置保存失败') });
    } finally { setBusy(''); }
  };

  const feedback = key => message?.key === key && <small className={`action-feedback action-feedback--${message.tone}`}
    role="status" title={message.text}>{message.text}</small>;

  return (
    <>
      <div className={`drawer-mask ${open ? 'is-open' : ''}`} onClick={onClose}/>
      <aside className={`space-drawer ${open ? 'is-open' : ''}`} aria-hidden={!open}
        role="dialog" aria-modal="true" aria-labelledby="space-drawer-title">
        <div className="space-drawer__handle"/>
        <header className="space-drawer__header">
          <div>
            <small>SPACE MANAGEMENT</small>
            <h2 id="space-drawer-title">空间设置</h2>
          </div>
          <div className="space-drawer__header-actions">
            <div className="feedback-button"><button className="btn" onClick={loadAssets} disabled={loading}>
              {loading ? '同步中…' : '刷新空间'}
            </button>{feedback('refresh')}</div>
            <button className="btn space-drawer__close" onClick={onClose} aria-label="关闭空间设置">
              <Icon name="x" size={14}/>
            </button>
          </div>
        </header>

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
              {feedback('space')}
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
            <p className="asset-section__intro">从首页的管理菜单添加设备；这里可移除已加入的设备并管理标签。</p>
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
                  {feedback(`device-${device.device_id}`)}
                </div>
              ))}
            </div>
          </section>

        </div>
        <footer className="space-drawer__footer">
          <span>{space?.name || '当前空间'} · {space?.device_ids?.length || 0} 台设备</span>
          <button className="btn btn--accent" onClick={onInvite} disabled={!space?.device_ids?.length}>
            查看空间二维码 <Icon name="arrow" size={14}/>
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
  const [errorTarget, setErrorTarget] = useState('load');
  const qrRef = useRef(null);

  const load = async () => {
    setOpen(true); setBusy(true); setError('');
    try { setLabel(await apiClient.request(`/api/admin/device-codes/${encodeURIComponent(device.device_id)}`)); }
    catch (requestError) { setErrorTarget('load'); setError(apiErrorText(requestError, '设备标签暂时不可用')); }
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
    }).catch(() => { if (active) { setErrorTarget('save'); setError('二维码生成失败，请重试'); } });
    return () => { active = false; };
  }, [open, label?.code]);

  const rotate = async () => {
    if (!window.confirm(`轮换「${device.name}」设备标签？旧标签将立即失效。`)) return;
    setBusy(true); setError('');
    try { setLabel(await apiClient.request(`/api/admin/device-codes/${encodeURIComponent(device.device_id)}/rotate`, { method: 'POST', body: {} })); }
    catch (requestError) { setErrorTarget('rotate'); setError(apiErrorText(requestError, '标签轮换失败')); }
    finally { setBusy(false); }
  };
  const save = () => {
    const canvas = qrRef.current?.querySelector('canvas');
    const image = qrRef.current?.querySelector('img');
    const href = canvas?.toDataURL('image/png') || image?.src;
    if (!href) { setErrorTarget('save'); setError('二维码尚未生成，请稍后重试'); return; }
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
        {busy && !label && <small className="action-feedback" role="status">正在读取设备标签…</small>}
        {label && <div className="device-label-qr" ref={qrRef}/>}
        {!label && !busy && errorTarget === 'load' && error && <div className="feedback-button">
          <button className="btn" onClick={load}>重新读取</button>
          <small className="action-feedback action-feedback--error" role="alert" title={error}>{error}</small>
        </div>}
        <div className="device-label-actions">
          <div className="feedback-button"><button className="btn btn--accent" onClick={save} disabled={!label || busy}>保存二维码</button>
            {errorTarget === 'save' && error && <small className="action-feedback action-feedback--error" role="alert" title={error}>{error}</small>}</div>
          <button className="btn" onClick={() => window.print()} disabled={!label || busy}>打印</button>
          <div className="feedback-button"><button className="btn btn--danger" onClick={rotate} disabled={busy}>轮换标签</button>
            {errorTarget === 'rotate' && error && <small className="action-feedback action-feedback--error" role="alert" title={error}>{error}</small>}</div>
        </div>
      </section>
    </div>, document.body)}
  </>;
}
