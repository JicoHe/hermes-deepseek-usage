import {
  Codicon,
  DropdownMenuItem,
  STATUSBAR_AREAS,
  cn,
  host,
  queryClient,
  useQuery,
  useValue
} from '@hermes/plugin-sdk'
import { useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'deepseek-usage'
const QUERY_KEY = [ID, 4]

let rest = null

function bindRest(value) {
  rest = value
  return () => {
    rest = null
  }
}

function call(path, options) {
  // Balance and usage are local machine state, not per-connection data.
  return rest
    ? rest(path, { ...options, target: 'local-primary' })
    : Promise.reject(new Error('deepseek usage API unavailable'))
}

function useSummary() {
  return useQuery({
    queryFn: () => call('/summary'),
    queryKey: QUERY_KEY,
    refetchInterval: 60_000,
    staleTime: 30_000
  })
}

function tokens(value) {
  const n = Number(value)
  if (!Number.isFinite(n) || n <= 0) return '0'
  if (n >= 1e6) return `${(n / 1e6).toFixed(n >= 1e7 ? 0 : 1)}M`
  if (n >= 1e3) return `${(n / 1e3).toFixed(n >= 1e5 ? 0 : 1)}k`
  return String(Math.round(n))
}

function money(value, currency, digits) {
  const n = Number(value)
  if (value == null || Number.isNaN(n)) return '—'
  const amount = n.toFixed(digits ?? (n >= 100 ? 1 : 2))
  if (currency === 'CNY') return `¥${amount}`
  return currency === 'USD' ? `$${amount}` : `${currency || '?'} ${amount}`
}

function clock(epoch) {
  if (!Number.isFinite(epoch)) return '—'
  return new Date(epoch * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

// DeepSeek's peak window in Beijing time: Mon–Fri 09:00–12:00 and 14:00–18:00.
// Computed in the renderer so the badge flips at the boundary without waiting
// for a fetch; the backend prices with the same rule.
function isPeakNow(at = new Date()) {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: 'Asia/Shanghai', weekday: 'short', hour: 'numeric', hour12: false
  }).formatToParts(at)
  const pick = type => parts.find(part => part.type === type)?.value
  const weekday = pick('weekday')
  const hour = Number(pick('hour')) % 24
  if (weekday === 'Sat' || weekday === 'Sun') return false
  return (hour >= 9 && hour < 12) || (hour >= 14 && hour < 18)
}

// One letter, accent-tinted tile: peak hours read as "active", not as a warning.
// color-mix keeps the fill derived from the theme accent instead of a hard-coded
// blue, so it follows any skin the user switches to.
function PeakBadge() {
  return jsx('span', {
    'aria-label': 'DeepSeek 高峰时段',
    className: cn(
      'inline-flex h-3 w-3 items-center justify-center rounded-[3px]',
      'text-[0.5rem] font-medium leading-none tracking-[0.06em]'
    ),
    style: {
      color: 'var(--ui-accent)',
      backgroundColor: 'color-mix(in srgb, var(--ui-accent) 14%, transparent)'
    },
    children: 'H'
  })
}


// A status bar has room for a phrase, not a sentence: one line per outcome.
function balancePhrase(balance) {
  if (!balance || balance.ok !== true) {
    switch (balance?.error) {
      case 'no_api_key':
      case 'key_unreadable':
      case 'invalid_key':
        return '未配置密钥'
      case 'timeout':
        return '未响应'
      case 'tls':
      case 'network':
        return '连接失败'
      case 'auth':
        return '密钥失效'
      case 'rate_limited':
        return '限流'
      default:
        return '暂不可用'
    }
  }
  if (balance.status === 'zero') return '余额为 0'
  if (balance.status === 'grant_only') return '仅赠送额度'
  if (balance.status === 'unavailable') return '账户不可用'
  return null
}

function Chip() {
  const { data, isLoading } = useSummary()
  const balance = data?.balance
  if (isLoading) {
    return jsx('span', { className: 'tabular-nums text-(--ui-text-tertiary)', children: 'DS …' })
  }
  const phrase = balancePhrase(balance)
  if (phrase) {
    return jsx('span', { className: 'text-(--ui-text-tertiary)', children: `DS ${phrase}` })
  }
  return jsxs('span', {
    className: 'flex items-center gap-1',
    children: [
      isPeakNow() ? jsx(PeakBadge, {}) : null,
      jsx('span', { className: 'text-(--ui-text-quaternary)', children: 'DS' }),
      jsx('span', {
        className: 'tabular-nums tracking-tight text-(--ui-text-secondary)',
        children: money(balance.total, balance.currency)
      })
    ]
  })
}

function Detail({ label, value }) {
  return jsxs('div', {
    className: 'flex items-baseline justify-between gap-4 py-0.5',
    children: [
      jsx('div', { className: 'text-(--ui-text-tertiary)', children: label }),
      jsx('div', { className: 'tabular-nums text-(--ui-text-secondary)', children: value })
    ]
  })
}

function Menu() {
  const { data, isLoading, dataUpdatedAt } = useSummary()
  const conversation = useValue(host.state.focusedUsage)
  const [refreshing, setRefreshing] = useState(false)
  const [expanded, setExpanded] = useState(false)

  const balance = data?.balance
  const today = data?.today
  const phrase = balancePhrase(balance)

  const refresh = async () => {
    if (refreshing) return
    setRefreshing(true)
    try {
      await queryClient.cancelQueries({ queryKey: QUERY_KEY })
      const next = await call('/refresh', { method: 'POST' })
      queryClient.setQueryData(QUERY_KEY, next)
    } catch (_error) {
      // Keep the last good numbers; the timestamp below shows their age.
    } finally {
      setRefreshing(false)
    }
  }

  const todayTokens = today?.ok ? today.input + today.output : null
  const spent = balance?.ok && balance.spend_ok && Number.isFinite(balance.spent_today)
    ? balance.spent_today
    : null
  const liveTotal = Number(conversation?.input || 0) + Number(conversation?.output || 0)
  const contextPercent = Number(conversation?.context_percent)

  return jsxs('div', {
    className: 'w-[19rem] p-3 text-xs',
    children: [
      jsxs('div', {
        className: 'flex items-center justify-between gap-2',
        children: [
          jsxs('div', {
            className: 'flex items-center gap-1.5',
            children: [
              jsx('div', { className: 'text-(--ui-text-tertiary)', children: 'DeepSeek' }),
              isPeakNow() ? jsx(PeakBadge, {}) : null
            ]
          }),
          jsxs(DropdownMenuItem, {
            'aria-label': '刷新',
            className: cn(
              'inline-flex items-center gap-1 rounded px-1.5 py-0.5 transition-colors',
              'text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-foreground',
              'disabled:cursor-wait disabled:opacity-60'
            ),
            disabled: refreshing,
            onSelect: event => {
              event.preventDefault()
              void refresh()
            },
            children: [
              jsx(Codicon, { name: refreshing ? 'loading' : 'refresh', size: '0.75rem' }),
              '刷新'
            ]
          })
        ]
      }),

      phrase
        ? jsxs('div', {
            className: 'pt-1',
            children: [
              jsx('div', {
                className: 'text-[0.9375rem] font-medium text-foreground',
                children: phrase
              }),
              jsx('div', {
                className: 'pt-0.5 text-[0.6875rem] text-(--ui-text-tertiary)',
                children: isLoading ? '正在读取…' : '用量照常统计'
              })
            ]
          })
        : jsxs('div', {
            className: 'pt-1',
            children: [
              jsxs('div', {
                className: 'flex items-baseline gap-1.5',
                children: [
                  jsx('span', {
                    className:
                      'tabular-nums text-[1.375rem] font-semibold leading-none tracking-tight text-foreground',
                    children: isLoading ? '—' : money(balance.total, balance.currency)
                  }),
                  jsx('span', {
                    className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
                    children: '余额'
                  })
                ]
              }),
              spent != null
                ? jsx('div', {
                    className: 'pt-1 text-[0.6875rem] text-(--ui-text-tertiary)',
                    children: `今日 −${money(spent, balance.currency)}`
                  })
                : null
            ]
          }),

      jsx('div', { className: 'mt-3 border-t border-(--ui-stroke-secondary)' }),

      jsxs('div', {
        className: 'flex items-baseline justify-between gap-4 py-1.5',
        children: [
          jsx('div', { className: 'text-(--ui-text-secondary)', children: '今日用量' }),
          jsx('div', {
            className: 'tabular-nums text-foreground',
            children: todayTokens == null ? '—' : `${tokens(todayTokens)} tokens`
          })
        ]
      }),

      jsxs('div', {
        className: 'border-t border-(--ui-stroke-secondary) pt-2',
        children: [
          jsxs('button', {
            type: 'button',
            'aria-expanded': expanded,
            className:
              'flex w-full items-center justify-between gap-2 rounded text-(--ui-text-tertiary) hover:text-foreground',
            onClick: () => setExpanded(value => !value),
            children: [
              jsx('span', { children: expanded ? '收起' : '详情' }),
              jsx(Codicon, { name: expanded ? 'chevron-up' : 'chevron-down', size: '0.75rem' })
            ]
          }),
          expanded
            ? jsxs('div', {
                className: 'space-y-2 pt-2',
                children: [
                  jsx(Detail, {
                    label: '充值 / 赠送',
                    value: balance?.ok
                      ? `${money(balance.topped_up, balance.currency)} / ${money(balance.granted, balance.currency)}`
                      : '—'
                  }),
                  jsx(Detail, {
                    label: '今日输入 / 输出',
                    value: today?.ok
                      ? `${tokens(today.input)} / ${tokens(today.output)}${
                          today.cache_read ? ` · 缓存命中 ${tokens(today.cache_read)}` : ''
                        }`
                      : '—'
                  }),
                  jsx(Detail, {
                    label: '今日调用',
                    value: today?.ok ? `${today.calls} 次 · ${today.sessions} 会话` : '—'
                  }),
                  jsx(Detail, {
                    label: '估算成本',
                    value: (() => {
                      const est = today?.estimated_cost_cny
                      if (!today?.ok || !est?.ok) return '—'
                      const base = `¥${Number(est.cny || 0).toFixed(2)}`
                      const share = est.peak_share
                      if (typeof share !== 'number' || share <= 0 || share >= 1) return base
                      return `${base} · ${Math.round(share * 100)}% 高峰`
                    })()
                  }),
                  jsx(Detail, {
                    label: '本对话',
                    value: liveTotal
                      ? `${tokens(liveTotal)}${
                          Number.isFinite(contextPercent) ? ` · 上下文 ${Math.round(contextPercent)}%` : ''
                        }`
                      : '暂无记录'
                  }),
                  jsx(Detail, {
                    label: '更新',
                    value: dataUpdatedAt ? clock(dataUpdatedAt / 1000) : '—'
                  }),
                  jsx('div', {
                    className: 'pt-1 text-[0.625rem] text-(--ui-text-quaternary)',
                    title:
                      '用量与估算只计今天：昨天之前开启的会话按其今天产生的对话比例折算（对照当日余额差误差约 2%）。余额差自当天首次读数起算，含赠送额度；价目表来自 DeepSeek 官方，空闲/高峰按会话起止时间折算。',
                    children: `余额差自 ${clock(
                      balance?.opening_at
                    )} 起算 · 今日按活动折算`
                  })
                ]
              })
            : null
        ]
      })
    ]
  })
}

export default {
  id: ID,
  name: 'DeepSeek Usage',
  description: 'DeepSeek balance and Hermes-side usage in the status bar.',
  register(ctx) {
    ctx.onDispose(bindRest(ctx.rest))
    ctx.register({
      id: 'chip',
      area: STATUSBAR_AREAS.right,
      order: 127,
      data: {
        id: `${ID}:chip`,
        label: jsx(Chip, {}),
        title: 'DeepSeek 余额与用量',
        toggleLabel: 'DeepSeek 用量',
        variant: 'menu',
        menuAlign: 'end',
        menuClassName: 'w-auto p-0',
        menuContent: jsx(Menu, {})
      }
    })
  }
}
