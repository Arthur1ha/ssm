/* Presentation only. These definitions never add device capabilities or states. */
const AVATAR_PROFILES = Object.freeze({
  lamp: {color:'#C8F36A', ink:'#24351A', silhouette:'round'},
  robot: {color:'#64D9EE', ink:'#163642', silhouette:'angular'},
  neutral: {color:'#B8B9D1', ink:'#292A40', silhouette:'round'},
});

const AVATAR_SHAPES = Object.freeze({
  idle: {label:'伙伴形象', faceY:53,
    round:'M 50 13 C 72 13 88 29 88 51 C 88 75 73 88 50 88 C 27 88 12 73 12 51 C 12 29 28 13 50 13 Z',
    angular:'M 33 12 L 68 15 L 88 38 L 84 68 L 61 88 L 29 82 L 12 58 L 16 29 Z'},
  rock: {label:'石头', faceY:62, detail:'M 23 43 Q 31 44 34 51 L 44 51',
    round:'M 22 40 C 17 30 24 22 33 25 C 34 15 47 14 51 22 C 58 14 69 19 70 28 C 80 24 88 33 84 43 L 86 61 C 87 78 74 87 54 87 L 40 87 C 25 87 16 76 14 63 L 12 49 C 10 38 19 33 27 41 Z',
    angular:'M 22 40 L 20 29 L 28 22 L 35 25 L 39 17 L 49 17 L 54 24 L 63 20 L 72 25 L 74 32 L 83 31 L 89 40 L 87 64 L 78 81 L 60 88 L 37 84 L 23 75 L 16 64 L 11 48 L 16 40 L 24 41 Z'},
  scissors: {label:'剪刀', faceY:64,
    round:'M 32 48 L 19 21 C 13 8 27 1 33 13 L 47 39 L 57 10 C 61 -2 76 3 72 16 L 62 47 C 72 40 83 47 82 58 L 82 66 C 81 82 70 90 52 90 C 32 90 20 80 19 66 L 18 55 C 17 43 25 40 32 48 Z',
    angular:'M 32 48 L 18 19 L 20 9 L 28 6 L 35 13 L 47 39 L 58 9 L 65 3 L 73 7 L 74 15 L 63 46 L 75 43 L 84 51 L 83 68 L 75 83 L 58 91 L 38 87 L 25 79 L 19 65 L 17 53 L 22 44 L 28 44 Z'},
  paper: {label:'布', faceY:64,
    round:'M 24 48 L 23 24 C 23 13 36 13 37 24 L 38 41 L 39 14 C 39 2 52 2 53 14 L 53 40 L 56 19 C 58 8 70 11 68 22 L 66 45 L 70 32 C 74 23 85 27 82 38 L 77 65 C 74 83 65 91 49 90 C 34 90 24 81 19 70 L 10 52 C 5 42 16 36 22 45 L 30 56',
    angular:'M 24 48 L 22 24 L 26 16 L 34 16 L 39 24 L 39 41 L 40 13 L 44 5 L 51 5 L 55 14 L 54 40 L 58 19 L 64 12 L 71 16 L 71 24 L 67 45 L 72 32 L 79 27 L 85 33 L 83 42 L 78 65 L 69 83 L 52 91 L 35 87 L 24 78 L 17 65 L 9 51 L 12 42 L 19 42 L 30 56'},
});

const AVATAR_EXPRESSIONS = Object.freeze({
  neutral: {label:'平常', eyes:'open', mouth:'M 44 8 Q 50 14 56 8'},
  happy: {label:'开心', eyes:'smile', mouth:'M 43 8 Q 50 19 57 8 Z'},
  curious: {label:'好奇', eyes:'curious', mouth:'M 49 10 a 2 3 0 1 0 4 0 a 2 3 0 1 0 -4 0'},
  blink: {label:'眨眼', eyes:'closed', mouth:'M 44 8 Q 50 14 56 8'},
});
const AVATAR_MOTIONS = Object.freeze({none:'静止', pop:'轻弹', squeeze:'挤压', glance:'轻摆'});

function resolveAvatarProfile(agent) {
  const card = typeof agent === 'object' && agent ? agent : {};
  const id = typeof agent === 'string' ? agent : card.unit_id || card.agent_id || '';
  const kind = typeof agentKind === 'function' ? agentKind(card) : card.agent_type;
  if (kind === 'robot' || id === 'go2' || id.startsWith('go2_')) return AVATAR_PROFILES.robot;
  if (kind === 'led' || id === 'esp32_desk_led') return AVATAR_PROFILES.lamp;
  return AVATAR_PROFILES.neutral;
}

// Extend this table for new, explicitly structured presentation events. Never
// infer a gesture, mood or completion from persona prose or nearby events.
const AVATAR_EVENT_MAPPINGS = Object.freeze({
  rps_result(event, unitId) {
    const round = event.game_round;
    const shape = round?.choices?.[unitId];
    if (!['rock', 'paper', 'scissors'].includes(shape)) return null;
    return {shape, expression:'neutral', motion:'pop',
      label:`第 ${round.round} 轮 · ${AVATAR_SHAPES[shape].label}`,
      mapping:round.mapping?.[unitId]?.label || '', eventId:event.event_id};
  },
});
function avatarEventPresentation(event, unitId) {
  return AVATAR_EVENT_MAPPINGS[event?.stage]?.(event, unitId) || null;
}
function latestAvatarPresentation(events, unitId, activityId) {
  for (let index = events.length - 1; index >= 0; index--) {
    const event = events[index];
    if (activityId && event.activity_id !== activityId) continue;
    const view = avatarEventPresentation(event, unitId);
    if (view) return view;
  }
  return null;
}
function avatarStatePresentation(state, member) {
  if (member.disconnected || member.stale) return {shape:'idle', expression:'neutral', label:''};
  if (state.activity_state === 'rest') return {shape:'idle', expression:'blink', label:'休息中'};
  if (state.life_state === 'explore') return {shape:'idle', expression:'curious', label:'探索中'};
  if (state.life_state === 'play') return {shape:'idle', expression:'curious', label:'互动中'};
  return {shape:'idle', expression:'neutral', label:''};
}
Object.assign(window, {AVATAR_PROFILES, AVATAR_SHAPES, AVATAR_EXPRESSIONS, AVATAR_MOTIONS,
  AVATAR_EVENT_MAPPINGS, resolveAvatarProfile, avatarEventPresentation, latestAvatarPresentation, avatarStatePresentation});
