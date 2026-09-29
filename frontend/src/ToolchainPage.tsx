import { useEffect, useState } from 'react'
import { AlertTriangle, CheckCircle2, RefreshCw } from 'lucide-react'
import { getToolchain, type ToolchainJenkins, type ToolchainStatus } from './api/netciClient'
import { PageHeader } from './PortalShell'

type LoadState = 'loading' | 'ready' | 'error'

export function ToolchainPage() {
  const [data, setData] = useState<ToolchainStatus | null>(null)
  const [state, setState] = useState<LoadState>('loading')
  const [error, setError] = useState('')

  const loadData = () => {
    setState('loading')
    setError('')
    getToolchain()
      .then((res) => {
        setData(res)
        setState('ready')
      })
      .catch((err) => {
        setError(err instanceof Error ? err.message : String(err))
        setState('error')
      })
  }

  useEffect(() => {
    loadData()
  }, [])

  return (
    <section className="page" data-testid="toolchain-page">
      <PageHeader
        title="Quản lý Toolchain"
        description="Theo dõi phiên bản công cụ chuẩn, phát hiện sai lệch (drift) trên các Jenkins controller và độ tươi mới CSDL Trivy (ADR-056)."
        action={
          <button
            type="button"
            className="login-secondary"
            onClick={loadData}
            style={{ padding: '6px 14px', minHeight: '36px', fontSize: '13px' }}
          >
            <RefreshCw size={14} /> Làm mới
          </button>
        }
      />

      {state === 'loading' && (
        <div className="panel empty-table" data-testid="toolchain-loading">
          Đang tải dữ liệu toolchain…
        </div>
      )}

      {state === 'error' && (
        <div className="inline-error" role="alert" data-testid="toolchain-error">
          Không thể tải dữ liệu toolchain: {error}
        </div>
      )}

      {state === 'ready' && data && (
        <>
          {/* Cảnh báo độ tươi mới CSDL Trivy */}
          {data.trivyDb.stale ? (
            <div
              className="inline-error"
              role="alert"
              data-testid="trivy-stale-warning"
              style={{
                background: '#fff1f3',
                border: '1px solid #f2b5bd',
                color: '#a9293a',
                padding: '12px 16px',
                borderRadius: '8px',
                marginBottom: '20px',
                display: 'flex',
                alignItems: 'center',
                gap: '10px',
              }}
            >
              <AlertTriangle size={20} color="var(--red)" />
              <div>
                <strong>Cảnh báo: Cơ sở dữ liệu Trivy đã quá hạn (Stale)!</strong>
                <div style={{ fontSize: '12px', marginTop: '2px' }}>
                  CSDL quan sát lần cuối vào: {data.trivyDb.observedUpdatedAt || 'Chưa có dữ liệu'} (Vượt quá ngưỡng tối đa cho phép {data.trivyDb.maxAgeHours} giờ).
                  Cần cập nhật CSDL Trivy trên các controller để đảm bảo phát hiện chính xác lỗ hổng bảo mật.
                </div>
              </div>
            </div>
          ) : (
            <div
              data-testid="trivy-fresh-status"
              style={{
                background: '#f0fdf4',
                border: '1px solid #bbf7d0',
                color: '#166534',
                padding: '10px 16px',
                borderRadius: '8px',
                marginBottom: '20px',
                display: 'flex',
                alignItems: 'center',
                gap: '8px',
              }}
            >
              <CheckCircle2 size={18} color="var(--green)" />
              <span>
                <strong>Cơ sở dữ liệu Trivy hợp lệ (Fresh):</strong> Cập nhật lúc {data.trivyDb.observedUpdatedAt || 'N/A'} (Ngưỡng tối đa: {data.trivyDb.maxAgeHours} giờ).
              </span>
            </div>
          )}

          {/* Sai lệch phiên bản (Drift) */}
          {data.drift.length > 0 ? (
            <div
              className="panel"
              data-testid="drift-panel"
              style={{
                border: '1px solid var(--red)',
                borderRadius: '8px',
                marginBottom: '24px',
                overflow: 'hidden',
              }}
            >
              <div
                style={{
                  background: 'rgba(229, 72, 77, 0.1)',
                  padding: '12px 16px',
                  borderBottom: '1px solid var(--red)',
                  color: 'var(--red)',
                  display: 'flex',
                  alignItems: 'center',
                  gap: '8px',
                }}
              >
                <AlertTriangle size={18} />
                <strong>Phát hiện sai lệch phiên bản ({data.drift.length} cảnh báo drift)</strong>
              </div>
              <table
                className="data-table"
                data-testid="drift-table"
                style={{ width: '100%', borderCollapse: 'collapse', textAlign: 'left' }}
              >
                <thead>
                  <tr className="table-head">
                    <th style={{ padding: '10px 14px' }}>Công cụ</th>
                    <th style={{ padding: '10px 14px' }}>Khai báo chuẩn</th>
                    <th style={{ padding: '10px 14px' }}>Thực tế quan sát</th>
                    <th style={{ padding: '10px 14px' }}>Controller</th>
                  </tr>
                </thead>
                <tbody>
                  {data.drift.map((item, idx) => (
                    <tr
                      key={`${item.tool}-${item.controllerId || idx}`}
                      data-testid="drift-row"
                      style={{
                        background: 'rgba(229, 72, 77, 0.05)',
                        color: 'var(--red)',
                        fontWeight: 600,
                        borderBottom: '1px solid rgba(229, 72, 77, 0.2)',
                      }}
                    >
                      <td style={{ padding: '10px 14px' }}>{item.tool}</td>
                      <td style={{ padding: '10px 14px' }}>{item.declared}</td>
                      <td style={{ padding: '10px 14px' }}>{item.observed || 'Không tìm thấy (null)'}</td>
                      <td style={{ padding: '10px 14px' }}>{item.controllerId || 'default'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <div
              data-testid="no-drift-banner"
              style={{
                background: '#f0fdf4',
                border: '1px solid #bbf7d0',
                color: '#166534',
                padding: '10px 16px',
                borderRadius: '8px',
                marginBottom: '24px',
                display: 'flex',
                alignItems: 'center',
                gap: '8px',
              }}
            >
              <CheckCircle2 size={18} color="var(--green)" />
              <span>
                <strong>Không phát hiện sai lệch (Zero Drift):</strong> Toàn bộ công cụ quan sát trên các controller đều khớp với cấu hình chuẩn.
              </span>
            </div>
          )}

          {/* Bảng công cụ khai báo chuẩn */}
          <div className="panel table-panel" style={{ marginBottom: '24px' }}>
            <div
              style={{
                padding: '14px 16px',
                borderBottom: '1px solid var(--border)',
                display: 'flex',
                justifyContent: 'space-between',
                alignItems: 'center',
              }}
            >
              <strong>Khai báo chuẩn (Declared Toolchain)</strong>
              {data.declared.toolbox && (
                <span style={{ fontSize: '12px', color: 'var(--muted)' }}>
                  Toolbox Image: <code>{data.declared.toolbox.image}:{data.declared.toolbox.tag}</code>
                </span>
              )}
            </div>
            <table
              className="data-table"
              data-testid="declared-table"
              style={{ width: '100%', borderCollapse: 'collapse', textAlign: 'left' }}
            >
              <thead>
                <tr className="table-head">
                  <th style={{ padding: '10px 14px' }}>Công cụ</th>
                  <th style={{ padding: '10px 14px' }}>Phiên bản khai báo</th>
                  <th style={{ padding: '10px 14px' }}>Mã băm SHA256 / Gói</th>
                  <th style={{ padding: '10px 14px' }}>Nguồn tải</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(data.declared.tools || {}).map(([name, tool]) => (
                  <tr key={name} style={{ borderBottom: '1px solid var(--border)', fontSize: '13px' }}>
                    <td style={{ padding: '10px 14px' }}>
                      <strong>{name}</strong>
                    </td>
                    <td style={{ padding: '10px 14px' }}>
                      <code>{tool.version}</code>
                    </td>
                    <td style={{ padding: '10px 14px', fontFamily: 'monospace', fontSize: '11px' }}>
                      {tool.sha256 ? `${tool.sha256.slice(0, 16)}...` : tool.package ? `apt: ${tool.package}` : '—'}
                    </td>
                    <td style={{ padding: '10px 14px', fontSize: '12px', color: 'var(--muted)' }}>
                      {tool.url ? (
                        <a
                          href={tool.url}
                          target="_blank"
                          rel="noreferrer"
                          style={{ color: 'var(--blue)', textDecoration: 'none' }}
                        >
                          Tải về
                        </a>
                      ) : tool.source ? (
                        tool.source
                      ) : (
                        '—'
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* Bảng quan sát thực tế theo controller */}
          <div className="panel table-panel">
            <div style={{ padding: '14px 16px', borderBottom: '1px solid var(--border)' }}>
              <strong>Quan sát thực tế theo Controller (Observed per Controller)</strong>
            </div>
            <table
              className="data-table"
              data-testid="observed-table"
              style={{ width: '100%', borderCollapse: 'collapse', textAlign: 'left' }}
            >
              <thead>
                <tr className="table-head">
                  <th style={{ padding: '10px 14px' }}>Controller ID</th>
                  <th style={{ padding: '10px 14px' }}>Thời gian ghi nhận</th>
                  <th style={{ padding: '10px 14px' }}>Syft</th>
                  <th style={{ padding: '10px 14px' }}>Trivy</th>
                  <th style={{ padding: '10px 14px' }}>Cosign</th>
                  <th style={{ padding: '10px 14px' }}>Buildah</th>
                  <th style={{ padding: '10px 14px' }}>Trivy DB UpdatedAt</th>
                </tr>
              </thead>
              <tbody>
                {data.observed.length === 0 ? (
                  <tr>
                    <td colSpan={7} style={{ textAlign: 'center', padding: '32px' }}>
                      <div className="empty-table">
                        <p>Chưa có dữ liệu quan sát từ agent nào</p>
                      </div>
                    </td>
                  </tr>
                ) : (
                  data.observed.map((obs) => {
                    const isDrifted = (toolName: string) =>
                      data.drift.some(
                        (d) =>
                          d.tool.toLowerCase() === toolName.toLowerCase() &&
                          (!d.controllerId || d.controllerId === obs.controllerId)
                      )

                    return (
                      <tr key={obs.controllerId} style={{ borderBottom: '1px solid var(--border)', fontSize: '13px' }}>
                        <td style={{ padding: '10px 14px' }}>
                          <strong>{obs.controllerId}</strong>
                        </td>
                        <td style={{ padding: '10px 14px', color: 'var(--muted)', fontSize: '12px' }}>
                          {obs.when}
                        </td>
                        <td
                          style={{
                            padding: '10px 14px',
                            color: isDrifted('syft') ? 'var(--red)' : 'inherit',
                            fontWeight: isDrifted('syft') ? 700 : 'normal',
                          }}
                        >
                          {obs.toolVersions.syft || '—'}
                        </td>
                        <td
                          style={{
                            padding: '10px 14px',
                            color: isDrifted('trivy') ? 'var(--red)' : 'inherit',
                            fontWeight: isDrifted('trivy') ? 700 : 'normal',
                          }}
                        >
                          {obs.toolVersions.trivy || '—'}
                        </td>
                        <td
                          style={{
                            padding: '10px 14px',
                            color: isDrifted('cosign') ? 'var(--red)' : 'inherit',
                            fontWeight: isDrifted('cosign') ? 700 : 'normal',
                          }}
                        >
                          {obs.toolVersions.cosign || '—'}
                        </td>
                        <td
                          style={{
                            padding: '10px 14px',
                            color: isDrifted('buildah') ? 'var(--red)' : 'inherit',
                            fontWeight: isDrifted('buildah') ? 700 : 'normal',
                          }}
                        >
                          {obs.toolVersions.buildah || '—'}
                        </td>
                        <td style={{ padding: '10px 14px', fontSize: '12px', color: 'var(--muted)' }}>
                          {obs.toolVersions.trivyDbUpdatedAt || '—'}
                        </td>
                      </tr>
                    )
                  })
                )}
              </tbody>
            </table>
          </div>
          {data.jenkins && <JenkinsPluginsPanel jenkins={data.jenkins} />}
        </>
      )}
    </section>
  )
}

const DRIFT_KIND: Record<string, string> = { version: 'khác phiên bản', missing: 'thiếu', undeclared: 'không khai báo' }

/** netCI decides the controller image and every plugin; a controller that differs, or whose
 *  plugin list cannot be read, takes no build while enforcement is on (ADR-059). */
function JenkinsPluginsPanel({ jenkins }: { jenkins: ToolchainJenkins }) {
  const [showAll, setShowAll] = useState(false)
  const plugins = Object.entries(jenkins.plugins)
  return (
    <div className="panel table-panel" data-testid="jenkins-plugins" style={{ marginTop: '24px' }}>
      <div style={{ padding: '14px 16px', borderBottom: '1px solid var(--border)' }}>
        <strong>Jenkins controller &amp; plugin (ADR-059)</strong>
        <p style={{ margin: '6px 0 0', fontSize: '13px', color: 'var(--muted)' }}>
          netCI quyết định image controller và phiên bản của từng plugin (kể cả phụ thuộc) trong toolchain/versions.yaml.
          {jenkins.enforced
            ? ' Controller lệch khai báo, hoặc không đọc được danh sách plugin, sẽ không nhận build.'
            : ' Đang ở chế độ warn: lệch được ghi nhận nhưng build vẫn chạy.'}
        </p>
        {jenkins.controller && (
          <p style={{ margin: '6px 0 0', fontSize: '13px' }}>
            Image: <code className="mono">{jenkins.controller.image}:{jenkins.controller.tag}</code> · base{' '}
            <code className="mono">{jenkins.controller.base.split('@')[0]}</code> · {plugins.length} plugin
          </p>
        )}
      </div>
      <table className="data-table" style={{ width: '100%', borderCollapse: 'collapse', textAlign: 'left' }}>
        <thead>
          <tr className="table-head">
            <th style={{ padding: '10px 14px' }}>Controller</th>
            <th style={{ padding: '10px 14px' }}>Trạng thái</th>
            <th style={{ padding: '10px 14px' }}>Chi tiết</th>
          </tr>
        </thead>
        <tbody>
          {jenkins.controllers.length === 0 && (
            <tr><td colSpan={3} style={{ padding: '24px', textAlign: 'center', color: 'var(--muted)' }}>Chưa cấu hình Jenkins controller nào (NETCI_CI_MODE khác jenkins)</td></tr>
          )}
          {jenkins.controllers.map((c) => (
            <tr key={c.controllerId} style={{ borderBottom: '1px solid var(--border)', fontSize: '13px', verticalAlign: 'top' }}>
              <td style={{ padding: '10px 14px' }}><strong>{c.controllerId}</strong></td>
              <td style={{ padding: '10px 14px' }}>
                {!c.readable
                  ? <span style={{ color: 'var(--red)', fontWeight: 700 }}><AlertTriangle size={14} /> Không đọc được</span>
                  : c.drift.length
                    ? <span style={{ color: 'var(--red)', fontWeight: 700 }}><AlertTriangle size={14} /> Lệch {c.drift.length} plugin</span>
                    : <span style={{ color: 'var(--green)', fontWeight: 700 }}><CheckCircle2 size={14} /> Khớp ({c.pluginCount} plugin)</span>}
              </td>
              <td style={{ padding: '10px 14px' }}>
                {!c.readable && <span style={{ color: 'var(--muted)' }}>{c.error} — cần quyền Overall/SystemRead cho tài khoản netCI</span>}
                {c.drift.length > 0 && (
                  <ul style={{ margin: 0, paddingLeft: '18px' }}>
                    {c.drift.map((d) => (
                      <li key={d.plugin}>
                        <code className="mono">{d.plugin}</code> {DRIFT_KIND[d.kind] ?? d.kind}: đang chạy {d.observed ?? '—'}, khai báo {d.declared ?? '—'}
                      </li>
                    ))}
                  </ul>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div style={{ padding: '10px 16px' }}>
        <button type="button" className="secondary-button" onClick={() => setShowAll((v) => !v)}>
          {showAll ? 'Ẩn danh sách plugin khai báo' : `Xem ${plugins.length} plugin khai báo`}
        </button>
        {showAll && (
          <div style={{ columns: '3 240px', marginTop: '10px', fontSize: '12px' }}>
            {plugins.map(([name, version]) => (
              <div key={name}>
                <code className="mono">{name}</code> {version}{jenkins.requires.includes(name) ? ' ★' : ''}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
