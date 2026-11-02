/**
 * 前端运行配置（前后端分离）。
 *
 * 同源部署（nginx 把 /api 反代到后端）时留空即可；
 * 前后端分开部署时填后端地址，例如 "https://api.example.com"。
 * 注意：跨域时后端需设置 CX_CORS_ORIGINS，且前端要用 https + 正确的 Cookie。
 */
window.CX_API_BASE = "";
