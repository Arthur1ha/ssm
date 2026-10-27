/* SpacePeopleDrawer — shared people library for the current space. */
async function encodePersonPhoto(file) {
  // 当前 nginx 未配置上传体积，使用小于默认 1 MiB 的请求体，原图大小不设门槛。
  const uploadBase64Budget = 900_000;
  const url = URL.createObjectURL(file);
  const image = new Image();
  try {
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('照片读取超时，请重试')), 30000);
      image.onload = () => { clearTimeout(timer); resolve(); };
      image.onerror = () => { clearTimeout(timer); reject(new Error('照片无法读取，请换一张照片')); };
      image.src = url;
    });
    const scale = Math.min(1, 4000 / Math.max(image.naturalWidth, image.naturalHeight));
    const canvas = document.createElement('canvas');
    canvas.width = Math.max(1, Math.round(image.naturalWidth * scale));
    canvas.height = Math.max(1, Math.round(image.naturalHeight * scale));
    canvas.getContext('2d').drawImage(image, 0, 0, canvas.width, canvas.height);
    let blob;
    for (;;) {
      blob = await new Promise((resolve, reject) => canvas.toBlob(
        value => value ? resolve(value) : reject(new Error('照片处理失败，请重试')), 'image/jpeg', 0.85));
      if (Math.ceil(blob.size / 3) * 4 <= uploadBase64Budget) break;
      if (Math.min(canvas.width, canvas.height) <= 64) throw new Error('照片无法处理，请换一张照片');
      const width = Math.max(64, Math.floor(canvas.width * 0.8));
      const height = Math.max(64, Math.floor(canvas.height * 0.8));
      const resized = document.createElement('canvas');
      resized.width = width; resized.height = height;
      resized.getContext('2d').drawImage(canvas, 0, 0, width, height);
      canvas.width = width; canvas.height = height;
      canvas.getContext('2d').drawImage(resized, 0, 0);
    }
    return await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result).split(',')[1]);
      reader.onerror = () => reject(new Error('照片读取失败'));
      reader.readAsDataURL(blob);
    });
  } finally { URL.revokeObjectURL(url); }
}

async function peopleRequest(path, options, timeoutMs = 30000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try { return await apiClient.request(path, { ...options, signal: controller.signal }); }
  catch (error) {
    if (controller.signal.aborted) throw new Error('请求超时，请刷新人员库确认结果后重试');
    if (error?.status === 502) throw new Error('照片暂时无法处理，请稍后重试');
    throw error;
  } finally { clearTimeout(timer); }
}

function SpacePeopleDrawer({ open, onClose }) {
  const { useCallback, useEffect, useState } = React;
  const [people, setPeople] = useState([]);
  const [name, setName] = useState('');
  const [role, setRole] = useState('owner');
  const [photo, setPhoto] = useState(null);
  const [busy, setBusy] = useState(false);
  const [phase, setPhase] = useState('');
  const [message, setMessage] = useState(null);

  const loadPeople = useCallback(async ({ preserveMessage = false } = {}) => {
    if (!preserveMessage) setMessage(null);
    try { setPeople(await peopleRequest('/api/people')); }
    catch (error) { setMessage({ key: 'refresh', tone: 'error', text: apiErrorText(error, '人员库加载失败') }); }
  }, []);

  useEffect(() => { if (open) loadPeople(); }, [open, loadPeople]);
  useEffect(() => {
    if (!open) return undefined;
    const closeOnEscape = event => { if (event.key === 'Escape') onClose(); };
    window.addEventListener('keydown', closeOnEscape);
    return () => window.removeEventListener('keydown', closeOnEscape);
  }, [open, onClose]);

  const createPerson = async event => {
    event.preventDefault();
    const form = event.currentTarget;
    const cleanName = name.trim();
    if (!cleanName || !photo || busy) return;
    setBusy(true); setPhase('处理照片中…'); setMessage(null);
    try {
      const image = await encodePersonPhoto(photo);
      setPhase('上传中…');
      await peopleRequest('/api/people', { method: 'POST', body: { name: cleanName, role, image } }, 60000);
      setName(''); setPhoto(null); form.reset();
      setBusy(false);
      setMessage({ key: 'create', tone: 'ok', text: `已添加「${cleanName}」` });
      await loadPeople({ preserveMessage: true });
    } catch (error) {
      setMessage({ key: 'create', tone: 'error', text: apiErrorText(error, '人员添加失败') });
    } finally { setBusy(false); setPhase(''); }
  };

  const deletePerson = async person => {
    if (busy || !window.confirm(`从空间人员库删除「${person.name}」？`)) return;
    setBusy(true); setMessage(null);
    try {
      await apiClient.request(`/api/people/${encodeURIComponent(person.person_id)}`, { method: 'DELETE' });
      await loadPeople();
      setMessage({ key: 'delete', tone: 'ok', text: `已删除「${person.name}」` });
    } catch (error) {
      setMessage({ key: 'delete', tone: 'error', text: apiErrorText(error, '人员删除失败') });
    } finally { setBusy(false); }
  };

  const roleLabel = value => ({ owner: '主人', family: '家人', friend: '朋友', stranger: '陌生人', known: '熟人' }[value] || value || '未知');
  const feedback = key => message?.key === key && <small className={`action-feedback action-feedback--${message.tone}`}
    role="status" title={message.text}>{message.text}</small>;

  return <>
    <div className={`drawer-mask ${open ? 'is-open' : ''}`} onClick={onClose}/>
    <aside className={`space-drawer ${open ? 'is-open' : ''}`} aria-hidden={!open}
      role="dialog" aria-modal="true" aria-labelledby="space-people-title">
      <div className="space-drawer__handle"/>
      <header className="space-drawer__header">
        <div><small>SPACE PEOPLE</small><h2 id="space-people-title">空间人员库</h2></div>
        <div className="space-drawer__header-actions">
          <div className="feedback-button"><button className="btn" onClick={loadPeople} disabled={busy}>刷新</button>{feedback('refresh')}</div>
          <button className="btn space-drawer__close" onClick={onClose} aria-label="关闭人员库"><Icon name="x" size={16}/></button>
        </div>
      </header>
      <div className="space-drawer__body">
        <section className="asset-section">
          <div className="asset-section__heading"><div><small>KNOWN PEOPLE</small><h3>已登记人员</h3></div><span>{people.length} 人</span></div>
          <div className="people-list">
            {people.length ? people.map(person => <article className="person-row" key={person.person_id}>
              <div className="person-row__avatar">{(person.name || '?').slice(0, 1)}</div>
              <div><strong>{person.name}</strong><small>{roleLabel(person.role)} · {person.face_count || 0} 张照片</small></div>
              <button className="asset-icon-action" title={`删除${person.name}`} disabled={busy}
                onClick={() => deletePerson(person)}><Icon name="x" size={12}/></button>
            </article>) : <p className="asset-empty">人员库为空。上传一张照片添加人员。</p>}
          </div>
          {feedback('delete')}
          <form className="person-enrol" onSubmit={createPerson}>
            <input value={name} onChange={event => setName(event.target.value)} maxLength="48" placeholder="人员名称" required/>
            <select value={role} onChange={event => setRole(event.target.value)} aria-label="人员关系">
              <option value="owner">主人</option><option value="family">家人</option>
              <option value="friend">朋友</option><option value="stranger">陌生人</option>
            </select>
            <label className="person-enrol__photo">{photo ? photo.name : '选择一张人员照片'}
              <input type="file" accept="image/jpeg,image/png,image/webp" required
                onChange={event => setPhoto(event.target.files?.[0] || null)}/>
            </label>
            <button className="btn btn--accent" disabled={busy || !name.trim() || !photo}>
      {busy ? phase : '上传并添加人员'}</button>
            {feedback('create')}
          </form>
        </section>
      </div>
    </aside>
  </>;
}

window.SpacePeopleDrawer = SpacePeopleDrawer;
