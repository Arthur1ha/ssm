const { useState: useStateR, useEffect: useEffectR } = React;

function RulesScreen({ embedded = false, onRulesChange, open }) {
  const [teachings, setTeachings] = useStateR([]);
  const [editing, setEditing] = useStateR(null);
  const [error, setError] = useStateR('');
  const [pendingDelete, setPendingDelete] = useStateR(null);
  const [cards, setCards] = useStateR([]);
  const [bindingEditor, setBindingEditor] = useStateR(null);
  const load = async () => {
    try {
      const [items, devices] = await Promise.all([
        apiClient.request('/api/teachings'), apiClient.request('/api/devices')]);
      setTeachings(items);
      setCards(devices);
      onRulesChange?.(items);
      setError('');
    } catch (err) { setError(err.message || '读取教导失败'); }
  };
  useEffectR(() => { load(); }, []);
  useEffectR(() => { if (open) load(); }, [open]);
  const remove = async (item) => {
    if (pendingDelete !== item.teaching_id) { setPendingDelete(item.teaching_id); return; }
    try {
      await apiClient.request(`/api/teachings/${encodeURIComponent(item.teaching_id)}`, { method: 'DELETE' });
      setPendingDelete(null);
      await load();
    } catch (err) { setError(err.message || '撤销失败'); }
  };
  const save = async () => {
    try {
      await apiClient.request(`/api/teachings/${encodeURIComponent(editing.teaching_id)}`, {
        method: 'PATCH', body: { name: editing.name, trigger: editing.trigger,
          behavior: editing.behavior },
      });
      setEditing(null);
      await load();
    } catch (err) { setError(err.message || '修改失败'); }
  };
  const saveBinding = async () => {
    try {
      await apiClient.request(`/api/teachings/${encodeURIComponent(bindingEditor.teaching_id)}/binding`, {
        method: 'PUT', body: { observation_id: bindingEditor.observation_id,
          where: JSON.parse(bindingEditor.where), skill_id: bindingEditor.skill_id,
          params: JSON.parse(bindingEditor.params), enabled: bindingEditor.enabled },
      });
      setBindingEditor(null);
      await load();
    } catch (err) { setError(err.message || '绑定失败'); }
  };
  const groups = Object.groupBy ? Object.groupBy(teachings, item => item.unit_id) :
    teachings.reduce((all, item) => { (all[item.unit_id] ||= []).push(item); return all; }, {});
  return (
    <div style={{ height: '100%', overflowY: 'auto', padding: '18px 16px 80px', color: 'var(--color-text)' }}>
      {!embedded && <h2>教导记忆</h2>}
      <p style={{ fontSize: 12, color: 'var(--color-text-dim)' }}>教导保存后需绑定已声明的观察与技能，启用后才参与智能体自主判断。</p>
      {error && <p role="alert" style={{ color: 'var(--color-danger)' }}>{error}</p>}
      {!teachings.length && <p style={{ color: 'var(--color-text-dim)', padding: '32px 0' }}>还没有教导。可以在对话中告诉管家想教哪个智能体、何时做什么。</p>}
      {Object.entries(groups).map(([unitId, items]) => (
        <section key={unitId} style={{ marginTop: 24 }}>
          <h3 style={{ fontSize: 14 }}>{unitId}</h3>
          {items.map(item => <article key={item.teaching_id} style={{ padding: 14, margin: '10px 0',
            border: '1px solid var(--color-border)', borderRadius: 'var(--radius-card)',
            background: 'var(--color-surface-2)' }}>
            {editing?.teaching_id === item.teaching_id ? <>
              {['name', 'trigger', 'behavior'].map(key =>
                <label key={key} style={{ display: 'block', fontSize: 12, marginBottom: 8 }}>{({name:'名称',trigger:'何时',behavior:'做什么',})[key]}
                  <input value={editing[key]} maxLength={key === 'name' ? 120 : 500}
                    onChange={e => setEditing({ ...editing, [key]: e.target.value })}
                    style={{ display:'block', width:'100%', boxSizing:'border-box', padding:8,
                      color:'var(--color-text)', background:'var(--color-surface-1)', border:'1px solid var(--color-border)' }}/>
                </label>)}
              <button onClick={save}>保存修改</button> <button onClick={() => setEditing(null)}>取消</button>
            </> : <>
              <strong>{item.name}</strong>
              <div style={{ marginTop: 6, fontSize: 13 }}>当 {item.trigger} → {item.behavior}</div>
              <div style={{ marginTop: 6, fontSize: 11, color:'var(--color-text-dim)' }}>{item.status === 'needs_review' ? '待检查' : '已记住'} · {({unbound:'尚未绑定',ready:'已绑定，未启用',enabled:'已启用',capability_unavailable:'能力不足，已暂停'})[item.execution_status || 'unbound']} · 修订 {item.revision}</div>
              {bindingEditor?.teaching_id === item.teaching_id && (() => {
                const card = cards.find(entry => entry.unit_id === item.unit_id) || {};
                return <div style={{ marginTop: 10 }}>
                  <label>观察能力 <select value={bindingEditor.observation_id} onChange={e => setBindingEditor({...bindingEditor, observation_id:e.target.value})}>
                    <option value="">请选择</option>{(card.observations || []).map(obs => <option key={obs.id} value={obs.id}>{obs.id}</option>)}
                  </select></label>{' '}
                  <label>自主技能 <select value={bindingEditor.skill_id} onChange={e => setBindingEditor({...bindingEditor, skill_id:e.target.value})}>
                    <option value="">请选择</option>{(card.autonomous_skills || []).map(id => <option key={id} value={id}>{id}</option>)}
                  </select></label>
                  <label style={{display:'block'}}>观察条件（JSON）<input value={bindingEditor.where} onChange={e => setBindingEditor({...bindingEditor, where:e.target.value})} /></label>
                  <label style={{display:'block'}}>技能参数（JSON）<input value={bindingEditor.params} onChange={e => setBindingEditor({...bindingEditor, params:e.target.value})} /></label>
                  <label><input type="checkbox" checked={bindingEditor.enabled} onChange={e => setBindingEditor({...bindingEditor, enabled:e.target.checked})} />启用</label>{' '}
                  <button onClick={saveBinding}>保存绑定</button>{' '}<button onClick={() => setBindingEditor(null)}>取消</button>
                </div>;
              })()}
              <div style={{ marginTop: 10 }}><button onClick={() => setEditing({ ...item })}>修改</button>{' '}
                <button onClick={() => setBindingEditor({teaching_id:item.teaching_id,
                  observation_id:item.binding?.observation_id || '', where:JSON.stringify(item.binding?.where || {}),
                  skill_id:item.binding?.skill_id || '', params:JSON.stringify(item.binding?.params || {}),
                  enabled:item.execution_status === 'enabled'})}>执行绑定</button>{' '}
                <button onClick={() => remove(item)}>{pendingDelete === item.teaching_id ? '确认撤销？' : '撤销'}</button></div>
            </>}
          </article>)}
        </section>
      ))}
    </div>
  );
}
window.RulesScreen = RulesScreen;
