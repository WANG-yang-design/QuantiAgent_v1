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
      // 用户可见提示(本地单用户工具: 提示去配置修改)
      if (typeof window !== "undefined") {
        window.dispatchEvent(new CustomEvent("auth-error"));
      }
    }
    return Promise.reject(err);
  }
);

export const api = {
  get: (url, params) => client.get(url, { params }).then((d) => d),
  post: (url, body) => client.post(url, body).then((d) => d),
  put: (url, body) => client.put(url, body).then((d) => d),
  patch: (url, body) => client.patch(url, body).then((d) => d),
  delete: (url) => client.delete(url).then((d) => d),
};

/** 轮询工具: 每 interval ms 轮询直到 status 非 RUNNING/PENDING。
 * 网络错误(服务重启等)做有限次指数退避重试, 避免整体失败。 */
export function poll(url, onUpdate, { interval = 2000, timeout = 600000 } = {}) {
  return new Promise((resolve, reject) => {
    const started = Date.now();
    let retries = 0;
    const MAX_RETRIES = 3;
    const tick = async () => {
      try {
        const data = await api.get(url);
        retries = 0;
        onUpdate?.(data);
        if (data.status === "DONE" || data.status === "FAILED") return resolve(data);
        if (Date.now() - started > timeout) return reject(new Error("轮询超时"));
        setTimeout(tick, interval);
      } catch (e) {
        // 轮询中偶发网络错误: 指数退避重试, 连续失败后放弃
        if (retries < MAX_RETRIES && Date.now() - started < timeout) {
          retries += 1;
          return setTimeout(tick, interval * Math.pow(2, retries));
        }
        reject(e);
      }
    };
    tick();
  });
}
