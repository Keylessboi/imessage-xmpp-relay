# Attachments: cluster manifests to apply and security notes

Proposed manifests for the lead to apply in the GitOps repo (nothing here was
applied or edited there). Names assume the existing `imessage` app and
namespace; adjust to the real ones. The server pod gains a second listener
(`/dl`, port 8791) that is published through Traefik; the control API
(`:8790`) is never exposed.

## What the server needs

| Setting | Value |
|---|---|
| `IMESSAGE_FILE_HMAC_KEY_FILE` | `/run/files/files-hmac-key` (mounted from the Secret below; at least 16 bytes, 32+ random recommended) |
| `IMESSAGE_FILES_LISTEN` | `0.0.0.0:8791` (default) |
| `IMESSAGE_ATTACH_DIR` | `/data/attachments` (default; on the existing PVC, see subPath) |
| `IMESSAGE_ATTACH_MAX_BYTES` / `_QUOTA_BYTES` / `_MAX_AGE_SECS` | `104857600` / `2147483648` / `604800` (defaults) |
| `IMESSAGE_FILE_URL_TTL_SECS` | `1800` (default) |

The bridge needs `PUBLIC_FILE_BASE_URL=https://imessage-files.sandstorm.chat`
(and optionally `OUTBOUND_MAX_BYTES`, `OUTBOUND_FETCH_TIMEOUT`).

Generate the key owner-side, never in the repo or a terminal log, and put it in
Doppler as `IMESSAGE_FILE_HMAC_KEY` (for example `openssl rand -hex 32`). It is
C4: with it anyone can mint download links for files that exist on the PVC.
Rotating it simply invalidates outstanding links. Without
`IMESSAGE_FILE_HMAC_KEY_FILE` the whole feature is off: nothing is downloaded
and port 8791 is not opened.

## DopplerSecret (append to apps/doppler/dopplersecrets.yaml)

```yaml
apiVersion: secrets.doppler.com/v1alpha1
kind: DopplerSecret
metadata:
  name: imessage-files-hmac
spec:
  tokenSecret:
    name: doppler-token-secret
  project: kubernetes
  config: prd
  secrets: [IMESSAGE_FILE_HMAC_KEY]      # pin the key, or the whole config is mirrored
  processors:
    IMESSAGE_FILE_HMAC_KEY: {asName: files-hmac-key, type: plain}
  managedSecret:
    name: imessage-files-hmac
    namespace: imessage
    type: Opaque
  resyncSeconds: 300
```

## Pod changes (Deployment, server container)

```yaml
          ports:
            - {name: api,   containerPort: 8790}
            - {name: files, containerPort: 8791}
          env:
            - {name: IMESSAGE_FILE_HMAC_KEY_FILE, value: /run/files/files-hmac-key}
            - {name: IMESSAGE_ATTACH_DIR,         value: /data/attachments}
          volumeMounts:
            - {name: files-hmac, mountPath: /run/files/files-hmac-key, subPath: files-hmac-key, readOnly: true}
            # Attachments share the PVC with the IDS state but get their own
            # subPath, so quota pressure and eviction never touch state.plist:
            - {name: data, mountPath: /data/attachments, subPath: attachments}
      volumes:
        - name: files-hmac
          secret:
            secretName: imessage-files-hmac
            defaultMode: 0440
```

The server image runs as uid 10001 with `fsGroup`, so `/data/attachments` is
writable if the PVC mount is. Size the PVC for `IMESSAGE_ATTACH_QUOTA_BYTES`
plus headroom: the quota is enforced on stored bytes after each download and
hourly, not before, and an in-flight download can add up to one file cap.
Outbound uploads are spooled to `/data/attachments/outbound` and deleted when
the request ends.

## Service

A second Service so the files port is addressable on its own (the existing
Service for `:8790` stays as is and is NOT referenced by the Ingress):

```yaml
apiVersion: v1
kind: Service
metadata:
  name: imessage-files
  namespace: imessage
spec:
  selector:
    app.kubernetes.io/name: imessage        # same selector as the server pod
  ports:
    - {name: files, port: 80, targetPort: files}
```

## Ingress (modelled on apps/memos/ingress.yaml)

No forward-auth: the HMAC signature is the gate and chat clients (and the people
behind them) must be able to fetch the link. crowdsec and rate-limit stay.

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: imessage-files
  namespace: imessage
  annotations:
    traefik.ingress.kubernetes.io/router.entrypoints: websecure
    cert-manager.io/cluster-issuer: letsencrypt-prod
    traefik.ingress.kubernetes.io/router.middlewares: kube-system-crowdsec@kubernetescrd,kube-system-rate-limit@kubernetescrd
spec:
  ingressClassName: traefik
  tls:
    - hosts: [imessage-files.sandstorm.chat]
      secretName: imessage-files-tls
  rules:
    - host: imessage-files.sandstorm.chat
      http:
        paths:
          - path: /dl/
            pathType: Prefix
            backend:
              service:
                name: imessage-files
                port: {number: 80}
```

Only `/dl/` is routed, and the server itself answers 404 to every other path on
that port, so a path mistake here still cannot reach the API. DNS:
`imessage-files.sandstorm.chat` needs the same record as the other public hosts.

## NetworkPolicy

Add to the namespace's policy: Traefik may reach the files port and nothing
else; the bridge (same pod) keeps reaching `:8790` over localhost/pod network.

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: imessage-files-from-traefik
  namespace: imessage
spec:
  podSelector:
    matchLabels:
      app.kubernetes.io/name: imessage
  policyTypes: [Ingress]
  ingress:
    - from:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: kube-system
          podSelector:
            matchLabels:
              app.kubernetes.io/name: traefik
      ports:
        - {port: 8791, protocol: TCP}
```

If the namespace is default-deny, the existing allow for `:8790` must NOT be
widened to Traefik. Traefik's labels here are an assumption; match the real ones.

Egress for the OUTBOUND fetch (bridge container): it needs 80/443 to the public
internet. Keep an egress rule that blocks RFC 1918 ranges (as in
`deploy/gitops/openbubbles-relay/networkpolicy.yaml`). That is a second layer
under the bridge's own SSRF checks, not a replacement. The server also needs
the existing egress to Apple (MMCS uses HTTPS to Apple hosts).

## Security notes

- **Files are untrusted third-party content.** Names, declared mime type, size
  and bytes all come from the sender. The server never uses the sender's name or
  path: files are stored under a random 128-bit id, the display name is reduced
  to `[A-Za-z0-9._ ()-]` (last path component, no leading dot, 100 bytes), the
  mime type is sniffed from the first bytes and a claimed type is honoured only
  when well-formed and not HTML/XML/SVG/script/`text/*`.
- **Always a download.** `Content-Disposition: attachment`, `nosniff`,
  `Content-Security-Policy: default-src 'none'; sandbox`, `no-store`. Nothing is
  rendered inline from this origin. A chat client's own preview/thumbnailer is
  outside this server's control; it should treat the file as hostile too.
- **Signed, short-lived links.** `exp` and the id are covered by an HMAC-SHA256;
  the signature is verified (constant time) before expiry or existence is
  revealed. Default TTL 30 minutes. The link is a bearer capability: anyone who
  sees it (link previews, logs of the chat service, shared screenshots) can
  download until it expires. Link previews by a chat client's server also fetch
  it, which is fine (it is a download, not a render) but means the URL leaves
  your network.
- **Separate port, one route.** The API on `:8790` has no authentication of its
  own and is only for in-cluster use. The files listener serves exactly
  `GET /dl/<id>`; there is no directory listing and every other path or method
  is 404/405. The Ingress routes `/dl/` only.
- **Bounds.** Per-file cap enforced while writing (not just from the declared
  size), a total byte quota with oldest-first eviction, and a maximum age; an
  hourly sweep also removes stale half-written files. Downloads run in their own
  tasks so a slow MMCS fetch cannot stall message handling.
- **End-to-end encryption does not cover these links.** A file URL is an
  ordinary HTTPS link: the file leaves iMessage's encryption and sits on the PVC
  in plaintext until evicted. The PVC is the trust boundary; use the encrypted
  volume the registration state already uses. In client mode with OMEMO the URL
  itself is carried in the encrypted body, but the file behind it is not
  encrypted. (Component mode has no OMEMO at all.)
- **Outbound SSRF.** The owner-supplied URL is fetched by the bridge with the
  rules in `bridge/README.md` (public addresses only, ports 80/443, redirect
  re-checks, byte cap). The remaining outbound risk is the owner's own account
  being compromised, which can already send any text as the number.
- **Not yet covered:** malware scanning of stored files; per-sender quotas; Live
  Photo, sticker and voice-message mapping; and any proof that Apple's MMCS
  accepts this identity (see README).
