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
    unknown:'参与状态待确认',left:'已退出活动'}[membership?.status] || '';
  const mode = membership?.status === 'joined' && state.autonomy_mode === 'manual' ? '仅听指令'
    : membership?.status === 'joined' && state.control_state === 'holding' ? '暂时保留你的设置' : join;
  const brightness = state.ism && state.ism !== 'OFF' && Number.isInteger(state.brightness)
    && state.brightness >= 0 && state.brightness <= 255
    ? `亮度${state.brightness <= 85 ? '偏暗' : state.brightness <= 170 ? '适中' : '偏亮'}` : '';
  const notice = !connected ? '状态连接正在恢复' : disconnected ? join
    : stale ? age !== null ? `最近反馈 · ${age < 120 ? '1 分钟前' : `${Math.floor(age / 60)} 分钟前`}`
      : '尚未收到状态反馈' : '';
  return { label: disconnected ? '离线' : stale ? '状态待确认' : label,
    detail: notice || [membership?.status !== 'joined' || mode !== join ? mode : '', brightness].filter(Boolean).join(' · '),
    previous: stale && age !== null && !disconnected ? label : '',
    color: !disconnected && !stale && state.ism !== 'OFF' && agentKind(card || {}) === 'led'
      ? getLedStateInfo(state).color : null,
    stale, disconnected };
}

function isActivityLightingAction(action) {
  return ['set_led_state','set_led_color','blink_led'].includes(action);
}

function activityMessageReceipt(event, events) {
  if (event.reply_kind === 'accepted') return '已接受邀请';
  if (event.reply_kind === 'declined') return '未接受邀请';
  if (event.reply_to || !event.message_id || !event.target_unit_id) return '';
  const replies = events.filter(item => item.reply_to === event.message_id);
  if (replies.some(item => item.reply_kind === 'accepted')) return '对方已接受';
  if (replies.some(item => item.reply_kind === 'declined')) return '对方未接受';
  if (replies.length) return '已收到回应';
  const receipts = events.filter(item => item.message_id === event.message_id
    && item.event_type !== 'message' && item.result_kind === 'message');
  if (receipts.some(item => item.status === 'executing')) return '对方已开始处理';
  if (receipts.some(item => item.status === 'accepted')) return '对方已接受';
  if (receipts.some(item => item.status === 'received' || item.public_result?.delivery_status === 'received'))
    return '对方已收到';
  return event.interaction_kind === 'game_invite' ? '等待对方决定' : '等待回应';
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
    activity_message_denied:'补充消息未通过活动转达校验',
    localization_unavailable:'暂时未取得可用的位置数据',
    map_unavailable:'暂时未取得可用的导航地图',
    posture_not_confirmed:'暂时还不能确认站立姿态',
    motion_not_observed:'本次没有确认到实际移动',
    activity_already_started_for_intent:'活动已经启动，无需重复安排',
    run_deadline_exceeded:'本次处理时间已用尽',
    cargo_search_unconfirmed:'本次找货尚未完成',
    cargo_push_unconfirmed:'本次推送尚未确认完成',
    delivery_signal_changed:'约定的灯色或亮度发生变化，推送已停止',
    delivery_signal_unconfirmed:'无法继续确认约定的灯光信号，推送已停止',
    delivery_signal_changed_or_stale:'约定的灯光信号发生变化，或其状态被系统判为过期',
    delivery_observation_unavailable:'未能取得所需的现场画面，无法继续配送',
    game_expired:'本局任务的执行期限已到',
    autonomy_interrupted_or_offline:'设备离线或自主任务被中断',
    courier_stopped:'送货员决定结束本次配送',
    delivery_completion_unconfirmed:'未能确认本次配送完成',
    cargo_identity_unconfirmed:'未能继续确认是同一件货物',
    cargo_push_stop_unconfirmed:'推送后的停稳结果尚未确认',
    delivery_service_unavailable:'配送服务当前不可用',
    go2_activity_unavailable:'机器狗当前无法开始本局任务',
    delivery_unconfirmed:'本次配送结果尚未确认',
    lamp_busy:'灯正在处理其他任务',
    acceptance_unconfirmed:'尚未确认双方接受本局邀请',
    control_error:'运动执行中断，具体原因见动作回执',
    no_known_free_forward_path:'目标方向暂未确认有可通行的空间',
    turn_clearance_unavailable:'暂未确认有足够的转身空间',
    disconnected:'设备当前未连接',
    control_lease_changed:'设备控制权已交给其他操作',
    odom_session_mismatch:'位置数据所属的连接已变化，需要重新观察',
    odom_session_changed:'位置数据所属的连接已变化，需要重新观察',
    fresh_still_frame_unavailable:'暂未取得停稳后的新画面',
    pose_changed_during_detection:'观察期间位置发生变化，需要重新观察',
  };
  if (labels[reason]) return labels[reason];
  const cause = String(reason).split(':').pop().trim();
  if (labels[cause]) return labels[cause];
  return !/[{}\[\]]/.test(reason) && /[\u3400-\u9fff]/.test(reason)
    && !/\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b|\bHTTP(?:\/\d(?:\.\d)?)?\s*\d{3}\b|\b\d{3}\s+(?:Bad Request|Unauthorized|Forbidden|Not Found|Internal Server Error|Bad Gateway|Service Unavailable)\b|\b\w*(?:Error|Exception)\b/i.test(reason)
    && !/\b(?:[A-Z][A-Za-z0-9]*\.)+[A-Z][A-Za-z0-9]*\b/.test(reason) ? reason : '';
}

function activityGameEnd(event) {
  // Legacy game executors already wrote explicit terminal responses/settlements.
  // Never infer an ending from an action failure or conversational wording.
  if (!event.game_id || !['ok','blocked','rejected','cancelled','error','failed','timeout','unknown'].includes(event.status)) return null;
  if (event.event_type !== 'game_ended' && !(event.trigger_message_id &&
      (event.event_type === 'response' || event.stage === 'settlement'))) return null;
  const name = {push_delivery:'送货游戏',color_hunt:'找颜色游戏',rps_siege:'猜拳攻灯'}[event.game_id] || '游戏';
  let reason = activityReasonText(event) || (event.status === 'ok' ? '' : '结束原因尚未记录');
  if (event.event_type === 'game_ended' && event.status !== 'ok') {
    const stage = {preparing:'准备',cargo:'选择货物',find_cargo:'找货',destination:'选择目的地',
      signal:'设置指令灯色',delivery:'配送规划',push:'推送'}[event.stage];
    if (stage) reason += `（${stage}阶段）`;
    if (event.physical_stop === 'unknown' && event.reason !== 'stop_unconfirmed')
      reason += '；退出时尚未确认停稳';
  }
  return {name,reason};
}

function activityGameStart(event) {
  return event.event_type === 'game_started' && Boolean(event.game_id);
}

function activityDecisionBasis(event) {
  const basis = event?.decision_basis;
  if (!basis || typeof basis.reason !== 'string' || !basis.reason.trim()) return null;
  return {reason:basis.reason.trim(), evidence:Array.isArray(basis.evidence)
    ? basis.evidence.filter(item => item && typeof item.summary === 'string' && item.summary.trim()) : []};
}

function activityEventKind(event = {}) {
  const type = String(event.event_type || '').toLowerCase();
  // A received message is an observation for the decision model, while its
  // original content remains speech in the conversation.
  if (type.includes('question')) return 'question';
  if (type.includes('message') || type.includes('proposal') || type.includes('reply')) return 'message';
  if (event.fact_kind === 'question') return 'question';
  if (event.fact_kind === 'observation') return 'observation';
  if (type === 'search_progress') return ['search_observe', 'search_candidate', 'search_confirmed']
    .includes(event.stage) ? 'observation' : 'status';
  if (type.includes('observation') || type.includes('observed') || type === 'person_identified') return 'observation';
  return 'status';
}

function activityDecisionText(event, text) {
  const original = String(text || '').trim();
  // Conversation bubbles show what was actually said. Decision evidence stays
  // available in the progress drawer without adding another speech paragraph.
  if (['message', 'question'].includes(activityEventKind(event))) return original;
  const reason = activityDecisionBasis(event)?.reason;
  return reason && original !== reason ? [reason, original].filter(Boolean).join('\n') : original || reason || '';
}

function activityDecisionEvidence(item, event, events, now = Date.now() / 1000) {
  const labels = {observation:'环境观察', peer_state:'同伴状态', environment:'环境反馈',
    activity:'活动情境', constraint:'用户要求', action_result:'动作反馈'};
  const recordedAt = Number(item.as_of);
  const hasTime = Number.isFinite(recordedAt) && recordedAt > 0;
  const maxAge = item.validity === 'until_changed' ? null : item.kind === 'observation' ? 600
    : ['peer_state', 'environment'].includes(item.kind) ? 60 : null;
  const atDecision = Number(event.ts);
  const expiredThen = item.status === 'stale' || maxAge !== null && hasTime
    && Number.isFinite(atDecision) && atDecision - recordedAt > maxAge;
  const expiredNow = maxAge !== null && hasTime && now - recordedAt > maxAge;
  const ref = String(item.ref || '').replace(/^event:/, '');
  const original = item.kind === 'observation' ? events.find(row => row.event_id === ref) : null;
  return {label:labels[item.kind] || '', source:item.source || original?.unit_id || '',
    recordedAt:hasTime ? recordedAt : null, summary:item.summary,
    notice:item.status === 'unknown' ? '这条依据在决策时尚未确认，不能据此陈述事实'
      : expiredThen ? '这条依据在决策时已过期，不能据此确认当时情况'
      : expiredNow ? '记录已过期，当前情况需要重新确认'
      : !hasTime ? '记录时间未提供，无法确认信息新鲜度' : '',
    missingOriginal:item.kind === 'observation' && !original};
}

Object.assign(window, {activityStateAge, activityLightColor, activityPhysicalLabel, activityMemberView,
  activityMessageReceipt, activityReasonText,
  activityGameEnd, activityGameStart,
  activityDecisionBasis, activityEventKind, activityDecisionText, activityDecisionEvidence,
  isActivityLightingAction});

function activityMediaUrl(event) {
  const candidate = event.media_ref || event.media_url;
  const path = typeof candidate === 'string' ? candidate : candidate?.url;
  if (typeof path !== 'string' || !path.startsWith('/api/')) return '';
  return path;
}
