# Stage 1: refuse to build an image from code that does not type-check.
FROM node:24-slim AS check
WORKDIR /app
COPY package.json package-lock.json tsconfig.json ./
RUN npm ci
COPY src ./src
COPY tests ./tests
RUN npx tsc --noEmit

# Stage 2: runtime. TypeScript runs directly through tsx; no separate build output.
FROM node:24-slim
WORKDIR /app
ENV NODE_ENV=production INBOX_DB_PATH=/data/inbox.db NODE_NO_WARNINGS=1
COPY package.json package-lock.json ./
RUN npm ci --omit=dev
COPY --from=check /app/src ./src
COPY prompts ./prompts
COPY ui ./ui
COPY data/emails ./data/emails
EXPOSE 8002
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
  CMD node -e "fetch('http://localhost:8002/health').then(r => process.exit(r.ok ? 0 : 1)).catch(() => process.exit(1))"
CMD ["npx", "tsx", "src/main.ts"]
