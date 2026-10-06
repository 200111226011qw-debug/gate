# 检测 Worker 脚本（CheckSocks5）

本目录说明 gate 项目配套的 **检测 Worker** 脚本来源与自动同步机制。

## 脚本来源

- 上游仓库: https://github.com/lsh8848/cm-Workers-CheckSocks5
- 脚本路径: `main/_worker.js`
- 用途: 验证 SSTP 节点可用性并读取出口 IP（住宅/机房判定）

## 自动同步（每 12 小时）

本仓库 workflow（`.github/workflows/check.yml`）每次运行都会自动从上游拉取最新 `_worker.js`，发布到 GitHub Pages 产物：

```
https://200111226011qw-debug.github.io/gate/check-worker.js
```

即：**检测 Worker 的脚本随 12 小时定时任务一起自动更新**，你随时打开上面的地址即可拿到最新代码。

## 部署到 Cloudflare（手动一次）

1. 打开 https://dash.cloudflare.com → Workers 和 Pages → 创建 → 创建 Worker
2. 打开 `https://200111226011qw-debug.github.io/gate/check-worker.js`，复制全部内容
3. 粘贴进 Worker 编辑器 → 保存并部署
4. 记下 Worker 域名，形如 `https://xxx.你的用户名.workers.dev`
5. 验证：浏览器打开 `https://你的Worker域名/check?sstp=vpn:vpn@任意节点:端口` 返回 JSON 即成功
6. 在 gate 仓库 **Settings → Secrets and variables → Actions** 添加：
   - Name: `DOMAIN`
   - Secret: `https://你的Worker域名/check?sstp=vpn:vpn@`

> 之后每次上游更新脚本，只需从 `check-worker.js` 重新复制粘贴到 Worker 编辑器即可（可选操作，不影响自动流水线）。
