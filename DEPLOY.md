# Deploying the OpenVCR landing + waitlist

The site is a static page plus one Python serverless function
(`api/waitlist.py`), deployed to Vercel. Storage is a **private Vercel Blob**
container (durable across cold starts); the function talks to it with a
stdlib-only presign protocol (see `BlobStore` in `api/waitlist.py`). Without
the token it falls back to a local file, which is fine for development.

## One-time setup

```console
$ cd landing
$ vercel link --yes --project openvcr
$ vercel storage create openvcr-waitlist --type blob --access private --region iad1 --json
$ vercel storage connect openvcr-waitlist --auth token --add-rw-token --yes
$ vercel env pull .env.local      # BLOB_READ_WRITE_TOKEN should appear
$ vercel deploy --prod
```

Signups are stored as one append-style JSONL object,
`waitlist/signups.jsonl`, in the private container.

## Exporting the list

```python
from api.waitlist import BlobStore
store = BlobStore(token, store_id)   # from your env
print(store.read().decode())
```

or use the Vercel dashboard → Storage → openvcr-waitlist → files.

## Local development

```console
$ python3 scripts/serve_landing.py 8080   # static + the real dispatch() path
```

The form, validation, honeypot, and rate limiting are identical locally; only
the store differs (local file instead of Blob).

## GitHub Pages (fallback, static-only)

If you prefer not to run Vercel: the page is plain static files. Point
`fetch("/api/waitlist")` in `app.js` at any JSON endpoint with the same
contract (`POST {email} → {ok, position}`, `GET → {ok, count}`) and host the
static files anywhere.
