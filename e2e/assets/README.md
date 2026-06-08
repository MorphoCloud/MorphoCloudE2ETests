# Deployable assets

Files here are **not** part of the Python harness — they are deployed into
`MorphoCloud/Test-Instances`.

## `e2e-verify-instance.yml` — in-guest readiness probe

The reliable-readiness check's layer 3 (DESIGN.md §3): a `workflow_dispatch` workflow
that runs **on the Test-Instances self-hosted runner**, so it uses the runner's
already-authorized SSH key (no extra credential). Given an `issue_number`, it resolves
the instance's floating IP from OpenStack, SSHes in, and asserts the data volume is
mounted, `/home/exouser` is symlinked onto it, and Slicer is present — failing the run
(non-zero) on the first broken invariant. The harness (`_lifecycle.assert_ready`)
dispatches it and treats the run conclusion as the readiness verdict.

### Deploy

```bash
cp e2e/assets/e2e-verify-instance.yml \
   ~/Desktop/Projects/Test-Instances/.github/workflows/e2e-verify-instance.yml
cd ~/Desktop/Projects/Test-Instances
git add .github/workflows/e2e-verify-instance.yml
git commit -m "test: add E2E in-guest readiness probe"
git push origin main
```

### Keep it from being pruned by vendorize

It lives **only** in Test-Instances (test-only; not in MWF). Confirm `nox -s vendorize`
does not delete target-only files; if it prunes, add `e2e-verify-instance.yml` to the
noxfile exclude list. After any vendorize, check the file is still present.
