function trainingParameterRows(definition = {}) {
  const params = {...definition.defaults, ...definition.trigger?.params};
  const rows = [];
  const rgb = ['r','g','b'].map(key => params[key]);
  if (rgb.every(value => Number.isInteger(value) && value >= 0 && value <= 255)) {
    rows.push({key:'rgb',label:'RGB',value:rgb.join(', '),color:`rgb(${rgb.join(',')})`});
    for (const key of ['r','g','b']) delete params[key];
  }
  const labels = {r:'红色',g:'绿色',b:'蓝色',brightness:'亮度',count:'次数'};
  for (const [key,value] of Object.entries(params)) {
    if (value == null) continue;
    const text = key === 'count' && value === 0 ? '持续闪烁'
      : typeof value === 'boolean' ? value ? '是' : '否'
      : typeof value === 'object' ? JSON.stringify(value) : String(value);
    rows.push({key,label:labels[key] || key,value:text});
  }
  if (definition.cooldown_s != null) rows.push({key:'cooldown_s',label:'间隔',value:`${definition.cooldown_s} 秒`});
  return rows;
}

function TrainingRuleDetails({record,transition}) {
  const definition = record.definition || {};
  const summary = record.summaryText || record.name || record.source_text;
  const situation = definition.situation
    ? definition.trigger && summary && summary !== definition.situation
      ? `${definition.situation} · ${summary}` : definition.situation
    : summary;
  const parameters = trainingParameterRows(definition);
  return <div className="training-inspector__rule" data-teaching-id={record.teaching_id}>
    <p>{situation}<span className="training-inspector__action"> → {stateTransitionLabel(transition)}</span></p>
    {parameters.length > 0 && <dl className="training-inspector__params" aria-label="动作参数">
      {parameters.map(parameter => <div key={parameter.key}>
        <dt>{parameter.label}</dt><dd>
          {parameter.color && <span className="training-inspector__color" aria-hidden="true" style={{backgroundColor:parameter.color}}/>}
          {parameter.value}
        </dd>
      </div>)}
    </dl>}
  </div>;
}

function TransitionInspector({transition,projection = []}) {
  const rules = projection.filter(record => record.desired_status === 'enabled');
  const contextualRules = rules.filter(record => record.definition?.trigger || record.definition?.situation);
  const requestRules = rules.filter(record => !record.definition?.trigger && !record.definition?.situation);
  const condition = transition.trigger_condition || (transition.action
    ? `收到${stateTransitionLabel(transition)}指令` : `${stateTransitionLabel(transition)}时`);
  return <div className="training-inspector"><p>{condition}</p>
    {requestRules.length > 0 && <ul className="training-inspector__rules" aria-label="你制定的规则">
      {requestRules.map(record => <li key={record.teaching_id}><TrainingRuleDetails record={record} transition={transition}/></li>)}
    </ul>}
    {contextualRules.map(record => <React.Fragment key={record.teaching_id}>
      <div className="device-whiteboard__rule-or">或</div>
      <TrainingRuleDetails record={record} transition={transition}/>
    </React.Fragment>)}
  </div>;
}
window.TransitionInspector = TransitionInspector;
