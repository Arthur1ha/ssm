/* One interface for every live companion; parents choose placement and size. */
function companionHistoryPresentation(unitId,snapshot={}) {
  let shape=snapshot.shape || 'idle',emotion=normalizeAvatarEmotion(snapshot.emotion),behavior=snapshot.behavior || 'idle';
  const events=Array.isArray(snapshot.events)?snapshot.events:[];
  let foundEmotion=false,foundGesture=false,foundState=false;
  for(let index=events.length-1;index>=0;index--) {
    const event=events[index];
    if(snapshot.activity_id && event.activity_id!==snapshot.activity_id)continue;
    const identity=event.type==='peer_message'?event.sender:event.agent_id || event.unit_id;
    if(!foundGesture) {
      const gesture=avatarEventPresentation(event,unitId);
      if(gesture){shape=gesture.shape;foundGesture=true;}
    }
    if(identity!==unitId)continue;
    if(!foundEmotion && Object.prototype.hasOwnProperty.call(event,'emotion')) {
      emotion=normalizeAvatarEmotion(event.emotion);foundEmotion=true;
    }
    if(!foundState && event.state_at_event?.status==='current' && event.state_at_event.values) {
      const state=event.state_at_event.values;
      const captured=avatarStatePresentation(state);
      behavior=captured.behavior;
      if(!foundEmotion)emotion=captured.expression;
      foundState=true;
    }
  }
  return {shape,emotion,behavior,animated:false};
}

function CompanionAvatar({unitId,agent,size=48,mode='live',snapshot,label,className=''}) {
  const id=unitId || (typeof agent==='string'?agent:agent?.unit_id || agent?.agent_id) || '';
  const store=companionPresentation;
  const subscribe=React.useCallback(listener=>mode==='live'?store.subscribe(listener):()=>{},[store,mode]);
  const read=React.useCallback(()=>store.getSnapshot(mode==='live'?id:''),[store,id,mode]);
  const view=React.useSyncExternalStore(subscribe,read,read);
  React.useEffect(()=>{
    if(mode!=='live')return;
    return typeof apiClient!=='undefined'?store.attach(apiClient):undefined;
  },[store,mode]);
  React.useEffect(()=>{
    if(mode==='live' && agent && typeof agent==='object')store.updateDevice(id,{card:agent,state:agent.state});
  },[store,id,agent,mode]);
  const preview=snapshot || {};
  const presentation=mode==='history'?companionHistoryPresentation(id,snapshot):mode==='preview'?{
    shape:preview.shape || 'idle',emotion:normalizeAvatarEmotion(preview.emotion),behavior:preview.behavior || 'idle',
    animated:Boolean(preview.animated),motion:preview.motion,animationKey:preview.animationKey,
    animationStartedAt:preview.animationStartedAt,motionDurationMs:preview.motionDurationMs,motionEpoch:preview.motionEpoch,
    online:preview.online,lightingState:preview.lightingState,lightingColor:preview.lightingColor,
    lightingBrightness:preview.lightingBrightness,
  }:view;
  const displayAgent=agent || presentation.agent || id;
  const name=typeof getAgentDisplayName==='function'?getAgentDisplayName(displayAgent):id;
  return <ShapeFace {...presentation} unitId={id} agent={displayAgent} size={size}
    label={label || [name,presentation.label].filter(Boolean).join('，')}
    className={`companion-avatar ${className}`}/>;
}
window.CompanionAvatar=CompanionAvatar;
window.companionHistoryPresentation=companionHistoryPresentation;
