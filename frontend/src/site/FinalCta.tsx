import { AGENT_HREF, AGENT_LABEL, BILLS_HREF, BILLS_LABEL, DEMO_LABEL, DOCS_HREF, DOCS_LABEL, EXT, PURCHASES_HREF, PURCHASES_LABEL } from "./links"
import { BufferWordmark } from '../components/BufferMark'
import { SectionLink } from './shared'

export default function FinalCta() {
  return (
    <>
      <section aria-labelledby="cta-h" className="hero-bg on-dark">
        <div className="mx-auto max-w-[1180px] px-4 py-20 sm:px-8 md:py-28">
          <h2 id="cta-h" className="max-w-[720px] text-[clamp(32px,5vw,54px)] font-semibold leading-[1.06] tracking-[-0.03em] !text-white">See a cash crunch get caught before it happens.</h2>
          <p className="mt-5 max-w-[560px] text-[18px] leading-[1.6] text-slate-300">Open the demo, approve a rush order in chat, and watch the next 14 days change in every other app.</p>
          <div className="mt-9 flex flex-wrap gap-3">
            <a href={PURCHASES_HREF} {...EXT} className="rounded-lg bg-brand px-5 py-3 text-[15px] font-semibold text-white hover:bg-brand-ink">{DEMO_LABEL}</a>
            <a href={DOCS_HREF} {...EXT} className="rounded-lg border border-white/25 px-5 py-3 text-[15px] font-semibold text-white hover:bg-white/10">{DOCS_LABEL}</a>
          </div>
        </div>
      </section>
      <footer className="on-dark border-t border-white/10 bg-[#07121f] text-slate-300">
        <div className="mx-auto flex max-w-[1180px] flex-col gap-8 px-4 py-10 sm:px-8 md:flex-row md:items-start md:justify-between">
          <div className="max-w-[420px]">
            <BufferWordmark size={26} tone="dark" />
            <p className="mt-3 text-[14px] leading-[1.6]">Hackathon prototype. Synthetic data. Not financial advice. No real money moves.</p>
          </div>
          <nav aria-label="Footer" className="grid grid-cols-2 gap-x-12 gap-y-2 text-[14px]">
            <a className="hover:text-white" href={PURCHASES_HREF} {...EXT}>{PURCHASES_LABEL}</a>
            <SectionLink to="how" className="hover:text-white">How it works</SectionLink>
            <a className="hover:text-white" href={BILLS_HREF} {...EXT}>{BILLS_LABEL}</a>
            <SectionLink to="api" className="hover:text-white">API</SectionLink>
            <a className="hover:text-white" href={AGENT_HREF} {...EXT}>{AGENT_LABEL}</a>
            <a className="hover:text-white" href={DOCS_HREF} {...EXT}>{DOCS_LABEL}</a>
            <SectionLink to="evaluation" className="hover:text-white">Evaluation</SectionLink>
          </nav>
        </div>
      </footer>
    </>
  )
}
