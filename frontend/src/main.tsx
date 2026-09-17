import React from 'react'
import ReactDOM from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { BrowserRouter } from 'react-router-dom'
import App from './App'
import './styles.css'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Campaign state changes on the server, not in the client, so a refocus
      // refetch is the cheapest way to stay current without extra polling.
      refetchOnWindowFocus: true,
      staleTime: 2_000,
      retry: 1,
    },
    // Mutations here start background jobs; retrying one automatically could
    // enqueue duplicate work, so it is left to the user with an idempotency key.
    mutations: { retry: 0 },
  },
})

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </React.StrictMode>,
)
