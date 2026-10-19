# OpenText Knowledge Base

Mirrors the documents of an OpenText Content Server folder, and of every folder
under it, into a Fred library: a team gives it a folder and the account to read
it with, and every run brings the library to what that folder now holds.

It is a complete Knowledge Base image, started exactly like every other one:

```bash
python -m opentext_kb publish   # deployment step: declare this KB, exit
python -m opentext_kb run       # serve runs until stopped
```

Read [`knowledge_base.py`](opentext_kb/knowledge_base.py) first — the
declaration and the handler are the whole authoring surface — then
[`synchronize.py`](opentext_kb/synchronize.py) for what one run does, and
[`content_server.py`](opentext_kb/content_server.py) for how Content Server is
read.

Its shape is the WebDAV Knowledge Base's, unchanged: the plan, the run, the
boundary with Fred and the report are that sample's. What is its own is the
client, and the protocol it speaks has been run in production against a
Content Server behind a gateway.

---

## Content Server, and what may stand in front of it

Two calls of Content Server's REST API (v2) are made, and only these two —
the ones proven in production: `GET /nodes/{id}/nodes?limit=&page=` lists a folder
page by page, and `GET /nodes/{id}/content` fetches a document. The account
(HTTP Basic) decides what the walk can see.

A gateway may stand in front. `opentext.gateway` names it:

| `gateway` | What it adds | Used for |
|---|---|---|
| `none` (default) | nothing | a Content Server reached directly |
| `thales-api-manager` | a key, a user and an HMAC signature over every request ([`api_manager.py`](opentext_kb/api_manager.py)) | a Content Server published behind Thales's API Manager |

The API Manager's signature is pinned by a test to the value its reference
client computes.

## One pod, every team, each with its own account

| Identity | What it decides | Who owns it | Where it lives |
|---|---|---|---|
| The **Content Server account** | what the walk can see | the team | its form: `account`, `password` (a secret field) |
| A **gateway's credentials** (the API Manager's: a subscription) | whether the call is let through at all | the operator | `opentext.api_manager.username` in `configuration.yaml`, `$OPENTEXT_KB_API_KEY` and `$OPENTEXT_KB_API_SECRET` |

The account is what bounds the data: with one account shared by the pod, any
team creating an instance could mirror any folder that account can open. Each
team brings its own, and only what it may open reaches its library.

One pod serves every team's instances, and a run is isolated by construction:
it builds everything — settings, account, HTTP client, library — from its own
instance's configuration and the library Fred hands it, and nothing it builds
outlives it. [`tests/test_isolation.py`](tests/test_isolation.py) runs two
teams at once through the handler and checks each folder is read only with its
team's account, into its team's library.

---

## What a run does

Nothing is kept between runs; Fred's library is the record.

1. Ask the library what it holds: each document's key and version.
2. Walk the folder, level by level, a few folders at once
   (`opentext.listing_concurrency`), following every page.
3. Decide: write what is new or changed, leave what is unchanged, remove what
   Content Server no longer holds — removals only after a complete walk.
4. Write, then remove, and report.

| What | Where it lives |
|---|---|
| What the folder holds now | Content Server, walked on every run |
| What the library already has | the library, read on every run |
| A document's identity | its path under the folder, which Fred stores as the source key |
| A document's version | Content Server's modification date and size, given to Fred and read back |

A `/` inside a Content Server name is a character there and a folder separator
in Fred's library, so it is stored as `／` (U+FF0F): the library shows the name
as Content Server does without inventing a folder.

**Deletion is earned.** A run removes a document only after a complete walk. A
walk cut short (`MAX_FOLDERS`, `MAX_DEPTH`), a run bounded by `max_files`, and a
folder that lists **empty while the library holds documents** all remove
nothing. A renamed folder, a revoked right or a wrong id all list as empty, and
reading that as "everything was deleted" would empty a team's library on a
configuration mistake. A team that really empties its folder deletes the base.

**Skips never remove.** A file over `opentext.max_file_mib` is skipped and its
document kept.

---

## What a team fills in

| Field | Meaning |
|---|---|
| `folder_id` | the folder's node id, the number at the end of its address |
| `account`, `password` | the Content Server account the folder is read with |
| `include`, `exclude` | gitignore patterns, case ignored (Content Server is full of `.PDF`); default: PDF, Word, PowerPoint, Markdown, text |
| `profile` | Fred's ingestion profile: fast, medium, rich |
| `max_files` | the most documents one run carries; a bounded run removes nothing |
| `trust_any_certificate` | only when the deployment cannot be given the server's authority; reported on every run |

What the operator decides is the `opentext:` section of
[`config/configuration.yaml`](config/configuration.yaml): the address, the
gateway, page size, concurrency, the file size bound.

---

## Trying it

Python 3.12, Make and Git. `make dev test code-quality` here; dependencies come
from PyPI through the committed `uv.lock`.

### Without any network: the fake Content Server

[`fake_content_server/`](fake_content_server/) serves a local folder through
the same two calls. By default it stands behind the API Manager gateway, with
the signature checked by its own implementation of the reference formula — so a
mistake in the client fails against it instead of being reproduced on both
sides (`FAKE_CONTENT_SERVER_GATEWAY=none` drops the gateway). The test suite
runs the real client against it; so can you:

```bash
make fake-content-server-run     # serves fake_content_server/sample on http://localhost:8090
make fake-content-server-sync    # one dry run against it, in another terminal
```

Edit files under the served folder and run again: additions, edits and
deletions are what the next run reports. On disk, `∕` (U+2215) stands for a `/`
inside a name. Its credentials are public constants: account `fake-account` /
`fake-password`, subscription `fake-api-key` / `fake-api-secret`, API Manager
user `fake-apim-user`, root folder `2000`.

### On Fred's k3d

From [fred-deployment-factory](https://github.com/fred-agent/fred-deployment-factory),
with Fred deployed (`make k3d-app APP=fred`):

```bash
make k3d-app APP=fred VALUES=../opentext-knowledge-base/deploy/k3d/fred-values.yaml  # once: lets it publish
make k3d-app DIR=../opentext-knowledge-base                                          # the KB and the fake
```

Then in Fred, create an **OpenText Content Server folder** Knowledge Base for a
team: folder `2000`, account `fake-account`, password `fake-password`. The
library fills with the sample tree.

### Against a real Content Server

The same `make sync`, with the account's password in `$OPENTEXT_KB_PASSWORD`:

```bash
make sync FOLDER=123456789 ACCOUNT=my-account BASE_URL=https://cs.example.com/otcs/api/v2
```

Behind the API Manager, add `GATEWAY=thales-api-manager API_USERNAME=<its user>`
and the subscription in `config/.env` (`OPENTEXT_KB_API_KEY`,
`OPENTEXT_KB_API_SECRET`).

Dry by default: it logs what it would write. `LIBRARY=<id>` writes into a Fred
library for real. The server's certificate authority, if Python does not trust
it, goes in `$OPENTEXT_KB_CA_FILE` (added to the trust store, never replacing
it). A deployment sets `opentext.base_url` (and the gateway, if any) in a values
file applied over [`deploy/values.yaml`](deploy/values.yaml).

---

## Not yet proven

Written from a production client's code and checked against the fake only:

- **Behind the API Manager:** the timestamp is plain UTC (the reference client
  sends the same in a pod, whose clock is UTC); a folder is recognised by the
  production rule (`container`, `is_folder`, or a `type` of folder), so
  shortcuts are listed as documents; what the server answers when the node is
  a document rather than a folder.
- **A Content Server reached directly** (`gateway: none`): nobody has run it.
  The production client's Basic authentication goes through the gateway;
  Content Server on its own usually expects a ticket (`POST /api/v1/auth`, then
  an `OTCSTicket` header), which this pod does not do yet.

The first run against each should confirm these.
