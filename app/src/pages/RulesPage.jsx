const { useState: useStateR, useEffect: useEffectR } = React;

const RULE_ACTION_NAMES = {
  Hello: '挥手打招呼', StandUp: '起立', StandDown: '趴下', StopMove: '停止移动',
  Stretch: '伸展', Dance1: '跳第一支舞', Dance2: '跳第二支舞',
  BRIGHT: '调亮', DIM: '调暗', OFF: '关灯',
};
const RULE_OBSERVATION_NAMES = {
  'vision.person_presence': '画面中有人', 'vision.face_presence': '画面中有人脸',
  'vision.owner_recognized': '识别到主人',
  'sensor.sound_detected': '检测到声音', 'sensor.light_changed': '光线发生变化',
};
const RULE_LIGHT_LEVELS = { DARK: '很暗', DIM: '较暗', NORMAL: '正常', BRIGHT: '明亮' };

function ruleObservationCopy(id, where) {
  const values = where || {};
  const base = RULE_OBSERVATION_NAMES[id] || (id ? '设备条件已变化' : '未选择触发条件');
  if (id === 'vision.owner_recognized' && values.recognized === true) return '识别到已登记的主人';
  if (values.detected === false) return ({
    'vision.person_presence': '画面中没有人', 'vision.face_presence': '画面中没有人脸',
    'sensor.sound_detected': '没有检测到声音',
  })[id] || `${base}：未检测到`;
  if (values.detected === true) return base;
  if (values.level) return `环境光线${RULE_LIGHT_LEVELS[values.level] || `为 ${values.level}`}`;
  return Object.keys(values).length ? base : `${base}（未填写具体条件）`;
}

function ruleActionCopy(skillId, params, card) {
  if (!skillId) return '未选择动作';
  const values = params || {};
  if (skillId === 'go2_sport') return RULE_ACTION_NAMES[values.cmd] || '请选择动作';
  if (skillId === 'set_light_state') return RULE_ACTION_NAMES[values.state] || '请选择灯光状态';
  if (skillId === 'blink_light') return values.count ? `让灯闪烁 ${values.count} 次` : '让灯闪烁';
  if (skillId === 'set_light_color') return `设置灯光颜色${['r', 'g', 'b'].every(key => values[key] !== undefined) ? `（红 ${values.r}、绿 ${values.g}、蓝 ${values.b}）` : ''}`;
  return card?.skills?.find(skill => skill.id === skillId)?.name || '设备动作已变化';
}

function ruleInitialValue(field) {
  if (field.type === 'boolean') return true;
  return '';
}

function ruleValue(field, value) {
  if (field.type === 'integer' || field.type === 'number') return value === '' ? '' : Number(value);
  if (field.type === 'boolean') return value === 'true';
  return value;
}

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
      const card = cards.find(entry => entry.unit_id === bindingEditor.teaching_unit_id) || {};
      const observation = card.observations?.find(entry => entry.id === bindingEditor.observation_id);
      const skill = card.skills?.find(entry => entry.id === bindingEditor.skill_id);
      const where = Object.fromEntries(Object.entries(bindingEditor.where).filter(([, value]) => value !== ''));
      const params = Object.fromEntries(Object.entries(bindingEditor.params).filter(([, value]) => value !== ''));
      if (!observation || !skill || !Object.keys(where).length) {
        setError('请选择设备能识别的触发条件和动作。'); return;
      }
      if (observation.available === false) {
        setError('该触发条件当前不可用，请检查设备能力。'); return;
      }
      if ((skill.params_schema?.required || []).some(key => params[key] === undefined)) {
        setError('请填写动作所需的全部参数。'); return;
      }
      await apiClient.request(`/api/teachings/${encodeURIComponent(bindingEditor.teaching_id)}/binding`, {
        method: 'PUT', body: { observation_id: bindingEditor.observation_id,
          where, skill_id: bindingEditor.skill_id, params, enabled: bindingEditor.enabled },
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
    enabled: '已启用', capability_unavailable: '能力已变化' })[item.execution_status || 'unbound'] || '等待设置';
  return (
    <div className="rules-screen">
      {!embedded && <h2 className="rules-screen__title">教导与自动规则</h2>}
      <p className="rules-screen__intro">这里保存的是你教给设备的期望。设置可识别的条件和动作并启用后，设备在“自主反应”模式下才会把它作为行动候选，仍会检查当前状态和安全条件。</p>
      {error && <p className="rules-screen__error" role="alert">{error}</p>}
      {!teachings.length && <div className="rules-empty"><span aria-hidden="true">当 → 就</span>
        <strong>还没有教导</strong><p>在对话里告诉管家：哪个设备，在什么时候，做什么。</p></div>}
      {Object.entries(groups).map(([unitId, items]) => (
        <section key={unitId} className="rules-group">
          <div className="rules-group__heading"><h3>{deviceName(unitId)}</h3><span>{items.length} 条教导</span></div>
          {items.map(item => {
            const card = cards.find(entry => entry.unit_id === item.unit_id) || {};
            return <article key={item.teaching_id} className={`rule-card${item.execution_status === 'enabled' ? ' rule-card--enabled' : ''}`}>
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
              <div className="rule-card__top"><span className="rule-card__eyebrow">已记住的教导</span>
                <span className={`rule-card__status rule-card__status--${item.execution_status || 'unbound'}`}>{statusText(item)}</span></div>
              <h3 className="rule-card__title">{item.name}</h3>
              <div className="teaching-card__sequence">
                <div className="teaching-card__line"><span>当</span><p>{item.trigger}</p></div>
                <div className="teaching-card__line"><span>就</span><p>{item.behavior}</p></div>
              </div>
              {item.binding && <div className="rule-card__mapping">
                <span className="rule-card__mapping-label">实际匹配</span>
                <strong>{ruleObservationCopy(item.binding.observation_id, item.binding.where)} <span aria-hidden="true">→</span> {ruleActionCopy(item.binding.skill_id, item.binding.params, card)}</strong>
                {['vision.person_presence', 'vision.face_presence'].includes(item.binding.observation_id) && <small>此条件只判断是否有人或人脸，不能据此确认是不是主人。</small>}
              </div>}
              {item.execution_status !== 'enabled' && <p className="rule-card__hint">{item.execution_status === 'capability_unavailable'
                ? '设备能力发生变化，请重新设置触发条件。' : item.execution_status === 'ready'
                  ? '映射已保存，启用后才会参与设备的自主决策。' : '这条教导已保存，还不会触发设备动作。'}</p>}
              {item.execution_status === 'enabled' && <p className="rule-card__hint">需开启设备的“自主反应”模式。条件命中后由设备判断；安全限制或用户操作可能使它不执行。</p>}
              {bindingEditor?.teaching_id === item.teaching_id && (() => {
                const observation = card.observations?.find(entry => entry.id === bindingEditor.observation_id);
                const skill = card.skills?.find(entry => entry.id === bindingEditor.skill_id);
                const fields = observation?.fields || {};
                const properties = skill?.params_schema?.properties || {};
                return <div className="rule-binding">
                  <strong className="rule-binding__heading">设置设备实际能识别的条件与动作</strong>
                  <p className="rule-binding__intro">这里的条件和动作决定何时把教导交给设备判断。上面的教导文字不会直接变成执行指令。</p>
                  <label className="rule-field">设备感知到什么 <select value={bindingEditor.observation_id} onChange={e => {
                    const selected = card.observations?.find(obs => obs.id === e.target.value);
                    setBindingEditor({...bindingEditor, observation_id:e.target.value,
                      where:Object.fromEntries(Object.entries(selected?.fields || {}).map(([key, type]) => [key, ruleInitialValue({type})]))});
                  }}>
                    <option value="">请选择</option>{(card.observations || []).map(obs => <option key={obs.id} value={obs.id}
                      disabled={obs.available === false}>
                      {RULE_OBSERVATION_NAMES[obs.id] || obs.name || obs.id}{obs.available === false ? ' · 暂不可用' : ''}
                    </option>)}
                  </select></label>
                  {Object.entries(fields).map(([key, type]) => <label key={key} className="rule-field">
                    {key === 'detected' ? '检测结果' : key === 'recognized' ? '识别结果' : key === 'level' ? '光线级别' : key}
                    {key === 'level' ? <select value={bindingEditor.where[key] ?? ''} onChange={e => setBindingEditor({...bindingEditor,
                      where:{...bindingEditor.where, [key]:e.target.value}})}>
                      <option value="">请选择</option>{Object.entries(RULE_LIGHT_LEVELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                    </select> : type === 'boolean' ? <select value={String(bindingEditor.where[key] ?? true)} onChange={e => setBindingEditor({...bindingEditor,
                      where:{...bindingEditor.where, [key]:ruleValue({type}, e.target.value)}})}>
                      <option value="true">{key === 'recognized' ? '确认是主人' : '检测到'}</option>
                      {key !== 'recognized' && <option value="false">未检测到</option>}
                    </select> : <input value={bindingEditor.where[key] ?? ''} onChange={e => setBindingEditor({...bindingEditor,
                      where:{...bindingEditor.where, [key]:ruleValue({type}, e.target.value)}})} placeholder="填写设备报告的值" />}
                  </label>)}
                  {['vision.person_presence', 'vision.face_presence'].includes(bindingEditor.observation_id) && <p className="rule-binding__caution">此条件只能判断有没有人或人脸，不能据此识别“我”或“主人”。</p>}
                  <label className="rule-field">设备做什么 <select value={bindingEditor.skill_id} onChange={e => {
                    const selected = card.skills?.find(entry => entry.id === e.target.value);
                    setBindingEditor({...bindingEditor, skill_id:e.target.value,
                      params:Object.fromEntries(Object.entries(selected?.params_schema?.properties || {}).map(([key, field]) => [key, ruleInitialValue(field)]))});
                  }}>
                    <option value="">请选择</option>{(card.skills || []).map(entry => <option key={entry.id} value={entry.id}
                      disabled={!(card.autonomous_skills || []).includes(entry.id)}>
                      {entry.name || entry.id}{(card.autonomous_skills || []).includes(entry.id) ? '' : ' · 仅手动调用'}
                    </option>)}
                  </select></label>
                  {Object.entries(properties).map(([key, field]) => <label key={key} className="rule-field">
                    {key === 'cmd' ? '具体动作' : key === 'state' ? '灯光状态' : field.description || ({count:'闪烁次数',r:'红色',g:'绿色',b:'蓝色',brightness:'亮度'})[key] || key}
                    {field.enum ? <select value={bindingEditor.params[key] ?? ''} onChange={e => setBindingEditor({...bindingEditor,
                      params:{...bindingEditor.params, [key]:ruleValue(field, e.target.value)}})}>
                      <option value="">请选择</option>{field.enum.map(value => <option key={value} value={value}>{RULE_ACTION_NAMES[value] || value}</option>)}
                    </select> : <input type={['integer','number'].includes(field.type) ? 'number' : 'text'}
                      min={field.minimum} max={field.maximum} value={bindingEditor.params[key] ?? ''}
                      onChange={e => setBindingEditor({...bindingEditor,
                        params:{...bindingEditor.params, [key]:ruleValue(field, e.target.value)}})} />}
                  </label>)}
                  <div className="rule-binding__preview"><span>实际映射</span><strong>{ruleObservationCopy(bindingEditor.observation_id, bindingEditor.where)} <span aria-hidden="true">→</span> {ruleActionCopy(bindingEditor.skill_id, bindingEditor.params, card)}</strong></div>
                  <details className="rule-binding__capabilities"><summary>查看设备声明的全部能力</summary>
                    <div className="rule-binding__capability-section"><strong>可观察的条件</strong>
                      {(card.observations || []).map(obs => <p key={obs.id}>{RULE_OBSERVATION_NAMES[obs.id] || obs.name || obs.id}<small>{obs.available === false ? '当前不可用 · ' : ''}{obs.description || '可用于设置触发条件'}</small></p>)}
                    </div>
                    <div className="rule-binding__capability-section"><strong>公开技能</strong>
                      {(card.skills || []).map(entry => <p key={entry.id}>{entry.name || entry.id}
                        <small>{(card.autonomous_skills || []).includes(entry.id) ? '可用于自主规则' : '仅手动或对话调用'} · {entry.description}</small></p>)}
                    </div>
                  </details>
                  <label className="rule-binding__toggle"><input type="checkbox" checked={bindingEditor.enabled} onChange={e => setBindingEditor({...bindingEditor, enabled:e.target.checked})} />启用后参与自主决策</label>
                  <div className="rule-card__actions"><button className="teaching-button teaching-button--primary" onClick={saveBinding}>保存设置</button>
                    <button className="teaching-button teaching-button--quiet" onClick={() => setBindingEditor(null)}>取消</button></div>
                </div>;
              })()}
              <div className="rule-card__actions"><button className="rule-action" onClick={() => setEditing({ ...item })}>修改</button>
                <button className="rule-action" onClick={() => setBindingEditor({teaching_id:item.teaching_id,
                  teaching_unit_id:item.unit_id, observation_id:item.binding?.observation_id || '', where:{...(item.binding?.where || {})},
                  skill_id:item.binding?.skill_id || '', params:{...(item.binding?.params || {})},
                  enabled:item.execution_status === 'enabled'})}>设置触发与动作</button>
                <button className={`rule-action rule-action--danger${pendingDelete === item.teaching_id ? ' is-confirming' : ''}`}
                  onClick={() => remove(item)}>{pendingDelete === item.teaching_id ? '确认撤销' : '撤销'}</button></div>
            </>}
          </article>;
          })}
        </section>
      ))}
    </div>
  );
}
window.RulesScreen = RulesScreen;
window.ruleObservationCopy = ruleObservationCopy;
window.ruleActionCopy = ruleActionCopy;
