# Publishing AI mileage

The public site is a static Cloudflare Pages deployment at [aimilage.michaljablonski.dev](https://aimilage.michaljablonski.dev). Its source repository is [Jabolo/tokenometer](https://github.com/Jabolo/tokenometer). The hostname intentionally follows the owner's chosen spelling.

## Publication boundary

Only the exact manifest in `tokenometer/release_guard.py` belongs in the public Git tree. New files are ignored by default. The repository includes code, synthetic fixture tests, minimal operations documentation and an aggregate `web/data.json`. Local histories, captures, SQLite ledgers, private logs, machine configuration, old account investigations and notebook outputs stay outside the Git tree. The Pages asset root is **web/**, never the repository root.

Before staging or publishing changes:

```sh
python3 -m tokenometer.release_guard check-release --root .
python3 -m tokenometer.release_guard check-staged --root .
python3 -m unittest discover -s tests -q
```

The guard rejects unknown paths and aggregate keys, private metadata and inconsistent totals. It is a bounded release check, not a claim that arbitrary code or text can be proven free of all secrets. Review new source changes before updating the explicit manifest. The GitHub workflow validates the committed public tree and uses read-only GitHub repository permissions. Its separate deployment job runs only after checks pass on a push to main, using Cloudflare credentials held in the production environment secrets. Pull requests run checks only; they receive no deployment credentials. The workflow does not read the Mac or collect provider history.

## Hosting

Cloudflare Pages serves authored HTML, CSS, JavaScript and SVG; no Functions or application backend are required. The project is `ai-mileage`, using Pages Direct Upload from GitHub Actions. The native Cloudflare Git installation was unavailable during setup, so deployment uses the documented Wrangler CI route. The production branch is `main`. The static assets directory is `web`. Security headers are authored in `web/_headers`; the data response requests no-store caching so a new saved view is visible after deployment. The page fetches only its own aggregate file and includes no third-party trackers or font services.

Daily operation is described in [DAILY-REFRESH.md](DAILY-REFRESH.md). Collection requires the Mac's local histories. Running a GitHub-hosted schedule alone cannot access those files.

## Verify a deployment

Check the live HTTPS page, its `/data.json`, the source commit selected for production, and both provider cutoffs. Totals and event count must equal the reviewed public aggregate. Unknown paths and private file probes should return 404. Confirm the CSP and no-store response headers. A successful build alone does not establish current data or a correct custom domain.

Recorded coverage can remain partial after a successful deployment. The public timestamp distinguishes collection freshness from export time; a Mac that is asleep or logged out leaves the previous static view online rather than showing a fabricated zero.

## Rollback and pause

Stop the local scheduled job with the uninstall command in DAILY-REFRESH.md. The public page keeps its last deployment. To revert a public data/code change, create a reviewed Git revert and push `main`; do not rewrite public history. Cloudflare Pages can also promote a prior successful deployment for immediate rollback. Resume collection only after correcting the private failure and reviewing the aggregate.

Never copy authentication values into source, Git commit messages, public build output or this document. Use the existing Git credential helper for the Mac job. The Cloudflare account ID and API token are held only in the GitHub production environment secrets and are never part of the data export. The Mac job needs only its existing Git credentials. Deployment-token permissions should be restricted to Pages Write for the hosting account; DNS changes belong to the one-time operator setup, not the daily pipeline.

## Primary references

- [Cloudflare Pages Git integration](https://developers.cloudflare.com/pages/configuration/git-integration/)
- [Cloudflare Pages build configuration](https://developers.cloudflare.com/pages/configuration/build-configuration/)
- [Cloudflare Pages custom domains](https://developers.cloudflare.com/pages/configuration/custom-domains/)
- [Cloudflare Pages Direct Upload with CI](https://developers.cloudflare.com/pages/how-to/use-direct-upload-with-continuous-integration/)
