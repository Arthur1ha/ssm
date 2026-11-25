/* AdminSpaceDrawer — current space settings and device membership.
 * This component is mounted by App for space managers.
 */
function AdminSpaceDrawer({ open, onClose, onDevicesChanged, onSpaceRenamed }) {
  const { useCallback, useEffect, useRef, useState } = React;
  const [space, setSpace] = useState(null);
  const [spaceName, setSpaceName] = useState('');
  const [deviceCandidates, setDeviceCandidates] = useState([]);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState('');
  const [message, setMessage] = useState(null);
  const [deviceMenu, setDeviceMenu] = useState('');
  const [menuAbove, setMenuAbove] = useState(false);
  const menuRef = useRef(null);
  const menuTriggerRef = useRef(null);

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
          ? apiErrorText(failures[0].reason, '空间信息加载失败')
          : '部分空间信息暂时无法读取，可稍后刷新',
      });
    }
    setLoading(false);
  }, []);

  useEffect(() => { if (open) loadAssets(); else setDeviceMenu(''); }, [open, loadAssets]);
  useEffect(() => {
    if (!deviceMenu) return undefined;
    const closeOutside = event => { if (!menuRef.current?.contains(event.target)) setDeviceMenu(''); };
    const closeOnEscape = event => {
      if (event.key !== 'Escape') return;
      event.preventDefault(); event.stopImmediatePropagation();
      setDeviceMenu(''); menuTriggerRef.current?.focus();
    };
    document.addEventListener('pointerdown', closeOutside);
    window.addEventListener('keydown', closeOnEscape, true);
    menuRef.current?.querySelector('[role="menuitem"]')?.focus();
    return () => {
      document.removeEventListener('pointerdown', closeOutside);
      window.removeEventListener('keydown', closeOnEscape, true);
    };
  }, [deviceMenu]);
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

  const removeDevice = async deviceId => {
    if (!space || busy) return;
    const current = new Set(space.device_ids || []);
    if (!current.has(deviceId)) return;
    setDeviceMenu('');
    current.delete(deviceId);
    setBusy(`device-${deviceId}`); setMessage(null);
    try {
      const updated = await apiClient.request('/api/admin/space', {
        method: 'PATCH', body: { device_ids: [...current] },
      });
      setSpace(updated);
      setDeviceCandidates(rows => rows.map(row => ({ ...row, added: current.has(row.device_id) })));
      onDevicesChanged?.();
    } catch (error) {
      setMessage({ key: `device-${deviceId}`, tone: 'error', text: apiErrorText(error, '设备配置保存失败') });
    } finally { setBusy(''); }
  };

  const feedback = key => message?.key === key && <small className={`action-feedback action-feedback--${message.tone}`}
    role={message.tone === 'error' ? 'alert' : 'status'} title={message.text}>{message.text}</small>;

  return (
    <>
      <div className={`drawer-mask ${open ? 'is-open' : ''}`} onClick={onClose}/>
      <aside className={`management-dialog space-drawer ${open ? 'is-open' : ''}`} aria-hidden={!open} inert={open ? undefined : ''} style={{ visibility: open ? 'visible' : 'hidden' }}
        role="dialog" aria-modal="true" aria-labelledby="space-drawer-title">
        <header className="space-drawer__header">
          <div>
            <h2 id="space-drawer-title">空间管理</h2>
          </div>
          <div className="space-drawer__header-actions">
            <div className="feedback-button"><button className={`icon-button ${loading ? 'is-loading' : ''}`} onClick={loadAssets} disabled={loading} aria-label="刷新空间" title="刷新空间">
              <Icon name="refresh" size={18}/>
            </button>{feedback('refresh')}</div>
            <button className="icon-button space-drawer__close" onClick={onClose} aria-label="关闭空间管理">
              <Icon name="x" size={16}/>
            </button>
          </div>
        </header>

        <div className="space-drawer__body" aria-busy={loading}>
          <section className="asset-section asset-section--space">
            <form className="asset-inline-form" {...ssmInputAttributes.form} onSubmit={saveSpaceName}>
              <label htmlFor="space-name">空间名称</label>
              <div><input id="space-name" {...ssmInputAttributes.text} maxLength="80" value={spaceName}
                onChange={event => setSpaceName(event.target.value)} placeholder="给这个空间命名"/>
                <button className="btn btn--accent" disabled={busy === 'space' || !spaceName.trim() || spaceName.trim() === space?.name}>
                  {busy === 'space' ? '保存中…' : '保存'}
                </button></div>
              {feedback('space')}
            </form>

          </section>

          <section className="asset-section asset-section--devices">
            <div className="asset-section__heading">
              <h3>设备</h3>
              {Boolean(space?.device_ids?.length) && <span>{space.device_ids.length} 台</span>}
            </div>
            <div className="space-device-options space-device-options--grouped">
              {deviceCandidates.filter(device => space?.device_ids?.includes(device.device_id)).map(device => (
                <div key={device.device_id} className="space-device-option">
                  <span className="space-device-option__icon"><Icon name={device.device_id === 'go2' ? 'robot' : 'bulb'} size={22}/></span>
                  <div className="space-device-option__copy">
                    <div className="space-device-option__heading"><strong>{device.name}</strong>
                      <span className={`space-device-option__state ${device.online ? 'is-online' : ''}`}>
                        <i/>{device.online ? '在线' : '离线'}
                      </span>
                    </div>
                    <small>{device.description}</small>
                  </div>
                    <div className="space-device-option__menu" ref={deviceMenu === device.device_id ? menuRef : null}>
                      <button type="button" className="icon-button" aria-label={`${device.name}更多操作`}
                        aria-haspopup="menu" aria-expanded={deviceMenu === device.device_id}
                        aria-controls={`device-menu-${device.device_id}`} disabled={loading || Boolean(busy)}
                        onClick={event => {
                          menuTriggerRef.current = event.currentTarget;
                          const trigger = event.currentTarget.getBoundingClientRect();
                          const body = event.currentTarget.closest('.space-drawer__body').getBoundingClientRect();
                          setMenuAbove(body.bottom - trigger.bottom < 108 && trigger.top - body.top >= 108);
                          setDeviceMenu(current => current === device.device_id ? '' : device.device_id);
                        }}><Icon name="more" size={18}/></button>
                      <div className={`space-device-menu ${menuAbove ? 'space-device-menu--above' : ''}`}
                        hidden={deviceMenu !== device.device_id} id={`device-menu-${device.device_id}`} role="menu" aria-label={`${device.name}操作`}
                        onBlur={event => { if (!event.currentTarget.parentElement.contains(event.relatedTarget)) setDeviceMenu(''); }}
                        onKeyDown={event => {
                          if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
                          event.preventDefault();
                          const items = [...event.currentTarget.querySelectorAll('[role="menuitem"]:not([disabled])')];
                          const index = items.indexOf(document.activeElement);
                          const next = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1
                            : (index + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
                          items[next]?.focus();
                        }}>
                        <DeviceLabelButton device={device} menuItem onOpen={() => setDeviceMenu('')} returnFocusRef={menuTriggerRef}/>
                        <button type="button" role="menuitem" className="btn btn--danger" onClick={() => removeDevice(device.device_id)}>
                          <Icon name="trash" size={15}/> 移除设备
                        </button>
                      </div>
                    </div>
                  {feedback(`device-${device.device_id}`)}
                </div>
              ))}
            </div>
            {!loading && !space?.device_ids?.length && <p className="asset-empty">暂无设备</p>}
            {deviceCandidates.some(device => !space?.device_ids?.includes(device.device_id)) && <details className="space-device-history">
              <summary>历史设备</summary>
              {deviceCandidates.filter(device => !space?.device_ids?.includes(device.device_id)).map(device => (
                <div className="space-device-history__row" key={device.device_id}>
                  <span>{device.name}</span><DeviceLabelButton device={device}/>
                </div>
              ))}
            </details>}
          </section>

        </div>

      </aside>
    </>
  );
}

window.AdminSpaceDrawer = AdminSpaceDrawer;

function DeviceLabelButton({ device, menuItem = false, onOpen, returnFocusRef }) {
  const { useEffect, useRef, useState } = React;
  const [label, setLabel] = useState(null);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [errorTarget, setErrorTarget] = useState('load');
  const qrRef = useRef(null);
  const dialogRef = useRef(null);
  const triggerRef = useRef(null);
  useEffect(() => {
    if (!open) return undefined;
    const parent = triggerRef.current?.closest('[role="dialog"]');
    const wasInert = parent?.inert;
    if (parent) parent.inert = true;
    dialogRef.current?.querySelector('button')?.focus();
    const onKeyDown = event => {
      if (event.key === 'Escape') {
        event.preventDefault(); event.stopImmediatePropagation(); setOpen(false);
      }
      if (event.key !== 'Tab') return;
      const buttons = [...(dialogRef.current?.querySelectorAll('button:not([disabled])') || [])];
      const first = buttons[0], last = buttons[buttons.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault(); last?.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault(); first?.focus();
      }
    };
    window.addEventListener('keydown', onKeyDown, true);
    return () => {
      window.removeEventListener('keydown', onKeyDown, true);
      if (parent) parent.inert = wasInert || parent.getAttribute('aria-hidden') === 'true';
      const trigger = returnFocusRef?.current || triggerRef.current;
      if (trigger?.isConnected && !parent?.inert) trigger.focus();
    };
  }, [open, returnFocusRef]);

  const load = async () => {
    setOpen(true); setBusy(true); setError('');
    onOpen?.();
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
      renderQrCode(qrRef.current, QRCode, label.code);
    }).catch(() => { if (active) { setErrorTarget('save'); setError('二维码生成失败，请重试'); } });
    return () => { active = false; };
  }, [open, label?.code]);

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
    <button ref={triggerRef} type="button" role={menuItem ? 'menuitem' : undefined} className={menuItem ? undefined : 'btn'} onClick={load}><Icon name="scan" size={15}/>设备标签</button>
    {open && ReactDOM.createPortal(<div className="device-label-backdrop" onClick={() => setOpen(false)}>
      <section ref={dialogRef} className="management-dialog device-label-dialog" role="dialog" aria-modal="true" aria-label={`${device.name}设备标签`} onClick={event => event.stopPropagation()}>
        <header><div><h3>{device.name}</h3></div><button className="icon-button" onClick={() => setOpen(false)} aria-label="关闭设备标签"><Icon name="x" size={16}/></button></header>
        {busy && !label && <small className="action-feedback" role="status">正在读取设备标签…</small>}
        {label && <div className="device-label-qr" ref={qrRef}/>}
        {!label && !busy && errorTarget === 'load' && error && <div className="feedback-button">
          <button className="btn" onClick={load}>重新读取</button>
          <small className="action-feedback action-feedback--error" role="alert" title={error}>{error}</small>
        </div>}
        <div className="device-label-actions">
          <div className="feedback-button"><button className="btn btn--accent" onClick={save} disabled={!label || busy}>保存</button>
            {errorTarget === 'save' && error && <small className="action-feedback action-feedback--error" role="alert" title={error}>{error}</small>}</div>

        </div>
      </section>
    </div>, document.body)}
  </>;
}
