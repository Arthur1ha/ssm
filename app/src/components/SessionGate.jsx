function normalizeSession(data) {
  const raw = data?.session || data;
  const actor = raw?.actor || raw?.role;
  if (!raw || !['admin', 'visitor'].includes(actor)) return null;
  return {
    ...raw,
    actor,
    expires_at: Number(raw.expires_at || 0),
    space_name: raw.space_name || data?.space_name || 'SSM 体验空间',
  };
}

function consumeInviteTokenFromUrl() {
  const fragment = new URLSearchParams(window.location.hash.replace(/^#\??/, ''));
  const token = fragment.get('invite') || fragment.get('token') || '';
  const url = new URL(window.location.href);
  // Query credentials are never accepted. Remove them defensively so a bad
  // link cannot leave a token-shaped secret in navigation/referrer history.
  url.searchParams.delete('invite');
  url.searchParams.delete('token');
  if (/^#\??(?:invite|token)=/.test(url.hash)) url.hash = '';
  history.replaceState(null, '', url.pathname + url.search + url.hash);
  return token;
}

function SessionGate({ children }) {
  const { useCallback, useEffect, useState } = React;
  const [session, setSession] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const applySession = useCallback(data => {
    const next = normalizeSession(data);
    if (!next) throw new Error('服务端没有返回有效会话');
    apiClient.setCsrfToken(data?.csrf_token || data?.session?.csrf_token || next.csrf_token || '');
    setSession(next);
    setError('');
    apiClient.openEvents();
    return next;
  }, []);

  const loadMe = useCallback(async () => {
    const data = await apiClient.request('/api/me');
    return applySession(data);
  }, [applySession]);

  useEffect(() => {
    let active = true;
    const boot = async () => {
      // Consume and scrub the fragment before the first network round trip.
      // A query-string token is deliberately ignored.
      const inviteToken = consumeInviteTokenFromUrl();
      try {
        const data = await apiClient.request('/api/me', { suppressUnauthorized: true });
        if (active) applySession(data);
      } catch (meError) {
        if (!inviteToken) {
          if (active) setSession(null);
          return;
        }
        try {
          const data = await apiClient.request('/api/visitor/session', {
            method: 'POST', body: { token: inviteToken }, suppressUnauthorized: true,
          });
          if (!active) return;
          applySession(data);
        } catch (exchangeError) {
          if (active) setError(apiErrorText(exchangeError, '邀请无效或已过期'));
        }
      } finally {
        if (active) setLoading(false);
      }
    };
    boot();
    const unauthorized = () => {
      apiClient.closeEvents();
      apiClient.setCsrfToken('');
      setSession(null);
      setError('会话已结束，请重新进入');
    };
    apiClient.addEventListener('unauthorized', unauthorized);
    return () => {
      active = false;
      apiClient.removeEventListener('unauthorized', unauthorized);
    };
  }, [applySession]);

  useEffect(() => {
    if (!session?.expires_at) return;
    const delay = Math.max(0, session.expires_at * 1000 - Date.now());
    const timer = setTimeout(() => {
      apiClient.closeEvents();
      apiClient.setCsrfToken('');
      setSession(null);
      setError('会话已过期，请重新进入');
    }, delay);
    return () => clearTimeout(timer);
  }, [session?.expires_at]);

  const adminLogin = async accessCode => {
    const data = await apiClient.request('/api/admin/session', {
      method: 'POST', body: { access_code: accessCode }, suppressUnauthorized: true,
    });
    applySession(data);
    try { await loadMe(); } catch (_) {}
  };

  const logout = async () => {
    try { await apiClient.request('/api/session', { method: 'DELETE' }); }
    finally {
      apiClient.closeEvents();
      apiClient.setCsrfToken('');
      setSession(null);
    }
  };

  if (loading) return <SessionLoading/>;
  if (!session) return <SessionEntry error={error} onAdminLogin={adminLogin}/>;
  return children({ session, logout, refreshSession: loadMe });
}

function SessionLoading() {
  return (
    <main className="session-entry" aria-busy="true">
      <div className="session-entry__mark">SSM</div>
      <div className="session-entry__pulse"/>
      <p>正在确认通行权限…</p>
    </main>
  );
}

function SessionEntry({ error, onAdminLogin }) {
  const [showLogin, setShowLogin] = React.useState(false);
  const [code, setCode] = React.useState('');
  const [busy, setBusy] = React.useState(false);
  const [message, setMessage] = React.useState(error || '');

  React.useEffect(() => setMessage(error || ''), [error]);

  const submit = async event => {
    event.preventDefault();
    if (!code.trim() || busy) return;
    setBusy(true);
    setMessage('');
    try { await onAdminLogin(code); }
    catch (loginError) { setMessage(apiErrorText(loginError, '访问码不正确')); }
    finally { setBusy(false); }
  };

  return (
    <main className="session-entry">
      <section className="session-entry__panel">
        <div className="session-entry__brand">
          <span>SSM</span><i/> <small>SMART SYSTEM MESH</small>
        </div>
        <div className="session-entry__signal" aria-hidden="true">
          <span/><span/><span/><span/>
          <b>MAIN</b>
        </div>
        <p className="session-entry__eyebrow">固定体验空间</p>
        <h1>让空间里的智能体<br/>一起回应你</h1>
        <p className="session-entry__copy">扫描管理员分享的体验码会自动进入。空间建设者可使用访问码管理设备与长期规则。</p>

        {message && <div className="session-entry__error" role="alert">{message}</div>}

        {showLogin ? (
          <form className="session-entry__form" onSubmit={submit}>
            <label htmlFor="admin-code">管理员访问码</label>
            <input id="admin-code" type="password" autoComplete="current-password"
              value={code} onChange={event => setCode(event.target.value)} autoFocus/>
            <div className="session-entry__actions">
              <button type="button" className="btn" onClick={() => setShowLogin(false)}>返回</button>
              <button type="submit" className="btn btn--accent" disabled={busy || !code.trim()}>
                {busy ? '正在验证…' : '进入管理席'}
              </button>
            </div>
          </form>
        ) : (
          <div className="session-entry__choices">
            <button className="btn btn--accent" onClick={() => setShowLogin(true)}>
              <Icon name="user" size={14}/> 管理员进入
            </button>
            <p><Icon name="scan" size={13}/> 体验者请扫描邀请二维码</p>
          </div>
        )}
      </section>
    </main>
  );
}

function SessionBadge({ session }) {
  const [now, setNow] = React.useState(() => Date.now());
  React.useEffect(() => {
    if (session.actor !== 'visitor') return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [session.actor]);
  if (session.actor === 'admin') {
    return <span className="session-badge session-badge--admin"><i/> 管理员</span>;
  }
  const left = Math.max(0, Math.ceil(session.expires_at - now / 1000));
  const minutes = Math.floor(left / 60);
  const seconds = left % 60;
  return (
    <span className="session-badge session-badge--visitor" title="临时会话剩余时间">
      <i/> 临时体验 · {minutes}:{String(seconds).padStart(2, '0')}
    </span>
  );
}

function AdminInviteDrawer({ open, onClose }) {
  const { useEffect, useRef, useState } = React;
  const [invite, setInvite] = useState(null);
  const [inviteUrl, setInviteUrl] = useState('');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const qrRef = useRef(null);

  const load = async () => {
    try { setInvite(await apiClient.request('/api/admin/invite')); }
    catch (error) { setMessage(apiErrorText(error)); }
  };
  useEffect(() => { if (open) load(); }, [open]);
  useEffect(() => {
    if (!open || !inviteUrl || !qrRef.current) return;
    qrRef.current.innerHTML = '';
    if (window.QRCode) new QRCode(qrRef.current, { text: inviteUrl, width: 176, height: 176,
      colorDark: '#0c0c15', colorLight: '#ffffff', correctLevel: QRCode.CorrectLevel.M });
  }, [open, inviteUrl]);

  const rotate = async () => {
    setBusy(true); setMessage('');
    try {
      const data = await apiClient.request('/api/admin/invite/rotate', { method: 'POST', body: {} });
      const token = data.token || data.invite_token || '';
      const url = data.url || data.invite_url || (token
        ? `${window.location.origin}${window.location.pathname}#invite=${encodeURIComponent(token)}` : '');
      setInvite(data); setInviteUrl(url);
      setMessage('新邀请已启用，旧二维码已失效');
    } catch (error) { setMessage(apiErrorText(error)); }
    finally { setBusy(false); }
  };
  const disable = async () => {
    setBusy(true); setMessage('');
    try {
      const data = await apiClient.request('/api/admin/invite/disable', { method: 'POST', body: {} });
      setInvite(data); setInviteUrl(''); setMessage('访客邀请已停用');
    } catch (error) { setMessage(apiErrorText(error)); }
    finally { setBusy(false); }
  };
  const copy = async () => {
    try { await navigator.clipboard.writeText(inviteUrl); setMessage('邀请链接已复制'); }
    catch { setMessage('无法自动复制，请长按下方链接'); }
  };

  return (
    <>
      <div className={`drawer-mask ${open ? 'is-open' : ''}`} onClick={onClose}/>
      <aside className={`invite-drawer ${open ? 'is-open' : ''}`} aria-hidden={!open}>
        <div className="invite-drawer__handle"/>
        <header><div><small>ACCESS PASS</small><h2>访客邀请</h2></div>
          <button className="btn" onClick={onClose} aria-label="关闭"><Icon name="x" size={14}/></button></header>
        <div className="invite-drawer__body">
          <div className={`invite-state ${invite?.enabled ? 'is-enabled' : ''}`}>
            <i/><span>{invite?.enabled ? '邀请已启用' : '邀请未启用'}</span>
            {invite?.version !== undefined && <code>V{invite.version}</code>}
          </div>
          {inviteUrl ? (
            <div className="invite-pass">
              <div className="invite-pass__qr" ref={qrRef}/>
              <p>体验者扫码后会获得临时会话。刷新邀请会立即使这张码失效。</p>
              <button className="btn" onClick={copy}><Icon name="link" size={13}/> 复制邀请链接</button>
              <code>{inviteUrl}</code>
            </div>
          ) : (
            <div className="invite-empty"><Icon name="scan" size={26}/><p>生成后二维码只在当前页面显示，请及时分享。</p></div>
          )}
          {message && <div className="invite-message" role="status">{message}</div>}
          <div className="invite-drawer__actions">
            <button className="btn btn--accent" onClick={rotate} disabled={busy}>
              {invite?.enabled ? '刷新邀请' : '生成邀请'}
            </button>
            <button className="btn btn--danger" onClick={disable} disabled={busy || !invite?.enabled}>停用邀请</button>
          </div>
        </div>
      </aside>
    </>
  );
}

window.SessionGate = SessionGate;
window.SessionBadge = SessionBadge;
window.AdminInviteDrawer = AdminInviteDrawer;
