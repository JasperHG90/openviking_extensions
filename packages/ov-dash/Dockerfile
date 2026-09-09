# Build the client and bundle the server, then ship neither the toolchain nor
# node_modules: the server is bundled to one file and the client to static
# assets, so the runtime image carries only what it runs.

# Pinned to the machine doing the building, not the machine being built for.
# This stage emits JavaScript and static assets, which are the same bytes on
# every architecture, so running npm, vite and esbuild under emulation for a
# cross build costs minutes and buys nothing. Only the runtime stage below is
# per-architecture.
FROM --platform=$BUILDPLATFORM node:22-alpine AS build
WORKDIR /app

# Dependencies first, so a source-only change reuses this layer.
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY tsconfig.json vite.config.ts svelte.config.js index.html ./
COPY src ./src

RUN npm run build:client && npm run build:server


FROM node:22-alpine AS runtime
WORKDIR /app

ENV NODE_ENV=production

# node:alpine already ships an unprivileged `node` user; the app needs no write
# access to its own directory, so it runs as that user against a read-only tree.
COPY --from=build --chown=root:root /app/dist ./dist

USER node
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:'+(process.env.PORT||8080)+'/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

CMD ["node", "dist/server/main.js"]
