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

> **入口条目格式**：仅支持「域名」或「IPv4:端口」（端口可省，默认 443）。**IPv6 字面量（如 `[2606:4700::1]:443`）暂不支持** —— 节点行格式与 edgetunnel 解析侧都只认域名/IPv4；混入 IPv6 条目会被脚本剔除并告警，不会中断发布。重复条目会被自动去重，不影响轮换分布。

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
| `PREFER_NEW_NODES` | `1` | 检测额度优先给「上一版没有的新节点」+ 住宅型；置 `0` 关闭。⚠️ 副作用见下方说明 |
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

## 发布后心跳（heartbeat.py / heartbeat.yml）

`check.yml` 每 12 小时才产出一版，而 VPN Gate 的 SSTP 节点常在几十分钟内成批失效。
`vpngate.py` 的 stale 保护在**发布层** —— 它保证「这一轮产不出东西时别把订阅改差」，
但管不了「发布成功之后、下一轮之前节点死掉」。这段空窗里订阅照发，客户端拿到的是死节点，
而 gate 要等 12 小时才知道。

`heartbeat.yml` 每小时把**线上已发布**的 `nodes.txt` 拿回来抽查，把连续多轮确认不通的节点摘掉并重发，
把「订阅变差」的窗口从 12 小时压到 1 小时。

三条不可退让的约束（破坏性操作必须有约束，否则它比问题本身更危险）：

1. **只摘「确认节点侧不通」的。** `target_ratelimit`（限流未测）与 `worker_error`（检测服务异常）
   一律保留 —— 它们代表「没测成」而不是「测出来不通」。把未测当不通，会在 Worker 抽风的那一小时里
   把整个订阅洗掉。
2. **同一节点连续 `HEARTBEAT_CONFIRM` 轮确认才摘，且当轮必须仍是 `node_dead`。** 证据跨轮累积在
   `heartbeat.json`；单轮结论可能是误判（路由抖动、证书问题）。旧证据不能支撑新动作 —— 一个已连判
   两轮的节点，第三轮因 Worker 抽风变成「未测」时，绝不能因为 streak 仍是 2 就被摘。
3. **摘后不足 `HEARTBEAT_MIN_ALIVE`，或单轮确认不通占比超过 `HEARTBEAT_MAX_DEAD_RATIO` → 不摘**，
   改用退出码 `3` 让 workflow 跑一次完整重跑。大面积失效要重新抓源，不是就地砍订阅。

### 规模假设（先看这一条，再决定要不要开）

`auto` 是**分段**的，不是一条公式。写成一条会在 N 大小时算错：

```text
N ≤ CEIL（默认 48）:  budget = max(FLOOR, N)                          # 一轮全测
N > CEIL:             budget = min(CEIL, ceil(N / (12 / confirm)))     # 按 12h 窗口分摊
```

第一段存在的理由：分批有代价。同样是 N=17，`budget=12` 要 `ceil(17/12) × 2 = 4h` 才凑够确认，
一轮全测只要 2h，而「窗口短」正是心跳存在的全部意义。

**注意第二段只在 `N > CEIL` 时生效。** 如果拿它去算 N=48，会得到 `min(48, ceil(48/6)) = 8` ——
和实际行为（48）差 6 倍。先判 N 落在哪一段，再套公式。

| N（线上节点数） | auto 取值 | 覆盖一轮 | 2 轮确认 | 结论 |
| ---: | ---: | ---: | ---: | :--- |
| 17 | 17（全测） | 1h | 2h | 一轮测完 |
| 48 | 48（全测） | 1h | 2h | 一轮测完 |
| 49 | 9 → 12（下限） | 5h | 10h | 刚过第一段边界 |
| 100 | 17 | 6h | 12h | 刚好压进发布周期 |
| 200 | 34 | 6h | 12h | 刚好压进发布周期 |
| **288** | 48 | 6h | 12h | **auto 的硬边界** |
| 4000 | 48 | 84h | 168h | 无解，只剩 `scale_warning` |

**288 不是估值，是算出来的硬边界**：`CEIL × 12 / confirm = 48 × 12 / 2`。超过它，`auto` 也凑不齐
12 小时窗口 —— 别把 `auto` 当成「永远有效」。到了那一步只有三条路：调小 `HEARTBEAT_CONFIRM`
（用更少的确认换时效）、调大 `CEIL`（用更多请求换覆盖）、或者接受「心跳只测不摘，摘除交给 12 小时
主流程」。`test_auto_has_a_hard_validity_ceiling_at_288` 把这个数字钉在测试里，改 `CEIL` 或
`CONFIRM` 时会立刻失败 —— 否则「auto 就没事了」会变成一句静默的错误。

超出的**症状不是报错，而是「每小时都在跑、从来不摘」**：证据还没攒够，`check.yml` 就发布了新产物，
把 `heartbeat.json` 换掉了。按固定值配置且超出时，`heartbeat.json` 会带 `scale_warning` 并打
`::warning::`，别忽略它。

### 状态为什么会丢（以及怎么不丢）

Pages 部署是**整站替换**：谁部署，站点上就只剩谁的 `public/`。`vpngate.py` 的产物里没有
`heartbeat.json`，所以 `check.yml` 部署完的瞬间会把死链证据清零 —— 即使预算算得刚好，证据也会
在每 12 小时被抹一次。因此 `check.yml` 里多了一步 **`Preserve heartbeat state`**：部署前把线上的
`heartbeat.json` / `heartbeat-history.jsonl` 下载回 `public/`（取不到就跳过，不阻断主流程）。

没有这一步，`confirm=2` 在 `ceil(N/budget) > 6` 时会永远凑不齐。这也是为什么「只改 `heartbeat.py`
不改 `check.yml`」在 N 稍大时是无效配置。

### 退出码

| 退出码 | 含义 | workflow 行为 |
| ---: | :--- | :--- |
| `0` | 正常（可能没有任何改动，见 `heartbeat.json.changed`） | 照常部署 |
| `1` | 拿不到线上 `nodes.txt` | `::error::`，跳过部署，线上保持原样 |
| `3` | 大面积失效：单轮死亡占比过半（`escalate`），或摘了会低于 `MIN_ALIVE`（`hold`） | 同一次运行内执行 `vpngate.py`，再部署 |
| `4` | 镜像站点时遇到临时读取失败（503/超时/空 body） | `::warning::`，跳过部署，下一小时自动重试 |

`3` 是**在同一份 workflow 里**跑 `python vpngate.py`，不用 `workflow_dispatch`（那需要
`actions: write`，容易踩 403），也不靠空 commit 触发。

**两种大面积失效共用这一个出口**：`escalate`（单轮死亡占比过半）与 `hold`（摘了会低于
`HEARTBEAT_MIN_ALIVE`）。`decide()` 先判 `hold` 再判 `escalate`，所以最严重的场景全被 `hold`
兜住 —— 只让 `escalate` 重跑，等于漏掉最该重跑的那一档。

### 冷却：是持续态，不是时长

这里**没有** `HEARTBEAT_ESCALATE_COOLDOWN` 这个变量。所谓冷却不是「停 6 小时」，而是
「计数超过 `HEARTBEAT_ESCALATE_MAX` 之后，每一轮都只记录、不重跑」，直到某一轮恢复正常：

```text
escalate_streak 的变化规则
  escalate / hold   -> +1        (大面积失效，累加)
  ok / pruned       -> 0         (真的测完了，且没触发失效闸门 —— 只有这才算恢复)
  invalid_output    -> 0         (检测完成了，只是写出来的产物不合格；不算失效)
  notest            -> 保持不变   (一个都没测成：既证明不了失效，也证明不了恢复)
```

`invalid_output` 的归属容易含糊，所以代码里是**显式赋值**而不是靠计算顺序：它上面那次清零来自
`verdict == "pruned"`，属于「顺序恰好」，一旦有人把改写产物的那段挪到 streak 计算之前，行为就变了。

`notest` 不清零这一条容易被忽略，但后果很实在：Worker 抽风一小时 → 攒够的计数归零 → 又要重数两轮。
这与 `decide()` 里「未测不该让死链计数凭空复活」是同一条原则 —— 没测成不等于好了。

于是「节点长期大面积死透」时的行为是：**每小时检测（照常）、每轮判 `escalate_cooldown`（不重跑）、
计数继续累加**，直到 `check.yml` 的 12 小时主流程重新抓源。这是一个稳定态，不是 bug。成本是每小时
十几次 Worker 请求（相对完整流水线的上百次可忽略），换来的是恢复后立刻能确认，而不是两眼一黑。

节点真的恢复后（`ratio` 回落到阈值以下、或摘除不再跌破下限），那一轮判 `ok`/`pruned`，计数清零，
心跳回到正常摘除逻辑。

**连续 `notest` 时的行为**（这是个容易被误猜的地方）：`outcome` 如实写 `notest`，**不会**变成
`escalate_cooldown` —— 冷却只在「本轮真的判失效（`escalate`/`hold`）且计数已超限」时出现，检测端全挂
时什么都没测到，把它写成「确认失效、只是冷却」会误导读者。`escalate_streak` 原样保持，不增不减。

由此带来一个取舍：如果 `streak` 已经达到 `MAX`，检测端挂了 6 小时再恢复、节点仍然全死，那么恢复后
第一轮就又是 `escalate_cooldown`（计数已是 `MAX+1`），不重跑。这是有意为之 —— **旧证据只会导致
「少做」，不会导致「乱做」**。最长延迟就是一个发布周期（12 小时），由 `check.yml` 强制重跑兜底，
所以不需要给证据加额外的保质期。

**冷却期间仍然检测。** 冷却的目的是「别再每小时跑一遍完整流水线」，不是「别再看了」：只关掉重跑
那一步，采样、判定、写 `heartbeat.json` / 时序记录照常。另外 `escalate` 与 `hold` 都**不摘任何
节点**（`decide()` 在这两种情况下都返回空的 `prune`）—— 大面积失效要靠重新抓源，不是就地砍订阅。

### 怎么读 `heartbeat.json`

| 字段 | 取值 | 含义 |
| :--- | :--- | :--- |
| `outcome` | `ok` / `pruned` / `hold` / `notest` / `invalid_output` / `escalate` / `escalate_cooldown` | 本轮**结论**（已叠加冷却） |
| `verdict` | 同上，但没有 `escalate_cooldown` / `invalid_output` | `decide()` 的原始判定。进冷却后 `outcome` 会变成 `escalate_cooldown`，靠它才能知道「为什么」 |
| `escalate_streak` | 整数 | 连续大面积失效的轮数；`ok`/`pruned` 清零，`notest` 保持 |
| `untested_ratio` | 0–1 | 持续 > 0.3 说明大量请求被判 `worker_error`/熔断，心跳在「只测不摘」 |

七个 `outcome` 的区别：

- `ok` —— 测完了，没有达到确认阈值的节点，不动手
- `pruned` —— 摘掉了确认失效的节点
- `hold` —— 该摘，但摘了会低于 `HEARTBEAT_MIN_ALIVE`。**不摘任何节点**（`prune` 为空），改触发重跑 ——
  重跑是重新抓源，就地砍订阅只会让节点数掉到下限以下。这是最严重的一档。
- `notest` —— 一个都没测成（检测端不可用 / 全被熔断）。**不摘、不重跑、`streak` 保持不变**，
  本轮无结论
- `invalid_output` —— 改写后的 `nodes.txt` 没过格式自检，放弃本次改写，原文一字不动。
  **不重跑**（重跑修不了格式），但 `escalate_streak` **清零** —— 检测本身完成了，只是产物不合格
- `escalate` —— 单轮死亡占比超阈值，触发重跑
- `escalate_cooldown` —— 计数已超 `HEARTBEAT_ESCALATE_MAX`，只记录、不重跑

### 「线上没有」和「这次没读到」不是一回事

镜像时每个文件都区分三种结果：

- `404/410` → **缺失**，允许继续（首次部署还没生成 `endpoints.json` 就是这种）；
- `5xx/超时/200+空 body` → **读取失败**，本轮**不部署**（退出码 4）。整站替换下，漏镜像一个文件
  等于主动把它从线上删掉，一次网络抖动换一个线上故障不划算。
- `403/401/429` 也归**读取失败**，不是缺失。它们说的是「这个文件存在但你这次拿不到」
  （权限/限流），按缺失处理就会在部署时把它删掉。只有 `404/410` 代表「它本来就不在」。

`index.html` 是唯一允许兜底的：确实 404 就用仓库模板 `render_html()` 生成；读取失败则仍然中止，
因为兜底会把线上页面换成旧模板。

状态码是从异常里取的，因为 `vg._http_get` 对 4xx 是**抛 `_NoRetryHTTPError`**（而不是返回
`resp.status_code`），`r.status_code == 404` 那个分支永远走不到。取值顺序：`exc.status_code` →
`exc.response.status_code`（`requests.HTTPError` 只挂 `response`，408/429 重试耗尽时就是它）→
从消息里用 `\bHTTP[ /](\d{3})\b` 抠。三级都取不到就按读失败处理 —— 保守方向不会误删线上文件。

### 时序记录（`heartbeat-history.jsonl`）

每轮每个被测节点追加一行 `{t,h,p,ok,kind,ms,act}`，超过上限只保留最近若干行。它是「按历史存活率
排序」替代「按本轮延迟排序」唯一缺的数据，而数据本身是检测的副产品。

**上限默认是 `auto`，不是固定 4000。** 固定行数是个陷阱：4000 行在 N=17、每轮 12 行时够两周，
在一轮全测 48 个时只够 3.5 天 —— 「保留约两周」这句话必须由 N 决定。`auto` 按
`每轮行数 × 24 × HEARTBEAT_HISTORY_DAYS`（默认 7 天）算，且不低于 `HEARTBEAT_HISTORY_FLOOR`（4000）。
设 `HEARTBEAT_HISTORY_MAX=0` 关闭。

两种「读不到旧文件」必须分开，因为它们该触发相反的动作：

- `404`（从没跑过 / 首轮）→ 从空开始，正常追加；
- `5xx`/超时 → **本轮跳过追加**。用空历史覆盖线上等于把攒了几周的数据一次删光，比少记一轮严重得多。

**「跳过追加」只影响这个文件**：判定、摘除、`heartbeat.json` 全都照常。时序记录是可观测性的副产品，
不能反过来阻塞主判定 —— 那是把辅助数据当成了主链路。

### 状态丢了怎么办

线上「没有 `heartbeat.json`」有两种原因，后果完全不同：首轮（正常）与状态被删（`Preserve` 步骤失败 /
人为清理）。后者会让 `streak` 归零，连判过一轮的节点要重新攒够 `confirm` 轮 —— **功能静默退化，而表面
上一切正常**。单看一个文件无法区分，所以：

- `load_prev_heartbeat()` 在「有已发布节点却读不到状态」时打一条明确告警，让日志里至少留痕；
- `check.yml` 的 `Preserve` 步骤区分 `404/410`（真没有，正常跳过）与其它状态码（读失败，
  打 `::warning::`）。静默跳过是这套机制里最危险的失败方式 —— check 日志全绿、心跳也在跑，
  而证据每 12 小时被抹一次。

### 与其它机制的关系

- **`MERGE_LAST_GOOD=1`**：`check.yml` 会把上一版仍可用的节点合并回新版本，因此心跳摘掉的节点
  可能在下一轮 check 中复现。这是预期行为 —— check 会重新验证可用性，心跳只是临时摘除。
- **Worker 额度**：心跳请求量小但持续。如果 `heartbeat.json.untested_ratio` 长期 > 0.3，说明大量
  请求被判为 `worker_error`/熔断，心跳实际在「只测不摘」，应降预算或扩 Worker。
- **面板口径**：`countries` / `available` / `stats.success` / `residential_est` / `datacenter_est`
  是**当前状态**（摘除后同步裁剪）；`raw_nodes` / `sstp_nodes` / `checked` / `carried_over` 是
  **那一轮的历史**，保持原值不伪造。所以「已检测 62 / 可用 3」是有意为之，不是数字对不上。

| 名称 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `HEARTBEAT_BUDGET` | `auto` | 单轮复检数。`auto` 分段：`N ≤ CEIL` → `max(FLOOR, N)`；`N > CEIL` → `min(CEIL, ceil(N/(12/confirm)))` |
| `HEARTBEAT_BUDGET_FLOOR` / `_CEIL` | `12` / `48` | `auto` 的上下限 |
| `HEARTBEAT_CONCURRENCY` | `2` | 并发，必须低于主流程 |
| `HEARTBEAT_TIMEOUT` | `30` | 单次复检超时（秒），压过 10 分钟会与下一轮重叠 |
| `HEARTBEAT_CONFIRM` | `2` | 连续 N 轮确认才摘除 |
| `HEARTBEAT_MIN_ALIVE` | `MIN_SUCCESS` | 摘除后剩余下限，低于此值不动手 |
| `HEARTBEAT_MAX_DEAD_RATIO` | `0.5` | 单轮确认不通占比超过该值 → 升级为完整重跑 |
| `HEARTBEAT_ESCALATE_MAX` | `2` | 连续几轮大面积失效后停止重跑。**冷却没有时长**，是「超过后每轮都只记录」的持续态 |
| `HEARTBEAT_HISTORY_MAX` | `auto` | 时序记录保留行数；`auto` = 每轮行数 × 24 × 天数，`0` 关闭 |
| `HEARTBEAT_HISTORY_DAYS` | `7` | `auto` 的目标保留天数 |
| `HEARTBEAT_HISTORY_FLOOR` | `4000` | `auto` 的行数下限（小站点不会少于这个） |

另外两点值得知道：

- **部署是整站替换。** 心跳会先把线上 `index.html` / `status.json` / `check-worker.js` /
  `endpoints.json` / `data.json` 镜像到 `public/`，再改其中需要改的文件。
- **不碰 `status.json`。** 那份文件是「那一轮生成」的结论；心跳改写它会让面板数字与 `data.json`
  对不上。心跳自己的结论写在 `heartbeat.json`（含 `outcome` / 本轮口径 / 死链证据 / 采样游标 /
  `escalate_streak` / `scale_warning`）。
- 采样带游标轮转，`ceil(N/budget)` 轮内可覆盖全表；不想用就删掉 `heartbeat.yml`，或在 Actions 里
  disable 该 workflow（`heartbeat.json` / `heartbeat-history.jsonl` 会在下一次 check 部署时消失，
  不影响任何订阅）。

离线测试（54 个用例，覆盖「该摘 / 不该摘」的边界、镜像失败分支、预算伸缩与落盘结果）：

```bash
PYTHONPATH=tests python -m unittest discover -s tests -p "test_heartbeat*.py" -v
```

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
| `rate_limited_ports` | 本轮被熔断的端口列表（只有 `skipped > 0` 的端口才在这里） |
| `port_429_detail` | **所有出现 429 的端口**（含未触发熔断的），每项 `candidates` / `candidates_in_source` / `probed_429` / `skipped`；`skipped=0` 表示有 429 但未达熔断阈值 |
| `port_429_detail[].candidates` | 该端口**本轮计划检测**的候选数 —— 受 `CHECK_BUDGET` / `MAX_CHECK_NODES` 截断影响，**不是数据源原始总数** |
| `port_429_detail[].candidates_in_source` | 该端口在**数据源里**的候选总数（截断前）。判断「是否整段误杀」要看这个 |
| `new_nodes` | 本轮新增可用节点数（上一版没有的） |

> ⚠️ **读 `failure_breakdown` 别重复计数**：被端口熔断跳过的节点 `failure_kind` 同样是
> `target_ratelimit`（它是「未测」，不是「不通」），所以
> **`target_ratelimit` = 真撞 429 的数量 + 熔断跳过的数量**，二者是包含关系、不能相加。
> 想看有几个节点真的撞到了 429，读 `target_ratelimit_probed`。
> `port_429_detail` 里对应端口（如 `port=443`）那一项的 `skipped`，是 `target_ratelimit` 的一部分，不是额外一拨。`skipped=0` 表示该端口出现了 429 但没触发熔断。
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
> 注：`port_429_detail` 是 **list-of-dict**（形如
> `[{"port": 443, "candidates": 20, ...}]`），不是以端口为 key 的 dict ——
> JSON 的 key 只能是字符串，写成 dict 会让消费方拿到 `"443"` 而不是 `443`。
>
> **日志时间戳的时区**：日志行首的 `[HH:MM:SS]` 是 **runner 本地时间**。
> GitHub Actions 的 runner 是 UTC，与 `status.json` 的 `generated_at`（UTC）一致，
> 线上可直接对齐；但**本地运行**时日志是本地时间而 `status.json` 是 UTC，会差一个时区偏移。

### 一轮跑完怎么判读（按顺序）

**第 1 步：`port_429_detail` 里有没有 443？**

| 情况 | 有没有 443 | 含义 | 下一步 |
| :--- | :--- | :--- | :--- |
| 443 被检测，撞了 1–3 次 429、没熔断 | **有**（`probed_429 > 0`, `skipped = 0`） | 限流缓解但仍存在 | 算 `probed_429 : skipped` 与 `skipped / candidates_in_source`，判阈值是否太高 |
| 443 被检测，0 次 429 | 没有 | 这轮 443 没限流 | **不需要判阈值**（没限流时阈值无关紧要） |
| 443 根本没被检测（被截断 / 源里没有） | 没有 | 没数据 | 看日志 `CLOUDFLARE WORKER` 段或 `status.candidates` 确认 |

> ⚠️ 「没有 443」**不等于埋点又漏了**。只有「该判但没数据」才是盲区，而那种情况
> 已被 `port_429_detail` 的扩展覆盖（1–3 次 429 未熔断也会记录）。
> 判读时先确认 443 是否被检测，再区分「不需要判」还是「没数据」。

**第 2 步：`new_nodes` 是否正常？**

不要只比绝对值 —— `new_nodes` 受 VPN Gate 换血影响（源里新节点基数每轮都不同）。更稳的是看比例：

- `new_nodes / checked` 是否与上轮同阶（参考值：约 96%）
- 或 `new_nodes` 是否与「源里新节点数 × 正常成功率」同阶

`new_nodes=40` 而源里新节点只有 50，与 `new_nodes=44` 而源里新节点 80 **含义不同**，不能直接比绝对值。

关键规则：

1. **绝不发布空订阅**：只要本轮 0 个可用节点，就沿用上一版 `nodes.txt`（不会把 edgetunnel 的订阅清空）。
2. **数据源多源 + 退避重试**：官方 API（`http://www.vpngate.net/api/iphone/`）失败自动重试，再回退 GitHub 镜像。
3. **区分「节点不通」与「检测服务故障」**：Worker 返回 429、5xx、超时、`/api/lookup` 报错 → 属于检测服务故障，会自动重试；只有 `SSTP ...` 这类才是节点真的不通。4xx 配置错误（如 403）同样归入 `worker_error` 并计入 `worker_errors`，但**不会**触发重试 —— 重试同样请求只会得到同样 4xx，徒增额度消耗。
4. **诊断摘要**：无论成功还是降级，日志末尾都会打印一段「诊断摘要」（python/requests 版本、Worker 地址、并发与重试、入口池大小、成功/失败计数、耗时），出问题直接照抄即可。
5. **完整 traceback**：脚本异常时打印完整调用栈，不再只留一行信息。
6. **发布前产物自检**：`validate_nodes_text()` 逐行校验 `nodes.txt`（入口与节点主机必须是域名），格式不合格就**拒绝发布**并降级沿用上一版 —— 防止「运行绿色但订阅是坏的」（2026-10-07 踩过这个坑：入口池被按字符拆开，每行入口变成单个字母，而当时测试只数行数）。
7. **入口池解析**：`_env_csv()` 统一读取「逗号分隔」配置，**返回条目列表**，调用方不需要（也无法忘记）`.split(",")`。

> 想本地验证这些行为（无需联网）：`python tests/run_tests.py`（17 组故障场景）。
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
> `rate_limited_ports`（被熔断的端口）、`port_429_detail`（**所有出现 429 的端口**，
> 含未熔断的：候选数 / 真撞 429 节点数 / 跳过数）、`new_nodes`（本轮新增可用节点数）。
> 三类之和恒等于失败总数 —— 不对就是脚本的归因出 bug 了，日志里会有「失败归因计数不自洽」告警。
>
> ⚠️ **读 `failure_breakdown` 时别重复计数**：被端口熔断跳过的节点 `failure_kind` 也是
> `target_ratelimit`（它是「未测」不是「不通」），所以
> **`target_ratelimit` = 真撞 429 的节点数 + 熔断跳过的节点数**，两者是包含关系、不能相加。
> 想看有几个节点真撞到了 429，读 `target_ratelimit_probed`。
> 同理 `port_429_detail` 里对应端口（如 `port=443`）那一项的 `skipped`，
> 也是 `target_ratelimit` 的一部分，不是额外一拨（`skipped=0` 表示有 429 但未达熔断阈值）。

### `PREFER_NEW_NODES` 的固有副作用（启用前必读）

把检测额度优先给「新节点」换来的是**新鲜度**，代价是**单节点检测成本上升**：

- 新节点是上一版没验证过的，质量未知，**超时概率高于**已被验证过的老节点。
- 2026-10-21 实测对比：`CHECK_BUDGET=60` 轮（新节点优先）`elapsed=103.6s / 60 个 = 1.73s/节点`；
  全量轮 `duration_s` 总共 82.3s / 88 个（即使全算检测时间也只有 ≤0.94s/节点）。
  即**单节点成本约 1.8 倍以上**。这个对比是干净的：`elapsed` 只统计 `check_all`，
  不含数据源抓取等任何前置步骤。
- 注意 `duration_s − elapsed`（那轮为 32.6s）**不等于** fetch 耗时 —— 它包含
  `check_all` 之外的所有步骤：数据源抓取（含重试）、下载上一版 nodes.txt/data.json、
  优选 API（3 个接口，单个超时 20s 起步）、入口池 DNS/TCP 检测、写产物。
  日志没有逐段时间戳，因此不再往下细分归因。
- 因此在 `CHECK_BUDGET` 生效（源规模 > budget）时，**总耗时可能不降反升**。
  不要指望「设了 budget 就一定更快」。

> ⚠️ **没测 ≠ 不可用**：被 `CHECK_BUDGET` / `MAX_CHECK_NODES` 截掉的节点**本轮完全没被检测**，
> 它们的可用性是未知的，不能当成「确认不可用」。2026-10-21 那轮 83 个候选截到 60 个，
> 成功 37；全量轮（88 个候选）成功 46 —— 差的 9 个里有约 6 个来自被截掉的 23 个，
> 这是**估算**（按成功率外推），不是实测值。

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

### 运行时间逼近 45 分钟超时怎么办
443 长期限流时，全局自适应退避会反复暂停所有检测线程（特征：`status.json` 的 `rate_limited_ports` 长期含 443、日志多次出现「全局自适应退避触发」）。两个旋钮可缓解：
- 设 `CHECK_BUDGET`（如 `60`）限制单轮检测量；
- 调低 `PORT_CB_THRESHOLD`（如 `2`）让端口熔断更早生效，把额度让给其它端口。

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
| 2026-10-30 | v1.7.5 | **`invalid_output` 的计数归属写死**（五轮评审）：① `escalate_streak` 对它**显式赋 0**，不再依赖「`verdict=pruned` 先算、`status` 后改」这个顺序巧合 —— 原来行为是对的，但把「改写产物」那段挪到 streak 计算之前就会变；② README 的 `streak` 规则表补上 `invalid_output -> 0` 一行并说明理由（检测本身完成，只是产物不合格，不算失效）；③ 新增**真实可触发**的端到端用例：`abc#...@abc:443` 这种没有点的主机名能过 `NODE_LINE_RE` 但过不了 `validate_nodes_text`，一行即可触发该分支，断言 `outcome=invalid_output` / `exit 0` / `nodes.txt` 一字不动 / `pruned_count=0` / `escalate_streak=0`。测试 72 → 73 |
| 2026-10-30 | v1.7.4 | **两个边界收口**（四轮评审）：① `hold` 时 `prune` **本来就是空集**（`decide()` 返回 `"prune": set()`），所以重跑闸门放开后不会出现「摘到低于 `MIN_ALIVE`」—— 已在 README 的 `outcome` 表里写明「不摘任何节点」，并保留 `test_hold_triggers_rerun_not_just_escalate` 的 `nodes.txt` 断言；② README 补「连续 `notest`」的行为：`outcome` 如实写 `notest`，**不会**变成 `escalate_cooldown`（冷却只在「本轮真判失效且超限」时出现），`escalate_streak` 原样保持；并说明由此带来的取舍 —— 旧证据只会导致「少做」不会导致「乱做」，最长延迟一个发布周期，由 `check.yml` 兜底。测试 71 → 72（连跑 3 轮 `notest` 验证计数不变、状态不伪装） |
| 2026-10-30 | v1.7.3 | **冷却语义与重跑闸门修正**（三轮评审）：① `hold`（摘了会低于 `MIN_ALIVE`）现在**也会触发完整重跑** —— `decide()` 先判 `hold` 再判 `escalate`，只让 `escalate` 重跑等于漏掉最严重的一档，而它的 `reason` 里本来就写着「应重跑完整流水线」；② `notest`（一个都没测成）不再清零 `escalate_streak`，`hold` 也不再清零而是累加 —— 只有 `ok`/`pruned` 算恢复，与 `decide()` 对「未测」的口径一致；③ `heartbeat.json` 新增 `verdict` 字段保留 `decide()` 原始判定（进冷却后 `outcome` 变 `escalate_cooldown`，否则「为什么进冷却」就丢了）；④ 产物格式自检失败改用 `outcome=invalid_output`，不再与 `decide()` 的 `hold` 混用同一个名字（前者重跑也没用，后者重跑才是解法）；⑤ README 写明**冷却没有时长**（是「计数超 `MAX` 后每轮只记录」的持续态，不是「停 6 小时」）、`streak` 三条变化规则、七种 `outcome` 的含义；⑥ README 把 `auto` 预算写成**分段公式**并补 N=49 的边界行 —— 用单一公式算 N=48 会得到 8，与实际 48 差 6 倍。测试 68 → 71 |
| 2026-10-30 | v1.7.2 | **规模与失败可见性收口**（二轮评审）：① `auto` 预算改为优先「一轮全测」—— `N ≤ CEIL` 时每轮测完全部节点，确认窗口 = `confirm` 小时（N=17 从 4h 缩到 2h），`N > CEIL` 才分批；② 把 `auto` 的**硬边界 288**（`CEIL × 12 / confirm`）写进 README 并用 `test_auto_has_a_hard_validity_ceiling_at_288` 钉住，避免「auto 永远有效」的误读；③ `check.yml` 的 `Preserve heartbeat state` 区分 `404/410`（真没有，静默）与其它码（读失败，`::warning::`），并注释了「必须在 `vpngate.py` 之后、`Upload Pages artifact` 之前」的顺序约束；④ `load_prev_heartbeat()` 在「有已发布节点却读不到状态」时告警 —— 状态被清空会让 `streak` 归零、功能静默退化，而表面上一切正常；⑤ 时序记录上限改 `auto`（每轮行数 × 24 × 天数，下限 4000）：固定 4000 在「一轮全测 48 个」时只够 3.5 天，「保留约两周」必须由 N 决定；⑥ `_status_of` 补 `exc.response.status_code` 一级并加词边界（`requests.HTTPError` 只挂 `response`），`403/401/429` 明确归「读失败」而非「缺失」；⑦ README 写明 `escalate_cooldown` 期间**仍然检测**，只关掉重跑。测试 54 → 68（新增 288 边界、一轮全测、403/401/429 归因、状态码三级取值、行数上限换算、状态缺失告警与其静默边界） |
| 2026-10-30 | v1.7.1 | **心跳的规模与状态修正**（评审 P0/P1）：① `check.yml` 新增 `Preserve heartbeat state` —— Pages 是整站替换，原先每次 check 发布都会把 `heartbeat.json` 连同死链证据一起清掉，`confirm=2` 在 `ceil(N/budget) > 6` 时永远凑不齐（症状是「每小时都在跑、从来不摘」）；② 预算支持 `HEARTBEAT_BUDGET=auto`，按 `ceil(N/(12/confirm))` 反推（下限 12 / 上限 48），超出发布周期时写 `scale_warning` 并告警；③ 退出码 `4` 区分「线上没有这个文件」(404，允许继续) 与「这次没读到」(5xx/超时/200+空 body，禁止部署) —— `vg._http_get` 对 4xx 是抛 `_NoRetryHTTPError`，404 判断必须从异常里取状态码才生效（冒烟抓到）；④ 连续 `HEARTBEAT_ESCALATE_MAX` 次完整重跑后进入 `escalate_cooldown`，不再每小时烧一遍上百请求；⑤ 新增 `heartbeat-history.jsonl` 时序记录（每轮每节点一行，默认保留 4000 行，读失败则整轮不写，避免覆盖已攒数据）；⑥ `heartbeat.json` 增加 `untested_ratio` / `escalate_streak` / `staging_missing` / `staging_errors` / `budget_auto`。测试 30 → 54 个（新增镜像失败分支、预算伸缩、升级冷却、时序裁剪与「常量与使用点形状」结构断言） |
| 2026-10-30 | v1.7.0 | **新增发布后心跳**（`heartbeat.py` + `heartbeat.yml`）：每小时复检线上 `nodes.txt`，把连续 `HEARTBEAT_CONFIRM` 轮确认不通的节点摘掉并重发，把「发布后 12 小时空窗」压到 1 小时。三条硬约束：只摘 `failure_kind=node_dead`（`target_ratelimit`/`worker_error` 一律保留，未测不等于不通）、证据跨轮累积且当轮必须仍是 `node_dead`（旧证据不能支撑摘除动作）、摘后低于 `HEARTBEAT_MIN_ALIVE` 或单轮死亡占比超 `HEARTBEAT_MAX_DEAD_RATIO` 则改用退出码 3 触发完整重跑。复用 `vpngate.check_one`/`PortCircuitBreaker`/`AdaptiveLimiter`，归因与熔断口径零分叉；部署前先镜像线上整站产物（Pages 部署是整站替换，漏镜像会把 `index.html`/`check-worker.js` 从线上删掉）；`data.json` 同步裁剪且 `raw_nodes`/`checked` 等「那一轮」字段保持原值，心跳结论独立写 `heartbeat.json`，不改 `status.json`。新增 30 个离线用例（`test_heartbeat.py` 判定边界 + `test_heartbeat_flow.py` 全流程落盘） |
| 2026-10-24 | v1.6.4 | **复查完善**：`_http_get` 对 4xx（非 408/429）不再重试（URL 写错/鉴权失败重试无意义，与 `check_one` 口径一致）；`filter_edge_hosts` 新增第 0 步——入口去重 + IPv6 字面量明确剔除并告警（此前 IPv6 条目会被 DNS 检测误报「无 A 记录」，`EDGE_DNS_CHECK=0` 时更会流进产物导致发布前自检整轮拒发；节点行格式仅支持域名/IPv4，README 已声明）；`parse_mirror_json` 的 `country_short` 不再用国家全名兜底（避免 `countries.code` 出现 "Japan" 类非 2 位码，展示层回退 `country_long`）；`data.json` stats 新增 `carried_over`（MERGE 模式下区分「本轮实测」与「携带合并」）；workflow 开启 Pages 步骤增加返回码检查，失败时打明确告警并指引手动开启，查询返回 403 时同样告警（注：GITHUB_TOKEN 无 `administration` 权限键可用，该建议不可行）；`Show site URL` 改为按仓库动态拼接（fork/改名后自动正确）；`HOSTS_ENTRY` 显式路径共用同一道「去重+IPv6 剔除」清理（`_drop_unsupported_entries`），人工指定仍优先于黑名单/DNS 检测，`build_nodes_text` 的回退分支同样过清理（入口池全为不支持的格式时准确降级为「入口池配置异常」，而非误导性的「节点行格式自检未通过」），清理告警按「IPv6 条目 / 含冒号脏条目」分类；测试新增 `mirror_404` 场景与 `filter_edge_hosts` 单元断言 |
| 2026-10-24 | v1.6.3 | **检查与完善**：`_is_cloudflare` 改用 Cloudflare 官方公布网段（ips-v4/ips-v6）精确匹配 —— 旧版 4 个「首两段」近似段漏掉 `141.101` / `108.162` / `188.114` / `190.93` / `173.245` / `103.2x` / `131.0.72` 等官方段，入口解析到这些段会被误判「疑似污染」而剔除；同时 `104.28-31` / `172.72-127` / `198.41.0-127` 放得过宽（CF 实际只到 /13 与 /17）；IPv6 前缀 `2a06:4700` 系笔误（官方段是 `2606:4700::/32` 等 7 段）。workflow「首次运行自动开启 Pages」改用 POST 创建站点（PUT 只能更新已存在的站点，对未开启 Pages 的仓库恒 404，该步骤此前从不生效），且不再携带与 `build_type=workflow` 矛盾的 `source` 参数；`CHECK_TIMEOUT` 代码默认值 90→60 与文档/CI 对齐；`log()` 加锁防并发日志交错；优选 API 行的主机名必须含点（拒绝 `abc` 这类单标签脏数据）；还原 v1.6.1 变更记录（此前表格串行损坏）；测试新增 `_is_cloudflare` 网段与 `_normalize_optimal_line` 解析的单元断言 |
| 2026-10-21 | v1.6.2 | **`port_429_detail` 记录范围扩展**：从「只记被熔断的端口」改为「所有出现 429 的端口 ∪ 熔断端口」，并同步更名（原名 `rate_limited_ports_detail` 是 `rate_limited_ports` 的附属，扩展后会含不在其中的端口，保留原名会误导）。原因：只记熔断端口会形成**反向激励** —— 越是「阈值可能设错了」的情况（限流缓解到没触发熔断）越拿不到 `candidates_in_source`，阈值就永远判不了。现在 `skipped=0` 表示「有 429 但未熔断」；`candidates` 与 `probed_429 + skipped` 的守恒只对已熔断端口成立。测试新增 `port_detail_nonempty` 守卫（`rl_breakdown` 场景：9 个端口各撞一次 429、不熔断，仍须进 detail）。README 补 `PREFER_NEW_NODES` 的固有副作用（新节点超时概率更高，单节点成本约 1.8 倍，设 budget 不一定更快）与「没测 ≠ 不可用」说明 |
| 2026-10-21 | v1.6.1 | **修复 `PREFER_NEW_NODES` 排序方向写反**（写成「老节点优先」，与语义相反；`CHECK_BUDGET=0` 时不触发截断分支故一直未暴露）；`status.json` 增加 `target_ratelimit_probed` 与 `port_429_detail`，以区分「限流 14」中的包含关系；`port_429_detail` 采用 list-of-dict（`port` 为 int，避免 JSON key 字符串化）；测试新增 `budget_new_first` 场景 |
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
