function TrainingEventChain({view}) {
  const paths = view.paths?.length ? view.paths : [{source:view.source,target:view.target,stepIndex:0}];
  const steps = [...new Set(paths.map(path => path.stepIndex ?? 0))].sort((a,b) => a-b);
  if (steps.length > 1) return <div className="training-event-chain training-event-chain--ordered" aria-label="规则执行顺序">
    <span className="training-event-chain__trigger">{view.eventText}</span>
    {steps.map((stepIndex,index)=>{
      const alternatives=paths.filter(path=>path.stepIndex===stepIndex);
      const actions=[...new Set(alternatives.map(path=>path.actionText || path.target))];
      return <React.Fragment key={stepIndex}>
        <span className="training-event-chain__connector"><span aria-hidden="true">↓</span>{index ? '完成后' : '触发一次'}</span>
        <span className="training-event-chain__action" title={alternatives.map(path=>`${path.source} → ${path.target}`).join('；')}>
          <span className="training-event-chain__number">{index+1}</span>{actions.join(' / ')}
        </span>
      </React.Fragment>;
    })}
  </div>;
  return <div className="training-event-chain" aria-label="状态转移">
    {steps.map(stepIndex => {
      const alternatives = paths.filter(path => (path.stepIndex ?? 0) === stepIndex);
      const targets = new Map();
      alternatives.forEach(path => {
        const target = path.target || view.target;
        if (!targets.has(target)) targets.set(target,new Set());
        targets.get(target).add(path.source || view.source);
      });
      return <div className="training-event-chain__step" key={stepIndex}>
        {steps.length > 1 && <span className="training-event-chain__step-label">第 {stepIndex + 1} 步</span>}
        {[...targets].map(([target,sources]) => <span className="training-event-chain__path" key={target}>
          <span aria-label="源状态">{[...sources].join(' / ')}</span>
          <span aria-hidden="true">→</span>
          <span aria-label="目标状态">{target}</span>
        </span>)}
      </div>;
    })}
  </div>;
}

function TrainingPreview({proposal,onConfirm,onCancel,onLocate,catalog,record,error}) {
  const [pending,setPending] = React.useState('');
  const [actionError,setActionError] = React.useState('');
  const latch = React.useRef(false);
  if (!proposal) return null;
  const editable = trainingProposalOpen(proposal);
  const view = trainingTransitionView(proposal,catalog);
  const updating = proposal.operation === 'update' && proposal.before;
  const removing = proposal.operation === 'remove';
  const confirmedRevision = Number.isInteger(proposal.expected_revision) ? proposal.expected_revision + 1 : proposal.after?.revision;
  const current = !!record && record.teaching_id === proposal.teaching_id && record.revision === confirmedRevision;
  const loaded = proposal.confirmed && !removing && current && record.desired_status === 'enabled'
    && record.loaded_revision === record.revision && record.load_status === 'loaded'
    && !trainingNeedsConfirmation(record,catalog);
  const changed = proposal.confirmed && record && !current;
  const needsConfirmation = current && trainingNeedsConfirmation(record,catalog);
  const canLocate = loaded && view.transitionIds?.length > 0 && !!onLocate;
  const status = editable ? '待确认' : proposal.confirmed
    ? removing || changed && record.desired_status === 'removed' ? '事件已删除'
      : loaded ? '事件已生效' : changed ? '事件已更新' : '事件已保存'
    : proposal.cancelled ? '已取消' : proposal.superseded || proposal.superseded_by ? '已更新' : proposal.expired ? '已过期' : '';
  const badge = proposal.confirmed && !removing && !loaded && !changed
    ? needsConfirmation ? '需重新确认' : current && record.desired_status !== 'enabled' ? '未启用' : '等待设备加载'
    : proposal.confirmed ? '' : status;
  const run = async (action,callback) => {
    if (latch.current || !callback) return;
    latch.current=true;setPending(action);setActionError('');
    try { await callback(proposal); }
    catch (e) { setActionError(e.message || '操作未完成，请重试'); }
    finally { latch.current=false;setPending(''); }
  };
  const locate = () => onLocate?.({unit_id:proposal.unit_id,id:view.transitionIds?.[0],teaching_id:proposal.teaching_id});
  return <section className={`training-change${editable ? ' training-change--open' : ''}${loaded ? ' training-change--active' : ''}${canLocate ? ' training-change--locatable' : ''}`}
    aria-label={loaded ? '事件已生效' : editable ? '待确认事件' : '事件确认结果'} aria-busy={!!pending}>
    <div className="training-change__meta">
      <span className="training-change__kind">
        {loaded && <Icon name="check" size={14}/>}
        {proposal.confirmed ? status : removing ? '删除事件' : updating ? '更新事件' : '新增事件'}
      </span>
      {badge && <span className="training-change__status" role="status">{badge}</span>}
    </div>
    <TrainingEventChain view={view}/>
    <h3 className="training-event-sentence">{trainingRuleSentence(proposal,catalog)}</h3>
    {proposal.confirmed && !removing && !loaded && (changed || needsConfirmation) && <p className="training-event-notice" role="status">
      {changed ? record.desired_status === 'removed' ? '此事件已删除。' : '此事件已有后续更新。' : '设备能力已变化，请重新确认事件。'}
    </p>}
    {removing && !proposal.confirmed && editable && <p className="training-event-notice">确认后删除此自定义事件。</p>}
    {proposal.cancelled && <p className="training-event-notice">{removing ? '本次未删除事件。' : updating ? '本次未更新事件。' : '本次未添加事件。'}</p>}
    {(proposal.superseded || proposal.superseded_by) && <p className="training-event-notice">后续对话已调整此事件。</p>}
    {proposal.expired && !proposal.confirmed && <p className="training-event-notice">此事件提案已过期。</p>}
    {editable && proposal.confirmation_allowed === false && <p className="training-event-notice">{proposal.reasonText || '暂时无法添加此事件，请在对话中说明要求。'}</p>}
    {(error || actionError) && <p className="training-error" role="alert">{error || actionError}</p>}
    {editable && <div className="training-actions">
      <button type="button" className="btn training-actions__cancel" disabled={!!pending} onClick={() => run('cancel',onCancel)}>{pending === 'cancel' ? '正在取消…' : removing ? '保留事件' : '先不添加'}</button>
      <button type="button" className="btn btn--accent" disabled={!!pending || proposal.confirmation_allowed === false} onClick={() => run('confirm',onConfirm)}>{pending === 'confirm' ? '正在确认…' : removing ? '确认删除' : '确认并生效'}</button>
    </div>}
    {canLocate && <button type="button" className="training-event-locate" onClick={locate}
      aria-label="查看状态转移" title="查看状态转移"><Icon name="arrow" size={17}/></button>}
  </section>;
}
Object.assign(window,{TrainingPreview,TrainingEventChain});
