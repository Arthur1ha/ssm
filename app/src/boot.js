/* boot.js — 按 UI_VERSION 挂载 V1(App) 或 V2(AppV2) */
ReactDOM.createRoot(document.getElementById('root'))
  .render(UI_VERSION === 'v2' && window.AppV2 ? <AppV2/> : <App/>);
