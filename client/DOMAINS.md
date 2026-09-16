# Client-hardcoded domains

Infrastructure domains compiled into the **ROM: Golden Age** client. Three
sources, all part of the shipped client:

- **IL2CPP dump** — static strings in `GameAssembly` (the game code)
- **`global-metadata.dat`** — IL2CPP string literals
- **`resources.assets`** — Unity `ScriptableObject` strings

Library/demo noise is excluded (Unity, BestHTTP samples, Google/Apple/Microsoft,
tumblr/wattpad/apnews/amazon/pinimg, `someone.is.screwing.with.the.headers.com`,
`www.fatorcaos.com.br`, etc.).

## romgoldenage.com — first-party infra

| Function | Domains (by environment) | Source |
|---|---|---|
| **Patch / CDN** | `patch.romgoldenage.com`, `qa-patch.romgoldenage.com` | metadata + resources |
| **Auth** | `auth.romgoldenage.com`, `qa-auth.romgoldenage.com`, `review-auth.romgoldenage.com` | resources |
| **Billing** | `next-bill.romgoldenage.com`, `review-next-bill.romgoldenage.com` | metadata |

## rom-mobile.com

| Function | Domain | Source |
|---|---|---|
| **Certification / social login (PC)** | `qa-certification.rom-mobile.com` (`/v1/auth/pc/{google,apple}/authorize`) | resources |

## WEMIX — wallet / blockchain / login

| Service | live / stg / dev |
|---|---|
| **OAuth** | `oauth.wemixnetwork.com`, `stg-oauth.wemixnetwork.com`, `dev-oauth.wemixnetwork.com` |
| **Relay** | `relay.wemixnetwork.com`, `stg-relay.wemixnetwork.com`, `dev-relay.wemixnetwork.com` |
| **Pay** | `pay.wemix.com`, `stg-pay.wemix.com` |
| **Play (sauth)** | `sauth.wemixplay.com`, `stg-sauth.wemixplay.com` |
| **Play (wpapi)** | `wpapi.wemixplay.com`, `stg-wpapi.wemixplay.com` |

## Platform / third-party (also hardcoded in the client)

| Domain | Purpose |
|---|---|
| `launching.api.nhncloudservice.com` | NHN Launching — version/maintenance gating |
| `rom.oqupie.com` | Support / helpdesk portal |
| `rom.cross.shop` | Shop (Cross build) |
| `ipwhois.app` | IP geolocation (region selection) |
| `s2s.singular.net` | Telemetry / attribution — block |

## NOT hardcoded (for reference)

- **Regional AUTH / BILLING / TRANSLATE** hosts (`live-auth-region-sa.romgoldenage.com`,
  `live-auth-region-ap.romgoldenage.com`, `billing-{sa,ap}.romgoldenage.com`,
  `translation-{sa,ap}.romgoldenage.com`) and the game server (`:17701`) come from
  `domaindata.json` fetched at runtime — not compiled into the client.
- An internal RFC1918 build server (`10.x.x.x:8090`) lives in the **launcher**, not
  the client.
