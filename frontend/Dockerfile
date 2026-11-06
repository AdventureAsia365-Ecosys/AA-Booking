# syntax=docker/dockerfile:1
# AA-911 (root ADR 0019 d23) — Booking admin image: the Next.js standalone server, run as the `booking-admin` container
# of the staff web task on ECS Dev (AA-CIS-Infra modules/environment/staff_web.tf: Fargate ARM64, port 3001, health
# check /api/healthz; the CIS admin is the other container of the same task, on 3000). Built natively on an arm64
# runner (.github/workflows/deploy-admin-dev.yml). Same shape as AA-Partner web/Dockerfile and the CIS admin image.
FROM node:22-alpine AS build
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
# The build never needs a secret: every server value (BOOKING_API_URL, CIS_API_URL, the Cognito client id and secret) is
# read from the task's environment at run time (lib/config.ts, lib/cognito.ts). Nothing public is baked in either.
ARG GIT_SHA=unknown
ENV NEXT_OUTPUT=standalone
RUN NODE_OPTIONS=--max-old-space-size=2048 npm run build

FROM node:22-alpine AS runtime
ARG GIT_SHA=unknown
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3001 \
    HOSTNAME=0.0.0.0 \
    GIT_SHA=${GIT_SHA} \
    # Keep-alive must outlive the ALB idle timeout (100 s); Node's 5 s default makes the ALB reuse a closed
    # connection and answer 502 (lesson S231, AA-741). Read by the Next standalone server.
    KEEP_ALIVE_TIMEOUT=105000
# The server needs node only: drop npm, npx and corepack from the runtime image (smaller attack surface).
RUN rm -rf /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/corepack \
      /usr/local/bin/npm /usr/local/bin/npx /usr/local/bin/corepack
WORKDIR /app
COPY --from=build --chown=10001:10001 /app/.next/standalone ./
COPY --from=build --chown=10001:10001 /app/.next/static ./.next/static
COPY --from=build --chown=10001:10001 /app/public ./public
USER 10001
EXPOSE 3001
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["node", "-e", "fetch('http://127.0.0.1:'+(process.env.PORT||3001)+'/api/healthz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"]
# Fargate sets HOSTNAME to the task's own hostname at run time, overriding the ENV above, and the Next standalone
# server binds to HOSTNAME: it would listen on the private IP only and the 127.0.0.1 health check would fail (lesson
# S249, AA-935). Force all interfaces at start, without a shell.
CMD ["node", "-e", "process.env.HOSTNAME='0.0.0.0'; require('./server.js')"]
