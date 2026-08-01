import axios from "axios";

const TOKEN_KEY = "quantiagent_token";

export function getToken() {
  return localStorage.getItem(TOKEN_KEY) || "quantiagent-admin";
}
export function setToken(t) {
  localStorage.setItem(TOKEN_KEY, t);
}

const client = axios.create({ baseURL: "", timeout: 60000 });

client.interceptors.request.use((cfg) => {
  cfg.headers.Authorization = `Bearer ${getToken()}`;
  return cfg;
});

client.interceptors.response.use(
  (res) => res.data,
  (err) => {
    if (err.response?.status === 401) {
      console.error("鉴权失败, 检查 /api 令牌(web.admin_token)");
    }
    return Promise.reject(err);
  }
);

export const api = {
  get: (url, params) => client.get(url, { params }).then((d) => d),
  post: (url, body) => client.post(url, body).then((d) => d),
};

/** 轮询工具: 每 interval ms 轮询直到 status 非 RUNNING/PENDING */
export function poll(url, onUpdate, { interval = 2000, timeout = 600000 } = {}) {
  return new Promise((resolve, reject) => {
    const started = Date.now();
    const tick = async () => {
      try {
        const data = await api.get(url);
        onUpdate?.(data);
        if (data.status === "DONE" || data.status === "FAILED") return resolve(data);
        if (Date.now() - started > timeout) return reject(new Error("轮询超时"));
        setTimeout(tick, interval);
      } catch (e) {
        reject(e);
      }
    };
    tick();
  });
}
