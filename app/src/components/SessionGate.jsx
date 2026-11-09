function normalizeSession(data) {
  const raw = data?.session || data;
  const actor = raw?.actor || raw?.role;
  if (!raw || !['admin', 'visitor'].includes(actor)) return null;
  return {
    ...raw,
    actor,
    expires_at: Number(raw.expires_at || 0),
    space_name: raw.space_name || data?.space_name || '当前空间',
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
  const { useCallback, useEffect, useState, useRef } = React;
  const [session, setSession] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [sessionNotice, setSessionNotice] = useState('');
  const [bootError, setBootError] = useState('');
  const [bootAttempt, setBootAttempt] = useState(0);
  const inviteTokenRef = useRef(null);

  const applySession = useCallback(data => {
    const next = normalizeSession(data);
    if (!next) throw new Error('服务端没有返回有效会话');
    apiClient.setCsrfToken(data?.csrf_token || data?.session?.csrf_token || next.csrf_token || '');
    setSession(next);
    setError('');
    setSessionNotice('');
    apiClient.openEvents();
    return next;
  }, []);

  const loadMe = useCallback(async () => {
    const data = await apiClient.request('/api/me');
    return applySession(data);
  }, [applySession]);

  const leaveSession = useCallback(notice => {
    apiClient.closeEvents();
    apiClient.setCsrfToken('');
    history.replaceState(null, '', window.location.pathname + window.location.search);
    setSession(null);
    setError('');
    setSessionNotice(notice);
  }, []);

  useEffect(() => {
    let active = true;
    const boot = async () => {
      setLoading(true);
      setBootError('');
      // Consume and scrub the fragment before the first network round trip.
      // A query-string token is deliberately ignored.
      if (inviteTokenRef.current === null) inviteTokenRef.current = consumeInviteTokenFromUrl();
      const inviteToken = inviteTokenRef.current;
      try {
        const data = await apiClient.request('/api/me', { suppressUnauthorized: true });
        if (active) applySession(data);
      } catch (meError) {
        // A failed connection is not evidence that the cookie is invalid.
        if (meError.status !== 401) {
          if (active) setBootError('暂时连不上空间，请检查网络后重试');
          return;
        }
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
          if (!active) return;
          if (!exchangeError.status || exchangeError.status >= 500) {
            setBootError('暂时连不上空间，请稍后重试');
          } else {
            setError(exchangeError.status === 401 ? '邀请无效或已停用，请向空间主人获取新的分享链接。' : apiErrorText(exchangeError, '邀请无效或已过期'));
          }
        }
      } finally {
        if (active) setLoading(false);
      }
    };
    boot();
    const unauthorized = () => {
      leaveSession('会话已结束，请重新进入');
    };
    apiClient.addEventListener('unauthorized', unauthorized);
    return () => {
      active = false;
      apiClient.removeEventListener('unauthorized', unauthorized);
    };
  }, [applySession, bootAttempt, leaveSession]);

  useEffect(() => {
    if (!session?.expires_at) return;
    const delay = Math.max(0, session.expires_at * 1000 - Date.now());
    const timer = setTimeout(() => {
      leaveSession('会话已过期，请重新进入');
    }, delay);
    return () => clearTimeout(timer);
  }, [session?.expires_at, leaveSession]);

  const adminLogin = async () => {
    const data = await apiClient.request('/api/admin/session', {
      method: 'POST', suppressUnauthorized: true,
    });
    applySession(data);
    try { await loadMe(); } catch (_) {}
  };

  const visitorJoin = async inviteValue => {
    const token = extractInviteToken(inviteValue);
    if (!token) throw new Error('请粘贴访客邀请链接或二维码中的邀请码');
    const data = await apiClient.request('/api/visitor/session', {
      method: 'POST', body: { token }, suppressUnauthorized: true,
    });
    applySession(data);
  };

  const logout = async () => {
    try { await apiClient.request('/api/session', { method: 'DELETE' }); }
    finally {
      leaveSession('');
    }
  };

  if (loading) return <SessionLoading/>;
  if (bootError) return <SessionLoading error={bootError} onRetry={() => setBootAttempt(value => value + 1)}/>;
  if (!session) return <SessionEntry error={error} sessionNotice={sessionNotice} onAdminLogin={adminLogin} onVisitorJoin={visitorJoin}/>;
  return children({
    session, logout, refreshSession: loadMe,
  });
}

function extractInviteToken(value) {
  const raw = String(value || '').trim();
  if (!raw) return '';
  try {
    const url = new URL(raw, window.location.origin);
    const fragment = new URLSearchParams(url.hash.replace(/^#\??/, ''));
    return fragment.get('invite') || fragment.get('token') || (raw.includes('://') ? '' : raw);
  } catch (_) {
    return raw;
  }
}

function SessionLoading({ error, onRetry }) {
  return (
    <main className="session-entry" aria-busy={!error}>
      <div className="session-entry__mark">SSM</div>
      {!error && <div className="session-entry__pulse"/>}
      <p role={error ? 'alert' : 'status'}>{error || '正在确认通行权限…'}</p>
      {error && <button type="button" className="btn btn--accent" onClick={onRetry}>重新连接</button>}
    </main>
  );
}

function EntryNotice({ title, children, isError = false }) {
  return (
    <div className={`entry-notice${isError ? ' entry-notice--error' : ''}`} role={isError ? 'alert' : 'status'} aria-atomic="true">
      <span className="entry-notice__icon" aria-hidden="true">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="12" cy="12" r="9"/>
          {isError ? <path d="M12 7v6m0 4h.01"/> : <path d="M12 7v5l3 2"/>}
        </svg>
      </span>
      <div className="entry-notice__copy"><h2>{title}</h2><p>{children}</p></div>
    </div>
  );
}

function SessionEntry({ error, sessionNotice, onAdminLogin, onVisitorJoin }) {
  const [showJoin, setShowJoin] = React.useState(false);
  const [showOtherWays, setShowOtherWays] = React.useState(false);
  const [showPaste, setShowPaste] = React.useState(false);
  const [inviteValue, setInviteValue] = React.useState('');
  const [busy, setBusy] = React.useState(false);
  const [message, setMessage] = React.useState(error || '');
  const showResume = Boolean(sessionNotice) && !showOtherWays;

  React.useEffect(() => setMessage(error || ''), [error]);

  const enterSpace = async () => {
    if (busy) return;
    setBusy(true);
    setMessage('');
    try { await onAdminLogin(); }
    catch (loginError) { setMessage(apiErrorText(loginError, '暂时无法进入空间，请重试')); }
    finally { setBusy(false); }
  };

  const join = async event => {
    event.preventDefault();
    if (!inviteValue.trim() || busy) return;
    setBusy(true);
    setMessage('');
    try { await onVisitorJoin(inviteValue); }
    catch (joinError) { setMessage(joinError.status === 401 ? '邀请无效或已停用，请向空间主人获取新的分享链接。' : apiErrorText(joinError, '邀请无效或已过期')); }
    finally { setBusy(false); }
  };

  const joinFromScan = async value => {
    if (busy) return;
    const token = extractInviteToken(value);
    let validOrigin = true;
    if (value.includes('://')) {
      try { validOrigin = new URL(value).origin === window.location.origin; }
      catch (_) { validOrigin = false; }
    }
    if (!token || !validOrigin) {
      setMessage('这不是当前空间的分享二维码，请重新扫码');
      return;
    }
    setBusy(true);
    setMessage('');
    try { await onVisitorJoin(value); }
    catch (joinError) { setMessage(joinError.status === 401 ? '邀请无效或已停用，请向空间主人获取新的分享链接。' : apiErrorText(joinError, '邀请无效或已过期')); }
    finally { setBusy(false); }
  };

  const backToChoices = () => {
    setShowJoin(false);
    setShowPaste(false);
    setMessage('');
  };

  return (
    <main className="session-entry">
      <section className={`session-entry__panel${showResume && !showJoin ? ' session-entry__panel--resume' : ''}`}>
        <div className="session-entry__brand">
          <span>SSM</span>
        </div>

        {!showJoin && (message ? (
          <EntryNotice title="暂时无法进入" isError>{message}</EntryNotice>
        ) : showResume && (
          <p className="session-entry__notice" role="status">{sessionNotice}</p>
        ))}

        {showJoin ? (
          <section className="entry-subflow">
            <div className="entry-subflow__heading"><span className="entry-flow-mark entry-flow-mark--join"><Icon name="scan" size={16}/></span><h2>扫码加入</h2></div>
            <p>扫描空间分享二维码即可加入。</p>
            {message && <EntryNotice title="未能加入空间" isError>{message}</EntryNotice>}
            {!showPaste && <QrScanner onScan={joinFromScan} hint="将空间二维码放入框内，识别后自动加入"/>}
            <button type="button" className="entry-subflow__paste" onClick={() => setShowPaste(value => !value)}>
              {showPaste ? '收起链接输入' : '粘贴分享链接或邀请码'}
            </button>
            {showPaste && <form className="session-entry__form" onSubmit={join}>
              <label htmlFor="invite-link">空间分享链接或邀请码</label>
              <input id="invite-link" type="text" inputMode="url" autoComplete="off"
                value={inviteValue} onChange={event => setInviteValue(event.target.value)}
                placeholder="粘贴分享链接或邀请码"/>
              <button type="submit" className="btn btn--accent" disabled={busy || !inviteValue.trim()}>
                {busy ? '正在验证…' : '进入体验空间'}
              </button>
            </form>}
            <button type="button" className="btn entry-subflow__back" onClick={backToChoices}>返回</button>
          </section>
        ) : showResume ? (
          <div className="session-entry__resume">
            <button type="button" className="btn btn--accent" onClick={enterSpace} disabled={busy}>
              {busy ? '正在进入…' : '重新进入空间'}
            </button>
            <button type="button" className="session-entry__other" onClick={() => { setShowOtherWays(true); setMessage(''); }} disabled={busy}>
              其他进入方式
            </button>
          </div>
        ) : (
          <div className="session-entry__choices">
            <button className="entry-choice entry-choice--create" onClick={enterSpace} disabled={busy}>
              <span className="entry-choice__icon"><Icon name="settings" size={20}/></span>
              <span className="entry-choice__copy"><strong>{busy ? '正在进入…' : '创建空间'}</strong></span>
              <Icon name="arrow" size={17}/>
            </button>
            <button className="entry-choice entry-choice--join" onClick={() => { setShowJoin(true); setMessage(''); }}>
              <span className="entry-choice__icon"><Icon name="scan" size={20}/></span>
              <span className="entry-choice__copy"><strong>扫码加入</strong></span>
              <Icon name="arrow" size={17}/>
            </button>
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
    return <span className="session-badge session-badge--admin"><i/> 空间管理者</span>;
  }
  const left = Math.max(0, Math.ceil(session.expires_at - now / 1000));
  const minutes = Math.floor(left / 60);
  const seconds = left % 60;
  return (
    <span className="session-badge session-badge--visitor" title="空间参与会话剩余时间">
      <i/> 参与 · {minutes}:{String(seconds).padStart(2, '0')}
    </span>
  );
}

function AdminInviteDrawer({ open, onClose }) {
  const { useEffect, useRef, useState } = React;
  const [invite, setInvite] = useState(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [messageTarget, setMessageTarget] = useState('toggle');
  const qrRef = useRef(null);
  const inviteUrl = invite?.share_id
    ? `${window.location.origin}${window.location.pathname}#invite=${encodeURIComponent(invite.share_id)}` : '';

  const load = async () => {
    try { setInvite(await apiClient.request('/api/admin/invite')); }
    catch (error) { setMessageTarget('toggle'); setMessage(apiErrorText(error)); }
  };
  useEffect(() => { if (open) load(); }, [open]);
  useEffect(() => {
    if (!open || !inviteUrl || !qrRef.current) return;
    let active = true;
    qrRef.current.innerHTML = '';
    loadQrLibrary('generate').then(QRCode => {
      if (!active || !qrRef.current) return;
      new QRCode(qrRef.current, { text: inviteUrl, width: 176, height: 176,
        colorDark: '#0c0c15', colorLight: '#ffffff', correctLevel: QRCode.CorrectLevel.M });
    }).catch(() => {
      if (active) { setMessageTarget('copy'); setMessage('二维码加载失败，请重试或复制邀请链接'); }
    });
    return () => { active = false; };
  }, [open, inviteUrl]);

  const enable = async () => {
    setBusy(true); setMessage('');
    try {
      setInvite(await apiClient.request('/api/admin/invite/enable', { method: 'POST', body: {} }));
      setMessageTarget('toggle'); setMessage('空间分享已启用，二维码保持不变');
    } catch (error) { setMessageTarget('toggle'); setMessage(apiErrorText(error)); }
    finally { setBusy(false); }
  };
  const disable = async () => {
    setBusy(true); setMessage('');
    try {
      const data = await apiClient.request('/api/admin/invite/disable', { method: 'POST', body: {} });
      setInvite(data); setMessageTarget('toggle'); setMessage('空间分享已停用，访客会话已结束');
    } catch (error) { setMessageTarget('toggle'); setMessage(apiErrorText(error)); }
    finally { setBusy(false); }
  };
  const copy = async () => {
    setMessageTarget('copy');
    try { await navigator.clipboard.writeText(inviteUrl); setMessage('邀请链接已复制'); }
    catch { setMessage('无法自动复制，请长按下方链接'); }
  };

  return (
    <>
      <div className={`drawer-mask ${open ? 'is-open' : ''}`} onClick={onClose}/>
      <aside className={`management-dialog invite-drawer ${open ? 'is-open' : ''}`} aria-hidden={!open}
        inert={open ? undefined : ''} style={{ visibility: open ? 'visible' : 'hidden' }}
        role="dialog" aria-modal="true" aria-labelledby="invite-drawer-title">
        <header><h2 id="invite-drawer-title">分享空间</h2>
          <button className="icon-button" onClick={onClose} aria-label="关闭分享空间"><Icon name="x" size={16}/></button></header>
        <div className="invite-drawer__body">
          <div className={`invite-state ${invite?.enabled ? 'is-enabled' : ''}`}>
            <i/><span>{invite?.enabled ? '邀请已启用' : '邀请未启用'}</span>
            {invite?.version !== undefined && <code>V{invite.version}</code>}
          </div>
          {inviteUrl ? (
            <div className="invite-pass">
              <div className="invite-pass__qr" ref={qrRef}/>
              <p>{invite.enabled ? '扫码可加入当前空间。此二维码与空间绑定，保持不变。' : '分享已停用。重新启用后，此二维码仍可使用。'}</p>
              <button className="btn" onClick={copy}><Icon name="link" size={13}/> 复制邀请链接</button>
              {messageTarget === 'copy' && message && <small className="action-feedback" role="status" title={message}>{message}</small>}
              <code>{inviteUrl}</code>
            </div>
          ) : (
            <div className="invite-empty"><Icon name="scan" size={26}/><p>正在加载空间二维码…</p></div>
          )}
          <div className="invite-drawer__actions">
            <button className={`btn ${invite?.enabled ? 'btn--danger' : ''}`}
              onClick={invite?.enabled ? disable : enable} disabled={busy || !invite}>
              {invite?.enabled ? '停用分享' : '启用分享'}
            </button>
            {messageTarget === 'toggle' && message && <small className="action-feedback" role="status" title={message}>{message}</small>}
          </div>
        </div>
      </aside>
    </>
  );
}

window.SessionGate = SessionGate;
window.SessionBadge = SessionBadge;
window.AdminInviteDrawer = AdminInviteDrawer;
