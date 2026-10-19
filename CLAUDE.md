# CLAUDE.md — opentext-knowledge-base

Audience: AI coding assistants and the developers driving them. Read this, then
the README.

## What this repository is

One Fred **Knowledge Base**: a pod that mirrors the documents of an OpenText
Content Server folder into a Fred library, directly or through a gateway such
as Thales's API Manager. It is built on `fred-sdk[knowledge-base]` and ships as
an image deployed with Fred's generic knowledge-base chart. Its shape is the
WebDAV Knowledge Base's (`webdav-knowledge-base`); its protocol has been proven
in production against a Content Server behind that gateway.

| Path | What |
|---|---|
| `opentext_kb/` | The package. `knowledge_base.py` (declaration and handler) is the authoring surface; `synchronize.py` is one run; `content_server.py` is the client; `api_manager.py` is Thales's gateway. |
| `fake_content_server/` | A stand-in for Content Server (by default behind the API Manager gateway). A test fixture and a k3d stand-in, never deployed next to a real one. |
| `tests/` | Offline suite (no network, `pytest-socket`), the real client against the fake. |
| `config/` | `configuration.yaml` (a shape to copy) and `env.template` (the secrets). |
| `Dockerfile`, `dockerfiles/Dockerfile.fake-content-server` | The deployable image; the fake's image. |
| `deploy/values.yaml` | What every deployment starts from. |
| `deploy/k3d/`, `deploy/charts/fake-content-server` | The contract with fred-deployment-factory's local k3d: the KB and the fake. |

## Commands

```bash
make dev test code-quality          # locked deps from PyPI, offline tests, ruff/bandit/detect-secrets/basedpyright
make fake-content-server-run        # then, in another terminal:
make fake-content-server-sync       # one dry run against the fake, no Fred
make docker-build docker-smoke      # the image, checked offline
```

## What must stay true

- **Only the two calls proven in production** (`/nodes/{id}/nodes`, `/nodes/{id}/content`).
  A new endpoint is unverified until someone with access to a real Content
  Server confirms it.
- **Content Server's protocol and a gateway's stay apart.** Nothing Thales-specific
  outside `api_manager.py` and the `thales-api-manager` option.
- **A public repository.** No internal address, registry or deployment: those
  live in each organisation's own values files.
- **The API Manager's signature stays the reference client's.**
  `tests/test_content_server.py` pins it to a value that client computes; the
  fake checks it with its own implementation. Never make the fake import the
  client's signing code.
- **Deletion is earned.** A run retracts only after a complete walk, and an
  empty folder facing a populated library proves nothing. Never weaken this to
  make a test pass.
- **The account belongs to the team, a gateway's credentials to the operator.**
  The account bounds what a library can hold; never move it to the pod. One pod
  serves every team; nothing a run builds may outlive it (`tests/test_isolation.py`).
- **The pod keeps no state.** Fred's library is the record.
- **Identifiers do not move once deployed.** Package `opentext_kb`, definition
  `fred.opentext.folder`, prefix `fred.opentext`, client `kb-fred.opentext`:
  Fred binds the prefix to the first client that publishes it.
- **The committed `uv.lock` resolves fred from PyPI.** No `[tool.uv.sources]`.
- **Secrets never enter git.** `config/.env` is ignored.

## Working rules

- Before calling a change done: `make test code-quality`; for the image, also
  `make docker-build docker-smoke`; for `deploy/`, `make k3d-app-validate DIR=<this
  checkout>` from fred-deployment-factory.
- Keep the README the single place for how the pod behaves.
- Commits: `Type(scope): what changed` (`Feat`, `Fix`, `Chore`, `Docs`), one
  logical change each, no co-author line.
