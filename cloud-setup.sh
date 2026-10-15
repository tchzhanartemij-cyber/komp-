#!/bin/bash
# Ставит мод limits (рамка «◆ Usage» над полем ввода) в каждую облачную сессию.
# Вставь этот текст в Setup script облачного окружения.
set -e
DIR=/opt/limits-mod
rm -rf "$DIR"
mkdir -p "$DIR/.claude-plugin"
cat > "$DIR/.claude-plugin/plugin.json" <<'LIMITS_EOF'
{ "name": "limits", "version": "0.1.0", "description": "Usage panel above the prompt: context, 5-hour and weekly limits with reset times", "types": "./types/index.d.ts" }
LIMITS_EOF
mkdir -p "$DIR/.claude-plugin"
cat > "$DIR/.claude-plugin/marketplace.json" <<'LIMITS_EOF'
{
  "name": "limits-mod",
  "owner": { "name": "tchzhanartemij-cyber" },
  "plugins": [{ "name": "limits", "source": "./" }]
}
LIMITS_EOF
mkdir -p "$DIR/hooks"
cat > "$DIR/hooks/hooks.json" <<'LIMITS_EOF'
{ "modules": ["./register.tsx"] }
LIMITS_EOF
mkdir -p "$DIR/hooks"
cat > "$DIR/hooks/register.tsx" <<'LIMITS_EOF'
import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { Snapshot } from '../types'

const snapshot = atom({ plugin: 'limits', key: 'snapshot' } as const, null)
const isHidden = atom({ plugin: 'limits', key: 'isHidden' } as const, false)

const ACCENT = '#d97757'
const LABEL_WIDTH = 13

const NAMES: Record<string, string> = {
  five_hour: 'Session 5h',
  seven_day: 'Weekly 7d',
  spend_limit: 'Spend',
}

export function formatNumber(n: number): string {
  return String(Math.round(n)).replace(/\B(?=(\d{3})+(?!\d))/g, ' ')
}

export function formatDate(iso: string): string {
  const d = new Date(iso)
  const two = (n: number) => String(n).padStart(2, '0')
  return `${two(d.getDate())}.${two(d.getMonth() + 1)}.${d.getFullYear()}, ${two(d.getHours())}:${two(d.getMinutes())}:${two(d.getSeconds())}`
}

export function filled(percent: number, width: number): number {
  return Math.round((Math.min(100, Math.max(0, percent)) / 100) * width)
}

function limitColor(percent: number): string {
  if (percent >= 90) return 'red'
  if (percent >= 70) return 'yellow'
  return 'green'
}

export function statusText(s: Snapshot): string {
  const parts = s.limits.map(l => `${NAMES[l.kind] ?? l.kind} ${Math.round(l.percentUsed)}%`)
  if (s.contextPercent !== undefined) parts.push(`context ${s.contextPercent}%`)
  if (s.costUsd !== undefined) parts.push(`$${s.costUsd.toFixed(2)}`)
  return parts.join(' · ')
}

type Row = { label: string; percent: number; detail: string; color: string }

export function rows(s: Snapshot): Row[] {
  const context: Row = {
    label: 'Context',
    percent: s.contextPercent ?? 0,
    detail:
      s.contextTokens !== undefined
        ? `${formatNumber(s.contextTokens)} / ${formatNumber(s.contextWindow)} tokens`
        : `— / ${formatNumber(s.contextWindow)} tokens`,
    color: 'white',
  }
  const limits = s.limits.map(l => ({
    label: NAMES[l.kind] ?? l.kind,
    percent: Math.round(l.percentUsed),
    detail: l.resetsAt ? `resets ${formatDate(l.resetsAt)}` : '',
    color: limitColor(l.percentUsed),
  }))
  return [context, ...limits]
}

async function measure($: EngineInterface) {
  const [usage, model] = await Promise.all([$.session.usage(), $.session.model()])
  const next: Snapshot = {
    model,
    contextPercent: usage.context.percent,
    contextTokens: usage.context.tokens,
    contextWindow: usage.context.window,
    limits: usage.rateLimits.map(l => ({ kind: l.kind, percentUsed: l.percentUsed, resetsAt: l.resetsAt })),
    costUsd: usage.cost?.usd,
  }
  await update($, snapshot, () => next)
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'limits',
      description: 'Show or hide the Usage panel above the prompt',
    })
    $.ui.status(undefined)
    await measure($)

    return next(e)
  })

  on('command.run', { command: 'limits' }, async $ => {
    await measure($)
    const hidden = await update($, isHidden, h => !h)
    const s = await read($, snapshot)

    return { text: hidden ? 'Usage panel hidden.' : `Usage: ${(s && statusText(s)) || 'no data yet'}` }
  })

  on('session.measure', async ($, e, next) => {
    const crossed = e.rateLimits.filter(l => l.percentUsed >= 90)
    if (e.changed.includes('rateLimits') && crossed.length > 0) {
      $.ui.toast(`Limit almost used up: ${crossed.map(l => `${NAMES[l.kind] ?? l.kind} ${Math.round(l.percentUsed)}%`).join(', ')}`)
    }
    await measure($)

    return next(e)
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const s = await read($, snapshot)
    if (e.props.hasSurvey || s === null || (await read($, isHidden))) {
      return next(e)
    }

    const { Box, Text } = $.ui.resolve(e)
    const list = rows(s)
    const detailWidth = Math.max(...list.map(r => r.detail.length))
    // border 2 + padding 2 + label + percent 5 + space before detail
    const width = Math.max(8, e.props.bodyColumns - 4 - LABEL_WIDTH - 5 - 1 - detailWidth)

    return (
      <Box flexDirection="column" borderStyle="round" borderColor="gray" paddingX={1}>
        <Text color={ACCENT} bold>
          ◆ Usage
        </Text>
        {list.map(r => {
          const done = filled(r.percent, width)
          return (
            <Text wrap="truncate-end">
              <Text dimColor>{r.label.padEnd(LABEL_WIDTH)}</Text>
              <Text color={r.color}>{'─'.repeat(done)}</Text>
              <Text dimColor>{'─'.repeat(width - done)}</Text>
              <Text bold>{`${r.percent}%`.padStart(5)}</Text>
              <Text dimColor> {r.detail}</Text>
            </Text>
          )
        })}
      </Box>
    )
  })
}
LIMITS_EOF
mkdir -p "$DIR/types"
cat > "$DIR/types/index.d.ts" <<'LIMITS_EOF'
export type Limit = { kind: string; percentUsed: number; resetsAt?: string }

export type Snapshot = {
  model: string
  contextPercent?: number
  contextTokens?: number
  contextWindow: number
  limits: Limit[]
  costUsd?: number
}

declare module 'claude-code' {
  interface PluginState {
    limits: { snapshot: Snapshot | null; isHidden: boolean }
  }
}
LIMITS_EOF
claude plugin marketplace add "$DIR" >/dev/null 2>&1 || claude plugin marketplace update limits-mod >/dev/null 2>&1 || true
claude plugin install limits@limits-mod --scope user
