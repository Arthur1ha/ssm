/* Presentation only: grouping requires run/step/task identities, never proximity. */
function processRecord(event = {}) {
  const keys = ['event_type', 'run_id', 'parent_run_id', 'step_id', 'task_id', 'trace_id',
    'tool', 'action', 'target_unit_id', 'unit_id', 'skill_id', 'capability_name', 'arguments',
    'requested_text', 'result_state', 'status', 'reason', 'stage', 'ts', 'media_ref', 'media_url',
    'decision_basis', 'state_at_event', 'trigger_message_id', 'message_id', 'game_round'];
  return Object.fromEntries(keys.filter(key => event[key] !== undefined).map(key => [key, event[key]]));
}
function processGroupKey(event) {
  const unit = event.unit_id || event.agent || event.agent_id || '';
  return event.run_id ? `run:${unit}:${event.run_id}` : event.task_id || event.taskId
    ? `task:${unit}:${event.task_id || event.taskId}` : null;
}
function processCapability(event, agents = []) {
  const id = event.tool || event.action || event.skill_id;
  const card = agents.find(item => item.unit_id === (event.target_unit_id || event.unit_id || event.agent));
  const skill = card?.skills?.find(item => item.id === event.skill_id || item.id === id);
  return event.capability_name || skill?.name || agentCapabilityLabel(id);
}
function processArguments(event, agents = []) {
  const args = event.arguments || {};
  const fields = [];
  const card = agents.find(item => item.unit_id === (event.target_unit_id || event.unit_id || event.agent));
  const skill = card?.skills?.find(item => item.id === event.skill_id || item.id === (event.tool || event.action));
  const schema = skill?.params_schema?.properties || {};
  const rgb = args.color && typeof args.color === 'object' ? args.color : args;
  if ([rgb.r,rgb.g,rgb.b].every(n => Number.isInteger(n) && n >= 0 && n <= 255))
    fields.push({label:'颜色', value:`${activityLightColor(rgb) || '黑色'} · RGB ${rgb.r}, ${rgb.g}, ${rgb.b}`,
      color:`rgb(${rgb.r},${rgb.g},${rgb.b})`});
  if (typeof args.brightness === 'number') fields.push({label:'亮度',value:`${args.brightness}/255`});
  const labels = {state:'灯光',cmd:'动作',duration:'时长',duration_s:'时长',distance_m:'距离',angle_deg:'角度',
    speed:'速度',count:'次数',interval_ms:'间隔',target:'目标',object_name:'目标',query:'查找',mode:'模式'};
  const values = {ON:'常亮',OFF:'熄灭',BLINKING:'闪烁',StandDown:'趴下',StandUp:'站起',Hello:'打招呼',
    StopMove:'停止移动',Stretch:'伸展',Sit:'坐下'};
  Object.entries(args).forEach(([key,value]) => {
    if (['r','g','b','brightness','message','content','rule','text'].includes(key) || !['number','string','boolean'].includes(typeof value)) return;
    const unit = key.startsWith('duration') ? ' 秒' : key === 'distance_m' ? ' 米' : key === 'angle_deg' ? '°' : key === 'interval_ms' ? ' ms' : '';
    fields.push({label:schema[key]?.title || labels[key] || key,value:(values[value] || String(value).slice(0,160)) + unit});
  });
  return fields;
}
function processOutcome(event) {
  return ({ok:'已完成',accepted:'已完成',executing:'执行中',error:'执行失败',blocked:'未执行',rejected:'请求被拒绝',
    timeout:'结果待确认',unknown:'结果待确认',cancelled:'已取消',needs_clarification:'等待你补充信息'})[event.status]
    || ({decision:'已选择',dispatching:'等待反馈',observed:'收到反馈',planning:'整理上下文',
      considering:'核对反馈',finished:'处理结束',narrated:'生成回复'})[event.stage] || '';
}
function processNeedsAttention(event) {
  return ['error','blocked','rejected','timeout','unknown','needs_clarification'].includes(event.status)
    || activityEventKind(event) === 'question';
}
function processStepTitle(event) {
  const kind = activityEventKind(event);
  if (activityMediaUrl(event) || kind === 'observation') return '观察';
  if (kind === 'question') return '提出问题';
  if (kind === 'message') return '发出消息';
  return ({planning:'理解请求',decision:'选择行动',dispatching:'发起行动',observed:'收到反馈',
    considering:'核对反馈',finished:'结束处理',narrated:'生成回复',
    search_observing:'观察周围',search_selected:'选择观察点'})[event.stage] || '处理请求';
}
function processSteps(events) {
  const steps = [], keyed = new Map(), seen = new Set();
  events.forEach((event,index) => {
    const identity = event.event_id || event.eventId;
    if (identity && seen.has(identity)) return;
    if (identity) seen.add(identity);
    const unit = event.unit_id || event.agent || '';
    // A camera frame is an observation in its own right, even within one tool call.
    const media = activityMediaUrl(event);
    const key = media ? `photo:${media}` : event.step_id
      ? `step:${unit}:${event.run_id || ''}:${event.step_id}`
      : (event.task_id || event.taskId) && (event.action || event.tool)
        ? `task:${unit}:${event.task_id || event.taskId}:${event.action || event.tool}` : null;
    let step = key && keyed.get(key);
    if (step) {
      step.events.push(event);
      const terminal = ['ok','accepted','error','blocked','rejected','cancelled'];
      const waiting = ['timeout','unknown'];
      const hasResult = terminal.includes(step.event.status) || waiting.includes(step.event.status);
      const regression = hasResult && (!event.status || event.status === 'executing'
        || terminal.includes(step.event.status) && waiting.includes(event.status));
      if (regression) return;
      step.event = {...step.event,...event,arguments:{...step.event.arguments,...event.arguments},
        decision_basis:event.decision_basis || step.event.decision_basis};
    } else {
      step = {key:key || identity || `record:${index}`,event,events:[event]}; steps.push(step);
      if (key) keyed.set(key,step);
    }
  });
  return steps;
}
function activityProcessRows(events) {
  const rows = [], groups = new Map();
  events.filter(event => event.display !== false && event.event_type !== 'started').forEach((event,index) => {
    const kind = activityEventKind(event);
    const isSpeech = ['message','question'].includes(kind);
    const result = Boolean(event.game_round);
    const lifecycle = ['device_joined','device_left','ended'].includes(event.event_type);
    if (event.event_type === 'response' && event.status === 'ok') return;
    if (isSpeech || lifecycle || result) { rows.push({key:event.event_id || `event:${index}`,event,kind:result ? 'result' : isSpeech ? kind : 'status',process:[]}); return; }
    const key = processGroupKey(event) || `record:${event.event_id || index}`;
    let group = groups.get(key);
    if (!group) { group = {key,event,kind:'process',process:[]}; groups.set(key,group); rows.push(group); }
    group.process.push(event);
  });
  // Attach a process once to the final explicit same-run/task speech. A shared
  // invitation is context, never proof that every reply owns every game action.
  groups.forEach(group => {
    const owner = [...rows].reverse().find(row => ['message','question','result'].includes(row.kind)
      && processGroupKey(row.event) && processGroupKey(row.event) === processGroupKey(group.event));
    if (owner) { owner.process.push(...group.process); group.attached = true; }
  });
  return rows.filter(row => !row.attached);
}
Object.assign(window,{processRecord,processGroupKey,processCapability,processArguments,processOutcome,
  processNeedsAttention,processStepTitle,processSteps,activityProcessRows});
