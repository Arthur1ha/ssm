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
  const deviceName = unitId => cards.find(card => card.unit_id === unitId)?.name
    || getAgentDisplayName(unitId);
  const statusText = item => ({ unbound: '等待设置', ready: '等待启用',
    enabled: '运行中', capability_unavailable: '能力已变化' })[item.execution_status || 'unbound'] || '等待设置';
  return (
    <div className="rules-screen">
      {!embedded && <h2 className="rules-screen__title">自动规则</h2>}
      <p className="rules-screen__intro">教导会先记住。设置触发条件并启用后，设备才能自行行动。</p>
      {error && <p className="rules-screen__error" role="alert">{error}</p>}
      {!teachings.length && <div className="rules-empty"><span aria-hidden="true">当 → 就</span>
        <strong>还没有教导</strong><p>在对话里告诉管家：哪个设备，在什么时候，做什么。</p></div>}
      {Object.entries(groups).map(([unitId, items]) => (
        <section key={unitId} className="rules-group">
          <div className="rules-group__heading"><h3>{deviceName(unitId)}</h3><span>{items.length} 条教导</span></div>
          {items.map(item => <article key={item.teaching_id} className={`rule-card${item.execution_status === 'enabled' ? ' rule-card--enabled' : ''}`}>
            {editing?.teaching_id === item.teaching_id ? <>
              <div className="rule-card__top"><span className="rule-card__eyebrow">修改教导</span></div>
              {['name', 'trigger', 'behavior'].map(key =>
                <label key={key} className="rule-field">{({name:'名称',trigger:'当 · 触发情形',behavior:'就 · 期望行为',})[key]}
                  <input value={editing[key]} maxLength={key === 'name' ? 120 : 500}
                    onChange={e => setEditing({ ...editing, [key]: e.target.value })}/>
                </label>)}
              <div className="rule-card__actions"><button className="teaching-button teaching-button--primary" onClick={save}>保存修改</button>
                <button className="teaching-button teaching-button--quiet" onClick={() => setEditing(null)}>取消</button></div>
            </> : <>
              <div className="rule-card__top"><span className="rule-card__eyebrow">{item.status === 'needs_review' ? '待检查的教导' : '已记住的教导'}</span>
                <span className={`rule-card__status rule-card__status--${item.execution_status || 'unbound'}`}>{statusText(item)}</span></div>
              <h3 className="rule-card__title">{item.name}</h3>
              <div className="teaching-card__sequence">
                <div className="teaching-card__line"><span>当</span><p>{item.trigger}</p></div>
                <div className="teaching-card__line"><span>就</span><p>{item.behavior}</p></div>
              </div>
              {item.execution_status !== 'enabled' && <p className="rule-card__hint">{item.execution_status === 'capability_unavailable'
                ? '设备能力发生变化，请重新设置触发条件。' : item.execution_status === 'ready'
                  ? '触发条件已设置，启用后才会自动执行。' : '这条教导已保存，还不会自动执行。'}</p>}
              {bindingEditor?.teaching_id === item.teaching_id && (() => {
                const card = cards.find(entry => entry.unit_id === item.unit_id) || {};
                return <div className="rule-binding">
                  <strong className="rule-binding__heading">设置自动执行</strong>
                  <label className="rule-field">观察能力 <select value={bindingEditor.observation_id} onChange={e => setBindingEditor({...bindingEditor, observation_id:e.target.value})}>
                    <option value="">请选择</option>{(card.observations || []).map(obs => <option key={obs.id} value={obs.id}>
                      {({ 'vision.person_presence': '看到人', 'vision.face_presence': '看到人脸' })[obs.id] || obs.name || obs.id}
                    </option>)}
                  </select></label>
                  <label className="rule-field">自主技能 <select value={bindingEditor.skill_id} onChange={e => setBindingEditor({...bindingEditor, skill_id:e.target.value})}>
                    <option value="">请选择</option>{(card.autonomous_skills || []).map(id => <option key={id} value={id}>
                      {card.skills?.find(skill => skill.id === id)?.name || id}
                    </option>)}
                  </select></label>
                  <label className="rule-field">观察条件（JSON）<input value={bindingEditor.where} onChange={e => setBindingEditor({...bindingEditor, where:e.target.value})} /></label>
                  <label className="rule-field">技能参数（JSON）<input value={bindingEditor.params} onChange={e => setBindingEditor({...bindingEditor, params:e.target.value})} /></label>
                  <label className="rule-binding__toggle"><input type="checkbox" checked={bindingEditor.enabled} onChange={e => setBindingEditor({...bindingEditor, enabled:e.target.checked})} />启用自动执行</label>
                  <div className="rule-card__actions"><button className="teaching-button teaching-button--primary" onClick={saveBinding}>保存设置</button>
                    <button className="teaching-button teaching-button--quiet" onClick={() => setBindingEditor(null)}>取消</button></div>
                </div>;
              })()}
              <div className="rule-card__actions"><button className="rule-action" onClick={() => setEditing({ ...item })}>修改</button>
                <button className="rule-action" onClick={() => setBindingEditor({teaching_id:item.teaching_id,
                  observation_id:item.binding?.observation_id || '', where:JSON.stringify(item.binding?.where || {}),
                  skill_id:item.binding?.skill_id || '', params:JSON.stringify(item.binding?.params || {}),
                  enabled:item.execution_status === 'enabled'})}>设置自动执行</button>
                <button className={`rule-action rule-action--danger${pendingDelete === item.teaching_id ? ' is-confirming' : ''}`}
                  onClick={() => remove(item)}>{pendingDelete === item.teaching_id ? '确认撤销' : '撤销'}</button></div>
            </>}
          </article>)}
        </section>
      ))}
    </div>
  );
}
window.RulesScreen = RulesScreen;
