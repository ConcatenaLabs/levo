import { Routes, Route, Link } from 'react-router-dom'
import { Nav, Footer } from './components/Chrome'
import Home from './pages/Home'
import Projects from './pages/Projects'
import ProjectDetail from './pages/ProjectDetail'
import Account from './pages/Account'
import Launch from './pages/Launch'
import HowItWorks from './pages/HowItWorks'

function NotFound() {
  return (
    <div className="wrap section" style={{ maxWidth: 560 }}>
      <p className="eyebrow">404</p>
      <h2>Nothing here</h2>
      <p className="dim">That page does not exist. The sales list is a good place to start.</p>
      <Link className="btn" to="/projects">See open sales</Link>
    </div>
  )
}

export default function App() {
  return (
    <>
      <Nav />
      <main>
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/projects" element={<Projects />} />
          <Route path="/p/:slug" element={<ProjectDetail />} />
          <Route path="/account" element={<Account />} />
          <Route path="/launch" element={<Launch />} />
          <Route path="/how-it-works" element={<HowItWorks />} />
          <Route path="*" element={<NotFound />} />
        </Routes>
      </main>
      <Footer />
    </>
  )
}
