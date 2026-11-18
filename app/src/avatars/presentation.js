/* Read-only, browser-lifetime projection of existing device snapshots and events.
 * This owns presentation timing, never device state, commands or connections. */
function createCompanionPresentation(options = {}) {
  const now = options.now || (() => Date.now());
  const later = options.setTimeout || ((fn, delay) => setTimeout(fn, delay));
  const cancel = options.clearTimeout || (id => clearTimeout(id));
  const createdAt = now();
  const devices = new Map(), views = new Map(), listeners = new Set();
  const clients = new Map(), seen = new Map(), retiredActivities = new Map(), games = new Map();
  let connected = true, activeActivity = null, timer = null, eventFloor = createdAt - 1000;
  const empty = Object.freeze({agent:null,shape:'idle', emotion:'neutral', behavior:'idle', animated:false,
    online:null, label:'状态待同步', motion:'none', animationKey:null, animationStartedAt:null,
    motionDurationMs:0, motionEpoch:createdAt, lightingState:null, lightingColor:null, lightingBrightness:null});
  const emotionTime = typeof AVATAR_TIMING === 'undefined' ? 6000 : AVATAR_TIMING.emotion;
  const normalize = value => typeof normalizeAvatarEmotion === 'function' ? normalizeAvatarEmotion(value) : 'neutral';
  const bounded = (map, key, value, limit = 640) => {
    map.set(key,value);
    while (map.size > limit) map.delete(map.keys().next().value);
  };
  const record = id => {
    if (!devices.has(id)) devices.set(id,{card:null,state:{},retired:new Set(),health:'live',connected:true,
      motionEpoch:createdAt,emotion:null,gesture:null,request:null,runs:new Map(),repliedRuns:new Map(),order:{}});
    return devices.get(id);
  };
  const kind = (id, item) => {
    if (typeof agentKind === 'function') return agentKind(item.card || {unit_id:id});
    return id === 'go2' || id.startsWith('go2_') ? 'robot' : item.state.ism ? 'led' : '';
  };
  const isLamp = (id,item) => kind(id,item) === 'led' || ['OFF','ON','BLINKING'].includes(item.state.ism);
  const stamp = state => {
    const value = Number(state._received_at ?? state.ts);
    return Number.isFinite(value) && value > 0 ? value * 1000 : null;
  };
  const scopeValid = activityId => !activityId || Boolean(activeActivity
    && activeActivity.activity_id === activityId && !retiredActivities.has(activityId));
  const same = (a,b) => a && Object.keys(b).every(key => a[key] === b[key])
    && Object.keys(a).length === Object.keys(b).length;
  function project(id,item,time) {
    const state = item.state, lamp = isLamp(id,item);
    const cardOnline=item.card?._online ?? item.card?.online;
    const physicalOffline = cardOnline === false
      || state.connected === false || ['offline','disconnected'].includes(state.connection_state);
    const ts = stamp(state);
    const fresh = lamp && Boolean(state.ism) || ts !== null && ts <= time + 5000 && time - ts <= 60000;
    const clientSynced=[...clients.keys()].every(client=>client.eventsConnected!==false && client.devicesSynced!==false);
    const synced = connected && clientSynced && item.connected && item.health !== 'reconnecting';
    const online = !synced ? null : physicalOffline ? false
      : (cardOnline===true || state.connected===true) && fresh ? true : null;
    const sustained=avatarStatePresentation(state,{disconnected:physicalOffline,stale:online!==true});
    let behavior=sustained.behavior,label=online===false?'设备离线':online===null?'状态待同步':sustained.label || '等你安排';
    if(online===true && behavior!=='rest' && !(behavior==='explore' && item.request?.kind==='event')
        && item.request?.active && scopeValid(item.request.activityId)) {
      behavior=item.request.behavior;label=item.request.uncertain?'结果待确认':behavior==='working'?'正在执行':'正在处理';
    }
    const emotion = online === true && item.emotion?.until > time && scopeValid(item.emotion.activityId) ? item.emotion : null;
    const gesture = online === true && behavior !== 'rest' && item.gesture?.until > time
      && scopeValid(item.gesture.activityId) ? item.gesture : null;
    const baseEmotion = behavior === 'rest' ? 'relaxed' : 'neutral';
    const current = gesture || emotion;
    const mood = gesture ? gesture.expression : emotion?.value || baseEmotion;
    const motion = gesture?.motion || (emotion && typeof AVATAR_EXPRESSIONS !== 'undefined'
      ? AVATAR_EXPRESSIONS[emotion.value]?.motion : 'none') || 'none';
    const validRgb = [state.r,state.g,state.b].every(value => Number.isInteger(value) && value >= 0 && value <= 255);
    return Object.freeze({agent:item.card,shape:gesture?.shape || 'idle',emotion:mood,behavior,animated:online===true,
      online,label,motion,animationKey:current?.eventId || null,animationStartedAt:current?.startedAt ?? null,
      motionDurationMs:AVATAR_TIMING.motions[motion] || 0,motionEpoch:item.motionEpoch,
      lightingState:online===true && lamp ? state.ism : null,
      lightingColor:online===true && lamp && validRgb ? `rgb(${state.r}, ${state.g}, ${state.b})` : null,
      lightingBrightness:online===true && lamp && Number.isInteger(state.brightness) ? state.brightness : null});
  }
  function publish() {
    const time=now();let changed=false,nextDeadline=Infinity;
    for (const [id,item] of devices) {
      if (item.emotion && item.emotion.until <= time) item.emotion=null;
      if (item.gesture && item.gesture.until <= time) item.gesture=null;
      const view=project(id,item,time);
      if (!same(views.get(id),view)) {views.set(id,view);changed=true;}
      for (const transient of [item.emotion,item.gesture]) if (transient?.until > time) nextDeadline=Math.min(nextDeadline,transient.until);
      const ts=stamp(item.state);
      if (!isLamp(id,item) && ts !== null && ts + 60001 > time) nextDeadline=Math.min(nextDeadline,ts + 60001);
    }
    if (timer!==null) {cancel(timer);timer=null;}
    if (Number.isFinite(nextDeadline)) timer=later(()=>{timer=null;publish();},Math.max(1,nextDeadline-time));
    if (changed) listeners.forEach(listener=>listener());
  }
  function updateDevice(id,{card,state,connected:deviceConnected,health} = {}) {
    if (!id) return;
    const item=record(id);
    if (card) item.card=card;
    if (state && typeof state==='object' && Object.keys(state).length) {
      item.state=typeof mergeStateSnapshot==='function'
        ? mergeStateSnapshot(item.state,state,item.retired) : {...item.state,...state};
    }
    if (typeof deviceConnected==='boolean') item.connected=deviceConnected;
    if (health) item.health=health;
    publish();
  }
  function syncDevices(agents=[],unitData={},isConnected=true) {
    connected=Boolean(isConnected);
    const present=new Set();
    for (const card of agents) {
      const id=card.unit_id || card.agent_id;if(!id)continue;
      present.add(id);
      const item=record(id);item.card=card;item.connected=Boolean(isConnected);
      const state=unitData[id]?.state || card.state;
      if(state && Object.keys(state).length) {
        item.state=typeof mergeStateSnapshot==='function'
          ? mergeStateSnapshot(item.state,state,item.retired) : {...item.state,...state};
        if(isConnected)item.health='live';
      }
    }
    let removed=false;
    for (const id of devices.keys()) if(!present.has(id)){devices.delete(id);views.delete(id);removed=true;}
    publish();
    if(removed)listeners.forEach(listener=>listener());
  }
  function retireActivity(id,revision=0) {
    if(!id)return;
    bounded(retiredActivities,id,Math.max(Number(revision)||0,retiredActivities.get(id)||0),32);
    for(const item of devices.values()) {
      if(item.emotion?.activityId===id)item.emotion=null;
      if(item.gesture?.activityId===id)item.gesture=null;
      if(item.request?.activityId===id)item.request.active=false;
    }
    if(activeActivity?.activity_id===id)activeActivity=null;
  }
  function setActivity(activity) {
    const id=activity?.activity_id;
    if(!id){if(activeActivity)retireActivity(activeActivity.activity_id,activeActivity.revision);publish();return;}
    if(retiredActivities.has(id))return;
    if(activeActivity?.activity_id===id && Number(activity.revision||0)<Number(activeActivity.revision||0))return;
    if(['ending','ended'].includes(activity.status)){retireActivity(id,activity.revision);publish();return;}
    if(activeActivity?.activity_id!==id && activeActivity)retireActivity(activeActivity.activity_id,activeActivity.revision);
    activeActivity={activity_id:id,revision:Number(activity.revision)||0};
    publish();
  }
  function beginRequest(id,runId,requestOptions={}) {
    if(!id || !runId)return;
    const item=record(id),previous=item.request;
    if(previous?.runId===runId && previous.active)return;
    if(previous)bounded(item.runs,previous.runId,previous,32);
    item.repliedRuns.delete(runId);
    item.request={runId,startedAt:now(),active:true,behavior:requestOptions.behavior==='working'?'working':'thinking',
      activityId:requestOptions.activityId || null,kind:requestOptions.kind || 'chat',uncertain:false};
    item.emotion=null;
    bounded(item.runs,runId,item.request,32);
    publish();
  }
  function currentRun(item,event) {
    const rawId=event.run_id || event.task_id;
    const known=item.runs.get(rawId);
    const runId=known?.runId || rawId;
    if(!runId)return !item.request?.active;
    if(known && known!==item.request)return false;
    if(item.request?.active && item.request.runId!==runId)return false;
    if(item.request?.runId===runId && event.ts*1000<item.request.startedAt-1000)return false;
    return !item.request || runId===item.request.runId || event.ts*1000>=item.request.startedAt;
  }
  function acceptIdentity(id,event,field) {
    const identity=event.source_event_id || event.event_id;
    if(!identity)return null;
    const key=`${id}:${field}:${identity}`;
    if(seen.has(key))return null;
    bounded(seen,key,true);
    return identity;
  }
  function ingest(outer,ingestOptions={}) {
    if(!outer || typeof outer!=='object')return;
    const nested=outer.payload || outer.data;
    const event=nested && typeof nested==='object'?{...outer,...nested,type:nested.type || outer.type || outer.event}:outer;
    const time=now(),receivedAt=ingestOptions.receivedAt ?? time;
    if(!Number.isFinite(event.ts) || event.ts*1000<Math.max(eventFloor,time-10000)
        || event.ts*1000>time+5000 || receivedAt<createdAt)return;
    const activityId=event.activity_id;
    if(event.type==='activity' && ['ended','activity_ended'].includes(event.event_type)) {
      retireActivity(activityId,event.revision);publish();return;
    }
    if(event.type==='activity' && ['started','activity_started'].includes(event.event_type))
      setActivity({activity_id:activityId,revision:event.revision,status:'active'});
    if(!scopeValid(activityId))return;
    const id=event.type==='peer_message' ? event.sender : event.agent_id || event.unit_id;
    const item=id?record(id):null;
    const isReply=event.type==='agent_reply' || event.type==='peer_message'
      || event.type==='activity' && ['message','question'].includes(event.event_type);
    if(item && isReply && currentRun(item,event)) {
      const rawId=event.run_id || event.task_id;
      const runId=item.runs.get(rawId)?.runId || rawId;
      if(runId && item.request?.runId!==runId) {
        item.request={runId,startedAt:event.ts*1000,active:false,behavior:'thinking',kind:'event',activityId:activityId || null};
        bounded(item.runs,runId,item.request,32);
      } else if(item.request && item.request.runId===runId)item.request.active=false;
      const identity=Object.prototype.hasOwnProperty.call(event,'emotion') && acceptIdentity(id,event,'emotion');
      const order=event.ts*1000;
      if(identity && (!runId || !item.repliedRuns.has(runId)) && order>=(item.order.emotion || 0)) {
        item.order.emotion=order;
        item.emotion={value:normalize(event.emotion),eventId:identity,startedAt:receivedAt,
          until:Math.min(receivedAt+emotionTime,order+emotionTime),activityId:activityId || null};
        if(runId)bounded(item.repliedRuns,runId,identity,32);
      }
    }
    const gameId=event.game_instance_id;
    const gameKey=activityId && gameId ? `${activityId}:${gameId}` : null;
    if(event.type==='activity' && gameKey) {
      const game=games.get(gameKey) || {dispatched:0,finished:0,closed:false};
      const finishedRound=event.stage==='rps_result' ? event.game_round?.round : null;
      if(['game_closing','game_ended'].includes(event.event_type) || event.stage==='settlement') game.closed=true;
      if(Number.isInteger(finishedRound)) game.finished=Math.max(game.finished,finishedRound);
      bounded(games,gameKey,game,64);
      for(const target of devices.values()) if(target.gesture?.gameKey===gameKey
          && (game.closed || target.gesture.round<=game.finished)) target.gesture=null;
    }
    if(event.type==='activity' && event.stage==='rps_dispatched' && gameKey && event.game_throw?.choices) {
      const game=games.get(gameKey),round=event.game_throw.round;
      if(game.closed || !Number.isInteger(round) || round<=Math.max(game.dispatched,game.finished)) {publish();return;}
      game.dispatched=round;
      for(const unitId of Object.keys(event.game_throw.choices)) {
        const target=record(unitId);
        const gesture=typeof avatarEventPresentation==='function'?avatarEventPresentation(event,unitId):null;
        const identity=gesture && acceptIdentity(unitId,event,'gesture');
        const order=event.ts*1000;
        if(!gesture || !identity || order<(target.order.gesture || 0))continue;
        target.order.gesture=order;
        const currentEmotion=target.emotion?.until>time && scopeValid(target.emotion.activityId)?target.emotion.value:'neutral';
        target.gesture={...gesture,eventId:identity,expression:currentEmotion,
          startedAt:receivedAt,until:Infinity,activityId,gameKey,round};
      }
    }
    if(item && ['agent_progress','run_stage','reply_error'].includes(event.type) && currentRun(item,event)) {
      const rawId=event.run_id || event.task_id;
      const runId=item.runs.get(rawId)?.runId || rawId;
      const terminal=event.type==='reply_error' || ['finished','done','failed','denied','question'].includes(event.stage);
      if(terminal) {
        if(runId && (!item.request || item.request.runId!==runId)) {
          item.request={runId,startedAt:event.ts*1000,active:false,behavior:'thinking',kind:'event',activityId:activityId || null};
          bounded(item.runs,runId,item.request,32);
        } else if(item.request && item.request.runId===runId)item.request.active=false;
      } else if(runId && (!item.request || item.request.runId!==runId)) {
        beginRequest(id,runId,{behavior:['dispatching','executing'].includes(event.stage)?'working':'thinking',activityId,kind:'event'});
      } else if(item.request?.active && ['dispatching','executing'].includes(event.stage))item.request.behavior='working';
    }
    publish();
  }
  function finishRequest(id,runId,result={}) {
    const item=devices.get(id),request=item?.request;
    if(!request || request.runId!==runId)return;
    const pending=result?.execution_active!==false && (['executing','unknown','timeout'].includes(result?.result)
      || result?.result==='accepted' && result?.dispatch_only);
    if(pending && request.active)for(const alias of [result?.run_id,result?.task_id,result?.request_id]) {
      if(alias && (!item.runs.has(alias) || item.runs.get(alias)===request))bounded(item.runs,alias,request,32);
    }
    if(request.active && pending){request.behavior=result?.result==='executing'?'working':'thinking';request.uncertain=result?.result!=='executing';}
    else if(!pending)request.active=false;
    if((result?.reply || result?.response) && Object.prototype.hasOwnProperty.call(result,'emotion')
        && !item.repliedRuns.has(runId) && result.reply_source!=='runtime') {
      ingest({type:'agent_reply',agent_id:id,run_id:runId,emotion:result.emotion,
        event_id:result.event_id || `http:${runId}`,source_event_id:result.source_event_id,
        activity_id:result.activity_id || request.activityId,ts:Number.isFinite(result.ts)?result.ts:now()/1000},
      {receivedAt:now(),trustedCurrent:true});
    }
    publish();
  }
  function reset() {
    if(timer!==null){cancel(timer);timer=null;}
    devices.clear();views.clear();seen.clear();retiredActivities.clear();games.clear();activeActivity=null;eventFloor=now();
    listeners.forEach(listener=>listener());
  }
  function attach(client) {
    if(!client?.addEventListener)return()=>{};
    const existing=clients.get(client);
    if(existing){existing.count++;return()=>release(client);}
    const forward=event=>ingest(event.detail);
    const opened=()=>{connected=client.devicesSynced!==false;eventFloor=Math.max(eventFloor,Number(client.eventsOpenedAt)||now());clearTransient();};
    const syncing=()=>{connected=false;clearTransient();};
    const synced=()=>{connected=client.eventsConnected!==false;publish();};
    const unauthorized=()=>reset();
    const handlers={'server-event':forward,'events-open':opened,'events-error':syncing,
      'devices-syncing':syncing,'devices-synced':synced,unauthorized};
    Object.entries(handlers).forEach(([name,handler])=>client.addEventListener(name,handler));
    clients.set(client,{count:1,handlers});
    eventFloor=Math.max(eventFloor,Number(client.eventsOpenedAt)||0);
    connected=client.eventsConnected!==false && client.devicesSynced!==false;
    publish();
    return()=>release(client);
  }
  function release(client) {
    const entry=clients.get(client);if(!entry || --entry.count>0)return;
    Object.entries(entry.handlers).forEach(([name,handler])=>client.removeEventListener(name,handler));
    clients.delete(client);
  }
  function clearTransient() {
    for(const item of devices.values()) {
      item.emotion=null;item.gesture=null;
      // Explicit HTTP work remains pending while the existing caller is still
      // awaiting its receipt; event-only activity is re-established by new facts.
      if(item.request?.kind==='event')item.request.active=false;
    }
    publish();
  }
  return {createdAt,subscribe:listener=>{listeners.add(listener);return()=>listeners.delete(listener);},
    getSnapshot:id=>views.get(id)||empty,updateDevice,syncDevices,setActivity,beginRequest,finishRequest,ingest,attach,reset};
}

const companionPresentation=createCompanionPresentation();
window.createCompanionPresentation=createCompanionPresentation;
window.companionPresentation=companionPresentation;
