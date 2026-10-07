# VPNGate SSTP 家宽节点（edgetunnel 链式代理） 🚀

自动抓取 [VPN Gate](https://www.vpngate.net/) 的 SSTP 家宽/机房节点，调用检测 Worker 逐个验证可用性，按国家分组、标注住宅/机房，生成可直接通过 **URL 自动轮换** 的节点清单。**每 12 小时自动更新一次。**

> 核心价值：VPN Gate 的 SSTP 节点 12 小时就换一批，手动测试筛选太痛苦。本仓库把它全自动了——你只需把 `nodes.txt` 的网址填进 edgetunnel 后台一次，之后节点每 12 小时自动换，零手动。

---

## 架构（数据流向）

~~~text
VPN Gate 官方源
      │  (每 12 小时，GitHub Actions 定时抓取)
      ▼
筛选 SSTP 节点 → 去重
      │
      ▼
检测 Worker (CheckSocks5，部署在 Cloudflare)
      │  GET /check?sstp=vpn:vpn@host:port
      │  返回 success + 出口 IP(住宅/机房判定)
      ▼
保留成功节点 → 按国家分组 → 住宅/机房标注 → 延迟排序
      │
      ▼
生成 nodes.txt (GitHub Pages 发布)
      │
      ▼
edgetunnel 后台「自定义优选IP」框填 https://…/nodes.txt
      │  edgetunnel 每次生成订阅时自动 fetch → 解析 $sstp:// → 套链式代理
      ▼
客户端订阅 edgetunnel 订阅 → 使用 SSTP 家宽节点 (每 12 小时自动换)
~~~

---

## 一、完整部署教程（从零开始）

### 前置条件
- 一个 Cloudflare 账号（免费即可）
- 一个 GitHub 账号
- 一个已转入 Cloudflare 的域名（可选，但强烈推荐）

### 第 1 步：部署 edgetunnel（核心使用端）

1. 登录 Cloudflare 控制台，点击左侧 **Workers 和 Pages**
2. 点击 **创建** → 选择 **创建 Worker**，起名 `edgetunnel`，点击 **部署**
3. 打开 https://github.com/cmliu/edgetunnel/blob/main/_worker.js ，复制全部代码
4. 回到 Cloudflare Worker 编辑器，粘贴代码，点击 **保存并部署**
5. 在 **设置** → **变量** 中，添加变量 `ADMIN`，值填你的管理员密码
6. 在 **绑定** 中，添加 KV 命名空间绑定，变量名称填 `KV`
7. （推荐）在 **触发器** 中绑定自定义域名
8. 浏览器访问 `https://你的域名/admin`，登录后台
9. **在后台首页记下你的 UUID 和节点域名**（后面要用）

> **关键**：`UUID` 和 `节点域名` 是 edgetunnel 自己的配置，**不需要**在 `vpngate.py` 中设置。`vpngate.py` 只负责生成 `nodes.txt`，edgetunnel 会用它自己的 UUID/域名去生成最终订阅。

### 第 2 步：部署检测 Worker（CheckSocks5）

1. 打开 https://github.com/lsh8848/cm-Workers-CheckSocks5 ，点 **Fork**
2. 进 Cloudflare 控制台 → Workers 和 Pages → 创建 → 创建 Worker
3. 把 `_worker.js` 的全部内容粘贴进编辑器，点「部署」
4. 记下这个 Worker 的域名，形如 `https://xxx.你的用户名.workers.dev`
5. 验证：浏览器打开 `https://你的Worker域名/check?sstp=vpn:vpn@任意节点:端口` ，能返回 JSON 即成功
6. **脚本自动同步**：本仓库 workflow 每 12 小时自动从上游拉取最新 `_worker.js`，发布到 `https://你的GitHub用户名.github.io/仓库名/check-worker.js`。以后想更新 Worker 代码，直接打开这个地址复制粘贴即可（详见 `check-worker/README.md`）

### 第 3 步：Fork 本仓库

在 GitHub 上打开本仓库，点 **Fork**，复制到你账号下。

### 第 4 步：修改配置（重点）

进你 fork 的仓库，修改 `vpngate.py`：

| 文件 | 位置 | 改成什么 | 为什么 |
| :--- | :--- | :--- | :--- |
| .github/workflows/check.yml | env 里的 `CHECK_WORKER` | 你的检测 Worker 域名，形如 `https://xxx.workers.dev/check?sstp=vpn:vpn@` | 检测统一走你自己的 Worker |
| vpngate.py | `NODES_URL` | 把里面写死的固定地址换成 `你的用户名/仓库名` | 自动更新时用到的固定地址 |

> **注意**：`vpngate.py` 中**不需要**配置 `EDT_UUID` 和 `EDT_DOMAIN`。你之前看到的这两个变量是旧版遗留，现已删除。edgetunnel 后台会自己处理 UUID 和域名。

### 第 5 步：开启 GitHub Pages 与 Actions

1. 进你 fork 的仓库 → Settings → Pages，Source 设为 **GitHub Actions**
2. 进 Actions 页，若提示启用 Actions 就点启用
3. 手动触发一次：Actions → VPN Gate Node Check → Run workflow → Run workflow
4. 等它跑完（约 1 分钟），看到绿色 ✓ 即成功

### 第 6 步：确认产物

跑完后，你的站点地址是：
~~~text
https://你的GitHub用户名.github.io/仓库名/nodes.txt
~~~
浏览器打开，能看到一堆 `优选域名:443#国家-住宅-XX …` 的行，就说明全部打通了。

---

## 二、使用教程（URL 自动轮换，一次配置永久生效）

1. 进 edgetunnel 后台（你的域名/admin），找到「自定义优选IP」文本框
2. 粘贴**一行网址**：
   ~~~text
   https://你的GitHub用户名.github.io/仓库名/nodes.txt
   ~~~
3. 点保存
4. 客户端刷新订阅 → 每次刷新 edgetunnel 都重新拉取一次 nodes.txt，节点自动更新

> 原理：`nodes.txt` 是纯节点行版本（无注释头），每行 `入口域名:443#国家-住宅-01$sstp://vpn:vpn@节点:端口`。edgetunnel 下次生成订阅时会 fetch 这个网址、逐行解析成优选入口 + 链式代理指令。你只填一次，之后节点每 12 小时自动换、零手动。

---

## 三、如何更换优选域名

入口地址用的是「优选域名」，决定客户端连 Cloudflare 用哪个 IP、稳不稳。

### 在哪个文件改
- 文件：`vpngate.py`
- 位置：`_DEFAULT_EDGE_HOSTS`（当前 125 个，bestcf 三批实测结果合并去重）

### 改法（两种，任选其一）
1. **改常量**（推荐）：打开 `vpngate.py`，把 `_DEFAULT_EDGE_HOSTS` 里的域名列表换成你测出来的（逗号分隔，格式 `域名:443`），提交推送，等下一次自动运行或手动触发 Action。
2. **用环境变量覆盖**（不动代码）：在 `.github/workflows/check.yml` 主步骤的 `env:` 里加一行 `EDGE_HOSTS: "a.com:443,b.com:443"`。

### 关于「锁定」（更新时不动）
`EDGE_HOST_LOCKED` 默认等于 `_DEFAULT_EDGE_HOSTS`：**锁定名单里的域名在自动更新中永远保留** —— 跳过 DNS 污染检测与 TCP 连通性检测。

> 设计意图：`_DEFAULT_EDGE_HOSTS` 放的是你自己实测过、确定可用的域名，机器人不要自作主张去动它们。
> 若某个域名希望重新参与自动检测，用 `EDGE_HOST_LOCKED` 环境变量传入一个「不含它」的列表即可。

---

## 四、配置速查表（vpngate.py / workflow env）

| 名称 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `_DEFAULT_EDGE_HOSTS` | 125 个实测域名 | 入口优选域名池（换域名改这里） |
| `EDGE_HOSTS` | = 默认池 | 覆盖入口池（环境变量） |
| `EDGE_HOST_LOCKED` | = 默认池 | 锁定名单：**按主机名存**（不带 `:端口`），不参与 DNS/TCP 检测，更新时不动 |
| `CHECK_WORKER` | 无（必填） | 检测 Worker，形如 `https://xxx/check?sstp=vpn:vpn@`（Actions 里来自 `secrets.DOMAIN`） |
| `CHECK_CONCURRENCY` | `4` | 检测并发。**不要调大**：Worker 上游有免费额度，32 并发会大面积 429 |
| `CHECK_TIMEOUT` | `60` | 单次检测超时（秒） |
| `CHECK_RETRIES` | `2` | 检测服务异常（429/5xx/超时）时的额外重试次数 |
| `CHECK_BACKOFF` | `2` | 重试退避基数（秒），线性增长 + 抖动 |
| `FETCH_RETRIES` | `3` | 每个数据源的抓取重试次数 |
| `MIN_SUCCESS` | `5` | 可用节点少于该值时，若上一版更多则保留上一版 |
| `KEEP_LAST_GOOD` | `1` | 置 `0` 关闭「沿用上一版产物」的降级能力 |
| `STRICT` | `0` | 置 `1`：降级也算失败（运行标红），默认只告警不标红 |
| `NODES_URL` / `DATA_URL` | 本仓库 Pages 地址 | 自动更新用到的固定地址（fork 后改成你自己的） |

> 再次强调：`vpngate.py` **不需要**配置 `EDT_UUID` 和 `EDT_DOMAIN`，这两个参数属于 edgetunnel 本身。

---

## 五、自动更新的健壮性设计（为什么不会再「整轮挂掉」）

每次运行都会在 `public/status.json`（同时发布到 Pages）写下结论：

| `outcome` | 含义 | 退出码 |
| :--- | :--- | :--- |
| `fresh` | 本轮拿到新数据并已发布 | 0（绿色） |
| `stale` | 本轮数据源/检测服务异常 → **沿用上一版产物**，订阅不中断 | 0（绿色 + `::warning::` 注解） |
| `failed` | 既拿不到新数据、又没有上一版产物 → 不发布空订阅 | 1（红色） |

关键规则：

1. **绝不发布空订阅**：只要本轮 0 个可用节点，就沿用上一版 `nodes.txt`（不会把 edgetunnel 的订阅清空）。
2. **数据源多源 + 退避重试**：官方 API（`http://www.vpngate.net/api/iphone/`）失败自动重试，再回退 GitHub 镜像。
3. **区分「节点不通」与「检测服务故障」**：Worker 返回 429、5xx、超时、`/api/lookup` 报错 → 属于检测服务故障，会自动重试；只有 `SSTP ...` 这类才是节点真的不通。
4. **诊断摘要**：无论成功还是降级，日志末尾都会打印一段「诊断摘要」（python/requests 版本、Worker 地址、并发与重试、入口池大小、成功/失败计数、耗时），出问题直接照抄即可。
5. **完整 traceback**：脚本异常时打印完整调用栈，不再只留一行信息。
6. **发布前产物自检**：`validate_nodes_text()` 逐行校验 `nodes.txt`（入口与节点主机必须是域名），格式不合格就**拒绝发布**并降级沿用上一版 —— 防止「运行绿色但订阅是坏的」（2026-10-07 踩过这个坑：入口池被按字符拆开，每行入口变成单个字母，而当时测试只数行数）。
7. **入口池解析**：`_env_csv()` 统一读取「逗号分隔」配置，**返回条目列表**，调用方不需要（也无法忘记）`.split(",")`。

> 想本地验证这些行为（无需联网）：`python tests/run_tests.py`（18 组故障场景，对比加固前后）。

---

## 六、常见问题

### 只有几个节点能连
入口优选域名大部分被墙。用 bestcf 重新测速，把 `_DEFAULT_EDGE_HOSTS` 换成实测能通的域名。

### 全部 -1
检查：edgetunnel 是否部署好、域名是否解析到 Cloudflare、UUID 是否填对、传输协议是否对得上。

### 12 小时没更新
到 Actions 页看最近一次运行；再看 `https://你的用户名.github.io/仓库名/status.json` 的 `outcome` 与 `reason`。

### 运行「0 秒」就失败（退出码 1）
说明脚本在**导入阶段**就炸了，最常见的两类原因：
1. `AttributeError: 'tuple' object has no attribute 'split'` —— 入口池常量被写成元组、又直接传给了 `os.environ.get()` 的默认值（2026-10-06 那次就是这个）。加固版已用 `_env_csv()` 统一归一化，不会再犯。
2. `ModuleNotFoundError: No module named 'requests'` —— pip 装依赖的解释器和跑脚本的解释器不是同一个。workflow 已改成 `python -m pip install` 并加了 `import requests` 自检。

### 运行是绿的，但节点没换（`status.json` 里 `outcome=stale`）
说明本轮没产出新数据，沿用了上一版。看 `reason` 字段：
- `检测服务全部异常 ... 429` → Worker 上游额度被打满。加固版已把并发降到 4 并自动重试；若仍然如此，说明 Worker 自身的上游额度/密钥需要处理（到 Cloudflare 看 Worker 日志）。
- `所有数据源都不可用` → VPN Gate 官方 API 与镜像同时挂了，等下一轮即可。
- `CHECK_WORKER 未配置或非法` → 仓库 Secrets 里的 `DOMAIN` 丢了，重新设置成 `https://你的Worker域名/check?sstp=vpn:vpn@`。

### 检测 Worker 报错 / 大面积成功率为 0
浏览器直接打开 `https://你的Worker/check?sstp=vpn:vpn@任意节点:端口` 看返回的 JSON：
- `"error": "... 429 Too Many Requests"` → Worker 上游限速，降低 `CHECK_CONCURRENCY` 或换用自带额度的查询接口。
- `"error": "SSTP server connection timed out"` → 该节点确实不通，属正常现象。

### 不知道 UUID 和节点域名在哪里看
登录 edgetunnel 后台（`https://你的域名/admin`），在后台首页就能看到。

---

*流水线：GitHub Actions（每 12 小时 cron） → vpngate.py（多数据源 + 重试 + 降级） → 检测 Worker → GitHub Pages*

---

## 引用与致谢

本项目的实现离不开以下开源项目和服务的支持，在此表示衷心的感谢：

| 项目 | 用途 | 链接 |
| :--- | :--- | :--- |
| **cmliu/edgetunnel** | VLESS 代理 + 链式代理，节点最终通过它使用 | https://github.com/cmliu/edgetunnel |
| **lsh8848/cm-Workers-CheckSocks5** | 检测 Worker：验证 SSTP 节点可用性并读取出口 IP | https://github.com/lsh8848/cm-Workers-CheckSocks5 |
| **fdciabdul/Vpngate-Scraper-API** | VPN Gate 节点数据的 GitHub 镜像（官方源失效时回退） | https://github.com/fdciabdul/Vpngate-Scraper-API |
| **VPN Gate** | SSTP 节点数据源 | https://www.vpngate.net/ |
| **Star History** | 提供项目热度曲线图生成服务 | https://star-history.com/ |

---

## 项目热度

[![Star History Chart](https://api.star-history.com/svg?repos=hezhanleiok/gate&type=Date)](https://star-history.com/#hezhanleiok/gate&Date)

---

**特别感谢**：
感谢所有为开源社区做出贡献的开发者们！没有你们的无私奉献，就没有这个项目的诞生。也感谢每一位使用、测试和反馈问题的用户，是你们的支持让这个项目不断完善。
