import { Navigate, Route, Routes } from 'react-router-dom'
import Layout from './components/Layout'
import { Loading } from './components/ui'
import { useAuth } from './auth/AuthContext'
import Login from './pages/Login'
import Dashboard from './pages/Dashboard'
import QuestionBank from './pages/QuestionBank'
import QuestionDetail from './pages/QuestionDetail'
import Exams from './pages/Exams'
import ExamSession from './pages/ExamSession'
import SessionResult from './pages/SessionResult'
import Osce from './pages/Osce'
import OsceStation from './pages/OsceStation'
import OsceResult from './pages/OsceResult'
import OsceCircuitRest from './pages/OsceCircuitRest'
import OsceCircuitResult from './pages/OsceCircuitResult'
import Documents from './pages/admin/Documents'
import Papers from './pages/admin/Papers'
import StationImages from './pages/admin/StationImages'
import Settings from './pages/admin/Settings'
import Users from './pages/admin/Users'
import Errors from './pages/admin/Errors'
import type { ReactElement } from 'react'

export default function App() {
  const { user, loading } = useAuth()

  if (loading) return <Loading label="Signing you in…" />
  if (!user) return <Login />

  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<Dashboard />} />
        <Route path="bank" element={<QuestionBank />} />
        <Route path="questions/:id" element={<QuestionDetail />} />
        <Route path="exams" element={<Exams />} />
        <Route path="sessions/:id" element={<ExamSession />} />
        <Route path="sessions/:id/result" element={<SessionResult />} />
        <Route path="osce" element={<Osce />} />
        <Route path="osce/sittings/:id" element={<OsceStation />} />
        <Route path="osce/sittings/:id/result" element={<OsceResult />} />
        <Route path="osce/circuits/:circuitId/rest" element={<OsceCircuitRest />} />
        <Route path="osce/circuits/:circuitId/result" element={<OsceCircuitResult />} />
        <Route path="admin/documents" element={<AdminOnly><Documents /></AdminOnly>} />
        <Route path="admin/papers" element={<AdminOnly><Papers /></AdminOnly>} />
        <Route path="admin/station-images" element={<AdminOnly><StationImages /></AdminOnly>} />
        <Route path="admin/users" element={<AdminOnly><Users /></AdminOnly>} />
        <Route path="admin/settings" element={<AdminOnly><Settings /></AdminOnly>} />
        <Route path="admin/errors" element={<AdminOnly><Errors /></AdminOnly>} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  )
}

function AdminOnly({ children }: { children: ReactElement }) {
  const { user } = useAuth()
  return user?.role === 'admin' ? children : <Navigate to="/" replace />
}
