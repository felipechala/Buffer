import { Logo, SectionLink } from './shared'

export default function FinalCta() {
  return (
    <>
      <section aria-labelledby="cta-h" className="hero-bg on-dark">
        <div className="mx-auto max-w-[1180px] px-4 py-20 sm:px-8 md:py-28">
          <h2 id="cta-h" className="max-w-[720px] text-[clamp(32px,5vw,54px)] font-semibold leading-[1.06] tracking-[-0.03em] !text-white">See a cash crunch get caught before it happens.</h2>
          <p className="mt-5 max-w-[560px] text-[18px] leading-[1.6] text-slate-300">Open the demo, approve a rush order in chat, and watch the next 14 days change in every other app.</p>
          <div className="mt-9 flex flex-wrap gap-3">
            <a href="#/purchases" className="rounded-lg bg-cg-accent px-5 py-3 text-[15px] font-semibold text-white hover:bg-blue-500">Try the live demo</a>
            <a href="/docs" className="rounded-lg border border-white/25 px-5 py-3 text-[15px] font-semibold text-white hover:bg-white/10">OpenAPI docs (local)</a>
          </div>
        </div>
      </section>
      <footer className="on-dark border-t border-white/10 bg-[#07121f] text-slate-300">
        <div className="mx-auto flex max-w-[1180px] flex-col gap-8 px-4 py-10 sm:px-8 md:flex-row md:items-start md:justify-between">
          <div className="max-w-[420px]">
            <div className="flex items-center gap-2.5 text-white"><Logo size={26} /><span className="text-[16px] font-bold">Buffer</span></div>
            <p className="mt-3 text-[14px] leading-[1.6]">Hackathon prototype. Synthetic data. Not financial advice. No real money moves.</p>
          </div>
          <nav aria-label="Footer" className="grid grid-cols-2 gap-x-12 gap-y-2 text-[14px]">
            <a className="hover:text-white" href="#/purchases">Purchase checks demo</a>
            <SectionLink to="how" className="hover:text-white">How it works</SectionLink>
            <a className="hover:text-white" href="#/bills">Bill pay demo</a>
            <SectionLink to="api" className="hover:text-white">API</SectionLink>
            <a className="hover:text-white" href="#/agent">Treasury agent</a>
            <a className="hover:text-white" href="/docs">OpenAPI docs (local)</a>
            <SectionLink to="evaluation" className="hover:text-white">Evaluation</SectionLink>
          </nav>
        </div>
      </footer>
    </>
  )
}
