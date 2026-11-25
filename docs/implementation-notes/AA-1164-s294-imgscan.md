# AA-1164 (S294 lane imgscan): Booking API image picks up Debian fixes at the next build

## Decisions
- The nightly Image Scan (running images) failed on CVE-2026-124851 (libzstd1, HIGH, P1, fixed in
  1.5.7+dfsg-1+deb13u1) in the running Booking API image. The running image was built on 24/11, before the Debian
  fix; the CIS API image built on 25/11 already carries the fixed package. The Dockerfile already runs
  `apt-get upgrade`, and both builds (Deploy Dev, CI Docker Build Check) are plain `docker build` on a fresh hosted
  runner with no layer cache, so a rebuild is the fix: this PR is that rebuild (it touches `backend/`, which triggers
  Deploy Dev on merge).
- Added `--pull` to both API image builds so the base tag is always read from the registry, never a copy the runner
  image might hold, and a test that keeps the builds free of any layer cache (`--cache-from`, buildx gha cache).

## Changed
- `backend/Dockerfile`: comment only (why the upgrade layer is always fresh). No app code changed.
- `.github/workflows/deploy-dev.yml`, `.github/workflows/ci.yml`: `docker build --pull`.
- `backend/tests/unit/test_s294_image_os_packages_fresh.py`: guard test.

## Tradeoffs
- No cache-busting build argument: there is no layer cache to bust, so an argument would only add noise. If a
  layer cache is ever added for speed, the guard test fails and the author must keep the upgrade layer fresh.
- A weekly forced rebuild of images that have not changed is not added here (follow-up idea): the nightly scan
  already reports a running image that falls behind.

## Should know
- The CI Docker Build Check of this PR runs Trivy on the rebuilt image (fails on fixable HIGH/CRITICAL): its green run
  is the first proof the fixed libzstd1 is in.
