// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { StrategyWalkForward } from './StrategyWalkForward'

const fixture = vi.hoisted(() => ({ result: {} as Record<string, unknown> }))

vi.mock('@tanstack/react-query', () => ({ useQuery: () => ({}) }))
vi.mock('@/components/data/DailyDataHealthNotice', () => ({ DailyResultStaleNotice: () => null }))
vi.mock('@/lib/walkforwardTask', () => ({
  useWalkForwardTask: () => ({ result: fixture.result }),
  tryReconnectWalkForward: () => false,
  clearWalkForward: () => {},
  startWalkForward: () => {},
  stopWalkForward: () => {},
}))

describe('walk-forward results', () => {
  let container: HTMLDivElement
  let root: Root

  beforeEach(() => {
    vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
    container = document.createElement('div')
    root = createRoot(container)
  })

  afterEach(() => {
    act(() => root.unmount())
    vi.unstubAllGlobals()
  })

  it('shows why overlapping folds cannot compound and retains individual returns and tail dates', () => {
    fixture.result = {
      n_folds: 1, n_skipped: 0, n_planned_folds: 1, elapsed_ms: 1,
      folds: [{ index: 0, test_start: '2024-10-08', test_end: '2024-10-09', evaluation_end: '2024-10-10',
        oos_stats: { total_return: 0.1 } }],
      summary: { compounded_oos_return: null, consistency: 1, oos_equity_curve: [],
        aggregation_eligible: false, aggregation_reasons: ['overlapping_windows'],
        fold_returns: [{ index: 0, return: 0.1, status: 'observed' }] },
    }
    act(() => root.render(<StrategyWalkForward />))
    expect(container.textContent).toContain('样本外窗口重叠')
    expect(container.textContent).toContain('实际评估截至 2024-10-10')
    expect(container.textContent).toContain('10.00%')
    expect(container.textContent).not.toContain('独立折收益乘积曲线')
  })

  it('shows skip evidence when every fold is skipped', () => {
    fixture.result = {
      n_folds: 0, n_skipped: 1, n_planned_folds: 1, elapsed_ms: 1,
      folds: [], skipped: [{ index: 0, test_start: '2024-10-08', test_end: '2024-10-09',
        reason: 'OOS 交易数未知' }],
      summary: { compounded_oos_return: null, oos_equity_curve: [], aggregation_eligible: false,
        aggregation_reasons: ['no_evaluated_folds'] },
    }
    act(() => root.render(<StrategyWalkForward />))
    expect(container.textContent).toContain('OOS 交易数未知')
    expect(container.textContent).not.toContain('0.00%')
  })
})
