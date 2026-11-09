/* Activity presentation reads device feedback and explicit message/task links.
 * Neighbouring timestamps and conversational claims never establish causality. */
function activityStateAge(state, now = Date.now() / 1000) {
  const ts = Number(state?._received_at ?? state?.ts);
  return Number.isFinite(ts) && ts > 0 && ts <= now + 5 ? Math.max(0, now - ts) : null;
}

function activityLightColor(state) {
  if (![state.r, state.g, state.b].every(n => Number.isInteger(n) && n >= 0 && n <= 255)) return '';
  const {h, s, v} = ssmColor.rgbToHsv(state.r, state.g, state.b);
  if (v === 0) return '';
  if (s < .16) return '白光';
  if (s < .72 && h >= 20 && h <= 65) return '暖白';
  if (h < 18 || h >= 345) return '红色';
  if (h < 45) return '橙色';
  if (h < 70) return '黄色';
  if (h < 165) return '绿色';
  if (h < 200) return '青色';
  if (h < 265) return '蓝色';
  if (h < 300) return '紫色';
  return '粉色';
}

function activityPhysicalLabel(card, state = {}) {
  if (agentKind(card || {}) === 'led') {
    const label = getLedStateInfo(state).label;
    return [label, state.ism !== 'OFF' ? activityLightColor(state) : ''].filter(Boolean).join(' · ');
  }
  const machine = card?.state_machine;
  if (!machine) return '状态待确认';
  const regions = stateMachineRegions(machine);
  return regions.filter(region => region.id !== 'connection'
    && region.state_field !== 'connection_state' && stateRegionActive(region, regions, state)
    && state[region.state_field] != null
    && !(region.id === 'activity' && state[region.state_field] === 'active'))
    .map(region => region.id === 'body' && (state.body_confirmation === 'unknown' || state.body_telemetry_fresh === false)
      ? '身体待确认' : stateRegionLabel(region, state))
    .join(' · ') || '状态待确认';
}

function activityMemberView(card, state, membership, connected, now) {
  const age = activityStateAge(state, now);
  const online = card ? (card._online ?? card.online) : null;
  const disconnected = online === false || state.connected === false || state.connection_state === 'offline';
  // A lamp retains its reported setting until another state arrives. Its edge
  // publishes changes, not a continuous physical-state heartbeat like Go2.
  const knownLamp = agentKind(card || {}) === 'led' && Boolean(state.ism);
  const stale = !knownLamp && (!connected || age === null || age > 60);
  const label = activityPhysicalLabel(card, state);
  const join = {joined:'自主活动中',offline:'未加入 · 设备离线',unavailable:'暂未加入',
    unknown:'参与状态待确认',left:'已退出活动'}[membership?.status] || '正在确认参与情况';
  const mode = membership?.status === 'joined' && state.autonomy_mode === 'manual' ? '仅听指令'
    : membership?.status === 'joined' && state.control_state === 'holding' ? '暂时保留你的设置' : join;
  const brightness = state.ism && state.ism !== 'OFF' && Number.isInteger(state.brightness)
    && state.brightness >= 0 && state.brightness <= 255
    ? `亮度${state.brightness <= 85 ? '偏暗' : state.brightness <= 170 ? '适中' : '偏亮'}` : '';
  return { label: disconnected ? '离线' : stale ? '状态待确认' : label,
    detail: !connected ? '状态连接正在恢复' : disconnected ? join
      : stale ? age !== null ? `最近反馈 · ${age < 120 ? '1 分钟前' : `${Math.floor(age / 60)} 分钟前`}`
        : '尚未收到状态反馈' : [mode, brightness].filter(Boolean).join(' · '),
    previous: stale && age !== null && !disconnected ? label : '',
    color: !disconnected && !stale && state.ism !== 'OFF' && agentKind(card || {}) === 'led'
      ? getLedStateInfo(state).color : null,
    stale, disconnected };
}

function activityTaskKey(event) {
  // A run may contain multiple different actions; a shared task alone is not a step ID.
  return event.task_id ? JSON.stringify([event.unit_id, event.task_id, event.action || '']) : null;
}

function isActivityLightingAction(action) {
  return ['set_led_state', 'set_led_color', 'blink_led'].includes(action);
}

function activityTaskRows(events) {
  const rows = [], tasks = new Map();
  for (const event of events) {
    if (!['action', 'action_progress', 'action_result'].includes(event.event_type)) {
      rows.push(event); continue;
    }
    const key = activityTaskKey(event);
    if (!key) { rows.push(event); continue; }
    const previous = tasks.get(key);
    if (previous?.event_type === 'action_result' && event.event_type === 'action_progress') continue;
    // Each lamp task identifies one physical dispatch. Graph, game and peer
    // receipts for that task are confirmations of the same operation. Go2 may
    // reuse a whole goal's task for multiple actual observations or actions.
    if (previous && (!(previous.event_type === 'action_result' && event.event_type === 'action_result')
      || isActivityLightingAction(event.action) && (previous.status === event.status
        || ['accepted', 'executing', 'timeout', 'unknown'].includes(previous.status)))) {
      const index = rows.indexOf(previous);
      const merged = {...previous, ...event,
        trigger_message_id:event.trigger_message_id || previous.trigger_message_id,
        decision_basis:event.decision_basis || previous.decision_basis};
      rows[index] = merged;
      tasks.set(key, merged);
    } else {
      rows.push(event);
      tasks.set(key, event);
    }
  }
  return rows;
}

function activityRelatedEvents(event, events) {
  const ids = new Set([event.message_id, event.reply_to, event.trigger_message_id].filter(Boolean));
  const linkedTasks = new Set([...(event.related_task_ids || []), ...(event.task_id ? [event.task_id] : [])]);
  if (!event.task_id) events.filter(item => item.event_type !== 'message'
    && (ids.has(item.message_id) || ids.has(item.trigger_message_id)))
    .forEach(item => { if (item.task_id) linkedTasks.add(item.task_id); });
  const task = events.filter(item => linkedTasks.has(item.task_id)
    && (event.event_type === 'message' || item.unit_id === event.unit_id)
    && (event.related_task_ids?.length || !event.action || !item.action || item.action === event.action));
  task.forEach(item => [item.message_id, item.reply_to, item.trigger_message_id].filter(Boolean)
    .forEach(id => ids.add(id)));
  const messages = events.filter(item => item.event_type === 'message'
    && (ids.has(item.message_id) || item.reply_to && ids.has(item.reply_to)));
  return { task, messages };
}

function activityMessageReceipt(event, events) {
  if (event.reply_kind === 'accepted') return '已接受邀请';
  if (event.reply_kind === 'declined') return '未接受邀请';
  if (event.reply_to || !event.message_id || !event.target_unit_id) return '';
  const replies = events.filter(item => item.reply_to === event.message_id);
  if (replies.some(item => item.reply_kind === 'accepted')) return '对方已接受';
  if (replies.some(item => item.reply_kind === 'declined')) return '对方未接受';
  if (replies.length) return '已收到回应';
  return event.interaction_kind === 'game_invite' ? '等待对方决定' : '等待回应';
}

function activityFeedbackChange(event, events, card) {
  if (event.event_type !== 'action_result' || event.status !== 'ok') return '';
  const after = event.state_at_event;
  const before = [...events].reverse().find(item => item.event_type === 'action' && item.unit_id === event.unit_id
    && item.task_id && item.task_id === event.task_id && item.action === event.action
    && item.ts <= event.ts)?.state_at_event;
  if (before?.status !== 'current' || after?.status !== 'current' || after.as_of < before.as_of) return '';
  const from = activityPhysicalLabel(card, before.values), to = activityPhysicalLabel(card, after.values);
  return from !== to && ![from, to].some(label => label.includes('待确认') || label.includes('待同步'))
    ? `设备反馈：${from} → ${to}` : '';
}

function activityReasonText(event) {
  const reason = event.reason || '';
  const labels = {
    stop_unconfirmed:'停止结果尚未确认', autonomy_stopped:'自主活动已结束',
    activity_ended:'活动已结束', user_intervention:'用户发出了新的安排',
    device_offline:'设备离线', body_clearance_unavailable:'尚未确认周围有足够的安全空间',
    edge_result_unconfirmed:'尚未收到设备的完成反馈',
    question_color_changed_before_arrival:'灯的颜色发生变化，本次目标需要重新确认',
    lighting_not_eligible:'当前条件不允许自主调灯',
  };
  if (labels[reason]) return labels[reason];
  return /[\u3400-\u9fff]/.test(reason) ? reason : '';
}

function activityDecisionBasis(event) {
  const basis = event?.decision_basis;
  if (!basis || typeof basis.reason !== 'string' || !basis.reason.trim()) return null;
  return {reason:basis.reason.trim(), evidence:Array.isArray(basis.evidence)
    ? basis.evidence.filter(item => item && typeof item.summary === 'string' && item.summary.trim()) : []};
}

function activityDecisionText(event, text) {
  const original = String(text || '').trim();
  const reason = activityDecisionBasis(event)?.reason;
  return reason && original !== reason ? [reason, original].filter(Boolean).join('\n') : original || reason || '';
}

function activityDecisionEvidence(item, event, events, now = Date.now() / 1000) {
  const labels = {observation:'环境观察', peer_state:'同伴状态', environment:'环境反馈',
    activity:'活动情境', constraint:'用户要求', action_result:'动作反馈'};
  const recordedAt = Number(item.as_of);
  const hasTime = Number.isFinite(recordedAt) && recordedAt > 0;
  const maxAge = item.kind === 'observation' ? 600
    : ['peer_state', 'environment'].includes(item.kind) ? 60 : null;
  const atDecision = Number(event.ts);
  const expiredThen = item.status === 'stale' || maxAge !== null && hasTime
    && Number.isFinite(atDecision) && atDecision - recordedAt > maxAge;
  const expiredNow = maxAge !== null && hasTime && now - recordedAt > maxAge;
  const ref = String(item.ref || '').replace(/^event:/, '');
  const original = item.kind === 'observation' ? events.find(row => row.event_id === ref) : null;
  return {label:labels[item.kind] || '参考信息', source:item.source || original?.unit_id || '',
    recordedAt:hasTime ? recordedAt : null, summary:item.summary,
    notice:item.status === 'unknown' ? '这条依据在决策时尚未确认，不能据此陈述事实'
      : expiredThen ? '这条依据在决策时已过期，不能据此确认当时情况'
      : expiredNow ? '这是当时参考的记录，当前情况需要重新确认'
      : !hasTime ? '记录时间未提供，无法确认信息新鲜度' : '这是决策时参考的记录',
    missingOriginal:item.kind === 'observation' && !original};
}

Object.assign(window, {activityStateAge, activityLightColor, activityPhysicalLabel, activityMemberView,
  activityTaskKey, activityTaskRows, activityRelatedEvents, activityMessageReceipt, activityReasonText,
  activityDecisionBasis, activityDecisionText, activityDecisionEvidence, isActivityLightingAction});
window.activityFeedbackChange = activityFeedbackChange;

function activityMediaUrl(event) {
  const candidate = event.media_ref || event.media_url;
  const path = typeof candidate === 'string' ? candidate : candidate?.url;
  if (typeof path !== 'string' || !path.startsWith('/api/')) return '';
  return path;
}
