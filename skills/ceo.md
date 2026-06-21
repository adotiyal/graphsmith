# CEO / CTO — Domain Knowledge

## The human is CEO *and* CTO
The person you serve wears both hats. They own:
- **Business:** what feature, for whom, scope, priority, success criteria, go/no-go.
- **Technical (CTO):** the tech stack, architecture trade-offs, infra/deploy choices,
  build-vs-buy, and any engineering decision an agent can't resolve among themselves.

There is no separate human CTO to defer to — when a technical decision needs a human,
it is THIS person, acting as CTO. They are the universal unblocker for the whole company.

## How escalation works
- Agents first try to resolve questions among themselves (bounded to a few rounds).
- Anything still unresolved — business or technical — escalates to the CEO/CTO, who
  resolves it. No agent is ever permanently blocked.
- The **tech stack is always finalized by the CEO/CTO.** The architect proposes a
  default (FastAPI + Next.js + Postgres) but must get the CEO/CTO to confirm or change
  it before committing the technical spec. Treat the stack as a CTO decision, not an
  architect default.

## Writing the brief
Translate the vision into scope + success criteria. Because the same person is CTO,
it is fine to note technical intent or constraints they stated (e.g. "must run on our
existing Postgres", "keep it serverless") — capture these so the architect surfaces
them at stack-confirmation time. Do not invent technical constraints they didn't state.