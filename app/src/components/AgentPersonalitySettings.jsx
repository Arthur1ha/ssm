/* One editor for the three agents. The API supplies each agent's live trait list. */
function AgentPersonalitySettings({ agentId, active = true }) {
  const { useCallback, useEffect, useState } = React;
  const [profile, setProfile] = useState(null);
  const [name, setName] = useState('');
  const [prompt, setPrompt] = useState('');
  const [traits, setTraits] = useState({});
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [feedback, setFeedback] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    setFeedback(null);
    try {
      const next = await apiClient.request(`/api/agents/${encodeURIComponent(agentId)}/personality`);
      setProfile(next);
      setAgentDisplayNames({ [agentId]: next.name });
      setName(next.name || '');
      setPrompt(next.prompt || '');
      setTraits({ ...next.traits });
    } catch (error) {
      setFeedback({ tone: 'error', text: apiErrorText(error, '人格配置暂时无法读取') });
    } finally { setLoading(false); }
  }, [agentId]);

  useEffect(() => { if (active) load(); }, [active, load]);

  const save = async event => {
    event.preventDefault();
    if (!profile || saving || !name.trim() || !prompt.trim()) return;
    setSaving(true);
    setFeedback(null);
    try {
      const next = await apiClient.request(`/api/agents/${encodeURIComponent(agentId)}/personality`, {
        method: 'PUT', body: { name: name.trim(), prompt: prompt.trim(), traits, revision: profile.revision },
      });
      setProfile(next);
      setAgentDisplayNames({ [agentId]: next.name });
      setName(next.name || '');
      setPrompt(next.prompt || '');
      setTraits({ ...next.traits });
      setFeedback({ tone: 'ok', text: '名字、人格与特质已保存，下次决策起使用新名字' });
    } catch (error) {
      const reason = error?.status === 409
        ? '配置已在其他窗口更新；当前输入已保留，请复制内容后重新打开管理再保存'
        : apiErrorText(error, '保存失败，输入已保留，请重试');
      setFeedback({ tone: 'error', text: reason });
    } finally { setSaving(false); }
  };

  const specs = profile?.trait_specs || {};
  const changed = profile && (name !== profile.name || prompt !== profile.prompt || Object.keys(specs)
    .some(key => Number(traits[key]) !== Number(profile.traits?.[key])));

  return <div className="agent-personality">
    {loading && !profile && <p className="asset-empty" role="status">正在读取人格配置…</p>}
    {!profile && !loading && <button type="button" className="btn" onClick={load}>重试加载</button>}
    {profile && <form className="personality-editor" onSubmit={save}>
      <label className="agent-personality__prompt-label" htmlFor={`personality-name-${agentId}`}>
        名字 <small>保存后会使用这个名字，无需在人格文案中重复填写</small>
      </label>
      <input id={`personality-name-${agentId}`} type="text" value={name} maxLength="24"
        onChange={event => setName(event.target.value)} placeholder="给智能体起个名字"/>
      <label className="agent-personality__prompt-label" htmlFor={`personality-prompt-${agentId}`}>
        人格文案 <small>身份、说话方式，以及遇到不确定情况时的处理习惯</small>
      </label>
      <textarea id={`personality-prompt-${agentId}`} value={prompt}
        onChange={event => setPrompt(event.target.value)} rows="5" maxLength="4000"
        placeholder="描述这个智能体是谁、怎样说话、如何处理不确定情况…"/>
      <div className="agent-personality__traits">
        {Object.entries(specs).map(([key, spec]) => {
          const min = Number(spec.min ?? 0);
          const max = Number(spec.max ?? 100);
          const value = Number(traits[key] ?? spec.default ?? min);
          return <label className="agent-personality__trait" key={key}>
            <span className="agent-personality__trait-head"><strong>{spec.label || key}</strong><output>{value}</output></span>
            <span className="agent-personality__trait-detail">{spec.description}</span>
            <input type="range" min={min} max={max} value={value}
              onChange={event => setTraits(current => ({ ...current, [key]: Number(event.target.value) }))}/>
            <span className="agent-personality__trait-scale"><small>{min}</small><small>{max}</small></span>
            {spec.effect && <span className="agent-personality__trait-effect">影响：{spec.effect}</span>}
          </label>;
        })}
      </div>
      <div className="agent-personality__actions">
        <small>版本 {profile.revision} · {prompt.length} / 4000</small>
        <button type="submit" className="btn btn--accent" disabled={saving || loading || !changed || !name.trim() || !prompt.trim()}>
          {saving ? '保存中…' : '保存人格'}
        </button>
      </div>
    </form>}
    {feedback && <p className={`agent-personality__feedback action-feedback action-feedback--${feedback.tone}`}
      role={feedback.tone === 'error' ? 'alert' : 'status'}>{feedback.text}</p>}
  </div>;
}
window.AgentPersonalitySettings = AgentPersonalitySettings;
