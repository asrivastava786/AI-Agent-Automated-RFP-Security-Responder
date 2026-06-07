# ─────────────────────────────────────────────────────────────────────────────
# Stage 1: deps – install node_modules
# ─────────────────────────────────────────────────────────────────────────────
FROM node:20-alpine AS deps

WORKDIR /app

COPY package.json package-lock.json* ./
RUN npm ci --ignore-scripts

# ─────────────────────────────────────────────────────────────────────────────
# Stage 2: builder – Next.js production build
# ─────────────────────────────────────────────────────────────────────────────
FROM node:20-alpine AS builder

WORKDIR /app

COPY --from=deps /app/node_modules ./node_modules
COPY . .

# Bake public env vars at build time (secrets must NOT go here)
ARG NEXT_PUBLIC_APP_URL
ENV NEXT_PUBLIC_APP_URL=${NEXT_PUBLIC_APP_URL}

# next.config must have output: "standalone" for minimal image
RUN npm run build

# ─────────────────────────────────────────────────────────────────────────────
# Stage 3: runtime – standalone Next.js server
# ─────────────────────────────────────────────────────────────────────────────
FROM node:20-alpine AS runtime

RUN addgroup --system --gid 1001 nodejs && \
    adduser  --system --uid  1001 nextjs

WORKDIR /app

# Standalone output contains the minimal server + node_modules subset
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static
COPY --from=builder --chown=nextjs:nodejs /app/public ./public

USER nextjs

ENV NODE_ENV=production \
    PORT=3000 \
    HOSTNAME=0.0.0.0

EXPOSE 3000

HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
    CMD wget -qO- http://localhost:3000/api/health || exit 1

CMD ["node", "server.js"]
