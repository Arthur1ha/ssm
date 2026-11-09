function TransitionInspector({transition,projection}) {
  const rules = projection.filter(record => record.desired_status === 'enabled');
  const eventRules = rules.filter(record => record.definition?.trigger);
  const requestRules = rules.filter(record => !record.definition?.trigger);
  const condition = transition.trigger_condition || (transition.action
    ? `收到${stateTransitionLabel(transition)}指令` : `${stateTransitionLabel(transition)}时`);
  return <div className="training-inspector"><p>{condition}</p>
    {requestRules.length > 0 && <ul className="training-inspector__rules" aria-label="你制定的规则">
      {requestRules.map(record => <li key={record.teaching_id}>{record.summaryText || record.name || record.source_text}</li>)}
    </ul>}
    {eventRules.map(record => <React.Fragment key={record.teaching_id}>
      <div className="device-whiteboard__rule-or">或</div>
      <p>{record.summaryText || record.name || record.source_text}</p>
    </React.Fragment>)}
  </div>;
}
window.TransitionInspector = TransitionInspector;
