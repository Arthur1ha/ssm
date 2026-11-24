/* Presentation only: grouping requires run/step/task identities, never proximity. */
function processRecord(event = {}) {
  const keys = ['event_id', 'source_event_id', 'event_type', 'agent_id', 'run_id', 'parent_run_id', 'step_id', 'task_id', 'trace_id',
    'sequence', 'model_turn', 'tool_call_id', 'fact_kind', 'result_kind', 'public_result', 'dispatch_only', 'observation_event_id', 'observation_id', 'transition_id', 'device_epoch', 'frame_id', 'as_of', 'conversation_id', 'reply_to', 'teaching_id', 'version', 'name', 'step_number', 'tool', 'action', 'target_unit_id', 'unit_id', 'skill_id', 'capability_name', 'arguments',
    'request_id','activity_id','question_id','observation','observation_summary','training_ref','revision','training_revision','step_index','reply_source','requested_text', 'result_state', 'status', 'reason', 'stage', 'ts', 'media_ref', 'media_url',
    'decision_basis', 'state_at_event', 'trigger_message_id', 'message_id', 'game_round', 'game_id'];
  return Object.fromEntries(keys.filter(key => event[key] !== undefined).map(key => [key, event[key]]));
}
function processGroupKey(event) {
  const unit = event.agent_id || event.agent || event.unit_id || '';
  return event.run_id ? `run:${unit}:${event.run_id}` : event.task_id || event.taskId
    ? `task:${unit}:${event.task_id || event.taskId}` : null;
}
function processCapability(event, agents = []) {
  const id = event.tool || event.action || event.skill_id;
  const card = agents.find(item => item.unit_id === (event.target_unit_id || event.unit_id || event.agent));
  const skill = card?.skills?.find(item => item.id === event.skill_id || item.id === id);
  return processUserText(publicCapabilityText(event.capability_name || skill?.name || agentCapabilityLabel(id))) || '设备操作';
}
function processFactTitle(event, agents = []) {
  if (event.result_kind === 'training' && event.public_result?.training_run_finished)
    return processUserText(event.name) || '规则执行结果';
  const id = event.skill_id || event.tool || event.action;
  if (id === 'go2_push_cargo') {
    const title = {assess:'观察货物与目的地',approach:'接近货物',push:'推送货物'}[event.arguments?.operation];
    if (title) return title;
  }
  if (id) return processCapability(event,agents);
  if (event.public_result?.game_rules) return processUserText(event.public_result.game_rules.name) || '游戏规则';
  if (activityMediaUrl(event)) return '观察照片';
  if (activityDecisionBasis(event) || event.fact_kind === 'decision') return '';
  return event.fact_kind === 'observation' || event.observation ? '环境观察' : '执行反馈';
}
function processStripIds(value) {
  // Remove correlation fields, not the diagnostic sentence containing them.
  return value.replace(/["']?\b(?:(?:task|run|parent_run|step|trace|event|source_event|request|frame|conversation|teaching|tool_call|unit|agent|session|message)[_\s-]?id|id)["']?\s*[:=]\s*(?:"[^"]*"|'[^']*'|[^\s,;}\]]+)\s*[,;]?/gi, '')
    .replace(/\b(?:task|run|step|tr|evt|req|request|frame|chat|session)_[a-f0-9_-]{8,}\b/gi, '')
    .replace(/[ \t]{2,}/g, ' ').replace(/[ \t]*\n[ \t]*/g, '\n').trim();
}
function processErrorText(value) {
  if (typeof value !== 'string' || !value.trim()) return '';
  let text = value.trim();
  const known = activityReasonText({reason:text});
  if (known) return known;
  // Historical SDK payloads stay in the logs. Only readable prose is public.
  try {
    const parsed = JSON.parse(text);
    const detail = parsed?.error || parsed?.detail || parsed;
    text = typeof detail === 'string' ? detail : detail?.message || '';
  } catch (_) {}
  return /[\u3400-\u9fff]/.test(text) && processUserText(text) || '这一步暂时未完成。';
}
function processUserText(value) {
  if (typeof value !== 'string') return '';
  let text = publicCapabilityText(processStripIds(value.trim()));
  let diagnostic = false;
  text = text.replace(/\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}/g, payload => {
    if (!/["'](?:error|error_code|status_code|error_type)["']\s*:/.test(payload)) return payload;
    diagnostic = true;
    return '相关处理暂未完成';
  });
  text = text.replace(/\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b/gi, code => {
    const readable = activityReasonText({reason:code});
    if (!readable) diagnostic = true;
    return readable || '相关处理暂未完成';
  });
  const hadProse = /[\u3400-\u9fff]/.test(text.replace(/相关处理暂未完成/g,''));
  text = text.replace(/\b(?:[A-Z][A-Za-z0-9]*\.)+[A-Z][A-Za-z0-9]*\b|\b\w*(?:Error|Exception)\b|\b(?:HTTP(?:\/\d(?:\.\d)?)?\s*)?\d{3}\s+(?:Bad Request|Unauthorized|Forbidden|Not Found|Internal Server Error|Bad Gateway|Service Unavailable)\b|\bHTTP(?:\/\d(?:\.\d)?)?\s*\d{3}\b/g,
    () => {diagnostic=true;return '相关处理暂未完成';});
  // Public facts still contain correlation fields. None are display copy.
  if (diagnostic && !hadProse || /^[{\[]/.test(text) || /Error code:|Traceback|Free quota exhausted|model.*(?:does not exist|not found|unavailable)|provider.*(?:rejected|error)/i.test(text)
      || /\b(?:task|run|parent_run|step|trace|event|source_event|request|frame|conversation|teaching|tool_call|unit|agent)_id\b/i.test(text)
      || /\b(?:task|run|step|tr|evt|req|frame|chat)_[a-f0-9_-]{8,}\b/i.test(text)
      || /^[a-z]+(?:_[a-z0-9]+)+$/i.test(text)) return '';
  return text;
}
function processActionDetail(event, agents = []) {
  const args = event.arguments || {};
  const parts = [];
  const card = agents.find(item => item.unit_id === (event.target_unit_id || event.unit_id || event.agent));
  const skill = card?.skills?.find(item => item.id === event.skill_id || item.id === (event.tool || event.action));
  const rgb = args.color && typeof args.color === 'object' ? args.color : args;
  const hasColor = [rgb.r,rgb.g,rgb.b].every(n => Number.isInteger(n) && n >= 0 && n <= 255);
  if (hasColor) parts.push(`设为${activityLightColor(rgb) || '黑色'}`);
  if (Number.isInteger(args.brightness) && args.brightness >= 0 && args.brightness <= 255)
    parts.push(`亮度设为 ${Math.round(args.brightness / 255 * 100)}%`);
  if (args.state === 'OFF') parts.push('关闭灯光');
  else if (args.state === 'ON' && !hasColor) parts.push('打开灯光');
  const id = event.skill_id || event.tool || event.action;
  if (['blink_led','blink_light','BLINK'].includes(id)) {
    if (Number.isInteger(args.count) && args.count > 0) parts.push(`闪烁 ${args.count} 次`);
    else if (args.count === 0) parts.push('持续闪烁');
  }
  const target = processUserText(args.target || args.object_name || args.query || args.cargo);
  if (target) parts.push(`目标：${target}`);
  const destination = processUserText(args.destination);
  if (destination) parts.push(`目的地：${destination}`);
  const direction = {forward:'向前',backward:'向后',left:'向左',right:'向右',turn_left:'向左',turn_right:'向右'}[args.direction] || '';
  if (Number.isFinite(args.distance_m)) parts.push(`${direction}移动 ${args.distance_m} 米`);
  if (Number.isFinite(args.angle_deg)) parts.push(`${direction}转动 ${args.angle_deg}°`);
  const duration = args.duration_s ?? args.duration;
  if (Number.isFinite(duration)) parts.push(`持续 ${duration} 秒`);
  const command = {StandDown:'趴下',StandUp:'站起',Hello:'打招呼',StopMove:'停止移动',
    Stretch:'伸展',Sit:'坐下',RiseSit:'从坐姿站起',Content:'表达开心',FingerHeart:'比心',
    Dance1:'跳舞',Dance2:'跳舞'}[args.cmd];
  const declaredCommand = skill?.params_schema?.properties?.cmd?.oneOf?.find(item => item.const === args.cmd)?.title;
  if (command || processUserText(declaredCommand)) parts.push(command || processUserText(declaredCommand));
  const mode = {reactive:'自主调光',manual:'手动模式',free_explore:'自由探索'}[args.mode];
  if (mode) parts.push(`切换为${mode}`);
  return {text:parts.join('，'),color:hasColor ? `rgb(${rgb.r},${rgb.g},${rgb.b})` : ''};
}
function processMembers(result) {
  const value = result.activity?.members || result.activity?.devices || result.members
    || result.member_results || result.join_results || result.stop_results;
  const members = Array.isArray(value) ? value : value && typeof value === 'object'
    ? Object.entries(value).map(([unit_id,item]) => ({unit_id,...item})) : [];
  return members.filter(item => item && typeof item === 'object');
}
function processReceiptNeedsAttention(result, depth = 0) {
  if (!result || typeof result !== 'object' || depth > 3) return false;
  if (['partial','error','failed','blocked','rejected','timeout','unknown','offline','unavailable'].includes(result.result || result.status)
      || ['ok','joined','confirmed','stop_confirmed'].some(key => result[key] === false)
      || result.failed === true || result.physical_stop === 'unknown') return true;
  return processMembers(result).some(item => processReceiptNeedsAttention(item,depth+1))
    || (Array.isArray(result.steps) ? result.steps : []).some(item => processReceiptNeedsAttention(item.result,depth+1));
}
function processOutcome(event) {
  if (event.result_kind === 'training') {
    const result = event.public_result || {};
    if (result.training_run_finished && Number.isInteger(result.step_count)
      && Number.isInteger(result.total_steps) && result.step_count > 0 && result.step_count < result.total_steps)
      return ['accepted','executing','timeout','unknown'].includes(event.status) ? '部分完成 · 结果待确认' : '部分完成';
    if (!result.training_run_finished && ['ok','accepted'].includes(event.status))
      return result.skipped ? '目标已满足' : '本步完成';
  }
  if (event.tool === 'send_message' && ['rejected','blocked','error','failed'].includes(event.status))
    return '消息未发出';
  if (event.result_kind === 'activity' && ['ok','accepted'].includes(event.status)
      && processReceiptNeedsAttention(event.public_result)) return '部分完成';
  if (event.dispatch_only && ['ok','accepted'].includes(event.status)) return '已提交';
  if (event.result_kind === 'message' && event.status === 'accepted')
    return event.public_result?.delivery_status === 'accepted' ? '已接受' : '已发出';
  if (event.result_kind === 'message' && event.status === 'ok') return '已处理消息';
  if (event.result_kind === 'message' && event.status === 'received') return '消息已送达';
  return ({ok:'已完成',accepted:'已完成',executing:'执行中',partial:'部分完成',error:'本次未完成',failed:'本次未完成',blocked:'本次未执行',rejected:'本次未执行',offline:'设备离线',unavailable:'暂不可用',
    timeout:'结果待确认',unknown:'结果待确认',cancelled:'已取消',needs_clarification:'等待你补充信息'})[event.status] || '';
}
function processNeedsAttention(event) {
  return ['partial','error','failed','blocked','rejected','timeout','unknown','offline','unavailable','needs_clarification'].includes(event.status)
    || activityEventKind(event) === 'question' || processReceiptNeedsAttention(event.public_result);
}
function processPendingLabel(event = {}) {
  if (['planning','considering'].includes(event.stage)) return '正在规划下一步';
  if (event.stage === 'summarizing') return '正在整理结果';
  if (event.tool || event.action || event.skill_id) {
    const title = processFactTitle(event);
    return `正在${title}`;
  }
  return '等待回复';
}
function processHasContent(event) {
  if (event.stage === 'narrated') return false;
  return Boolean(event.tool || event.action || event.skill_id || activityMediaUrl(event)
    || processUserText(activityDecisionBasis(event)?.reason) || processResultNotes(event).length);
}
function mergeProcessFact(previous, incoming) {
  const terminal = ['ok','accepted','partial','error','blocked','rejected','cancelled'];
  const waiting = ['timeout','unknown'];
  const previousTerminal = terminal.includes(previous.status)
    && !(previous.status === 'accepted' && previous.dispatch_only);
  const hasResult = previousTerminal || waiting.includes(previous.status);
  const pending = ['planning','planned','decision','dispatching','executing','considering','summarizing','queued','pending','ack'];
  const oldSequence = Number(previous.sequence), nextSequence = Number(incoming.sequence);
  const older = Number.isFinite(oldSequence) && Number.isFinite(nextSequence) && nextSequence < oldSequence;
  const regression = hasResult && (!incoming.status || pending.includes(incoming.status)
    || previousTerminal && waiting.includes(incoming.status));
  const merged = {...previous};
  Object.entries(incoming).forEach(([key,value]) => {
    if (value == null || value === '' || Array.isArray(value) && !value.length) return;
    if ((older || regression) && ['status','stage','fact_kind','text','reason','ts','sequence','public_result','state_at_event'].includes(key)) return;
    if (value && typeof value === 'object' && !Array.isArray(value)) {
      merged[key] = mergeProcessFact(previous[key] || {}, value);
    } else if (!older || previous[key] == null || previous[key] === '') merged[key] = value;
  });
  if (!older && !regression && previous.dispatch_only && incoming.dispatch_only !== true
      && (incoming.fact_kind === 'result' || ['executing','ok'].includes(incoming.status)))
    merged.dispatch_only=false;
  return merged;
}
function processSteps(events) {
  const steps = [], keyed = new Map();
  events.filter(processHasContent).forEach((event,index) => {
    const identity = event.source_event_id || event.event_id || event.eventId;
    const actor = event.agent_id || event.agent || event.unit_id || '';
    const media = activityMediaUrl(event);
    const call = event.step_id || event.tool_call_id;
    const key = media ? `photo:${actor}:${event.run_id || event.task_id || ''}:${event.observation_event_id || event.frame_id || identity || index}` : call
      ? `step:${actor}:${event.run_id || ''}:${call}`
      : (event.task_id || event.taskId) && (event.action || event.tool)
        ? `task:${actor}:${event.task_id || event.taskId}:${event.action || event.tool}` : identity;
    let step = key && keyed.get(key);
    if (step) {
      step.events.push(event);
      step.event = mergeProcessFact(step.event,event);
    } else {
      step = {key:key || `record:${index}`,event:{...event},events:[event]}; steps.push(step);
      if (key) keyed.set(key,step);
    }
  });
  return steps.sort((a,b) => a.event.run_id && a.event.run_id === b.event.run_id
    && a.event.sequence != null && b.event.sequence != null
      ? Number(a.events[0].sequence) - Number(b.events[0].sequence) : 0);
}
function processResultNotes(event) {
  const result = event.public_result || {};
  const notes = [];
  const failed = processNeedsAttention(event);
  const add = (text, attention = false, kind = 'feedback') => {
    const clean = attention ? processErrorText(text) : processUserText(text);
    if (clean && !notes.some(note => note.text === clean)) notes.push({text:clean,attention,kind});
  };
  const reasonText = record => {
    const reason = record.reason || record.error || '';
    return activityReasonText({reason}) || (failed || processReceiptNeedsAttention(record) ? processErrorText(reason) : '');
  };
  const members = processMembers(result);
  members.forEach(member => {
    const name = getAgentDisplayName(member.unit_id || member.agent_id);
    const mode = {reactive:'自主调光',free_explore:'自由探索'}[member.mode];
    const state = member.status || member.result;
    const text = {joined:`${name}已加入${mode ? `，进入${mode}` : ''}。`,
      offline:`${name}当前离线，未能加入活动。`,unavailable:`${name}暂时无法加入活动。`,
      failed:`${name}未能加入活动。`,rejected:`${name}未能加入活动。`,
      left:`${name}已退出活动。`,unknown:`${name}的参与状态尚未确认。`}[state];
    const reason = reasonText(member);
    add(text || `${name}的参与结果尚未确认。`,processReceiptNeedsAttention(member));
    if (reason && state !== 'offline') add(`${name}：${reason}`,true);
    if (member.physical_stop === 'unknown' || member.stop_confirmed === false)
      add(`${name}的停止结果尚未确认。`,true);
  });
  const observation = event.observation || {};
  const summary = observation.scene_summary || event.observation_summary;
  if (summary) add(summary,false,'observation');
  if (observation.found === false) add('这张画面中未发现目标。',false,'observation');
  else if (observation.found === true && !summary) add('已在画面中看到目标。',false,'observation');
  if (observation.proximity_reason) add(observation.proximity_reason,false,'observation');
  if (!summary && Object.prototype.hasOwnProperty.call(observation,'found') && observation.found == null)
    add('这张画面尚不能确认目标。',false,'observation');
  if (event.result_kind === 'query' || event.result_kind === 'search') {
    if (typeof result.connected === 'boolean') add(result.connected ? '设备当前已连接。' : '设备当前未连接。');
    const light = {ON:'常亮',OFF:'熄灭',BLINKING:'闪烁'}[result.ism_state];
    if (light) add(`灯光当前为${light}。`);
    const posture = {standing:'站立',sitting:'坐下',lying:'趴卧'}[result.body_state];
    if (posture) add(`当前观测到的姿态是${posture}。`);
    if (typeof result.battery_soc === 'number') add(`剩余电量 ${result.battery_soc}%。`);
    if (typeof result.found === 'boolean' && !Object.prototype.hasOwnProperty.call(observation,'found'))
      add(result.found ? '本次已发现目标。' : '本次未发现目标。',false,'observation');
  }
  const candidateName = processUserText(result.candidate?.name || result.candidate?.label);
  if (candidateName) add(`选定下一处观察位置：${candidateName}`);
  (Array.isArray(result.observations) ? result.observations : []).slice(0,16).forEach(item => {
    const values = item.values || {};
    if (item.status === 'stale') {add('这条环境读数已过期。',false,'observation');return;}
    const level = {DARK:'环境光较暗。',DIM:'环境光偏暗。',NORMAL:'环境光正常。',BRIGHT:'环境光较亮。'}[values.level];
    if (level) add(level,false,'observation');
    if (typeof values.detected === 'boolean') add(values.detected ? '传感器检测到信号。' : '传感器未检测到信号。',false,'observation');
  });
  if (Array.isArray(result.agents)) result.agents.slice(0,16).forEach(agent => {
    add(`${processUserText(agent.name) || getAgentDisplayName(agent.unit_id)}${agent.online ? '在线' : '离线'}。`);
  });
  if (event.result_kind === 'training' || event.result_kind === 'training_proposal') {
    const name = processUserText(event.name || result.name);
    const step = event.step_number ?? result.step_number
      ?? (Number.isInteger(result.step_index) ? result.step_index+1 : null);
    if (name || step != null) add([name ? `「${name}」` : '当前教导',step != null ? `第 ${step} 步` : ''].filter(Boolean).join(' · '));
    if (result.saved === true) add('教导已保存。');
    if (result.loaded === true) add('设备已加载这份教导。');
    else if (result.loaded === false) add('设备尚未确认加载这份教导。',true);
    if (result.skipped === true) add('当前已满足目标，这一步无需重复执行。');
    if (result.training_run_finished && Number.isInteger(result.step_count)
      && Number.isInteger(result.total_steps) && result.total_steps > 0)
      add(`本次完成 ${result.step_count}/${result.total_steps} 步。`);
  }
  const rules = result.game_rules;
  if (rules && typeof rules === 'object') {
    // Historical events may still contain full static game instructions.
    // Show only actual agreements and public mappings from this game.
    const mapping = Array.isArray(rules.mapping) ? rules.mapping : [];
    const state = {proposed:'玩法等待双方确认。',confirmed:'双方已确认玩法。',revealing:'正在揭晓本轮选择。'}[rules.status];
    if (state && (rules.status !== 'proposed' || mapping.length)) add(state,false,'rule');
    const choices = {rock:'石头',paper:'布',scissors:'剪刀'};
    mapping.slice(0,8).forEach(item => {
      const choice = choices[item.choice], label = processUserText(item.label);
      if (choice && label) add(`${choice}用${label}表示。`,false,'rule');
    });
  }
  const reason = reasonText(event) || reasonText(result);
  const message = failed ? processErrorText(result.message) : processUserText(result.message);
  const speech = ['message','question'].includes(activityEventKind(event));
  const text = speech ? '' : processUserText(event.text);
  if (message) add(message,failed);
  if (reason && !message.startsWith(reason) && reason !== summary) add(reason,failed);
  if (text && !message.startsWith(text) && text !== summary && text !== reason && text !== activityDecisionBasis(event)?.reason) add(text,failed);
  if (['timeout','unknown'].includes(event.status) && !reason && !message)
    add('尚未收到设备的执行确认，目前无法判断是否完成。',true);
  else if (failed && !members.length && !reason && !message && !text)
    add(event.status === 'needs_clarification' ? '需要你补充信息后才能继续。' : '这一步暂时未完成。',true);
  const receiptStatus = result.result || result.status;
  if (!members.length && event.result_kind === 'message') {
    if (result.delivery_status === 'received') add('消息已送达，等待对方回应。');
    else if (result.delivery_status === 'accepted') add('对方已接受请求，执行结果仍待反馈。');
    else if (receiptStatus === 'ok' || event.status === 'ok') add('已处理这条消息。');
    else if (receiptStatus === 'accepted') add('消息已发出，等待对方回应。');
  } else if (!members.length && !rules && !message && !reason && !failed
      && result.dispatch_only && ['ok','accepted'].includes(receiptStatus)) {
    add('请求已提交，执行结果仍待确认。');
  }
  (Array.isArray(result.steps) ? result.steps : []).slice(0,16).forEach(step => {
    const receipt = step.result || {};
    if (processReceiptNeedsAttention(receipt))
      add(reasonText(receipt) || '其中一步尚未完成。',true);
  });
  return notes;
}
function activityProcessRows(events) {
  const rows = [], groups = new Map();
  let section = 0;
  events.filter(event => event.display !== false && event.event_type !== 'started' && event.stage !== 'narrated').forEach((event,index) => {
    if (activityGameStart(event)) {
      rows.push({key:event.event_id || `event:${index}`,event,kind:'game_start',process:[],section});
      section++;
      return;
    }
    if (activityGameEnd(event)) {
      rows.push({key:event.event_id || `event:${index}`,event,kind:'game_end',process:[],section});
      section++;
      return;
    }
    const kind = activityEventKind(event);
    const isSpeech = ['message','question'].includes(kind);
    const result = Boolean(event.game_round);
    const lifecycle = ['device_joined','device_left','ended'].includes(event.event_type);
    if (event.event_type === 'response' && (event.status === 'ok'
      || event.result_kind === 'message' && ['received','accepted'].includes(event.status))) return;
    if (isSpeech || lifecycle || result) { rows.push({key:event.event_id || `event:${index}`,event,kind:result ? 'result' : isSpeech ? kind : 'status',process:[],section}); return; }
    if (!processHasContent(event)) return;
    const key = `${section}:${processGroupKey(event) || `record:${event.event_id || index}`}`;
    let group = groups.get(key);
    if (!group) { group = {key,event,kind:'process',process:[],section}; groups.set(key,group); rows.push(group); }
    group.process.push(event);
  });
  // Attach a process once to the final explicit same-run/task speech. A shared
  // invitation is context, never proof that every reply owns every game action.
  groups.forEach(group => {
    const owner = [...rows].reverse().find(row => ['message','question','result'].includes(row.kind)
      && row.section === group.section
      && processGroupKey(row.event) && processGroupKey(row.event) === processGroupKey(group.event));
    if (owner) { owner.process.push(...group.process); group.attached = true; }
  });
  return rows.filter(row => !row.attached);
}
Object.assign(window,{processRecord,processGroupKey,processCapability,processFactTitle,processUserText,processErrorText,processActionDetail,processOutcome,
  processNeedsAttention,processPendingLabel,processHasContent,mergeProcessFact,processResultNotes,processSteps,activityProcessRows});
