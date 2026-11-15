import { useEffect } from 'react'
import './site.css'
import Nav from './Nav'
import Hero from './Hero'
import Problem from './Problem'
import HowItWorks from './HowItWorks'
import Decisions from './Decisions'
import UseCases from './UseCases'
import ApiSection from './ApiSection'
import Trust from './Trust'
import Evaluation from './Evaluation'
import Stack from './Stack'
import Faq from './Faq'
import FinalCta from './FinalCta'

export default function Landing() {
  useEffect(() => { window.scrollTo(0, 0) }, [])
  return (
    <div className="site bg-white">
      <Nav />
      <main id="main" tabIndex={-1} className="outline-none">
        <Hero />
        <Problem />
        <HowItWorks />
        <Decisions />
        <UseCases />
        <ApiSection />
        <Trust />
        <Evaluation />
        <Stack />
        <Faq />
        <FinalCta />
      </main>
    </div>
  )
}
