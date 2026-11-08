function TrainingPreview({proposal,onConfirm,onCancel,onLocate,error}) {
  const [busy,setBusy] = React.useState(false);
  if (!proposal) return null;
  return <section className="training-change" aria-label="待确认的教导">
    <small>待确认的教导{proposal.confirmed ? ' · 已保存' : ''}</small>
    <strong>{proposal.transitionLabel}</strong><p>{proposal.changeText}</p><p>{proposal.scopeText}</p>
    {proposal.before && <p>修改前：{proposal.before.source_text || proposal.before.name} · {JSON.stringify(proposal.before.definition)}</p>}
    {proposal.after?.source_text && <p>原话：{proposal.after.source_text}</p>}
    {proposal.reasons?.map((reason,index) => <p key={index}>暂不可确认：{reason.code}</p>)}
    {error && <p role="alert">{error}</p>}
    <div className="training-actions"><button type="button" onClick={onCancel}>关闭</button>
      {onLocate && <button type="button" onClick={() => onLocate(proposal.unit_id,proposal.transition_ids?.[0])}>定位转换</button>}
      {!proposal.confirmed && <button type="button" disabled={busy || !proposal.confirmation_allowed} onClick={async () => {setBusy(true); try {await onConfirm();} finally {setBusy(false);}}}>{busy ? '正在保存…' : '确认教会它'}</button>}</div>
  </section>;
}
window.TrainingPreview = TrainingPreview;
