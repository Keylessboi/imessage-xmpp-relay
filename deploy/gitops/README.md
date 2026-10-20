# GitOps for openbubbles-relay

Manifests for the self-hosted OpenBubbles registration relay (the
NAC / validation-data oracle that replaces an always-on iPhone), written to the
conventions of an ArgoCD-managed GitOps repo. That repo is the source of truth for
anything cluster-shaped; this file records what to copy into it and what is
still unproven.

## What is here

| Path | What it is | Where it goes in your GitOps repo |
|---|---|---|
| deploy/gitops/openbubbles-relay/ | The app directory: 4 kustomize resources | apps/openbubbles-relay/ |
| .../namespace.yaml, networkpolicy.yaml, deployment.yaml | The Namespace (shared), the namespace-wide policy, the Deployment + LoadBalancer Service | unchanged |
| .../kustomization.yaml | Names the app, sets the wave and the house labels, includes ../../components/protect-state | unchanged |
| .../gitops-fragments/doppler-secret.yaml | The DopplerSecret, which must live next to the operator | appended to apps/doppler/dopplersecrets.yaml |
| .../gitops-fragments/secrets-register-entry.yaml | The rotation-register entry (HS-ROT-01) | inserted into security/registers/secrets.yaml |
| deploy/components/protect-state/ | A byte-identical copy of the real component, so the relative path in the kustomization resolves in THIS repo | nothing - do not copy it |

## What to copy where

1. Copy the app directory, name and all:

       cp -a deploy/gitops/openbubbles-relay ~/Projects/<your-gitops-repo>/apps/openbubbles-relay

   The root ApplicationSet's git directory generator scans apps/* and names the
   Application after the directory, so there is no list to edit and no
   Application to declare. Do not rename it: the name is the app name.

   gitops-fragments/ travels with the directory and is inert there. It is not
   listed under resources:, so it is never rendered and never applied; it stays
   beside the app so the next person finds all three artifacts together.

2. Append the DopplerSecret to apps/doppler/dopplersecrets.yaml. Every
   DopplerSecret in the cluster lives in that one file because the operator
   refuses to reconcile one that sits outside its own namespace - that file's
   header explains it.

3. Insert the register entry into security/registers/secrets.yaml, in id order
   (S-47 was the highest when this was written, so the fragment uses S-48).
   Without it, CI fails HS-ROT-01. That register is kind: inventory, so it takes
   effect without the owner's hardware-key signature; the exception registers
   (publication, permissions, waivers) are the ones that need one.

4. Add a row to docs/security/00-system-description.md, section 4. Policy 7.2.2
   requires a data class and a tier recorded there before the first deployment:

       | openbubbles-relay | iMessage relay: NAC/validation-data oracle replacing a powered-on iPhone | Bearer-code -> hardware-profile map (Secret); public IMD cache | C4 | 2 |

5. Nothing else. No ApplicationSet entry, no app list, no docs change beyond
   that row (a line in docs/RUNDOWN.md is optional and customary).

6. Do not copy deploy/components/. See the next section.

## Why this builds standalone

The app's kustomization includes the house component:

    components:
      - ../../components/protect-state

From apps/openbubbles-relay/ that resolves to the real
your GitOps repo/components/protect-state. From deploy/gitops/openbubbles-relay/ it
resolves to deploy/components/protect-state, which is a byte-identical copy
placed there for exactly this reason: so the directory can be rendered and
reviewed before it lands. It is the only reason deploy/ mirrors the GitOps tree
root (deploy/gitops/ <-> apps/, deploy/components/ <-> components/).

Never copy that copy into your GitOps repo. It would duplicate the real component, and
a stale duplicate is worse than none.

## Validate

The render is the cheapest gate, and the one this directory was written against.
kustomize itself is not installed on the development host; kubectl embeds kustomize v5.8.1,
which is the version this repo's CI renders with and the version this cluster's
ArgoCD (v3.5.3) ships - so the command below produces what ArgoCD will. From the
openbubbles-relay repo root:

    kubectl kustomize deploy/gitops/openbubbles-relay

It succeeded: exit 0, four documents, 3907 bytes - Namespace, Service,
Deployment, NetworkPolicy. Before pushing, run what CI runs, from the
your GitOps repo root. Most of these tools are missing on a typical development host, so they have NOT
been run here:

    python3 -c "import yaml,sys; list(yaml.safe_load_all(open(sys.argv[1])))" apps/openbubbles-relay/deployment.yaml
    kustomize build --enable-helm apps/openbubbles-relay >/dev/null
    python3 scripts/ci/check-invariants.py
    kustomize build apps/openbubbles-relay | kubectl apply --dry-run=server --validate=strict -f -

The last line is the highest-yield check in that repo and the one that cannot be
run from here: it needs the API server, and the development host is not on the cluster's
LAN.

What WAS checked locally, per file and on the render: every YAML file parses
under PyYAML; no duplicate mapping keys (the strictness Go's yaml - kustomize,
ArgoCD and the API server - applies); the Namespace carries
argocd.argoproj.io/sync-options: Prune=false,Delete=false from the component; no
PersistentVolumeClaim or PersistentVolume is rendered; the Deployment has both a
readiness and a liveness probe; the only namespaceSelector peer (kube-system) has
no NetworkPolicy of its own, so no cross-namespace flow is declared on one side
only; data-class and criticality labels are present and within the allowed sets.

## The image

Built from this repo's deploy/docker/relay.Dockerfile, from the repository root.
Build it where heavy builds belong (the NAS, as spatial-sidecar is built), not on
the development host:

    docker build -f deploy/docker/relay.Dockerfile -t openbubbles-relay:0.1.0 .

Then get it into the node's containerd. spatial-sidecar documents the pattern
this cluster uses: import it into containerd and also leave the tarball in
/var/lib/rancher/k3s/agent/images/ so k3s re-imports it after a restart. There is
no registry in front of it, which is why the Deployment sets
imagePullPolicy: Never - a missing image then reads ErrImageNeverPull, which says
what is wrong, instead of looping on a Docker Hub pull.

The tag is openbubbles-relay:0.1.0 and it must be bumped in deployment.yaml on
every build. A digest pin is not available for an image no registry serves; what
policy requires is that the tag is not floating. policy/supply.rego denies
latest/stable/edge and friends outright, so the Dockerfile's own -t
openbubbles-relay:latest suggestion must not be what gets deployed.

## The secret

profiles.json maps a bearer code to a hardware profile. It is C4: that code is
what authenticates a client to the relay. It is never committed, never printed,
and no agent reads its value. It reaches the cluster the way every other secret
here does - Doppler, through the operator:

1. In Doppler, project kubernetes, config prd, create the key
   OPENBUBBLES_PROFILES_JSON with the whole profiles.json as plain text. The
   owner does this by hand, in the dashboard or in a shell that does not echo it.
   Do not use doppler secrets upload: profiles.json is a JSON object, so upload
   would split it into one Doppler secret per account rather than one value
   holding the file.

2. The DopplerSecret (gitops-fragments/doppler-secret.yaml) mirrors that one key
   into namespace openbubbles-relay as Secret openbubbles-relay-profiles with key
   profiles.json - the same asName-as-filename trick apps/doppler/dopplersecrets.yaml
   already uses for immich.json. It is pinned with
   secrets: [OPENBUBBLES_PROFILES_JSON] because without that list the operator
   copies the ENTIRE Doppler config into the namespace.

3. The Deployment mounts that key read-only at /data/profiles.json, by subPath so
   /data itself stays writable, and points PROFILES_FILE at it. The pod cannot
   start without it: a missing Secret leaves the pod in ContainerCreating rather
   than starting a process that would exit on an unreadable file and crash-loop.

Rotation: change the value in Doppler, then restart the Deployment. The
application reads the file exactly once, before it binds its listener
(main.rs:105-108), so the operator rewriting the Secret is not enough:

    kubectl -n openbubbles-relay rollout restart deploy/openbubbles-relay

That is why the register entry says reload: manual. Do not add
secrets.doppler.com/reload: "true" to work around it - an annotation cannot make a
file that is read once be read again.

One piece of hygiene in this repository: it ships a .gitignore that already
ignores profiles.json and .env. Keep it that way, and never commit the C4
profile file to either repository.


## After the first sync: what to watch

"ArgoCD says Synced" is not verification - that repo's CONTRIBUTING records three
separate failures hidden behind Synced/Healthy in one week. Check in this order.

1. It actually applied. Synced can be a lie when a diff cannot see a change:
   compare what was COMPARED with what was APPLIED -

       kubectl -n argocd get application openbubbles-relay \
         -o jsonpath='{.status.sync.status} {.status.health.status} compared={.status.sync.revision} applied={.status.operationState.finishedAt}{"\n"}'

   expected: Synced Healthy with an applied timestamp at or after the commit.

2. The process came up.

       kubectl -n openbubbles-relay get deploy,pod -o wide
       kubectl -n openbubbles-relay logs deploy/openbubbles-relay

   Look for "loaded N profile(s) from /data/profiles.json" with N greater than
   zero, then "listening on 0.0.0.0:8787". A pod stuck in ContainerCreating is
   the missing Doppler Secret (FailedMount event); N=0 means the Secret exists
   but the Doppler value is empty; a crash-loop with a serde error means the
   value is not the raw file contents.

3. The data plane, not the liveness probe.

       kubectl -n openbubbles-relay exec deploy/openbubbles-relay -- \
         curl -fsS http://127.0.0.1:8787/health          # {"ok":true,"profiles":N}

   Then one real validation-data call, with a code from profiles.json. Run it
   yourself: do not paste a bearer code into a transcript.

       kubectl -n openbubbles-relay exec deploy/openbubbles-relay -- \
         curl -sS -o /dev/null -w '%{http_code}\n' -X POST \
         -H 'Authorization: Bearer <code>' \
         http://127.0.0.1:8787/api/v1/bridge/get-validation-data

   200 with base64 data is the whole point of the service. 404 means the code is
   not a key in profiles.json; 401 means no Authorization header arrived; 502
   means Apple or the mirror was unreachable, and the line above it in the log
   names which URL failed.

4. Egress and the writable cache - the two most likely things to be wrong, and
   neither shows up as a failing probe. The first validation-data call must log

       downloading IMDAppleServices from https://github.com/JJTech0130/nacserver/...
       cached IMDAppleServices to /data/cache/open-absinthe/IMDAppleServices

   The second call must NOT log the download again. Then confirm the file:

       kubectl -n openbubbles-relay exec deploy/openbubbles-relay -- \
         ls -l /data/cache/open-absinthe/

   ~2 MB owned by 10001 proves both the 443 egress rule and that fsGroup/emptyDir
   gave the non-root uid a writable cache. Repeated "download failed" lines mean
   the ipBlock rule is wrong; a permission error means it is not (both are in the
   NetworkPolicy and the fsGroup comment).

5. Reachability from the client.

       kubectl -n openbubbles-relay get svc openbubbles-relay   # ADDRESS = the LB IP

   From a host on the LAN: curl -fsS http://<ADDRESS>:8787/health. Then set the
   phone's relay URL to http://<ADDRESS>:8787.

   If that address answers from inside the cluster but not from the LAN, the
   ingress CIDRs (192.168.1.0/24, 100.64.0.0/10) do not match the address the
   client actually presents - and a kube-router REJECT reads as "connection
   refused", so it looks like nothing is listening. Find the real source address
   and widen the rule to it. Do not set it to 0.0.0.0/0 to make the symptom go
   away: the bearer code is the only authentication, and that would publish the
   endpoint to every pod and every node on the network.

## Not yet verified

Nothing here has been applied. No kubectl apply, no server-side dry-run, no ssh,
no docker build, no cluster query of any kind was made while writing it. What the
manifest says, versus what has been shown:

Proven: the directory renders with kustomize v5.8.1 (exit 0, four documents); every
file parses; no duplicate keys; the Namespace carries the protect-state
annotation; no PVC/PV is rendered; both probes are present; the label sets and
data-class/criticality values are within the policy's allowed sets; every claim
about the application's runtime (ports, endpoints, which file is read when, that
the download is lazy and sha1-verified) was read out of the source at the line
numbers quoted.

Unproven, and each is a real risk:

- Anything cluster-side: scheduling, image presence, the kubelet accepting the
  pod spec, Pod Security admission, and the API server's own validation
  (--dry-run=server was not run; yamllint, kubeconform, conftest and polaris are
  not installed here, so those CI jobs are unverified).
- The image does not exist yet, so imagePullPolicy: Never and the tag are untested.
- metalLB assigning an address, and externalTrafficPolicy: Local preserving the
  client's source IP under kube-router - the whole ingress NetworkPolicy depends
  on the second one, and it is the reason Local is set.
- The client's real source address. 100.64.0.0/10 assumes the phone reaches this
  through a tailnet subnet router advertising the LAN; 192.168.1.0/24 assumes it
  is on the LAN. Neither was observed.
- The Doppler value, the Secret, and the operator's asName/keys mapping.
- The register entry, and every CI job in your GitOps repo (nothing there was run or
  edited).
- The resource requests and limits in deployment.yaml are estimates. The
  per-request QEMU footprint was not measured; 512Mi is a guess that bounds a
  request burst rather than a measurement.
- readOnlyRootFilesystem is deliberately not set, because the emulator's scratch
  paths were not verified. Setting it blind is a startup crash.
- Plain HTTP: over the LAN the bearer code - a C4 credential - is cleartext.
  Policy section 6 requires encryption in transit for C4 on each hop. The VPN
  path is encrypted by WireGuard/Tailscale; the bare LAN path is not. Either use
  the VPN path, or front this with TLS (which means the Ingress path and all of
  section 9.7's obligations: cert-manager, CrowdSec, rate limit, and Authentik
  forward-auth or an owner-signed publication register entry). This is a decision
  for the owner, and it is the one open policy question in this manifest.

## Follow-ups

- imessage-server (:8790, its own /data volume) and bridge. Each becomes its own
  apps/ directory with its own kustomization, targeting namespace
  openbubbles-relay, shipping NO namespace.yaml (this directory owns it) and no
  NetworkPolicy ingress of its own for the flows this namespace's policy already
  admits: imessage-server -> openbubbles-relay:8787 and bridge ->
  imessage-server:8790. Do not write them blind: their interfaces are not
  known yet.
- Pin the LoadBalancer address once it is assigned (the commented annotation in
  deployment.yaml), so the phone's relay URL survives a Service recreation.
- If the community mirror ever disappears, the emptyDir cache becomes the only
  copy of IMDAppleServices: move it to a small local-path PVC and add the chown
  init container that a local-path volume needs (the fsGroup comment in
  deployment.yaml says why fsGroup cannot do it).
- Revisit the resource limits after watching one real validation-data call.

