import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { AsyncPanel, ErrorBoundary, describeError, useAsyncData } from './AsyncState'
import { NetciApiError } from './api/netciClient'

function Probe<T>({ load, render: renderData, isEmpty, empty }: {
  load: () => Promise<T>
  render: (data: T) => React.ReactNode
  isEmpty?: (data: T) => boolean
  empty?: React.ReactNode
}) {
  const state = useAsyncData(load)
  return <AsyncPanel state={state} isEmpty={isEmpty} empty={empty}>{renderData}</AsyncPanel>
}

describe('async states', () => {
  it('shows loading, then content — never both, never neither', async () => {
    let resolve: (value: string) => void = () => {}
    const load = vi.fn(() => new Promise<string>((done) => { resolve = done }))
    render(<Probe load={load} render={(data) => <p>{data}</p>} />)

    expect(screen.getByRole('status', { name: /Đang tải/i })).toBeTruthy()
    expect(screen.queryByText('the data')).toBeNull()

    resolve('the data')
    await screen.findByText('the data')
    expect(screen.queryByRole('status', { name: /Đang tải/i })).toBeNull()
  })

  it('distinguishes "failed to load" from "there is nothing"', async () => {
    // The distinction this whole module exists for: an empty screen that means "the API
    // is down" and an empty screen that means "you have no systems" must not look alike.
    const failing = render(<Probe load={() => Promise.reject(new NetciApiError(500, null, 'boom'))} render={() => <p>never</p>} />)
    await screen.findByRole('alert')
    expect(screen.getByRole('alert').textContent).toMatch(/sự cố|Không kết nối/i)
    failing.unmount()

    render(
      <Probe
        load={() => Promise.resolve([])}
        render={() => <p>never</p>}
        isEmpty={(data) => data.length === 0}
        empty={<p>Chưa có hệ thống nào</p>}
      />,
    )
    await screen.findByText('Chưa có hệ thống nào')
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('retries on demand rather than leaving the reader stuck', async () => {
    const user = userEvent.setup()
    const load = vi.fn()
      .mockRejectedValueOnce(new NetciApiError(503, null, 'unavailable'))
      .mockResolvedValueOnce('recovered')
    render(<Probe load={load} render={(data) => <p>{data as string}</p>} />)

    await screen.findByRole('alert')
    await user.click(screen.getByRole('button', { name: /Thử lại/i }))

    await screen.findByText('recovered')
    expect(load).toHaveBeenCalledTimes(2)
  })

  it('keeps showing the last good data when a reload fails, and says it is stale', () => {
    // Driven through AsyncPanel directly: this is about how the panel renders a failed
    // *reload*, which is the one state where data and error are both present. Stale data
    // that admits it is stale beats an empty screen that explains nothing.
    const reload = vi.fn()
    render(
      <AsyncPanel state={{ status: 'error', data: 'last good value', error: new NetciApiError(500, null, 'gone away'), reload }}>
        {(data) => <p>{data}</p>}
      </AsyncPanel>,
    )

    expect(screen.getByText('last good value')).toBeTruthy()
    expect(screen.getByRole('status').textContent).toMatch(/gần nhất/i)
    // And a way to try again, rather than requiring a page reload.
    screen.getByRole('button', { name: /Thử lại/i }).click()
    expect(reload).toHaveBeenCalled()
  })

  it('translates status codes into something the reader can act on', () => {
    expect(describeError(new NetciApiError(403, null, 'x'))).toMatch(/không có quyền/i)
    expect(describeError(new NetciApiError(429, null, 'x'))).toMatch(/quá nhiều/i)
    expect(describeError(new NetciApiError(500, null, 'x'))).toMatch(/sự cố/i)
    expect(describeError(new Error('offline'))).toMatch(/Không kết nối/i)
  })
})

describe('ErrorBoundary', () => {
  it('contains a render error instead of blanking the Portal', () => {
    const Broken = () => { throw new Error('render exploded') }
    // React logs the caught error; the test asserts behaviour, not console noise.
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})

    render(<ErrorBoundary><Broken /></ErrorBoundary>)

    expect(screen.getByRole('alert').textContent).toMatch(/Giao diện gặp lỗi/i)
    // The user is told their data is safe and given a way forward, not a white page.
    expect(screen.getByText(/không bị ảnh hưởng/i)).toBeTruthy()
    expect(screen.getByRole('button', { name: /Tải lại Portal/i })).toBeTruthy()
    consoleError.mockRestore()
  })

  it('renders its children untouched when nothing throws', () => {
    render(<ErrorBoundary><p>all fine</p></ErrorBoundary>)
    expect(screen.getByText('all fine')).toBeTruthy()
    expect(screen.queryByRole('alert')).toBeNull()
  })
})
