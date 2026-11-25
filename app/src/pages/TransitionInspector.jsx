function TrainingParameterInput({field,value,onChange}) {
  const schema=field.schema;
  const numeric=['integer','number'].includes(schema.type);
  const choices=schema.enum?.map(choice=>trainingValueText(choice,schema));
  const placeholder=field.path==='cooldown_s' ? '清空为无间隔' : choices ? `例如：${choices.slice(0,3).join('、')}` : schema.type==='boolean' ? '是 / 否'
    : numeric && (schema.minimum!=null || schema.maximum!=null) ? `${schema.minimum ?? '不限'}–${schema.maximum ?? '不限'}` : undefined;
  return <input {...ssmInputAttributes[numeric ? schema.type==='integer' ? 'numeric' : 'decimal' : 'text']}
    aria-label={field.label.endsWith('参数') ? field.label : `${field.label}参数`} value={value} onChange={event=>onChange(event.target.value)}
    type="text" required={field.path!=='cooldown_s'}
    minLength={schema.type==='string' && !schema.enum ? schema.minLength : undefined}
    maxLength={schema.type==='string' && !schema.enum ? schema.maxLength : undefined} placeholder={placeholder}/>;
}

function TrainingRuleContent({record,catalog,values,onChange,eventOnly=false}) {
  const projected=trainingRecordProjection(record?.after && !record.definition ? {...record.after,stepSummaries:record.stepSummaries} : record,catalog);
  const allGroups=trainingRuleView(projected,catalog);
  const groups=eventOnly ? allGroups.slice(0,1) : allGroups;
  const conditions=eventOnly ? [...new Set(trainingSteps(projected).map(step=>step.conditionText).filter(Boolean))].join('；') : '';
  return <div className="training-rule-content">
    {groups.map((group,index)=><section className="training-rule-group" key={index}>
      {!eventOnly && <div className="training-rule-group__heading"><span>{group.label}</span><strong>{group.value}</strong></div>}
      {group.description && <p className="training-rule-group__description">{group.description}</p>}
      {group.fields.length>0 && <div className="training-rule-group__parameters">
        <dl className="training-rule-parameters">
          {group.fields.map(field=><div key={field.path}>
            <dt>{eventOnly ? group.fields.length===1 ? '事件参数' : field.label : field.label}</dt><dd>
              {values && field.editable ? <TrainingParameterInput field={eventOnly && group.fields.length===1 ? {...field,label:'事件'} : field} value={values[field.path]}
                onChange={value=>onChange(field.path,value)}/> : <span>{trainingValueText(field.value,field.schema)}</span>}
            </dd>
            {!eventOnly && values && field.editable && field.schema.description && <p className="training-rule-parameters__hint">{field.schema.description}</p>}
          </div>)}
        </dl>
      </div>}
      {group.conditions && <p className="training-rule-group__condition"><span>条件</span>{group.conditions}</p>}
      {group.goal && <p className="training-rule-group__description">{group.goal}</p>}
    </section>)}
    {conditions && <p className="training-rule-group__condition"><span>条件</span>{conditions}</p>}
  </div>;
}

/* The same sentence stays in place while its parameter words become inputs. */
function TrainingCompactRuleEditor({record,catalog,values,onChange,transitionIds}) {
  const fields=trainingRuleView(record,catalog).flatMap(group=>group.fields).filter(field=>field.editable);
  const parts=trainingRuleParts(record,catalog,{transitionIds});
  const embedded=new Set(parts.filter(part=>part.field).map(part=>part.field.path));
  const additional=fields.filter(field=>!embedded.has(field.path));
  const label=field=>field.path==='event:target' ? '看到的对象' : field.path==='emotion' ? '情绪'
    : field.key==='count' ? '次数' : field.path==='cooldown_s' ? '执行间隔' : field.label;
  const input=field=>{
    const value=String(values[field.path] ?? '');
    return <span className="training-inline-input" key={field.path}>
      <span className="training-inline-input__size" aria-hidden="true">{value || ' '}{'\u200a'}</span>
      <input {...ssmInputAttributes[['number','integer'].includes(field.schema.type) ? field.schema.type==='integer' ? 'numeric' : 'decimal' : 'text']}
        type="text" data-field={field.key} data-path={field.path}
        aria-label={`事件参数：${label(field)}`} value={value} required={field.path!=='cooldown_s'}
        placeholder={field.path==='cooldown_s' ? '无间隔' : undefined}
        minLength={field.schema.type==='string' && !field.schema.enum ? field.schema.minLength : undefined}
        maxLength={field.schema.type==='string' && !field.schema.enum ? field.schema.maxLength : undefined}
        onChange={event=>onChange(field.path,event.target.value)}/>
    </span>;
  };
  const hints=fields.flatMap(field=>field.path==='event:target' ? ['可直接输入主人、花、树、盒子等。']
    : field.path==='cooldown_s' ? ['清空执行间隔即可取消间隔限制。']
    : field.schema.enum ? [`例如：${field.schema.enum.slice(0,4).map(value=>trainingValueText(value,field.schema)).join('、')}`]
    : field.key==='count' && field.schema.minimum===0 ? [`次数 ${field.schema.minimum}–${field.schema.maximum ?? '上限以设备能力为准'}；0 为持续闪烁。`] : []);
  return <>
    <div className="training-edit-sentence">{parts.map((part,index)=>part.field ? part.field.editable ? input(part.field) : <span key={index}>{trainingValueText(part.field.value,part.field.schema)}</span> : <span key={index}>{part.text}</span>)}</div>
    {additional.length>0 && <div className="training-edit-additional">{additional.map(field=><label key={field.path}>
      <span>{label(field)}</span>{input(field)}</label>)}</div>}
    {hints.length>0 && <p className="training-edit-hint">{[...new Set(hints)].join(' ')}</p>}
  </>;
}

function TrainingRunProgress({record,catalog,activity}) {
  if (!activity) return null;
  const summary=trainingActivitySummary(record,activity);
  const labels=trainingRuleView(record,catalog).slice(1,1+summary.total);
  const reasonLabels={autonomy_unavailable:'自主执行条件失效',training_interrupted:'执行已被中断',
    control_lease_transferred:'控制权已移交',device_busy:'设备正在执行其他动作',device_resting:'设备已休息',
    training_observation_unavailable:'无法确认触发观察',newer_user_goal:'用户已下达新目标',
    source_state_unavailable:'当前姿态不满足动作条件',training_revision_changed:'规则已更新',
    capability_changed:'设备能力已变化',execution_context_expired:'本次执行已过期'};
  return <div className="training-run-progress" aria-label="本次规则执行">
    <p className="training-run-progress__summary" role="status">{summary.text}</p>
    <ol>{labels.map((label,index)=>{
      const step=activity.steps?.[index];
      const receipt=step?.result?.public_result;
      const result=step?.result?.status || receipt?.result;
      const text=receipt?.skipped ? '已满足，跳过' : result === 'ok' ? '已完成'
        : ['accepted','executing','timeout','unknown'].includes(result) ? '等待确认'
          : result ? step.call ? '未完成' : '未执行'
            : step?.call ? '执行中' : summary.terminal ? '未执行' : '待执行';
      return <li key={index}><span>{label.value}</span><span>{text}</span></li>;
    })}</ol>
    {summary.error && <p>{reasonLabels[summary.error]
      || (typeof activityReasonText==='function' ? activityReasonText({reason:summary.error}) : '')
      || '本次执行未完成，请查看过程记录。'}</p>}
  </div>;
}

function TrainingRuleCard({record,catalog,onSave,onRemove,status,activity,changed,unconfirmed,className='',compact=false,transitionIds,onBusyChange}) {
  const [base,setBase]=React.useState(null);
  const [values,setValues]=React.useState({});
  const [busy,setBusy]=React.useState(false);
  const [error,setError]=React.useState('');
  const [saved,setSaved]=React.useState(null);
  const [success,setSuccess]=React.useState('');
  const [pendingAction,setPendingAction]=React.useState('');
  const latch=React.useRef(false);
  const editCatalog=React.useRef(null);
  const live=saved?.teaching_id===record.teaching_id && saved.revision>record.revision ? saved : record;
  const stale=base && (unconfirmed || live.revision!==base.revision || live.capability_hash!==base.capability_hash
    || catalog?.hash && base.capability_hash && catalog.hash!==base.capability_hash);
  const fields=trainingRuleView(live,catalog).flatMap(group=>group.fields);
  const editable=onSave && !unconfirmed && fields.some(field=>field.editable);
  const begin=()=>{
    editCatalog.current=catalog;
    setBase(live);setError('');setSuccess('');
    setValues(Object.fromEntries(fields.filter(field=>field.editable).map(field=>[field.path,trainingFieldInputValue(field)])));
  };
  let dirty=false;
  if (base) {
    try {const p=trainingParameterPatch(base,editCatalog.current,values);dirty=!!(p.steps.length || Object.keys(p.event_params).length || 'emotion' in p || 'cooldown_s' in p || 'event_target' in p);}
    catch (_) {dirty=true;}
  }
  const execute=async (callback,action='save')=>{
    if(latch.current)return;latch.current=true;setBusy(true);setError('');setSuccess('');setPendingAction(action);onBusyChange?.(true);
    try {await callback();}catch(e){setError(e.message || '本次修改未完成，请重试');}
    finally{latch.current=false;setBusy(false);setPendingAction('');onBusyChange?.(false);}
  };
  const submit=event=>{
    event.preventDefault();if(stale || !dirty)return;
    execute(async()=>{
      const result=await onSave(base,trainingParameterPatch(base,editCatalog.current,values));
      if(!result)throw new Error('事件未更新，请稍后重试');
      setSaved(result);setBase(null);setSuccess('已更新');
    });
  };
  const remove=()=>execute(async()=>{
    if(await onRemove(live)===false)throw new Error(compact ? '事件未删除，请稍后重试' : '规则未删除，请稍后重试');
  },'remove');
  const previous=trainingPreviousRecord(live);
  const title=compact ? trainingRuleSentence(live,catalog,{transitionIds}) : trainingRuleHeading(live,catalog);
  const loaded=live.desired_status==='enabled' && live.loaded_revision===live.revision && live.load_status==='loaded';
  const displayStatus=success || (!activity && status) || (live.desired_status==='disabled' ? '未启用' : unconfirmed ? '需重新确认' : loaded ? '已生效' : '等待设备加载');
  return <article className={`training-inspector__card ${className}${changed ? ' is-new' : ''}${unconfirmed ? ' is-unconfirmed' : ''}${compact ? ' training-inspector__card--compact' : ''}`}
    data-teaching-id={record.teaching_id} aria-busy={busy}>
    {!base && <div className="training-inspector__heading">
      <h3 className="training-rule-title">{title}</h3>
      <div className="training-record__actions">
        {(editable || compact && onRemove) && <button type="button" className="training-icon-action training-inspector__revise" aria-label={compact ? '修改自定义事件' : '修改参数'} title={compact ? '修改自定义事件' : '修改参数'} disabled={busy} onClick={begin}><Icon name="pencil" size={16} sw={1.5}/></button>}
        {!compact && onRemove && <button type="button" className="training-icon-action training-icon-action--danger" aria-label="删除规则" title="删除规则" disabled={busy} onClick={remove}><Icon name="trash" size={16}/></button>}
      </div>
    </div>}
    {(!base || !compact) && <span className={`training-inspector__status${!loaded && !success ? ' is-muted' : ''}${success ? ' training-save-success' : ''}`} role="status">{displayStatus}</span>}
    {!base && (live.definition?.steps?.length || 0)>1 && <TrainingEventChain view={trainingTransitionView(live,catalog)}/>}
    {!base && <TrainingRunProgress record={live} catalog={catalog} activity={activity}/>}
    <form {...ssmInputAttributes.form} onSubmit={submit} onKeyDown={event=>{
      if(event.key==='Escape' && base){event.stopPropagation();if(!busy){setBase(null);setError('');}}
    }}>
      <fieldset disabled={busy}>
        {compact ? base && <TrainingCompactRuleEditor record={base} catalog={editCatalog.current} values={values}
          transitionIds={transitionIds} onChange={(path,value)=>setValues(old=>({...old,[path]:value}))}/>
          : <TrainingRuleContent record={base || live} catalog={base ? editCatalog.current : catalog}
            values={base ? values : undefined} onChange={(path,value)=>setValues(old=>({...old,[path]:value}))}/>}</fieldset>
      {unconfirmed && <p className="training-inspector__notice">设备能力已变化，需要重新确认事件。</p>}
      {(error || stale) && <p className="training-error" role="alert">{stale ? '事件已更新，请重新载入后再修改' : error}</p>}
      {base && <div className="training-parameter-actions">
        {compact && onRemove && <button type="button" className="btn training-inline-remove" disabled={busy} onClick={remove}>{pendingAction==='remove' ? '删除中…' : '删除'}</button>}
        <button type="button" className="btn" disabled={busy} onClick={()=>{setBase(null);setError('');}}>{stale ? '重新载入' : '取消'}</button>
        <button type="submit" className={`btn${dirty ? ' btn--accent' : ''}`} aria-busy={busy} disabled={busy || stale || !dirty || !editable}>{pendingAction==='save' && <span className="training-save-spinner" aria-hidden="true"/>}{pendingAction==='save' ? '保存中…' : '保存'}</button>
      </div>}
    </form>
    {!compact && !base && previous && <details className="training-inspector__previous"><summary>修改记录</summary>
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

function TransitionInspector({transition,transitions,projection = [],catalog,activityFor,changedTeaching,onSave,onRemove,onBusyChange}) {
  const rules = projection.filter(record => record.desired_status !== 'removed');
  const events=[...new Map((transitions || [transition]).filter(Boolean).map(item=>[
    JSON.stringify([item.trigger,item.trigger_condition,item.event_label,item.guard_label,item.action,item.params]),item])).values()];
  return <div className="training-inspector">
    <section className="training-transition-events" aria-label="预置事件">
      <div className="training-event-section-heading"><h4>预置事件</h4></div>
      <ul>{events.map((item,index)=><li key={index}>
        <span className="training-native-summary">{item.trigger_condition || item.event_label || stateTransitionLabel(item)}</span>
        {item.guard_label && !item.trigger_condition?.includes(item.guard_label) && <div className="training-transition-events__guard">
          <span>条件</span><span>{item.guard_label || item.trigger_condition}</span>
        </div>}
      </li>)}</ul>
      {events.some(item=>item.src!=null && item.src===item.dst) && <details className="training-transition-events__self">
        <summary>自转移</summary><p>事件发生后，源状态与目标状态相同。</p>
      </details>}
    </section>
    {rules.length>0 && <section className="training-custom-events" aria-label="自定义事件">
      <div className="training-event-section-heading"><h4>自定义事件</h4></div>
      <ul className="training-inspector__rules">
      {rules.map(record => {
        const unconfirmed = trainingNeedsConfirmation(record,catalog);
        const activity = !unconfirmed && activityFor?.(record);
        const status = record.desired_status === 'disabled' ? '未启用' : unconfirmed ? '需重新确认'
          : trainingActivitySummary(record,activity)?.text || '';
        const changed = changedTeaching === record.teaching_id;
        return <li key={record.teaching_id}><TrainingRuleCard record={record} catalog={catalog}
          status={status} activity={activity}
          changed={changed} unconfirmed={unconfirmed} onSave={onSave} onRemove={onRemove} compact onBusyChange={onBusyChange}
          transitionIds={events.map(item=>item.id).filter(Boolean)}/></li>;
      })}
    </ul>
    </section>}
  </div>;
}
Object.assign(window, {TransitionInspector,TrainingRuleContent,TrainingRuleCard,TrainingRuleChanges,TrainingRunProgress});
