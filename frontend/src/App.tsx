import { useQuery } from '@tanstack/react-query'
import { NavLink, Navigate, Route, Routes } from 'react-router-dom'
import { api } from './api'
import NewCampaign from './pages/NewCampaign'
import CampaignView from './pages/CampaignView'
import History from './pages/History'

export default function App() {
  const { data: health } = useQuery({ queryKey: ['health'], queryFn: api.health })

  return (
    <div className="app">
      <header className="top">
        <h1>AI Campaign Creative Studio</h1>
        <nav>
          <NavLink to="/new" className={({ isActive }) => (isActive ? 'active' : '')}>
            New campaign
          </NavLink>
          <NavLink to="/history" className={({ isActive }) => (isActive ? 'active' : '')}>
            History
          </NavLink>
        </nav>
        {health && (
          <span
            className={`badge ${health.provider_mode === 'live' ? 'live' : 'fixture'}`}
            title={health.notes.join(' ')}
          >
            {health.provider_mode === 'live' ? 'LIVE PROVIDERS' : 'FIXTURE MODE'}
          </span>
        )}
      </header>

      {health && health.ffmpeg !== 'available' && (
        <div className="error-box">
          <strong>FFmpeg is not available.</strong> The video stage will fail until it
          is installed and on PATH (<span className="mono">winget install Gyan.FFmpeg</span>).
          Research and both image ads are unaffected.
        </div>
      )}

      {health?.failure_injection?.length ? (
        <div className="warn-box">
          <strong>Failure injection is active</strong> for:{' '}
          <span className="mono">{health.failure_injection.join(', ')}</span>. These stages
          will fail deliberately. Unset <span className="mono">FAILURE_INJECT</span> to disable.
        </div>
      ) : null}

      <Routes>
        <Route path="/" element={<Navigate to="/new" replace />} />
        <Route path="/new" element={<NewCampaign />} />
        <Route path="/campaigns/:id" element={<CampaignView />} />
        <Route path="/history" element={<History />} />
      </Routes>
    </div>
  )
}
