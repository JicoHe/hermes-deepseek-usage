# DeepSeek Usage · Hermes Desktop 插件

在 Hermes 桌面端状态栏上显示 DeepSeek 账户余额，点开能看今天的 token 用量和人民币花费。
处于**高峰时段**时，前面会多一个蓝色的 `H` 小方块——一眼看出现在按几倍单价计费。

```
        ╭───╮
        │ H │  DS ¥18.42        ← 高峰时段
        ╰───╯
              DS ¥18.42        ← 空闲时段
```

## 显示什么

**状态栏**（一行，只给一个判断）：

| 内容 | 含义 |
|---|---|
| `H` | 现在处于 DeepSeek 高峰时段（北京时间 周一至周五 09:00–12:00、14:00–18:00） |
| `DS ¥18.42` | 账户余额，来自 DeepSeek 官方 `/user/balance` |

**点开菜单**：

- 余额、今日余额差（真实扣费）
- 今日 token：输入 / 输出 / 缓存命中 / API 调用次数
- 估算成本：按官方价目表算的人民币金额，空闲价与高峰价分别加权
- 本对话用量

## 安装

```bash
hermes plugins install JicoHe/hermes-deepseek-usage --enable
hermes gateway restart
```

然后打开 Hermes → 设置 → 能力（Capabilities）→ 插件，把 **DeepSeek Usage** 的桌面半区打开。
桌面 UI 是包里的 `desktop/plugin.js`，Hermes 会自动复制到 `~/.hermes/desktop-plugins/deepseek-usage/`；
两个半区（后端 + 桌面）默认都是关的，需要各开一次，这是 Hermes 的安全边界。

也可以先克隆再本地安装：

```bash
git clone https://github.com/JicoHe/hermes-deepseek-usage ~/Projects/hermes-deepseek-usage
hermes plugins install ~/Projects/hermes-deepseek-usage --enable
hermes gateway restart
```

## API key 从哪来

按顺序找：

1. 环境变量 `DEEPSEEK_API_KEY`（推荐，写在 `~/.hermes/.env` 里）
2. `~/.codex/config.toml` 里的 `experimental_bearer_token`
   —— 如果你按 [DeepSeek 官方文档](https://api-docs.deepseek.com/zh-cn/quick_start/agent_integrations/codex) 把 Codex CLI 接到了 DeepSeek，这里已经有 key

key 只在 Python 后端读取，渲染层只能看到金额；错误信息里不会回显 key。
余额请求走 `curl`（不是 Python 自带 CA），因为在有 TLS 拦截代理的机器上后者会失败。

## 数字是怎么算的

| 数字 | 来源 | 口径 |
|---|---|---|
| 余额 | `GET /user/balance` | 官方接口，20 秒缓存 |
| 今日花费 | 日初余额 − 当前余额 | 真实扣费，含赠送额度消耗；充值会自动重新基准 |
| 今日 token | `state.db` → `session_model_usage` | 这张表是**按会话累计**的，所以跨天会话按"当天产生多少对话"折算（对照余额差，2026-11-03 实测误差约 2%） |
| 缓存命中 | 同上 `cache_read_tokens` | DeepSeek 上下文缓存，按缓存价计费，比未命中便宜 50 倍 |
| 估算成本 | 官方人民币价目表 | 每小时段按实际上所处时段加权，不引任何汇率 |

价目表（元 / 百万 token，括号内为高峰价），写在 `deepseek_usage/core.py` 的 `CNY_PRICES`：

| 模型 | 缓存命中 | 未命中输入 | 输出 |
|---|---|---|---|
| `deepseek-flash` | 0.02 (0.04) | 1.0 (2.0) | 4.0 (8.0) |
| `deepseek-v4-pro` | 0.15 (0.30) | 4.5 (9.0) | 13.5 (27.0) |

来源：<https://api-docs.deepseek.com/zh-cn/quick_start/pricing>（核对日期 2026-11-02）。
官方调价后改这一个字典即可，不需要动别的地方。

## 自己改

```
hermes-deepseek-usage/
├── plugin.yaml               # 包元数据
├── deepseek_usage/core.py    # 余额、用量聚合、价目表、时段判定
├── dashboard/plugin_api.py   # GET /api/plugins/deepseek-usage/summary
├── desktop/plugin.js         # 状态栏 chip + 下拉菜单（纯 ESM，无构建）
└── tests/test_core.py        # 37 个离线测试
```

最常见的三处改动：

```python
# deepseek_usage/core.py
CNY_PRICES = {...}            # 调价 / 加模型
BALANCE_TTL_SECONDS = 20      # 余额轮询间隔
USAGE_TTL_SECONDS = 15        # 用量缓存
```

```js
// desktop/plugin.js —— 芯片外观（H 方块是 12px 正方形）
'inline-flex h-3 w-3 items-center justify-center rounded-[3px]'
```

颜色全部走主题变量（`var(--ui-accent)` 等），所以换皮肤时会跟着变，不要写死色值。

开发时用软链接挂进插件目录，保存即热更新：

```bash
ln -s ~/Projects/hermes-deepseek-usage ~/.hermes/plugins/deepseek-usage
python3 -m unittest discover -s tests
node --input-type=module --check < desktop/plugin.js
hermes plugins validate .
```

## 已知限制

- DeepSeek 只开放了余额接口，没有逐次调用明细，所以时段分摊和跨天折算都是**估算**，界面上也照实标注
- Hermes 的 `state.db` 没有每次 API 调用的时间戳，高峰/空闲只能按会话起止窗口加权
- 用 Codex CLI 直连 DeepSeek 的消耗不经过 Hermes，不计入"今日 token"
- 法定节假日官方按空闲价，这里按周一至周五判断，节假日会**略微高估**
- 非 CNY 账户会原样显示币种，不做汇率换算（换算出来的数字不如不显示）

## English

A Hermes Desktop status-bar plugin for DeepSeek: account balance, today's tokens,
and an RMB cost estimate from DeepSeek's official price list — with peak / off-peak
hours applied per session. An accent-tinted `H` badge appears while peak pricing is
in effect.

```bash
hermes plugins install JicoHe/hermes-deepseek-usage --enable
hermes gateway restart
```

The key comes from `DEEPSEEK_API_KEY` or `~/.codex/config.toml`. It never reaches the
renderer. Today's usage is prorated for sessions that started earlier, and cost stays
in CNY — no FX conversion, ever. Tune `CNY_PRICES` in `deepseek_usage/core.py`.

## License

MIT © 2026 JicoHe. Built for [Hermes Agent](https://hermes-agent.nousresearch.com).
