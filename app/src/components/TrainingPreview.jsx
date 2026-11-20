function TrainingPreview({proposal,onConfirm,onCancel,catalog,error}) {
  const [busy,setBusy] = React.useState(false);
  if (!proposal) return null;
  const editable = trainingProposalOpen(proposal);
  const summary = trainingRuleHeading(proposal,catalog);
  const updating = proposal.operation === 'update' && proposal.before;
  const removing = proposal.operation === 'remove';
  const status = proposal.confirmed ? (proposal.operation === 'remove' ? '已删除' : '已保存') : proposal.cancelled ? '已取消'
    : proposal.superseded || proposal.superseded_by ? '已更新' : proposal.expired ? '已过期' : '';
  const run = async callback => { setBusy(true); try { await callback?.(proposal); } finally { setBusy(false); } };
  return <section className={`training-change${editable ? ' training-change--open' : ''}`} aria-label="规则修改" aria-busy={busy}>
    <div className="training-change__meta">
      <span className="training-change__kind">{removing ? '删除规则' : updating ? '修改规则' : '新增规则'}</span>
      {proposal.unit_id && <span className="training-change__agent">{getAgentDisplayName(proposal.unit_id)}</span>}
    </div>
    <div className="training-change__heading">
      <h3 className="training-rule-title">{summary}</h3>
      {status && <span className="training-change__status" role="status">{status}</span>}
    </div>
    {removing ? <p>保存后不再使用这条规则。</p> : <TrainingRuleContent record={proposal} catalog={catalog}/>}
    {updating && <TrainingRuleChanges before={proposal.before} after={proposal.after} catalog={catalog}/>}
    {proposal.cancelled && <p>这次没有修改规则。</p>}
    {(proposal.superseded || proposal.superseded_by) && <p>后续对话已调整这项要求。</p>}
    {editable && proposal.confirmation_allowed === false && <p>{proposal.reasonText || '暂时无法应用。'}</p>}
    {error && <p className="training-error" role="alert">{error}</p>}
    {editable && <div className="training-actions">
      <button type="button" className="btn btn--accent" disabled={busy || proposal.confirmation_allowed === false} onClick={() => run(onConfirm)}>{busy ? '正在保存…' : removing ? '确认删除' : '保存规则'}</button>
      <button type="button" className="btn training-actions__cancel" disabled={busy} onClick={() => run(onCancel)}>取消</button>
    </div>}
  </section>;
}
window.TrainingPreview = TrainingPreview;
