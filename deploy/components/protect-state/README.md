# This directory is a verbatim copy, and only exists so the app directory renders here

deploy/gitops/openbubbles-relay/kustomization.yaml includes the house component as

    components:
      - ../../components/protect-state

In your GitOps repo, apps/openbubbles-relay/ resolves that to the real
components/protect-state. In this repository, deploy/gitops/openbubbles-relay/
resolves it to this file - which is why deploy/ mirrors that repo's root
(deploy/gitops/ <-> apps/, deploy/components/ <-> components/).

kustomization.yaml here is byte-identical to the real component, so the render
done in this repository is the render ArgoCD will produce. Refresh it with:

    cp <your-gitops-repo>/components/protect-state/kustomization.yaml \
       deploy/components/protect-state/kustomization.yaml

Do NOT copy this directory into your GitOps repo: it would duplicate the real component.
