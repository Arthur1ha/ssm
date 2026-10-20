ReactDOM.createRoot(document.getElementById('root')).render(
  <SessionGate>
    {sessionProps => <App {...sessionProps}/>}
  </SessionGate>
);
