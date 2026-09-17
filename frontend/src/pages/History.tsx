import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { api } from '../api'

export default function History() {
  const { data, isLoading } = useQuery({ queryKey: ['campaigns'], queryFn: api.listCampaigns })

  if (isLoading) return <p className="muted">Loading history…</p>
  if (!data?.length) {
    return (
      <div className="card">
        <p className="muted">No campaigns yet. Start one from “New campaign”.</p>
      </div>
    )
  }

  return (
    <div className="card">
      <h2>Campaign history</h2>
      <p className="muted">
        Read from disk, so everything here survives a page refresh and a backend restart.
        Open any campaign to see its research, spec and assets.
      </p>
      <table>
        <thead>
          <tr>
            <th>Campaign</th>
            <th>Product</th>
            <th>Status</th>
            <th>Assets</th>
            <th>Mode</th>
            <th>Updated</th>
          </tr>
        </thead>
        <tbody>
          {data.map((c) => (
            <tr key={c.id}>
              <td><Link to={`/campaigns/${c.id}`}>{c.title}</Link></td>
              <td className="muted">{c.product_name}</td>
              <td>
                <span
                  className={`badge ${
                    c.status === 'completed' ? 'ok'
                    : c.status === 'failed' ? 'err'
                    : c.status === 'interrupted' ? 'warn' : ''
                  }`}
                >
                  {c.status.replace('_', ' ')}
                </span>
              </td>
              <td>{c.asset_count}</td>
              <td className="muted">{c.provider_mode}</td>
              <td className="muted">{new Date(c.updated_at).toLocaleString()}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
