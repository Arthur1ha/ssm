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
      actionText:summary?.actionText,conditionText:summary?.conditionText || '', goalText:summary?.goalText || ''};
  });
}
function trainingTransitionIds(record) {
  const definition = record?.definition || record?.after?.definition || {};
  return [...new Set((definition.steps || []).flatMap(step => step.transition_ids || []))];
}
function trainingAssociationLabel(record, catalog) {
  const definition = record?.definition || {};
  if (definition.trigger) return trainingTransitionView(record,catalog).eventText;
  return definition.situation || ({happy:'开心时',curious:'好奇时',confused:'困惑时',surprised:'惊讶时',
      angry:'生气时',sad:'难过时',relaxed:'放松时'})[definition.emotion] || trainingRuleText(record);
}
/* Comparison changes saved associations only. Card topology, live posture and
 * command admission keep using the current device snapshot in both views. */
function trainingPreviousRecord(record) {
  if (!record || record.revision === 1) return null;
  return record.history?.find(item => item.revision === record.revision - 1) || null;
}
function trainingComparisonRecords(records, teachingId, before) {
  if (!before || !teachingId) return records;
  return records.flatMap(record => {
    if (record.teaching_id !== teachingId) return [record];
    const previous = trainingPreviousRecord(record);
    return previous ? [previous] : [];
  });
}
function trainingRecordProjection(record, catalog) {
  if (!record || record.stepSummaries) return record;
  return {...record,stepSummaries:(record.definition?.steps || []).map((step,index) => ({
    step_index:index,label:[...new Set((step.transition_ids || []).map(id => {
      const edge = catalog?.transitions?.find(item => item.id === id);
      return edge ? `${edge.source_label || edge.src} → ${edge.destination_label || edge.dst}` : '能力已变化';
    }))].join('、'),conditionText:(step.conditions || []).map(condition => {
      const spec = catalog?.state_fields?.[condition.field] || {};
      if (condition.field === 'requester_is_owner' && condition.op === 'eq') return condition.value ? '请求者是主人' : '请求者不是主人';
      const values = condition.op === 'in' ? condition.value : [condition.value];
      return `${spec.label || condition.field}为${values.map(value => spec.labels?.[value] || String(value)).join('或')}`;
    }).join('，且'),goalText:step.goal_state
      ? `已经${catalog?.state_fields?.[catalog?.transitions?.find(edge => step.transition_ids.includes(edge.id))?.state_field]?.labels?.[step.goal_state] || step.goal_state}时跳过` : null,
  }))};
}
function trainingNeedsConfirmation(record, catalog) {
  return !!(record.execution_reasons?.some(reason => reason.code === 'capability_changed')
    || record.capability_hash && catalog?.hash && record.capability_hash !== catalog.hash);
}
/* Every phase belongs to one exact rule version, connection, run and step.
 * A receipt cannot stand in for a camera observation or measured posture. */
const trainingEmotionLabels = {neutral:'平常',happy:'开心',curious:'好奇',confused:'困惑',surprised:'惊讶',angry:'生气',sad:'难过',relaxed:'放松'};
const trainingParameterLabels = {count:'次数',r:'红色分量',g:'绿色分量',b:'蓝色分量',brightness:'亮度',cooldown_s:'执行间隔'};
function trainingValueText(value, schema = {}) {
  return schema['x-enum-labels']?.[value] || (typeof value === 'boolean' ? value ? '是' : '否'
    : value != null && typeof value === 'object' ? JSON.stringify(value) : String(value ?? ''));
}
function trainingFieldInputValue(field) {
  return trainingValueText(field.value,field.schema);
}
// A display parameter is not an identity assertion. The server resolves this
// vocabulary to the declared observation and validates the saved definition.
const trainingVisionTargets = {
  'vision.owner_recognized':'主人', 'vision.stranger_recognized':'陌生人',
  'vision.person_presence':'人', 'vision.face_presence':'人脸',
};
function trainingVisionTarget(trigger) {
  if (!trigger) return null;
  const keys=Object.keys(trigger.where || {});
  if (trigger.event_id === 'vision.target_presence' && trigger.where?.detected === true
      && keys.length===2 && typeof trigger.where.target === 'string') return trigger.where.target;
  const target = trainingVisionTargets[trigger.event_id];
  const guard=['vision.owner_recognized','vision.stranger_recognized'].includes(trigger.event_id) ? 'recognized' : 'detected';
  return target && keys.length===1 && trigger.where?.[guard]===true ? target : null;
}
function trainingTransitionView(record,catalog) {
  const definition=record?.definition || record?.after?.definition || {};
  const trigger=definition.trigger;
  const spec=catalog?.events?.find(item=>item.id===trigger?.event_id);
  const visualTarget=trainingVisionTarget(trigger);
  const event=visualTarget!=null ? '看到' : spec?.name || (trigger ? '事件待同步'
    : definition.emotion!=null ? '情绪表达' : definition.situation ? '情境匹配' : '用户请求');
  const eventParams=visualTarget!=null ? visualTarget : trigger
    ? Object.entries(trigger.where || {}).filter(([,value])=>typeof value!=='boolean')
      .map(([key,value])=>trainingValueText(value,spec?.params_schema?.properties?.[key])).join('、')
    : definition.emotion!=null ? trainingEmotionLabels[definition.emotion] || definition.emotion : definition.situation || '';
  const eventText=event+(eventParams ? `（${eventParams}）` : '');
  const paths=(definition.steps || []).flatMap((step,stepIndex)=>(step.transition_ids || []).map(id=>{
    const edge=catalog?.transitions?.find(item=>item.id===id);
    const source=edge?.source_label || catalog?.state_fields?.[edge?.state_field]?.labels?.[edge?.src] || '源状态待同步';
    const target=edge?.destination_label || catalog?.state_fields?.[edge?.state_field]?.labels?.[edge?.dst] || '目标状态待同步';
    const actionLabel=catalog?.presentation?.actions?.[edge?.action];
    const pathEvent=stepIndex===0 ? eventText : edge?.event_label || (actionLabel ? `请求${actionLabel}` : '事件待同步');
    return {source,target,actionText:actionLabel || record?.stepSummaries?.find(item=>item.step_index===stepIndex)?.actionText || target,
      transitionId:id,stepIndex,eventText:pathEvent,chain:`${source} → ${pathEvent} → ${target}`};
  }));
  const labels=key=>[...new Set(paths.filter(path=>path.stepIndex===(key==='source' ? 0 : (definition.steps || []).length-1))
    .map(path=>path[key]))].join(' / ');
  return {source:labels('source') || '源状态待同步',target:labels('target') || '目标状态待同步',event,eventParams,eventText,
    chain:paths.length ? [...new Set(paths.map(path=>path.chain))].join('；') : `源状态待同步 → ${eventText} → 目标状态待同步`,
    transitionIds:trainingTransitionIds(record),paths};
}
function trainingRuleView(record, catalog) {
  const definition = record?.definition || record?.after?.definition || {};
  const simple = value => ['string','number','boolean'].includes(typeof value);
  const field = (key,value,schema,path,label) => ({key,value,schema:schema || {},path,
    label:label || schema?.title || trainingParameterLabels[key] || key,editable:!!schema && !schema.readOnly && !('const' in schema)
      && simple(value) && ['string','number','integer','boolean'].includes(schema.type)});
  const trigger = definition.trigger;
  const event = catalog?.events?.find(item => item.id === trigger?.event_id);
  const visualTarget=trainingVisionTarget(trigger);
  const triggerGroup = trigger ? {label:'事件',value:visualTarget!=null ? '看到' : event?.name || '事件待同步',
    fields:visualTarget!=null ? [field('target',visualTarget,event ? {type:'string',minLength:1,maxLength:200} : null,'event:target','事件参数')]
      : Object.entries(trigger.where || {}).map(([key,value]) => field(key,value,
      event?.params_schema?.properties?.[key] || (event?.fields?.[key] ? {type:event.fields[key]} : null),`event:${key}`))}
    : definition.emotion != null ? {label:'触发',value:'情绪表达',fields:[field('emotion',definition.emotion,
      {type:'string',title:'情绪',enum:Object.keys(trainingEmotionLabels),'x-enum-labels':trainingEmotionLabels},'emotion')]}
    : {label:'触发',value:definition.situation ? '情境匹配' : (definition.origins || []).map(origin =>
      ({user_delegated:'用户请求',agent_autonomous:'自主决策'})[origin] || origin).join(' / ') || '按原规则',
      description:definition.situation,fields:[]};
  const steps = trainingSteps(record).map(step => {
    const edges = (step.transition_ids || []).map(id => catalog?.transitions?.find(edge => edge.id === id));
    const schemas = key => {
      if (!edges.length || edges.some(edge => !edge || edge.fixed_params?.includes(key) || !edge.params_schema?.properties?.[key])) return null;
      const specs = edges.map(edge => edge.params_schema.properties[key]);
      const result = {...specs[0]};
      if (specs.some(spec => spec.readOnly || 'const' in spec || spec.type !== result.type)) result.readOnly=true;
      for (const key of ['minimum','minLength']) {
        const values=specs.map(spec=>spec[key]).filter(Number.isFinite); if (values.length) result[key]=Math.max(...values);
      }
      for (const key of ['maximum','maxLength']) {
        const values=specs.map(spec=>spec[key]).filter(Number.isFinite); if (values.length) result[key]=Math.min(...values);
      }
      const enums=specs.filter(spec=>spec.enum).map(spec=>spec.enum);
      if (enums.length) result.enum=enums[0].filter(value=>enums.every(values=>values.includes(value)));
      return result;
    };
    const actions = [...new Set(edges.filter(Boolean).map(edge => catalog?.presentation?.actions?.[edge.action] || edge.action))];
    return {label:(definition.steps || []).length > 1 ? `动作 ${step.step_index+1}` : '动作',
      value:step.actionText || actions.join(' / ') || (step.label.includes('→') ? '能力待同步' : step.label),
      fields:Object.entries(step.defaults || {}).map(([key,value]) => field(key,value,schemas(key),`step:${step.step_index}:${key}`,
        catalog?.presentation?.parameters?.[edges.find(Boolean)?.action]?.[key])),
      conditions:step.conditionText,goal:step.goalText};
  });
  const limits = definition.cooldown_s != null ? {label:'执行间隔',value:'冷却时间',fields:[field('cooldown_s',definition.cooldown_s,
    catalog?.limits?.cooldown_s ? {type:'number',title:'秒',...catalog.limits.cooldown_s} : null,'cooldown_s')]} : null;
  return [triggerGroup,...steps,...(limits ? [limits] : [])];
}
function trainingRuleHeading(record,catalog) {
  const groups=trainingRuleView(record,catalog), trigger=groups[0];
  const argumentsText=trigger.fields.filter(field=>typeof field.value!=='boolean')
    .map(field=>trainingValueText(field.value,field.schema)).join('、');
  const cause=trigger.value+(argumentsText ? `（${argumentsText}）` : '');
  const actions=groups.filter(group=>group.label.startsWith('动作')).map(group=>group.value).join(' → ');
  return actions ? `${cause} → ${actions}` : cause;
}
// Only the saved definition and its current Card projection describe a rule.
// Alternatives in one step are not a sequence, and optional display summaries
// cannot replace an event, action parameter or condition from that definition.
function trainingRuleSentence(record,catalog,{transitionIds,_tokens}={}) {
  const projected=trainingRecordProjection(record?.after && !record.definition ? {...record.after,stepSummaries:record.stepSummaries} : record,catalog);
  const definition=projected?.definition || {};
  const groups=trainingRuleView(projected,catalog);
  const view=trainingTransitionView(projected,catalog);
  const parameterText=(path,value,schema)=>_tokens?.[path] || trainingValueText(value,schema);
  const endWhen=text=>/[时后]$/.test(text) ? text : `${text}时`;
  const visualTarget=trainingVisionTarget(definition.trigger);
  let cause;
  if(visualTarget!=null)cause=`看到${parameterText('event:target',visualTarget)}时`;
  else if(definition.emotion!=null)cause=`${parameterText('emotion',definition.emotion,{'x-enum-labels':trainingEmotionLabels})}时`;
  else if(definition.situation)cause=endWhen(definition.situation);
  else if(definition.trigger){
    const spec=catalog?.events?.find(event=>event.id===definition.trigger.event_id);
    const fields=Object.entries(definition.trigger.where || {}).map(([key,value])=>{
      const schema=spec?.params_schema?.properties?.[key];
      return `${schema?.title || trainingParameterLabels[key] || key}为${parameterText(`event:${key}`,value,schema)}`;
    });
    cause=`${view.event}${fields.length ? `（${fields.join('、')}）` : ''}事件发生时`;
  }else cause=(definition.origins || []).length===1 && definition.origins[0]==='agent_autonomous' ? '自主决策时' : '收到请求时';
  if(definition.situation && (definition.trigger || definition.emotion!=null))cause=`${cause}，且${endWhen(definition.situation)}`;
  const selectedIds=transitionIds && new Set(transitionIds);
  const steps=trainingSteps(projected).map(step=>{
    const candidates=(step.transition_ids || []).map(id=>catalog?.transitions?.find(edge=>edge.id===id)).filter(Boolean);
    const selected=selectedIds ? candidates.filter(edge=>selectedIds.has(edge.id)) : [];
    const edges=selected.length ? selected : candidates;
    const actions=[...new Set(edges.map(edge=>edge.action))];
    const group=groups[step.step_index+1];
    const defaults=step.defaults || {};
    const destinations=[...new Set(edges.map(edge=>edge.destination_label || catalog?.state_fields?.[edge.state_field]?.labels?.[edge.dst]).filter(Boolean))];
    const fallback=step.actionText || (step.label && !step.label.includes('→') && !/^第 \d+ 步$/.test(step.label) ? step.label : '') || '状态转移待同步';
    let action;
    const used=new Set();
    if(actions.includes('BLINK') || (!edges.length && /闪烁/.test(fallback))){
      const countPath=`step:${step.step_index}:count`;
      action='count' in defaults ? defaults.count===0
        ? `持续闪烁${_tokens?.[countPath] ? `（次数 ${_tokens[countPath]}）` : ''}`
        : `闪烁 ${parameterText(countPath,defaults.count)} 次` : '闪烁';
      used.add('count');
    }else if(destinations.length && (step.goal_state || edges.every(edge=>edge.src!==edge.dst) && !Object.keys(defaults).length)){
      action=`切换到${destinations.join('或')}`;
    }else action=edges.length
      ? [...new Set(edges.map(edge=>catalog?.presentation?.actions?.[edge.action]).filter(Boolean))].join('或') || fallback
      : fallback;
    const params=Object.entries(defaults).filter(([key])=>!used.has(key)).map(([key,value])=>{
      const field=group?.fields?.find(item=>item.key===key);
      return `${field?.label || trainingParameterLabels[key] || key} ${parameterText(`step:${step.step_index}:${key}`,value,field?.schema)}`;
    });
    if(params.length)action+=`（${params.join('、')}）`;
    const conditions=(step.conditions || []).filter(condition=>{
      // The visible state pair already states the exact source posture.
      return !edges.length || !edges.every(edge=>condition.field===edge.state_field
        && (condition.op==='eq' ? condition.value===edge.src : condition.op==='in' && condition.value.includes(edge.src)));
    }).map(condition=>{
      const spec=catalog?.state_fields?.[condition.field] || {};
      if(condition.field==='requester_is_owner' && condition.op==='eq')return condition.value ? '请求者是主人' : '请求者不是主人';
      const values=condition.op==='in' ? condition.value : [condition.value];
      return `${spec.label || condition.field}为${values.map(value=>spec.labels?.[value] || trainingValueText(value)).join('或')}`;
    });
    if(conditions.length)action=`满足${conditions.join('且')}时，${action}`;
    return action;
  });
  const interval=definition.cooldown_s!=null ? `；执行间隔 ${parameterText('cooldown_s',definition.cooldown_s)} 秒` : '';
  return `${cause}，${steps.join('，然后') || '状态转移待同步'}${interval}。`;
}
let trainingRulePartsRevision=0;
function trainingRuleParts(record,catalog,options={}) {
  const fields=trainingRuleView(record,catalog).flatMap(group=>group.fields).filter(field=>field.editable);
  const prefix=`\uE000SSM${++trainingRulePartsRevision}:`;
  const tokens=Object.fromEntries(fields.map((field,index)=>[field.path,`${prefix}${index}\uE001`]));
  const sentence=trainingRuleSentence(record,catalog,{...options,_tokens:tokens});
  const pattern=new RegExp(`${prefix}(\\d+)\uE001`,'g');
  const parts=[];let previous=0;
  for(const match of sentence.matchAll(pattern)){
    if(match.index>previous)parts.push({text:sentence.slice(previous,match.index)});
    parts.push({field:fields[Number(match[1])]});previous=match.index+match[0].length;
  }
  if(previous<sentence.length)parts.push({text:sentence.slice(previous)});
  return parts;
}
function trainingRuleChanges(before,after,catalog) {
  const flatten=record=>Object.fromEntries(trainingRuleView(trainingRecordProjection(record,catalog),catalog)
    .flatMap((group,index)=>[
      [`group:${index}`,{label:group.label,value:group.value}],
      ...(group.description ? [[`description:${index}`,{label:'情境',value:group.description}]] : []),
      ...(group.conditions ? [[`condition:${index}`,{label:'条件',value:group.conditions}]] : []),
      ...group.fields.map(field=>[field.path,{label:field.label,value:trainingValueText(field.value,field.schema)}])
    ]));
  const old=flatten(before), next=flatten(after);
  return [...new Set([...Object.keys(old),...Object.keys(next)])].flatMap(key=>
    old[key]?.value===next[key]?.value ? [] : [{key,label:(next[key] || old[key]).label,
      before:old[key]?.value ?? '无',after:next[key]?.value ?? '无'}]);
}
function trainingParameterPatch(record, catalog, values) {
  const patch = {event_params:{},steps:[]};
  for (const group of trainingRuleView(record,catalog)) for (const field of group.fields) {
    if (!field.editable || !(field.path in values)) continue;
    const raw=String(values[field.path]);
    const text=raw.trim();
    const schema=field.schema;
    let value;
    if(field.path==='cooldown_s' && ['', '不限', '无间隔'].includes(text))value=null;
    else if(schema.enum){
      let legacy;
      try{legacy=JSON.parse(text);}catch(_){}
      const choices=schema.enum.filter(choice=>trainingValueText(choice,schema)===text || String(choice)===text || JSON.stringify(choice)===JSON.stringify(legacy));
      if(choices.length!==1)throw new Error(`${field.label}支持：${schema.enum.map(choice=>trainingValueText(choice,schema)).join('、')}`);
      value=choices[0];
    }else if(schema.type==='boolean'){
      if(['是','true'].includes(text))value=true;
      else if(['否','false'].includes(text))value=false;
      else throw new Error(`${field.label}请输入“是”或“否”`);
    }else if(['integer','number'].includes(schema.type)){
      value=text==='' ? NaN : Number(text);
    }else value=field.path==='event:target' ? text : raw;
    if(typeof value==='number'){
      if(!Number.isFinite(value) || schema.type==='integer' && !Number.isInteger(value))throw new Error(`${field.label}请输入有效${schema.type==='integer'?'整数':'数字'}`);
      if(Number.isFinite(schema.minimum) && value<schema.minimum || Number.isFinite(schema.maximum) && value>schema.maximum)throw new Error(`${field.label}需在${schema.minimum ?? '不限'}至${schema.maximum ?? '不限'}之间`);
    }
    if(typeof value==='string'){
      const length=[...value].length;
      if(Number.isFinite(schema.minLength) && length<schema.minLength || Number.isFinite(schema.maxLength) && length>schema.maxLength)throw new Error(`${field.label}长度需在${schema.minLength ?? 0}至${schema.maxLength ?? '不限'}字之间`);
    }
    if (JSON.stringify(value) === JSON.stringify(field.value)) continue;
    if (field.path==='event:target' && trainingVisionTarget(record?.definition?.trigger)!=null) patch.event_target=value;
    else if (field.path.startsWith('event:')) patch.event_params[field.key]=value;
    else if (field.path.startsWith('step:')) {
      const index=Number(field.path.split(':')[1]);
      let step=patch.steps.find(item=>item.step_index===index);
      if (!step) {step={step_index:index,params:{}};patch.steps.push(step);}
      step.params[field.key]=value;
    } else patch[field.path]=value;
  }
  return patch;
}
function trainingActivityEvent(previous, item, unitId, now = Date.now()/1000) {
  const revision = item?.revision ?? item?.version;
  const terminal = item?.fact_kind === 'result' && item.public_result?.training_run_finished === true;
  const skipped = item?.fact_kind === 'result' && item.public_result?.skipped === true;
  if (item?.type !== 'agent_progress' || item.result_kind !== 'training'
    || (item.agent_id || item.unit_id) !== unitId || !item.teaching_id
    || !Number.isInteger(revision) || revision < 1
    || item.revision != null && item.version != null && item.revision !== item.version
    || !item.run_id || !item.step_id || !item.transition_id && !terminal && !skipped || !item.device_epoch
    || !Number.isInteger(item.step_index) || item.step_index < 0 || !Number.isInteger(item.sequence)
    || !Number.isFinite(item.ts) || item.ts > now + 1 || now - item.ts > 90
    || !['observation','call','result'].includes(item.fact_kind)) return previous;
  if (item.fact_kind === 'observation' && (!item.observation_event_id || !item.observation_id
    || !Number.isFinite(item.as_of) || item.as_of > now + 1 || now - item.as_of > 5)) return previous;
  const runKey = `${revision}:${item.device_epoch}:${item.run_id}`;
  const sameRun = previous && previous.revision === revision && previous.device_epoch === item.device_epoch
    && previous.run_id === item.run_id;
  const same = sameRun && previous.step_id === item.step_id;
  const sourceAsOf = Number.isFinite(item.as_of) ? item.as_of : null;
  if (previous && (revision < previous.revision || previous.retiredRuns?.includes(runKey)
    || (sameRun ? previous.run_result && !terminal || item.sequence <= previous.sequence || item.step_index < previous.step_index
      || same && !terminal && item.transition_id !== previous.transition_id
      : sourceAsOf != null && previous.sourceAsOf != null && previous.device_epoch === item.device_epoch
        ? sourceAsOf <= previous.sourceAsOf : item.ts <= previous.ts))) return previous;
  const retiredRuns = [...(previous?.retiredRuns || [])];
  if (previous && !sameRun) retiredRuns.push(`${previous.revision}:${previous.device_epoch}:${previous.run_id}`);
  const steps = {...(sameRun ? previous.steps : {})};
  if (!terminal) steps[item.step_index] = {...(same ? steps[item.step_index] : {}),step_id:item.step_id,
    transition_id:item.transition_id,step_index:item.step_index,[item.fact_kind]:item};
  return {...(same ? previous : {}),...(sameRun && previous.observation ? {observation:previous.observation} : {}),
    teaching_id:item.teaching_id,revision,device_epoch:item.device_epoch,
    run_id:item.run_id,step_id:item.step_id,step_index:item.step_index,transition_id:item.transition_id,
    sequence:item.sequence,ts:item.ts,sourceAsOf:sourceAsOf ?? (sameRun ? previous.sourceAsOf : null),
    retiredRuns:retiredRuns.slice(-16),steps,...(terminal ? {run_result:item} : {[item.fact_kind]:item})};
}
function trainingActivityFor(record, activities, epoch, now = Date.now()/1000) {
  const activity = activities?.[record.teaching_id];
  const step = record.definition?.steps?.[activity?.step_index];
  const terminal = activity?.run_result?.public_result?.training_run_finished === true;
  if (!activity || record.desired_status !== 'enabled' || activity.revision !== record.revision
    || activity.device_epoch !== epoch || now - activity.ts > (terminal || activity.result ? 12 : 90)
    || !step || !(step.transition_ids?.includes(activity.transition_id)
      || activity.transition_id == null && (terminal || activity.result?.public_result?.skipped === true))) return null;
  return activity;
}
function trainingActivitySummary(record, activity) {
  if (!activity) return null;
  const total = record.definition?.steps?.length || 0;
  const terminal = activity.run_result?.public_result;
  const observedCount = Object.values(activity.steps || {}).filter(step=>
    (step.result?.status || step.result?.public_result?.result) === 'ok').length;
  const completed = terminal && Number.isInteger(terminal.step_count)
    ? Math.max(0,Math.min(total,terminal.step_count)) : observedCount;
  const result = terminal?.result || activity.result?.status || activity.result?.public_result?.result;
  const count = `${completed}/${total}`;
  const text = terminal ? result === 'ok' ? `本次完成 · ${count}`
    : ['accepted','executing','timeout','unknown'].includes(result) ? `等待确认 · ${count}`
      : result === 'cancelled' ? `本次已中断 · ${count}` : completed ? `本次部分完成 · ${count}` : `本次未完成 · ${count}`
    : ['executing','timeout','unknown','accepted'].includes(result) ? `等待确认 · ${count}`
      : result && result !== 'ok' ? `本次未完成 · ${count}`
        : completed === total && total ? `正在收尾 · ${count}`
          : activity.call || completed ? `执行中 · ${count}` : '已感知';
  return {text,completed,total,terminal:!!terminal,result,error:terminal?.error || activity.result?.public_result?.error};
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
  for (const proposal of proposals.filter(item => item.source_kind !== 'parameter_edit').sort((a,b) => a.created_at - b.created_at)) {
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
  const [catalogs,setCatalogs] = useState({});
  const [proposals, setProposals] = useState([]);
  const [error, setError] = useState('');
  const [readErrors, setReadErrors] = useState({});
  const [proposalErrors, setProposalErrors] = useState({});
  const [resetVersion, setResetVersion] = useState(0);
  const [activities, setActivities] = useState({});
  const [recentChange, setRecentChange] = useState(null);
  const snapshotValue = useRef(null);
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
      if (snapshotValue.current) {
          const changed = (data.records || []).filter(record => !snapshotValue.current.records?.some(old =>
            old.teaching_id === record.teaching_id && old.revision === record.revision))
            .sort((a,b) => b.updated_at - a.updated_at)[0];
          if (changed) setRecentChange({teaching_id:changed.teaching_id,revision:changed.revision});
      }
      snapshotValue.current = data; setSnapshot(data);
      setCatalogs(previous=>({...previous,[unitId]:data.catalog}));
      setReadErrors(previous => ({...previous,snapshot:''})); return data;
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
        setCatalogs(previous=>({...previous,...Object.fromEntries(data.map((item,index)=>[item.unit_id || allUnits.current[index],item.catalog]))}));
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
      if (data.source_kind === 'parameter_edit') return;
      setProposals(previous => [...previous.filter(p => p.proposal_id !== id),data]);
      setProposalErrors(previous => ({...previous,[id]:''}));
      return data;
    } catch (e) {
      if (current(token)) setProposalErrors(previous => ({...previous,[id]:apiErrorText(e, '这次修改暂时无法读取，请稍后重试')}));
    }
  }, [unitId, sessionId, canEdit]);
  useEffect(() => {
    setSnapshot(null); setAllRecords([]); setCatalogs({}); setProposals([]); setError(''); setReadErrors({}); setProposalErrors({});
    setActivities({}); setRecentChange(null);
    snapshotValue.current = null;
    load(); loadHistory();
    return () => { generation.current++; };
  }, [load, loadHistory]);
  useEffect(() => {
    const refresh = () => { load(); loadHistory(); if (allUnits.current.length) loadAll(); };
    const reconnected = () => { setActivities({}); refresh(); };
    const onEvent = event => {
      const item = event.detail?.payload || event.detail || {};
      if (unitId && item.type === 'agent_progress' && item.result_kind === 'training') {
        setActivities(previous => {
          const next = trainingActivityEvent(previous[item.teaching_id],item,unitId);
          return next === previous[item.teaching_id] ? previous : {...previous,[item.teaching_id]:next};
        });
        return;
      }
      if (!item.type?.startsWith('training.') || unitId && item.unit_id && item.unit_id !== unitId) return;
      if (item.type === 'training.reset') {
        generation.current++;
        snapshotRequest.current++; allRequest.current++; historyRequest.current++;
        proposalRequests.current.clear();
        setSnapshot(null); setProposals([]); setProposalErrors({}); setError('');
        setActivities({}); setRecentChange(null);
        snapshotValue.current = null;
        setAllRecords(previous => item.unit_id ? previous.filter(record => record.unit_id !== item.unit_id) : []);
        setResetVersion(previous => previous + 1);
      }
      if (item.proposal_id) getProposal(item.proposal_id);
      refresh();
    };
    apiClient.addEventListener('server-event', onEvent);
    apiClient.addEventListener('events-open', reconnected);
    return () => { apiClient.removeEventListener('server-event', onEvent); apiClient.removeEventListener('events-open', reconnected); };
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
      const result=await apiClient.request(`/api/training/proposals/${encodeURIComponent(target.proposal_id)}/${action}`, {
        method:'POST', ...(action === 'confirm' ? {body:{expected_revision:target.expected_revision,capability_hash:target.capability_hash}} : {}),
      });
      if (!current(token)) return false;
      // A successful write owns its feedback even if a following read fails.
      setProposals(previous=>previous.map(item=>item.proposal_id!==target.proposal_id ? item : {
        ...item,...(action==='confirm' ? {confirmed:true,after:result?.teaching_id ? result : item.after} : {cancelled:true}),
      }));
      setProposalErrors(previous=>({...previous,[target.proposal_id]:''}));
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
    if (!canEdit) throw new Error('需要管理员操作');
    invalidateReads();
    const token = generation.current;
    try {
      const uid=record.unit_id || unitId;
      const data=await apiClient.request(`/api/devices/${encodeURIComponent(uid)}/training`);
      if (!current(token)) return false;
      const removed=await apiClient.request(`/api/training/${encodeURIComponent(record.teaching_id)}`, {
        method:'DELETE',body:{expected_revision:record.revision,capability_hash:data.catalog.hash}});
      if (!current(token)) return true;
      if(removed?.teaching_id!==record.teaching_id || removed.desired_status!=='removed'
          || !Number.isInteger(removed.revision) || removed.revision<=record.revision)
        throw new Error('尚未取得事件删除回执，请重新读取后确认');
      // The DELETE receipt owns the view immediately. An earlier read must not
      // resurrect this version while the audit/history refresh is still slow.
      invalidateReads();
      const apply=previous=>previous ? {...previous,records:(previous.records || []).map(item=>
        item.teaching_id===removed.teaching_id ? removed : item)} : previous;
      snapshotValue.current=apply(snapshotValue.current);
      setSnapshot(apply);
      setAllRecords(previous=>previous.map(item=>item.teaching_id===removed.teaching_id ? removed : item));
      setActivities(previous=>Object.fromEntries(Object.entries(previous).filter(([id])=>id!==removed.teaching_id)));
      setRecentChange(previous=>previous?.teaching_id===removed.teaching_id ? null : previous);
      setError('');
      void load();void loadHistory();if(allUnits.current.length)void loadAll();
      return true;
    } catch (e) {
      const reason=e.status===409 ? '事件已变化，请重新载入后再删除' : apiErrorText(e,'未确认删除结果，请重新读取后重试');
      throw new Error(reason);
    }
  };
  const saveParameters = async (record, patch) => {
    if (!canEdit) throw new Error('需要管理员操作');
    const token=generation.current;
    invalidateReads();
    let saved;
    try {
      saved=await apiClient.request(`/api/training/${encodeURIComponent(record.teaching_id)}/parameters`,{
        method:'PATCH',body:{expected_revision:record.revision,capability_hash:record.capability_hash,...patch}});
    } catch (error) {
      const code=error.data?.detail?.code;
      if (error.status===409) {
        await load(); if (allUnits.current.length) await loadAll();
        throw new Error(code==='capability_changed' ? '设备能力已变化，请重新确认规则' : '规则已更新，请重新载入后再修改');
      }
      const reason={parameter_not_editable:'当前事件不支持修改此参数',invalid_parameter_value:'事件参数不符合设备能力要求，请检查后重试',
        teaching_removed:'此事件已删除，请重新打开状态转移'}[code];
      if (reason) throw new Error(reason);
      if (error.status===422) throw new Error('事件参数格式不正确，请检查文本长度和取值后重试');
      throw new Error(apiErrorText(error,'参数未保存，请检查取值后重试'));
    }
    if (!current(token)) return saved;
    setSnapshot(previous=>previous ? {...previous,records:(previous.records || []).map(item=>item.teaching_id===saved.teaching_id ? saved : item)} : previous);
    setAllRecords(previous=>previous.map(item=>item.teaching_id===saved.teaching_id ? saved : item));
    setRecentChange({teaching_id:saved.teaching_id,revision:saved.revision});
    await load(); if (allUnits.current.length) await loadAll();
    return saved;
  };
  return {catalog:snapshot?.catalog, records:snapshot?.records || [], allRecords,
    catalogs,saveParameters,
    activities,recentChange,
    edges:snapshot?.edge_annotations || [], proposals, proposal, proposalErrors,
    load, loadAll, loadHistory, resetVersion, confirm:selected => act(selected,'confirm'), cancel:selected => act(selected,'cancel'), remove, error:error || Object.values(readErrors).find(Boolean) || ''};
}
Object.assign(window, {useTraining,useSessionTranscript,trainingRuleText,trainingSteps,trainingTransitionIds,
  trainingRuleView,trainingValueText,trainingFieldInputValue,trainingParameterPatch,trainingRuleHeading,trainingRuleSentence,trainingRuleParts,trainingRuleChanges,trainingTransitionView,
  trainingAssociationLabel,trainingPreviousRecord,trainingComparisonRecords,trainingRecordProjection,trainingNeedsConfirmation,
  trainingActivityEvent,trainingActivityFor,trainingActivitySummary,trainingProposalOpen,trainingTimeline});
