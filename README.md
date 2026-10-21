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
- 位置：`_DEFAULT_EDGE_HOSTS`（当前 124 个，bestcf 三批实测结果合并去重）

### 改法（两种，任选其一）
1. **改常量**（推荐）：打开 `vpngate.py`，把 `_DEFAULT_EDGE_HOSTS` 里的域名列表换成你测出来的（逗号分隔，格式 `域名:443`），提交推送，等下一次自动运行或手动触发 Action。
2. **用环境变量覆盖**（不动代码）：在 `.github/workflows/check.yml` 主步骤的 `env:` 里加一行 `EDGE_HOSTS: "a.com:443,b.com:443"`。

### 本地测速工具（BestCF）
本地测优选域名推荐用 BestCF 在线工具：`https://bestcf.fxxk.dedyn.io/`（纯前端，打开即用）。

- **判断可用**的双检逻辑：① TCP 443 握手能通；② 请求 `https://域名/cdn-cgi/trace` 返回含 `ip=` 行
- 只支持 Cloudflare 官方 CDN 域名；测出的域名按 `域名:443` 逗号分隔填入 `_DEFAULT_EDGE_HOSTS`（或 `EDGE_HOSTS`）
- 2026-10-12 的 125→124 入口池清理就是用这套逻辑实测的（唯一失效：`cmcc.cc.cd` DNS 解析失败）

### 关于「锁定」（更新时不动）
`EDGE_HOST_LOCKED` 默认等于 `_DEFAULT_EDGE_HOSTS`：**锁定名单里的域名在自动更新中永远保留** —— 跳过 DNS 污染检测与 TCP 连通性检测。

> 设计意图：`_DEFAULT_EDGE_HOSTS` 放的是你自己实测过、确定可用的域名，机器人不要自作主张去动它们。
> 若某个域名希望重新参与自动检测，用 `EDGE_HOST_LOCKED` 环境变量传入一个「不含它」的列表即可。

---

## 四、内置自动优选 API（不用再手动测优选）

静态优选域名池有个硬伤：它是**一次性测速、永远不变**的，IP 质量会随时间劣化，要换就得人肉重测、再提交一次。

现在 `vpngate.py` 每轮运行都会去「优选 API」现取一批当轮最优 IP，放在入口池**最前面**优先轮换，静态域名降级为兜底。

### 配置项

| 名称 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `OPTIMAL_API` | 电信 / 联通 / 移动三条线路 | 优选接口地址，英文逗号分隔可写多个；设 `off`（或 `0` / `none` / `false`）关闭 |
| `OPTIMAL_MAX` | `24` | 单轮最多采纳多少个优选 IP |
| `OPTIMAL_TIMEOUT` | `20` | 单个优选接口超时（秒） |

### 行为要点

- **接口格式**：响应是纯文本，每行 `IP#名字`，**不带端口**（`port` 只是查询参数），脚本自动补 `:443`。
- **失败不阻断发布**：任何一个接口超时/报错只告警；**全部失败则本轮纯用静态入口池**，绝不产出空订阅。
- **裸 IP 只测连通性**：优选 IP 没有 DNS 记录可查，因此只做 TCP 可达性测试，不参与 DNS 污染判定 —— 否则会被 100% 误判成「无 A 记录」而全量剔除。
- **本轮结果写进 `data.json`** 的 `optimal_api` 字段（数量 / 具体 IP / 用了哪些接口），方便排查「到底换没换」。
- **换线路**：改 `vpngate.py` 里的 `_DEFAULT_OPTIMAL_API`，或在 workflow `env` 里传 `OPTIMAL_API`。

> 优选 IP 来自第三方接口，采纳其结果等于信任该服务。它只影响「入口地址」，
> 不接触你的订阅内容、UUID 或 Cloudflare 凭据 —— 但如果介意，把它设成 `OPTIMAL_API=off` 即可完全回到纯静态池。

---

## 五、配置速查表（vpngate.py / workflow env）

| 名称 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `_DEFAULT_EDGE_HOSTS` | 124 个实测域名 | 入口优选域名池（换域名改这里） |
| `EDGE_HOSTS` | = 默认池 | 覆盖入口池（环境变量） |
| `EDGE_HOST_LOCKED` | = 默认池 | 锁定名单：**按主机名存**（不带 `:端口`），不参与 DNS/TCP 检测，更新时不动 |
| `OPTIMAL_API` | 电信/联通/移动 3 个接口 | 内置自动优选接口地址，英文逗号分隔；设 `off` 关闭（见第四节） |
| `OPTIMAL_MAX` | `24` | 单轮最多采纳多少个优选 IP |
| `OPTIMAL_TIMEOUT` | `20` | 单个优选接口超时（秒） |
| `CHECK_WORKER` | 无（必填） | 检测 Worker，形如 `https://xxx/check?sstp=vpn:vpn@`（Actions 里来自 `secrets.DOMAIN`） |
| `CHECK_CONCURRENCY` | `4` | 检测并发。**不要调大**：Worker 上游有免费额度，32 并发会大面积 429 |
| `CHECK_TIMEOUT` | `60` | 单次检测超时（秒） |
| `CHECK_RETRIES` | `2` | **检测服务异常**（5xx/网关/超时）时的额外重试次数 |
| `TARGET_RL_RETRIES` | `0` | **目标侧限流（429）**的额外重试次数，默认 `0` —— 重试无效且会加剧限流 |
| `PORT_CB_THRESHOLD` | `4` | 同一端口连续多少次目标侧限流后熔断该端口（本轮不再检测，额度让给其它端口） |
| `CHECK_BUDGET` | `0` | 单轮最多检测多少个节点（`0` = 不限）。额度会优先给新节点与住宅型 |
| `PREFER_NEW_NODES` | `1` | 检测额度优先给「上一版没有的新节点」+ 住宅型；置 `0` 关闭 |
| `GLOBAL_RL_RATIO` | `0.35` | 滑窗内目标侧限流占比超过该值 → 全局退避（放大请求间隔） |
| `GLOBAL_RL_PAUSE` | `10` | 限流占比超过阈值 2 倍时的全局暂停（秒） |
| `MERGE_LAST_GOOD` | `0` | 置 `1`：本轮通过的节点与上一版仍可用节点合并去重后一起发布 |
| `FETCH_RETRIES` | `3` | 每个数据源的抓取重试次数 |
| `MIN_SUCCESS` | `5` | 可用节点少于该值时，若上一版更多则保留上一版 |
| `KEEP_LAST_GOOD` | `1` | 置 `0` 关闭「沿用上一版产物」的降级能力 |
| `STRICT` | `0` | 置 `1`：降级也算失败（运行标红），默认只告警不标红 |
| `NODES_URL` / `DATA_URL` | 本仓库 Pages 地址 | 自动更新用到的固定地址（fork 后改成你自己的） |

> 再次强调：`vpngate.py` **不需要**配置 `EDT_UUID` 和 `EDT_DOMAIN`，这两个参数属于 edgetunnel 本身。

---

## 六、自动更新的健壮性设计（为什么不会再「整轮挂掉」）

每次运行都会在 `public/status.json`（同时发布到 Pages）写下结论：

| `outcome` | 含义 | 退出码 |
| :--- | :--- | :--- |
| `fresh` | 本轮拿到新数据并已发布 | 0（绿色） |
| `stale` | 本轮数据源/检测服务异常 → **沿用上一版产物**，订阅不中断 | 0（绿色 + `::warning::` 注解） |
| `failed` | 既拿不到新数据、又没有上一版产物 → 不发布空订阅 | 1（红色） |

排查时要读的字段：

| 字段 | 含义 |
| :--- | :--- |
| `failure_breakdown.target_ratelimit` | 目标侧限流**归因总数**（含熔断跳过，见下方警告） |
| `failure_breakdown.target_ratelimit_probed` | 真实撞到 429 的**节点数**（不含熔断跳过）。默认 `TARGET_RL_RETRIES=0` 时等于请求次数；把该值调大后一个节点可能发多次请求，此字段仍按节点计 |
| `failure_breakdown.node_dead` / `worker_error` | 节点真不通 / 检测服务异常（非节点侧） |
| `rate_limited_ports` | 本轮被熔断的端口列表 |
| `rate_limited_ports_detail` | 每个熔断端口的 `candidates` / `candidates_in_source` / `probed_429` / `skipped` |
| `rate_limited_ports_detail[].candidates` | 该端口**本轮计划检测**的候选数 —— 受 `CHECK_BUDGET` / `MAX_CHECK_NODES` 截断影响，**不是数据源原始总数** |
| `rate_limited_ports_detail[].candidates_in_source` | 该端口在**数据源里**的候选总数（截断前）。判断「是否整段误杀」要看这个 |
| `new_nodes` | 本轮新增可用节点数（上一版没有的） |

> ⚠️ **读 `failure_breakdown` 别重复计数**：被端口熔断跳过的节点 `failure_kind` 同样是
> `target_ratelimit`（它是「未测」，不是「不通」），所以
> **`target_ratelimit` = 真撞 429 的数量 + 熔断跳过的数量**，二者是包含关系、不能相加。
> 想看有几个节点真的撞到了 429，读 `target_ratelimit_probed`。
> `rate_limited_ports_detail` 里对应端口（如 `port=443`）那一项的 `skipped`，是 `target_ratelimit` 的一部分，不是额外一拨。
> 读 `candidates` 时注意它已被 `CHECK_BUDGET` 截断，要看数据源里的真实规模读 `candidates_in_source`。
>
> **怎么判断熔断阈值（`PORT_CB_THRESHOLD`）合不合适**：看 `probed_429 : skipped` 的比值。
> 比值接近或大于 1（跳过的比实测的还多）说明熔断来得太晚、多烧了额度；
> 实测数远大于阈值才熔断，说明阈值偏低。配合 `candidates_in_source` 还能看出是否整段误杀。
>
> **怎么判断 `PREFER_NEW_NODES` 是否真的生效**：先看日志 `VPN GATE` 那行的「去重后 N」。
> 只有 **N > `CHECK_BUDGET`** 时截断才真正发生 —— 若 N ≤ budget，排序函数虽然被调用，
> 但 `ordered[:budget]` 不截断，「谁排前面」对产物没有任何可观测影响，
> 该轮只是又一次回归检查，**不能据此说排序功能已验证**。
>
> 注：`rate_limited_ports_detail` 是 **list-of-dict**（形如
> `[{"port": 443, "candidates": 20, ...}]`），不是以端口为 key 的 dict ——
> JSON 的 key 只能是字符串，写成 dict 会让消费方拿到 `"443"` 而不是 `443`。

关键规则：

1. **绝不发布空订阅**：只要本轮 0 个可用节点，就沿用上一版 `nodes.txt`（不会把 edgetunnel 的订阅清空）。
2. **数据源多源 + 退避重试**：官方 API（`http://www.vpngate.net/api/iphone/`）失败自动重试，再回退 GitHub 镜像。
3. **区分「节点不通」与「检测服务故障」**：Worker 返回 429、5xx、超时、`/api/lookup` 报错 → 属于检测服务故障，会自动重试；只有 `SSTP ...` 这类才是节点真的不通。4xx 配置错误（如 403）同样归入 `worker_error` 并计入 `worker_errors`，但**不会**触发重试 —— 重试同样请求只会得到同样 4xx，徒增额度消耗。
4. **诊断摘要**：无论成功还是降级，日志末尾都会打印一段「诊断摘要」（python/requests 版本、Worker 地址、并发与重试、入口池大小、成功/失败计数、耗时），出问题直接照抄即可。
5. **完整 traceback**：脚本异常时打印完整调用栈，不再只留一行信息。
6. **发布前产物自检**：`validate_nodes_text()` 逐行校验 `nodes.txt`（入口与节点主机必须是域名），格式不合格就**拒绝发布**并降级沿用上一版 —— 防止「运行绿色但订阅是坏的」（2026-10-07 踩过这个坑：入口池被按字符拆开，每行入口变成单个字母，而当时测试只数行数）。
7. **入口池解析**：`_env_csv()` 统一读取「逗号分隔」配置，**返回条目列表**，调用方不需要（也无法忘记）`.split(",")`。

> 想本地验证这些行为（无需联网）：`python tests/run_tests.py`（16 组故障场景）。
> 沙箱环境若因批量删除被拦，换一个空的运行目录即可：`TEST_RUNS_DIR=tests/runs_v2 python tests/run_tests.py`。

---

## 七、常见问题

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
- `本轮所有节点都被目标服务器限流 (429 ...)` → **目标服务器把我们的出口 IP 限速了**，不是节点不通。已在 `failure_breakdown.target_ratelimit` 单列。脚本会自动按端口熔断、省下额度给其它端口；若长期这样，说明该批目标确实在限流 Cloudflare 出口，等下一轮换一批节点即可。
- `检测服务全部异常 ...` → Worker 上游额度被打满。并发已降到 4 并自动重试；若仍然如此，到 Cloudflare 看 Worker 日志。
- `所有数据源都不可用` → VPN Gate 官方 API 与镜像同时挂了，等下一轮即可。
- `CHECK_WORKER 未配置或非法` → 仓库 Secrets 里的 `DOMAIN` 丢了，重新设置成 `https://你的Worker域名/check?sstp=vpn:vpn@`。

> **怎么看本轮到底发生了什么**：直接看 `status.json` 的 `failure_breakdown`
> （`target_ratelimit` / `worker_error` / `node_dead` 三类分别多少个）、
> `rate_limited_ports`（被熔断的端口）、`rate_limited_ports_detail`（每个被熔断端口的
> 候选数 / 真撞 429 节点数 / 跳过数）、`new_nodes`（本轮新增可用节点数）。
> 三类之和恒等于失败总数 —— 不对就是脚本的归因出 bug 了，日志里会有「失败归因计数不自洽」告警。
>
> ⚠️ **读 `failure_breakdown` 时别重复计数**：被端口熔断跳过的节点 `failure_kind` 也是
> `target_ratelimit`（它是「未测」不是「不通」），所以
> **`target_ratelimit` = 真撞 429 的节点数 + 熔断跳过的节点数**，两者是包含关系、不能相加。
> 想看有几个节点真撞到了 429，读 `target_ratelimit_probed`。
> 同理 `rate_limited_ports_detail` 里对应端口（如 `port=443`）那一项的 `skipped`，
> 也是 `target_ratelimit` 的一部分，不是额外一拨。

### 关于「429 限流」与「节点不通」的区别（重要）
两者都会被检测 Worker 判成 `success=false`，但含义完全不同：

| 现象 | 含义 | 脚本处理 |
| :--- | :--- | :--- |
| `Target /api/lookup request failed: HTTP/1.1 429` | 目标服务器限流，**没测成**，不是节点的锅 | 归为 `target_ratelimit`，默认**不重试**；同一端口连续 4 次即熔断本轮该端口 |
| `SSTP server connection timed out` | 节点真的不通 | 归为 `node_dead`，不重试 |
| `HTTP 408/425/429/5xx` | 检测服务瞬时故障或上游限流 | 归为 `worker_error`，按 `CHECK_RETRIES` 重试 |
| `HTTP 4xx`（非 408/425/429，如 403/404） | Worker 路由配错 / 鉴权缺失 / Cloudflare 前置拦截 —— 配置问题 | 归 `worker_error`（非节点侧），但**不重试**（重试同样请求只会得到同样 4xx） |

> 2026-10-21 实测：`:443` 端口 100% 返回 429（两轮重复测试无一例外），非 443 端口大多正常。
> 旧版把 429 当成「检测服务异常」重试 2 次（退避仅 2~5 秒），对持续限流毫无作用，
> 反而把请求量放大 3 倍、加剧限流 —— 这是「抓不到新节点」的真正原因。

### 检测 Worker 报错 / 大面积成功率为 0
浏览器直接打开 `https://你的Worker/check?sstp=vpn:vpn@任意节点:端口` 看返回的 JSON：
- `"error": "... 429 Too Many Requests"` → Worker 上游限速，降低 `CHECK_CONCURRENCY` 或换用自带额度的查询接口。
- `"error": "SSTP server connection timed out"` → 该节点确实不通，属正常现象。

### 不知道 UUID 和节点域名在哪里看
登录 edgetunnel 后台（`https://你的域名/admin`），在后台首页就能看到。

---

*流水线：GitHub Actions（每 12 小时 cron） → vpngate.py（多数据源 + 重试 + 降级） → 检测 Worker → GitHub Pages*

---

## 更新记录

| 日期 | 版本 | 变更 |
| :--- | :--- | :--- |
| 2026-10-21 | v1.6.1 | **修复 `PREFER_NEW_NODES` 排序键写反**（写成「老节点优先」，与语义相反；`CHECK_BUDGET=0` 时不触发截断分支故一直未暴露）；`status.json` 增加 `target_ratelimit_probed` 与 `rate_limited_ports_detail`，以区分「限流 14」中的包含关系；`rate_limited_ports_detail` 采用 list-of-dict（`port` 为 int，避免 JSON key 字符串化）；测试新增 `budget_new_first` 场景 |
| 2026-10-21 | v1.6.0 | **修复「抓不到新节点」**：失败三级归因（`target_ratelimit`/`worker_error`/`node_dead`），429 默认不重试；新增按端口自适应熔断（`PORT_CB_THRESHOLD`）+ 全局滑窗退避（`GLOBAL_RL_*`）；新增检测预算 `CHECK_BUDGET` 与「新节点/住宅型优先」（`PREFER_NEW_NODES`）；`status.json` 增加 `failure_breakdown`/`rate_limited_ports`/`new_nodes`；修复降级分支不写 `status.json` 的 bug（`failed` 时产物缺失导致误判「脚本没跑」）；测试新增 `port_cb`、`rl_breakdown`、`worker_http403` 三场景 |
| 2026-10-12 | v1.5.1 | **配置修正**：本地实测 125 个静态优选域名（TCP 443 + `/cdn-cgi/trace` 双检），剔除 DNS 失效的 `cmcc.cc.cd:443`，入口池 125 → 124 个 |
| 2026-10-12 | v1.5.0 | **行为变更（minor）**：新增 `OPTIMAL_API` 内置自动优选 —— 每轮运行从优选接口现取优选 IP 置于入口池首，支持 `off`/`0`/`none`/`false` 关闭；`filter_edge_hosts` 新增裸 IP 分支（只做 TCP 检测，不参与 DNS 污染判定）；`data.json` 新增 `optimal_api` 字段；`tests/run_tests.py` 回归环境设 `OPTIMAL_API=off` 保证离线可复现 |

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
