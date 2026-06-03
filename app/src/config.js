const BROKER_URL  = (location.protocol === 'https:')
  ? `wss://${location.host}/mqtt`
  : 'ws://47.116.137.202:9001';
const BROKER_USER = 'ssm_user';
const BROKER_PASS = 'Wl4sErQrlrpEbm7r';
const LIME            = '#C8FF3E';
const NEARBY_RADIUS_M = 300;
const POPUP_RADIUS_M  = 5000;

function navigate(hash) {
  window.location.hash = hash;
}
