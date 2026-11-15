// Link targets for the landing page. In the static GitHub Pages build there is no backend,
// so demo and docs links point at the repository README instead.
export const STATIC = import.meta.env.VITE_STATIC_SITE === '1'
export const REPO = 'https://github.com/felipechala/Buffer'

export const PURCHASES_HREF = STATIC ? `${REPO}#quickstart` : '#/purchases'
export const BILLS_HREF = STATIC ? `${REPO}#quickstart` : '#/bills'
export const DOCS_HREF = STATIC ? `${REPO}#api-in-60-seconds` : '/docs'
export const AGENT_HREF = STATIC ? `${REPO}#the-treasury-agent` : '#/agent'

export const DEMO_LABEL = STATIC ? 'Run the demo' : 'Try the live demo'
export const NAV_DEMO_LABEL = STATIC ? 'Run the demo' : 'Live demo'
export const DOCS_LABEL = STATIC ? 'API reference' : 'OpenAPI docs (local)'
export const NAV_DOCS_LABEL = STATIC ? 'API reference' : 'API docs'
export const PURCHASES_LABEL = STATIC ? 'Run the purchase checks demo' : 'Purchase checks demo'
export const BILLS_LABEL = STATIC ? 'Run the bill pay demo' : 'Bill pay demo'
export const AGENT_LABEL = STATIC ? 'About the treasury agent' : 'Treasury agent'
// External links open in a new tab when static.
export const EXT = STATIC ? { target: '_blank', rel: 'noopener noreferrer' } as const : {}
