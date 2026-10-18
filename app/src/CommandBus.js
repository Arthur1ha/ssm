/* Device controls share result handling; transport acceptance is never completion. */
const commandBus = {
  async http(endpoint, body, method = 'POST') {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 30000);
    try {
      const response = await fetch(endpoint, {
        method, headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body), signal: controller.signal,
      });
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `请求失败 (${response.status})`);
      return data;
    } catch (error) {
      return { result: error.name === 'AbortError' ? 'timeout' : 'error', error: error.message };
    } finally { clearTimeout(timer); }
  },

  mqtt(unitId, action, params) {
    const task_id = 'ui_' + (globalThis.crypto?.randomUUID?.() || `${Date.now()}_${Math.random().toString(16).slice(2)}`);
    const topic = `ssm/result/${unitId}/${task_id}`;
    return new Promise(resolve => {
      let finished = false;
      const finish = result => {
        if (finished) return;
        finished = true;
        clearTimeout(timer);
        mqttBus.removeEventListener('message', receive);
        mqttBus.removeEventListener('disconnect', disconnected);
        mqttBus.unsubscribe(topic);
        resolve(result);
      };
      const receive = e => {
        if (e.detail.topic !== topic || e.detail.msg?.task_id !== task_id) return;
        if (['ok', 'blocked', 'error', 'cancelled', 'timeout'].includes(e.detail.msg.result)) finish(e.detail.msg);
      };
      const disconnected = () => finish({ result: 'error', error: '连接已断开，执行结果未知' });
      const timer = setTimeout(() => finish({ result: 'timeout' }), 20000);
      mqttBus.addEventListener('message', receive);
      mqttBus.addEventListener('disconnect', disconnected);
      mqttBus.subscribeReady(topic).then(() => {
        if (!finished) mqttBus.publish(`ssm/task/${unitId}/${task_id}`, {
          task_id, session_id: task_id, unit_id: unitId, agent_id: 'pwa',
          action, params, ts: Math.floor(Date.now() / 1000),
        });
      }).catch(error => finish({ result: 'error', error: error.message }));
    });
  },

  describe(result) {
    const messages = {
      ok: '执行完成', accepted: '指令已发送，尚未确认完成', executing: '正在执行',
      blocked: '当前状态或参数不允许执行', cancelled: '动作已被后续指令打断',
      timeout: '等待完成反馈超时，结果尚未确认', error: '执行失败',
    };
    return messages[result?.result] || '未收到有效执行结果';
  },

  // Serialize joystick writes, coalescing movement samples. A release always follows the in-flight sample.
  _velocities: new Map(),
  velocity(endpoint, params, report) {
    let entry = this._velocities.get(endpoint);
    if (!entry) {
      entry = { next: null, running: false };
      this._velocities.set(endpoint, entry);
    }
    entry.next = { params, report };
    if (entry.running) return;
    entry.running = true;
    const drain = async () => {
      try {
        while (entry.next) {
          const item = entry.next;
          entry.next = null;
          const result = await this.http(endpoint, item.params);
          item.report(result);
        }
      } finally { entry.running = false; }
    };
    drain();
  },
};
