# ChatLens — assistant working notes

## Project shape
- **Local-only**, single-user dashboard. Runs on the operator's laptop, viewed at `http://localhost:8090`. No auth, no remote deploy.
- Backend: FastAPI (`src/chatlens/`), SQLite at `./data/chatlens.db`.
- Frontend: static SPA (`src/chatlens/static/index.html` + `assets/app.js`), Tailwind via CDN.
- LLM: shells out to the local `claude` CLI in `--print` mode, piggy-backing on the operator's Claude Max plan. **No `ANTHROPIC_API_KEY` is involved.**
- WeChat data: pulled on demand via the local `wechat-cli` binary.

## Conventions
- File length: keep modules under 300 lines (per global CLAUDE.md).
- All `wechat-cli` calls go through `chatlens.wechat` — never shell out from routers.
- All LLM calls go through `chatlens.llm` — never spawn `claude` from routers.
- The dashboard auto-syncs (`POST /api/groups/sync` → `GET /api/groups`) on every load.

## Removed (don't re-add unless asked)
- Auth / user model / login screen
- Multi-tenant scaffolding, Dokploy/Docker deploy
- Skill engine, scheduler, alerts, leaderboard, guest-share collection
- Anthropic SDK / API-key paths

## Never
- Commit `.env` or `data/*.db`.
- Mutate the user's WeChat data — `wechat-cli` is read-only and we keep it that way.
- Bind the server to anything other than `127.0.0.1` by default.
