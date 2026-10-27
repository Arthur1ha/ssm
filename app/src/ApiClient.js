/* ApiClient — browser trust boundary: cookies + CSRF + session-scoped SSE.
 * The browser never supplies actor, persistence or space identifiers.
 */
class ApiClient extends EventTarget {
  constructor() {
    super();
    this.csrfToken = '';
    this.eventSource = null;
    this.eventsConnected = false;
  }

  setCsrfToken(token) {
    this.csrfToken = typeof token === 'string' ? token : '';
  }

  async request(path, options = {}) {
    const { suppressUnauthorized = false, ...fetchOptions } = options;
    const method = (options.method || 'GET').toUpperCase();
    const headers = new Headers(options.headers || {});
    headers.set('Accept', 'application/json');
    if (options.body !== undefined && !(options.body instanceof FormData)) {
      headers.set('Content-Type', 'application/json');
    }
    if (!['GET', 'HEAD', 'OPTIONS'].includes(method) && this.csrfToken) {
      headers.set('X-CSRF-Token', this.csrfToken);
    }

    const response = await fetch(path, {
      ...fetchOptions,
      method,
      headers,
      credentials: 'same-origin',
      body: options.body === undefined || options.body instanceof FormData
        ? options.body
        : JSON.stringify(options.body),
    });
    const contentType = response.headers.get('content-type') || '';
    const data = contentType.includes('application/json')
      ? await response.json().catch(() => ({}))
      : await response.text();
    if (!response.ok) {
      const message = typeof data === 'object' && data
        ? (data.detail || data.error || `请求失败 (${response.status})`)
        : (data || `请求失败 (${response.status})`);
      const error = new Error(typeof message === 'string' ? message : JSON.stringify(message));
      error.status = response.status;
      error.data = data;
      if (response.status === 401 && !suppressUnauthorized) this.dispatchEvent(new Event('unauthorized'));
      throw error;
    }
    return data;
  }

  openEvents() {
    if (this.eventSource) return;
    const source = new EventSource('/api/events', { withCredentials: true });
    this.eventSource = source;
    source.onopen = () => {
      this.eventsConnected = true;
      this.dispatchEvent(new Event('events-open'));
    };
    const forward = event => {
      let payload;
      try { payload = JSON.parse(event.data); }
      catch { payload = { text: event.data }; }
      this.dispatchEvent(Object.assign(new Event('server-event'), { detail: payload }));
      if (payload?.type === 'session_ended') this.dispatchEvent(new Event('unauthorized'));
    };
    source.onmessage = forward;
    [
      'planning', 'executing', 'ack', 'step', 'pending_rule', 'discovery_candidates',
      'done', 'partial', 'failed', 'denied', 'device', 'lease', 'session_ended',
      'run_stage', 'agent_progress', 'agent_reply', 'reply_error',
      'activity',
    ].forEach(name => source.addEventListener(name, forward));
    source.onerror = () => {
      this.eventsConnected = false;
      this.dispatchEvent(new Event('events-error'));
    };
  }

  closeEvents() {
    this.eventSource?.close();
    this.eventSource = null;
    this.eventsConnected = false;
  }
}

const apiClient = new ApiClient();

function apiErrorText(error, fallback = '请求没有完成，请稍后再试') {
  if (error?.status === 401) return '会话已结束，请重新进入';
  const readable = typeof error?.message === 'string'
    && /^[\u3400-\u9fff\u3000-\u303f\uff00-\uffef\s，。！？：；、（）“”·0-9]+$/.test(error.message);
  if (error?.status === 403) return readable ? error.message : '当前身份不能执行这个操作';
  if (error?.status === 409 || error?.status === 423) return readable ? error.message : '设备正在被其他体验者使用';
  return readable ? error.message : fallback;
}
