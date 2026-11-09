function trainingParameterRows(step = {}) {
  const params = {...step.defaults};
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
  return rows;
}

function TrainingRuleSteps({record, transition, showParameters = false}) {
  const steps = trainingSteps(record);
  if (!steps.length) return null;
  const currentSteps = transition ? steps.filter(step => step.transition_ids?.includes(transition.id)) : [];
  return <>
    {steps.length > 1 && currentSteps.length > 0 && <p className="training-inspector__action">
      此转换为第 {currentSteps.map(step => step.step_index + 1).join('、')} 步，按完整顺序执行。
    </p>}
    <ol className={`training-rule-steps${steps.length === 1 ? ' training-rule-steps--single' : ''}`} aria-label="规则执行顺序">
      {steps.map(step => {
        const parameters = showParameters ? trainingParameterRows(step) : [];
        return <li key={step.step_index}>
          <span>{step.label}</span>
          {step.conditionText && <p className="training-inspector__action">{step.conditionText}</p>}
          {step.goalText && <p className="training-inspector__action">{step.goalText}</p>}
          {parameters.length > 0 && <dl className="training-inspector__params" aria-label="动作参数">
            {parameters.map(parameter => <div key={parameter.key}>
              <dt>{parameter.label}</dt><dd>
                {parameter.color && <span className="training-inspector__color" aria-hidden="true" style={{backgroundColor:parameter.color}}/>}
                {parameter.value}
              </dd>
            </div>)}
          </dl>}
        </li>;
      })}
    </ol>
  </>;
}

function TrainingRuleDetails({record,transition}) {
  const definition = record.definition || {};
  const summary = record.summaryText || record.name || record.source_text;
  return <div className="training-inspector__rule" data-teaching-id={record.teaching_id}>
    <p>{summary}</p>
    {definition.situation && definition.situation !== summary && <p>{definition.situation}</p>}
    <TrainingRuleSteps record={record} transition={transition} showParameters/>
    {definition.cooldown_s != null && <p className="training-inspector__action">间隔 {definition.cooldown_s} 秒</p>}
  </div>;
}

function TransitionInspector({transition,projection = []}) {
  const rules = projection.filter(record => record.desired_status === 'enabled');
  const condition = transition.trigger_condition || (transition.action
    ? `收到${stateTransitionLabel(transition)}指令` : `${stateTransitionLabel(transition)}时`);
  return <div className="training-inspector"><p>{condition}</p>
    {rules.length > 0 && <ul className="training-inspector__rules" aria-label="你制定的规则">
      {rules.map(record => <li key={record.teaching_id}><TrainingRuleDetails record={record} transition={transition}/></li>)}
    </ul>}
  </div>;
}
Object.assign(window, {TransitionInspector,TrainingRuleSteps});
