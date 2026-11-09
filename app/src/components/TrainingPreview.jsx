function TrainingPreview({proposal,onConfirm,onCancel,onRevise,error}) {
  const [busy,setBusy] = React.useState(false);
  if (!proposal) return null;
  const editable = trainingProposalOpen(proposal);
  const summary = proposal.summaryText || trainingRuleText(proposal.after);
  const before = proposal.beforeSummaryText || (proposal.before && trainingRuleText(proposal.before));
  const effect = proposal.changeText;
  const status = proposal.confirmed ? (proposal.operation === 'remove' ? '已删除' : '已保存') : proposal.cancelled ? '已取消'
    : proposal.superseded || proposal.superseded_by ? '已更新' : proposal.expired ? '已过期' : '规则草案';
  const run = async callback => { setBusy(true); try { await callback?.(proposal); } finally { setBusy(false); } };
  return <section className={`training-change${editable ? ' training-change--open' : ''}`} aria-label="规则修改" aria-busy={busy}>
    <div className="training-change__meta">
      <span className="training-change__status">{status}</span>
      {proposal.unit_id && <span className="training-change__agent">{getAgentDisplayName(proposal.unit_id)}</span>}
    </div>
    <h3 className="training-rule-title">{proposal.operation === 'remove' ? '删除规则' : summary}</h3>
    {before && proposal.operation !== 'remove' && <p className="training-change__before">原来：{before}</p>}
    {proposal.operation === 'remove' ? <p>{before || summary}</p>
      : effect && effect !== summary && <p>{effect}</p>}
    {proposal.operation !== 'remove' && <TrainingRuleSteps record={proposal}/>}
    {proposal.confirmed && <p>{proposal.operation === 'remove' ? '这条规则已删除。' : '规则已保存。'}</p>}
    {proposal.cancelled && <p>这次没有修改规则。</p>}
    {(proposal.superseded || proposal.superseded_by) && <p>后续对话已调整这项要求。</p>}
    {proposal.expired && !proposal.confirmed && !proposal.cancelled && !proposal.superseded_by && <p>继续告诉我你的要求，我会重新核对这次修改。</p>}
    {editable && proposal.confirmation_allowed === false && <p>{proposal.reasonText || '这项要求暂时无法应用，可以继续在对话中调整。'}</p>}
    {error && <p className="training-error" role="alert">{error}</p>}
    {editable && <div className="training-actions">
      <button type="button" className="btn btn--accent" disabled={busy || proposal.confirmation_allowed === false} onClick={() => run(onConfirm)}>{busy ? '正在保存…' : '就这样'}</button>
      <button type="button" className="btn" disabled={busy} onClick={() => run(onCancel)}>取消</button>
      {onRevise && <button type="button" className="btn" disabled={busy} onClick={() => onRevise(proposal)}>继续修改</button>}
    </div>}
  </section>;
}
window.TrainingPreview = TrainingPreview;
