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
      setFeedback({ tone: 'ok', text: '已保存' });
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
    {profile && <form className="personality-editor" {...window.ssmInputAttributes.form} onSubmit={save}>
      <label className="agent-personality__prompt-label" htmlFor={`personality-name-${agentId}`}>
        名字
      </label>
      <input id={`personality-name-${agentId}`} type="text" value={name} maxLength="24"
        {...window.ssmInputAttributes.text}
        onChange={event => setName(event.target.value)} placeholder="给智能体起个名字"/>
      <label className="agent-personality__prompt-label" htmlFor={`personality-prompt-${agentId}`}>
        人格
      </label>
      <textarea id={`personality-prompt-${agentId}`} value={prompt}
        {...window.ssmInputAttributes.multiline}
        onChange={event => setPrompt(event.target.value)} rows="5" maxLength="4000"
        placeholder="描述它的性格与说话方式"/>
      <div className="agent-personality__traits" role="group" aria-label="人格特质">
        {Object.entries(specs).map(([key, spec]) => {
          const min = Number(spec.min ?? 0);
          const max = Number(spec.max ?? 100);
          const value = Number(traits[key] ?? spec.default ?? min);
          return <label className="agent-personality__trait" key={key}>
            <span className="agent-personality__trait-head"><strong>{spec.label || key}</strong></span>
            <span className="agent-personality__trait-detail">{spec.description}</span>
            <input id={`personality-trait-${agentId}-${key}`} type="range" min={min} max={max} value={value}
              aria-label={spec.label || key}
              onChange={event => setTraits(current => ({ ...current, [key]: Number(event.target.value) }))}/>
            <span className="agent-personality__trait-scale"><small>{min}</small>
              <output htmlFor={`personality-trait-${agentId}-${key}`}>{value}</output><small>{max}</small></span>
          </label>;
        })}
      </div>
      <div className="agent-personality__actions">
        <button type="submit" className="btn btn--accent" disabled={saving || loading || !changed || !name.trim() || !prompt.trim()}>
          {saving ? '保存中…' : '保存'}
        </button>
      </div>
    </form>}
    {feedback && <p className={`agent-personality__feedback action-feedback action-feedback--${feedback.tone}`}
      role={feedback.tone === 'error' ? 'alert' : 'status'}>{feedback.text}</p>}
  </div>;
}
window.AgentPersonalitySettings = AgentPersonalitySettings;
