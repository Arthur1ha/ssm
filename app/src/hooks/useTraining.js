/* Rules are interpreted by the existing agent Graph. This hook only projects
 * authenticated API history and submits explicit user decisions. */
// Navigation can remount a device page; only this page load owns its history.
const chatTranscripts = new Map();
function useSessionTranscript(sessionId, scope, initialRows = []) {
  const key = `${sessionId || ''}:${scope}`;
  const read = () => chatTranscripts.get(key) || initialRows;
  const [stored, setStored] = React.useState(() => ({key, rows:read()}));
  const rows = stored.key === key ? stored.rows : read();
  const setRows = React.useCallback(update => setStored(previous => {
    const base = previous.key === key ? previous.rows : read();
    const next = typeof update === 'function' ? update(base) : update;
    return {key, rows:next.slice(-300)};
  }), [key]);
  React.useEffect(() => {
    if (stored.key === key) chatTranscripts.set(key, stored.rows);
  }, [key, stored]);
  return [rows, setRows];
}
function trainingRuleText(record) {
  return record?.summaryText || record?.name || record?.source_text || '设备规则';
}
function trainingSteps(record) {
  const definition = record?.definition || record?.after?.definition || {};
  return (definition.steps || []).map((step, index) => {
    const summary = record?.stepSummaries?.find(item => item.step_index === index);
    return {...step, step_index:index, label:summary?.label || `第 ${index + 1} 步`,
      conditionText:summary?.conditionText || '', goalText:summary?.goalText || ''};
  });
}
function trainingTransitionIds(record) {
  const definition = record?.definition || record?.after?.definition || {};
  return [...new Set((definition.steps || []).flatMap(step => step.transition_ids || []))];
}
function trainingProposalOpen(proposal) {
  return !!proposal && !proposal.confirmed && !proposal.cancelled && !proposal.superseded
    && !proposal.superseded_by && !proposal.expired;
}
/* Attach each card to its originating request, including after reload. No rule
 * intent is inferred here: source text is used solely as a history anchor. */
function trainingTimeline(rows, proposals) {
  // Only the current authenticated proposal history owns rule cards. Clearing
  // training must not leave a stale, actionable card inside a cached transcript.
  const result = rows.filter(row => row.type !== 'rule');
  for (const proposal of [...proposals].sort((a,b) => a.created_at - b.created_at)) {
    const sourceText = proposal.source_text || proposal.after?.source_text;
    const id = `rule:${proposal.proposal_id}`;
    let anchor = proposal.source_run_id ? result.findIndex(row => row.type === 'user' && row.runId === proposal.source_run_id) : -1;
    if (anchor < 0 && sourceText) {
      for (let i = result.length - 1; i >= 0; i--) {
        if (result[i].type === 'user' && result[i].text === sourceText) { anchor = i; break; }
      }
    }
    if (anchor < 0 && sourceText) {
      result.push({id:`${id}:source`, type:'user', text:sourceText,
        requestId:id, runId:proposal.source_run_id});
      anchor = result.length - 1;
    }
    const owner = result[anchor];
    if (owner) {
      const request = owner.requestId;
      while (anchor + 1 < result.length && request != null
          && result[anchor + 1].requestId === request) anchor++;
    }
    const entry = {id, type:'rule', proposal, agent:proposal.unit_id,
      ...(owner ? {requestId:owner.requestId} : {})};
    result.splice(anchor < 0 ? result.length : anchor + 1, 0, entry);
  }
  return result;
}
function useTraining(unitId, {sessionId, canEdit = true} = {}) {
  const {useState, useEffect, useCallback, useRef} = React;
  const [snapshot, setSnapshot] = useState(null);
  const [allRecords, setAllRecords] = useState([]);
  const [proposals, setProposals] = useState([]);
  const [error, setError] = useState('');
  const [readErrors, setReadErrors] = useState({});
  const [proposalErrors, setProposalErrors] = useState({});
  const [resetVersion, setResetVersion] = useState(0);
  const generation = useRef(0);
  const allUnits = useRef([]);
  const historyRequest = useRef(0);
  const snapshotRequest = useRef(0);
  const allRequest = useRef(0);
  const proposalRequests = useRef(new Map());
  const scope = `${sessionId || ''}:${unitId || ''}`;
  const activeScope = useRef(scope);
  if (activeScope.current !== scope) { activeScope.current = scope; generation.current++; }
  const current = token => token === generation.current && activeScope.current === scope;
  const load = useCallback(async () => {
    if (!unitId) return;
    const sequence = ++snapshotRequest.current;
    const token = generation.current;
    try {
      const data = await apiClient.request(`/api/devices/${encodeURIComponent(unitId)}/training`);
      if (!current(token) || sequence !== snapshotRequest.current) return;
      setSnapshot(data); setReadErrors(previous => ({...previous,snapshot:''})); return data;
    } catch (e) { if (current(token) && sequence === snapshotRequest.current) setReadErrors(previous => ({...previous,snapshot:apiErrorText(e, '设备规则读取失败，请重试')})); }
  }, [unitId, sessionId]);
  const loadAll = useCallback(async agents => {
    if (agents) allUnits.current = agents.map(agent => typeof agent === 'string' ? agent : agent.unit_id);
    const sequence = ++allRequest.current;
    const token = generation.current;
    try {
      const data = await Promise.all(allUnits.current.map(uid => apiClient.request(`/api/devices/${encodeURIComponent(uid)}/training`)));
      if (current(token) && sequence === allRequest.current) {
        setAllRecords(data.flatMap(item => (item.records || []).map(record => ({...record, unit_id:item.unit_id || record.unit_id}))));
        setReadErrors(previous => ({...previous,all:''}));
      }
    } catch (e) { if (current(token) && sequence === allRequest.current) setReadErrors(previous => ({...previous,all:apiErrorText(e, '设备规则读取失败，请重试')})); }
  }, [unitId, sessionId]);
  const loadHistory = useCallback(async () => {
    if (!canEdit) return;
    const sequence = ++historyRequest.current;
    const token = generation.current;
    try {
      const data = await apiClient.request('/api/training/proposals' + (unitId ? `?unit_id=${encodeURIComponent(unitId)}` : ''));
      if (current(token) && sequence === historyRequest.current) {
        setProposals(data.proposals || []); setReadErrors(previous => ({...previous,history:''}));
      }
    } catch (e) { if (current(token) && sequence === historyRequest.current) setReadErrors(previous => ({...previous,history:apiErrorText(e, '对话中的规则暂时无法读取，请重试')})); }
  }, [unitId, sessionId, canEdit]);
  const getProposal = useCallback(async id => {
    if (!canEdit) return;
    const sequence = (proposalRequests.current.get(id) || 0) + 1;
    proposalRequests.current.set(id, sequence);
    const token = generation.current;
    try {
      const data = await apiClient.request(`/api/training/proposals/${encodeURIComponent(id)}`);
      if (!current(token) || sequence !== proposalRequests.current.get(id) || unitId && data.unit_id !== unitId) return;
      setProposals(previous => [...previous.filter(p => p.proposal_id !== id),data]);
      setProposalErrors(previous => ({...previous,[id]:''}));
      return data;
    } catch (e) {
      if (current(token)) setProposalErrors(previous => ({...previous,[id]:apiErrorText(e, '这次修改暂时无法读取，请稍后重试')}));
    }
  }, [unitId, sessionId, canEdit]);
  useEffect(() => {
    setSnapshot(null); setAllRecords([]); setProposals([]); setError(''); setReadErrors({}); setProposalErrors({});
    load(); loadHistory();
    return () => { generation.current++; };
  }, [load, loadHistory]);
  useEffect(() => {
    const refresh = () => { load(); loadHistory(); if (allUnits.current.length) loadAll(); };
    const onEvent = event => {
      const item = event.detail?.payload || event.detail || {};
      if (!item.type?.startsWith('training.') || unitId && item.unit_id && item.unit_id !== unitId) return;
      if (item.type === 'training.reset') {
        generation.current++;
        snapshotRequest.current++; allRequest.current++; historyRequest.current++;
        proposalRequests.current.clear();
        setSnapshot(null); setProposals([]); setProposalErrors({}); setError('');
        setAllRecords(previous => item.unit_id ? previous.filter(record => record.unit_id !== item.unit_id) : []);
        setResetVersion(previous => previous + 1);
      }
      if (item.proposal_id) getProposal(item.proposal_id);
      refresh();
    };
    apiClient.addEventListener('server-event', onEvent);
    apiClient.addEventListener('events-open', refresh);
    return () => { apiClient.removeEventListener('server-event', onEvent); apiClient.removeEventListener('events-open', refresh); };
  }, [load, loadAll, loadHistory, getProposal, unitId]);
  const proposal = [...proposals].sort((a,b) => (b.created_at || 0) - (a.created_at || 0)).find(trainingProposalOpen) || null;
  const invalidateReads = () => { snapshotRequest.current++; allRequest.current++; historyRequest.current++; };
  const act = async (selected, action) => {
    const target = selected || proposal;
    if (!target || !trainingProposalOpen(target)) return false;
    invalidateReads();
    proposalRequests.current.set(target.proposal_id,(proposalRequests.current.get(target.proposal_id) || 0) + 1);
    const token = generation.current;
    try {
      await apiClient.request(`/api/training/proposals/${encodeURIComponent(target.proposal_id)}/${action}`, {
        method:'POST', ...(action === 'confirm' ? {body:{expected_revision:target.expected_revision,capability_hash:target.capability_hash}} : {}),
      });
      if (!current(token)) return false;
      await getProposal(target.proposal_id); await loadHistory(); await load();
      if (allUnits.current.length) await loadAll();
      return true;
    } catch (e) {
      if (current(token)) setProposalErrors(previous => ({...previous,[target.proposal_id]:e.status === 409
        ? '规则已经变化，请继续在对话中说明新的要求。' : apiErrorText(e, '这次修改没有保存，请稍后重试')}));
      return false;
    }
  };
  const remove = async record => {
    invalidateReads();
    const token = generation.current;
    try {
      const uid = record.unit_id || unitId;
      const data = await apiClient.request(`/api/devices/${encodeURIComponent(uid)}/training`);
      const p = await apiClient.request('/api/training/proposals', {method:'POST',body:{unit_id:uid,operation:'remove',teaching_id:record.teaching_id,expected_revision:record.revision,capability_hash:data.catalog.hash,user_text:'删除这条规则'}});
      await apiClient.request(`/api/training/proposals/${encodeURIComponent(p.proposal_id)}/confirm`, {method:'POST',body:{expected_revision:p.expected_revision,capability_hash:p.capability_hash}});
      if (!current(token)) return true;
      await load(); await loadHistory(); if (allUnits.current.length) await loadAll(); setError(''); return true;
    } catch (e) { if (current(token)) setError(apiErrorText(e, '规则未删除，请稍后重试')); return false; }
  };
  return {catalog:snapshot?.catalog, records:snapshot?.records || [], allRecords,
    edges:snapshot?.edge_annotations || [], proposals, proposal, proposalErrors,
    load, loadAll, loadHistory, resetVersion, confirm:selected => act(selected,'confirm'), cancel:selected => act(selected,'cancel'), remove, error:error || Object.values(readErrors).find(Boolean) || ''};
}
Object.assign(window, {useTraining,useSessionTranscript,trainingRuleText,trainingSteps,trainingTransitionIds,trainingProposalOpen,trainingTimeline});
