# syntax=docker/dockerfile:1.12
# Admin console and BFF (Next.js standalone server). Build from the repository root:
#   docker build -f deploy/images/web.Dockerfile -t nexus-web .
# The image carries no environment-specific settings: NEXUS_API_URL and the other variables are
# read on the server at run time.
# Base images are pinned by tag and multi-arch index digest; update both together.
ARG NODE_IMAGE=node:24.22.3-trixie-slim@sha256:af02d57b57719b9657a45199a79264a9437940cd55b3c7b3fc99615654f124a0
ARG RUNTIME_IMAGE=gcr.io/distroless/nodejs24-debian13:nonroot@sha256:6abfcb367ebc291572c774c1592a986be2ab26a2486f73093401327495a5962a
ARG PNPM_VERSION=12.18.0

FROM ${NODE_IMAGE} AS build
ARG PNPM_VERSION
RUN npm install --global --no-fund --no-audit "pnpm@${PNPM_VERSION}"
ENV NEXT_TELEMETRY_DISABLED=1 PNPM_HOME=/pnpm
WORKDIR /app
COPY src/web/package.json src/web/pnpm-lock.yaml src/web/pnpm-workspace.yaml ./
RUN --mount=type=cache,id=pnpm-store,target=/pnpm/store \
    pnpm config set store-dir /pnpm/store \
    && pnpm install --frozen-lockfile
COPY src/web/ ./
RUN NEXUS_WEB_STANDALONE=1 pnpm build

FROM ${RUNTIME_IMAGE}
LABEL org.opencontainers.image.title="nexus-web" \
      org.opencontainers.image.description="Nexus admin console and BFF" \
      org.opencontainers.image.source="https://github.com/herta-network/Nexus"
WORKDIR /app
COPY --from=build /app/.next/standalone ./
COPY --from=build /app/.next/static ./.next/static
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    HOSTNAME=0.0.0.0 \
    PORT=3000
USER 65532:65532
EXPOSE 3000
# distroless/nodejs uses node as its entrypoint.
CMD ["server.js"]
