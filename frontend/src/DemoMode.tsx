import { FlaskConical } from 'lucide-react'

/**
 * Marking what is not real yet.
 *
 * Parts of the Portal are still backed by fixtures in `portalData.ts` rather than by the
 * netCI API — team lists, per-module activity, some illustrative panels. Rendering those
 * beside API-backed panels with no distinction is the interface equivalent of the false
 * green this platform exists to prevent: a demo that reads as a working feature is worse
 * than a missing feature, because nobody files a bug against it.
 *
 * The copy also used to say "Windows preview", which was wrong twice over: it runs on
 * Ubuntu too, and the point was never the operating system. What matters is that the data
 * lives in the browser and is gone on refresh.
 */

export type DemoReason = 'fixture' | 'browser-only'

const EXPLANATION: Record<DemoReason, string> = {
  fixture: 'Dữ liệu mẫu — netCI chưa có endpoint cho phần này.',
  'browser-only': 'Chỉ lưu trong trình duyệt phiên này; không gửi lên netCI và mất khi tải lại.',
}

/** An inline label for a panel that is not backed by the API. */
export function DemoBadge({ reason, detail }: { reason: DemoReason; detail?: string }) {
  return <span className="demo-badge" role="note" title={detail ?? EXPLANATION[reason]}>
    <FlaskConical size={13} aria-hidden="true" />
    {reason === 'fixture' ? 'Dữ liệu mẫu' : 'Chỉ trong trình duyệt'}
  </span>
}

/** A block-level version, for a whole section rather than a heading. */
export function DemoNote({ reason, detail }: { reason: DemoReason; detail?: string }) {
  return <div className="demo-note" role="note">
    <FlaskConical size={15} aria-hidden="true" />
    <span>{detail ?? EXPLANATION[reason]}</span>
  </div>
}
