/* Training configuration is separate from device state. Events invalidate snapshots. */
function trainingStorage(action, value) {
  try { if (action === 'get') return sessionStorage.getItem('ssm_training_proposal');
    if (action === 'set') sessionStorage.setItem('ssm_training_proposal', value);
    else sessionStorage.removeItem('ssm_training_proposal'); } catch (_) {}
  return null;
}
function useTraining(unitId) {
  const {useState, useEffect, useCallback, useRef} = React;
  const [snapshot, setSnapshot] = useState(null);
  const [proposal, setProposal] = useState(null);
  const [error, setError] = useState('');
  const activeUnit = useRef(unitId);
  const snapshotRequest = useRef(0);
  const proposalRequest = useRef(0);
  if (activeUnit.current !== unitId) {
    activeUnit.current = unitId;
    snapshotRequest.current++;
    proposalRequest.current++;
  }
  const load = useCallback(async () => {
    if (!unitId) return;
    const sequence = ++snapshotRequest.current;
    const current = () => sequence === snapshotRequest.current && activeUnit.current === unitId;
    try {
      const data = await apiClient.request(`/api/devices/${encodeURIComponent(unitId)}/training`);
      if (!current()) return;
      setSnapshot(data); setError(''); return data;
    } catch (e) { if (current()) setError(apiErrorText(e, '伙伴训练读取失败，请重试')); }
  }, [unitId]);
  const getProposal = useCallback(async id => {
    const sequence = ++proposalRequest.current;
    const current = () => sequence === proposalRequest.current && activeUnit.current === unitId;
    try {
      const data = await apiClient.request(`/api/training/proposals/${encodeURIComponent(id)}`);
      if (!current() || (unitId && data.unit_id !== unitId)) return;
      setProposal(data); trainingStorage('set', id); setError('');
    } catch (e) { if (current()) setError(apiErrorText(e, '提案读取失败，请重新发起教导')); }
  }, [unitId]);
  useEffect(() => { setSnapshot(null); setProposal(null); setError(''); load();
    return () => { snapshotRequest.current++; proposalRequest.current++; };
  }, [load]);
  useEffect(() => {
    const id = trainingStorage('get');
    if (id) getProposal(id);
    const onEvent = event => {
      const item = event.detail?.payload || event.detail || {};
      if (item.type === 'training.proposed' && item.proposal_id && (!unitId || !item.unit_id || item.unit_id === unitId)) getProposal(item.proposal_id);
      else if (['training.updated','training.loaded','training.applied'].includes(item.type) && item.unit_id === unitId) load();
    };
    const dismiss = () => { proposalRequest.current++; setProposal(null); };
    window.addEventListener('training-dismiss', dismiss);
    apiClient.addEventListener('server-event', onEvent);
    apiClient.addEventListener('events-open', load);
    return () => { window.removeEventListener('training-dismiss', dismiss); apiClient.removeEventListener('server-event', onEvent); apiClient.removeEventListener('events-open', load); };
  }, [getProposal, load, unitId]);
  const cancel = () => { proposalRequest.current++; setProposal(null); trainingStorage('remove'); setError(''); window.dispatchEvent(new Event('training-dismiss')); };
  const confirm = async () => {
    if (!proposal || proposal.confirmed) return;
    try {
      await apiClient.request(`/api/training/proposals/${encodeURIComponent(proposal.proposal_id)}/confirm`, {method:'POST',body:{expected_revision:proposal.expected_revision,capability_hash:proposal.capability_hash}});
      cancel(); await load(); return true;
    } catch (e) { setError(e.status === 409 ? '教导或设备能力已变化，请重新核对；草稿仍保留。' : apiErrorText(e)); return false; }
  };
  const setEnabled = async (record, enabled) => {
    try { await apiClient.request(`/api/training/${encodeURIComponent(record.teaching_id)}/enabled`, {method:'PUT',body:{enabled,expected_revision:record.revision}}); await load(); }
    catch (e) { setError(e.status === 409 ? '教导已变化，请重新核对。' : apiErrorText(e)); }
  };
  const remove = async record => {
    try { const p = await apiClient.request('/api/training/proposals', {method:'POST',body:{unit_id:unitId,operation:'remove',teaching_id:record.teaching_id,expected_revision:record.revision,capability_hash:snapshot.catalog.hash,user_text:'撤销这条教导'}}); setProposal(p); trainingStorage('set',p.proposal_id); return true; }
    catch (e) { setError(e.status === 409 ? '教导已变化，请重新核对。' : apiErrorText(e)); return false; }
  };
  return {catalog:snapshot?.catalog, records:snapshot?.records || [], edges:snapshot?.edge_annotations || [], proposal, load, confirm, cancel, setEnabled, remove, error};
}
window.useTraining = useTraining;
