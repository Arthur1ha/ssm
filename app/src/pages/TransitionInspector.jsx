function TrainingParameterInput({field,value,onChange}) {
  const schema=field.schema;
  const choices=schema.enum || (schema.type==='boolean' ? [true,false] : null);
  const props={'aria-label':`${field.label}参数`,value,onChange:event=>onChange(event.target.value)};
  if (choices) return <select {...props}>{choices.map(choice=><option key={JSON.stringify(choice)} value={JSON.stringify(choice)}>{trainingValueText(choice,schema)}</option>)}</select>;
  return <input {...props} type={['integer','number'].includes(schema.type) ? 'number' : 'text'} required
    min={schema.minimum} max={schema.maximum} minLength={schema.minLength} maxLength={schema.maxLength}
    step={schema.type==='integer' ? 1 : 'any'}/>;
}

function TrainingRuleContent({record,catalog,values,onChange}) {
  const projected=trainingRecordProjection(record?.after && !record.definition ? {...record.after,stepSummaries:record.stepSummaries} : record,catalog);
  const groups=trainingRuleView(projected,catalog);
  return <div className="training-rule-content">
    {groups.map((group,index)=><section className="training-rule-group" key={index}>
      <div className="training-rule-group__heading"><span>{group.label}</span><strong>{group.value}</strong></div>
      {group.description && <p className="training-rule-group__description">{group.description}</p>}
      {group.fields.length>0 && <div className="training-rule-group__parameters">
        <dl className="training-rule-parameters">
          {group.fields.map(field=><div key={field.path}>
            <dt>{field.label}</dt><dd>
              {values && field.editable ? <TrainingParameterInput field={field} value={values[field.path]}
                onChange={value=>onChange(field.path,value)}/> : <span>{trainingValueText(field.value,field.schema)}</span>}
            </dd>
            {values && field.editable && field.schema.description && <p className="training-rule-parameters__hint">{field.schema.description}</p>}
          </div>)}
        </dl>
      </div>}
      {group.conditions && <p className="training-rule-group__condition"><span>条件</span>{group.conditions}</p>}
      {group.goal && <p className="training-rule-group__description">{group.goal}</p>}
    </section>)}
  </div>;
}

function TrainingRuleCard({record,catalog,onSave,onRemove,status,changed,unconfirmed,className=''}) {
  const [base,setBase]=React.useState(null);
  const [values,setValues]=React.useState({});
  const [busy,setBusy]=React.useState(false);
  const [error,setError]=React.useState('');
  const [saved,setSaved]=React.useState(null);
  const latch=React.useRef(false);
  const editCatalog=React.useRef(null);
  const live=saved?.teaching_id===record.teaching_id && saved.revision>record.revision ? saved : record;
  const stale=base && (unconfirmed || live.revision!==base.revision || live.capability_hash!==base.capability_hash
    || catalog?.hash && base.capability_hash && catalog.hash!==base.capability_hash);
  const fields=trainingRuleView(live,catalog).flatMap(group=>group.fields);
  const editable=onSave && !unconfirmed && fields.some(field=>field.editable);
  const begin=()=>{
    editCatalog.current=catalog;
    setBase(live);setError('');
    setValues(Object.fromEntries(fields.filter(field=>field.editable).map(field=>[field.path,
      field.schema.enum || field.schema.type==='boolean' ? JSON.stringify(field.value) : String(field.value)])));
  };
  let dirty=false;
  if (base) {
    try {const p=trainingParameterPatch(base,editCatalog.current,values);dirty=!!(p.steps.length || Object.keys(p.event_params).length || 'emotion' in p || 'cooldown_s' in p);}
    catch (_) {dirty=true;}
  }
  const execute=async callback=>{
    if(latch.current)return;latch.current=true;setBusy(true);setError('');
    try {await callback();}catch(e){setError(e.message || '本次修改未完成，请重试');}
    finally{latch.current=false;setBusy(false);}
  };
  const submit=event=>{
    event.preventDefault();if(stale || !dirty)return;
    execute(async()=>{
      const result=await onSave(base,trainingParameterPatch(base,editCatalog.current,values));
      if(result)setSaved(result);setBase(null);
    });
  };
  const previous=trainingPreviousRecord(live);
  return <article className={`training-inspector__card ${className}${changed ? ' is-new' : ''}${unconfirmed ? ' is-unconfirmed' : ''}`}
    data-teaching-id={record.teaching_id} aria-busy={busy}>
    <div className="training-inspector__heading">
      <h3 className="training-rule-title">{trainingRuleHeading(live,catalog)}</h3>
      {!base && <div className="training-record__actions">
        {editable && <button type="button" className="training-icon-action training-inspector__revise" aria-label="修改参数" title="修改参数" disabled={busy} onClick={begin}><Icon name="edit" size={16}/></button>}
        {onRemove && <button type="button" className="training-icon-action training-icon-action--danger" aria-label="删除规则" title="删除规则" disabled={busy} onClick={()=>execute(async()=>{
          if(await onRemove(live)===false)throw new Error('规则未删除，请稍后重试');
        })}><Icon name="trash" size={16}/></button>}
      </div>}
    </div>
    {status && <span className="training-inspector__status" role="status">{status}</span>}
    <form onSubmit={submit} onKeyDown={event=>{
      if(event.key==='Escape' && base){event.stopPropagation();if(!busy){setBase(null);setError('');}}
    }}>
      <fieldset disabled={busy}><TrainingRuleContent record={base || live} catalog={base ? editCatalog.current : catalog}
        values={base ? values : undefined} onChange={(path,value)=>setValues(old=>({...old,[path]:value}))}/></fieldset>
      {unconfirmed && <p className="training-inspector__notice">设备能力已变化，需要重新确认规则。</p>}
      {(error || stale) && <p className="training-error" role="alert">{stale ? '规则已更新，请重新载入后再修改' : error}</p>}
      {base && <div className="training-parameter-actions">
        <button type="button" className="btn" disabled={busy} onClick={()=>{setBase(null);setError('');}}>{stale ? '重新载入' : '取消'}</button>
        <button type="submit" className="btn btn--accent" disabled={busy || stale || !dirty}>{busy ? '正在保存…' : '保存'}</button>
      </div>}
    </form>
    {!base && previous && <details className="training-inspector__previous"><summary>修改记录</summary>
      <TrainingRuleChanges before={previous} after={live} catalog={catalog}/></details>}
  </article>;
}

function TrainingRuleChanges({before,after,catalog}) {
  const changes=trainingRuleChanges(before,after,catalog);
  return changes.length>0 ? <dl className="training-rule-changes" aria-label="规则变更">
    {changes.map(change=><div key={change.key}><dt>{change.label}</dt><dd>
      <span className="training-rule-changes__before">{change.before}</span><span aria-label="改为">→</span><span>{change.after}</span>
    </dd></div>)}
  </dl> : null;
}

function TransitionInspector({transition,transitions,projection = [],catalog,activityFor,changedTeaching,onSave,onRemove}) {
  const rules = projection.filter(record => record.desired_status === 'enabled');
  const events=[...new Map((transitions || [transition]).filter(Boolean).map(item=>[
    JSON.stringify([item.trigger,item.guard_label || item.trigger_condition,item.action,item.params]),item])).values()];
  return <div className="training-inspector">
    <section className="training-transition-events" aria-label="触发事件">
      <h4>触发事件</h4>
      <ul>{events.map((item,index)=><li key={index}>
        <strong>{item.event_label || stateTransitionLabel(item)}</strong>
        {(item.guard_label || !item.action && item.trigger_condition) && <div className="training-transition-events__guard">
          <span>条件</span><span>{item.guard_label || item.trigger_condition}</span>
        </div>}
      </li>)}</ul>
      {events.some(item=>item.src!=null && item.src===item.dst) && <details className="training-transition-events__self">
        <summary>自转换</summary><p>事件发生后仍进入同一状态；不会沿箭头自动重复。</p>
      </details>}
    </section>
    {rules.length > 0 && <ul className="training-inspector__rules" aria-label="你制定的规则">
      {rules.map(record => {
        const unconfirmed = trainingNeedsConfirmation(record,catalog);
        const activity = !unconfirmed && activityFor?.(record);
        const status = unconfirmed ? '需重新确认' : activity?.result
          ? ['ok','accepted'].includes(activity.result.status) ? '已响应'
            : ['executing','timeout','unknown'].includes(activity.result.status) ? '等待确认' : '本次未完成'
          : activity?.call ? '执行中' : activity?.observation ? '已感知' : '';
        const changed = changedTeaching === record.teaching_id;
        return <li key={record.teaching_id}><TrainingRuleCard record={record} catalog={catalog}
          status={status || (changed ? trainingPreviousRecord(record) ? '已更新' : '新增' : '')}
          changed={changed} unconfirmed={unconfirmed} onSave={onSave} onRemove={onRemove}/></li>;
      })}
    </ul>}
  </div>;
}
Object.assign(window, {TransitionInspector,TrainingRuleContent,TrainingRuleCard,TrainingRuleChanges});
