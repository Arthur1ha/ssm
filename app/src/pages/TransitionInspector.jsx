function TransitionInspector({selection,projection}) {
  const rules = projection.filter(record => record.desired_status === 'enabled');
  return <div className="training-inspector"><strong>{selection.label}</strong>
    {rules.length > 0 && <ul className="training-inspector__rules" aria-label="你制定的规则">
      {rules.map(record => <li key={record.teaching_id}>{record.summaryText || record.name || record.source_text}</li>)}
    </ul>}
  </div>;
}
window.TransitionInspector = TransitionInspector;
