function TransitionInspector({selection,projection,onTeach,onClose}) {
  return <div className="training-inspector"><strong>{selection.label}</strong>
    {projection.map(record => <article key={record.teaching_id}><p>{record.source_text || record.name}</p><p>{record.changeText}</p><p>{record.scopeText}</p>
      <small>{record.desired_status === 'enabled' ? '已保存' : record.desired_status === 'paused' ? '已暂停' : '已撤销'} · {record.load_status === 'loaded' ? '已加载' : '等待设备加载'}</small>
      {!!record.execution_reasons?.length && <p>当前限制：{record.execution_reasons.map(r => r.code).join('、')}</p>}
      {onTeach && <button type="button" onClick={() => onTeach(record)}>追改</button>}</article>)}
    {onTeach && <button type="button" onClick={() => onTeach(null)}>教它这条转换</button>}</div>;
}
window.TransitionInspector = TransitionInspector;
