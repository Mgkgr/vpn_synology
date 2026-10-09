import { useEffect, useState } from 'react'
import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query'
import { Navigate, Route, Routes } from 'react-router-dom'

import { api, setUnauthorizedHandler } from './api/client'
import { AppShell } from './components/AppShell'
import { ClientsPage } from './pages/ClientsPage'
import { AdministratorsPage } from './pages/AdministratorsPage'
import { JournalPage } from './pages/JournalPage'
import { LoginPage } from './pages/LoginPage'
import { OverviewPage } from './pages/OverviewPage'
import { RoutesPage } from './pages/RoutesPage'
import { RulesPage } from './pages/RulesPage'
import { UpdatesPage } from './pages/UpdatesPage'
import { ComponentsPage } from './pages/ComponentsPage'
import { AntidpiStrategiesPage } from './pages/AntidpiStrategiesPage'
import { OutboundProfilesPage } from './pages/OutboundProfilesPage'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: false,
      refetchOnWindowFocus: false,
      staleTime: 15_000,
      gcTime: 10 * 60_000,
    },
  },
})

function Protected({ children, sessionKey }: { children: React.ReactNode; sessionKey: number }) {
  const session = useQuery({ queryKey: ['session', sessionKey], queryFn: api.restoreSession, retry: false, staleTime: Infinity })
  if (session.isLoading) return <main className="startup-state">Проверка защищённой сессии…</main>
  if (session.isError) return <Navigate to="/login" replace />
  return <AppShell>{children}</AppShell>
}

function ApplicationRoutes() {
  const [authVersion, setAuthVersion] = useState(0)
  const authenticated = () => setAuthVersion((version) => version + 1)
  useEffect(() => setUnauthorizedHandler(() => {
    queryClient.clear()
    setAuthVersion((version) => version + 1)
  }), [])
  return <Routes>
    <Route path="/login" element={<LoginPage onAuthenticated={authenticated} />} />
    <Route path="/overview" element={<Protected sessionKey={authVersion}><OverviewPage /></Protected>} />
    <Route path="/routes" element={<Protected sessionKey={authVersion}><RoutesPage /></Protected>} />
    <Route path="/routes/profiles" element={<Protected sessionKey={authVersion}><OutboundProfilesPage /></Protected>} />
    <Route path="/routes/antidpi/strategies" element={<Protected sessionKey={authVersion}><AntidpiStrategiesPage /></Protected>} />
    <Route path="/clients" element={<Protected sessionKey={authVersion}><ClientsPage /></Protected>} />
    <Route path="/rules" element={<Protected sessionKey={authVersion}><RulesPage /></Protected>} />
    <Route path="/updates" element={<Protected sessionKey={authVersion}><UpdatesPage /></Protected>} />
    <Route path="/journal" element={<Protected sessionKey={authVersion}><JournalPage /></Protected>} />
    <Route path="/administrators" element={<Protected sessionKey={authVersion}><AdministratorsPage /></Protected>} />
    <Route path="/system/components" element={<Protected sessionKey={authVersion}><ComponentsPage /></Protected>} />
    <Route path="*" element={<Navigate to="/overview" replace />} />
  </Routes>
}

export function App() {
  return <QueryClientProvider client={queryClient}><ApplicationRoutes /></QueryClientProvider>
}
