/* audio.js — TTS 音频播放工具
   Android Chrome 要求用户首次手势后解锁 AudioContext，
   之后 MQTT 触发的 Audio.play() 才不会被自动播放策略拦截。
*/
let _audioUnlocked = false;
function _unlockAudio() {
  if (_audioUnlocked) return;
  _audioUnlocked = true;
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    ctx.resume().then(() => ctx.close()).catch(() => {});
  } catch (e) {}
}
document.addEventListener('pointerdown', _unlockAudio, { passive: true });

function _base64ToBlob(b64, mimeType) {
  const bin   = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new Blob([bytes], { type: mimeType });
}

function playAudioB64(b64) {
  if (!b64) return;
  try {
    const blob  = _base64ToBlob(b64, 'audio/mpeg');
    const url   = URL.createObjectURL(blob);
    const audio = new Audio(url);
    audio.onended = () => URL.revokeObjectURL(url);
    audio.onerror = () => URL.revokeObjectURL(url);
    audio.play().catch(err => console.warn('[Speech] play() blocked:', err));
  } catch (e) {
    console.warn('[Speech] playAudioB64 error:', e);
  }
}
