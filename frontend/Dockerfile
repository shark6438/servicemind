FROM node:24-bookworm-slim@sha256:0e0ff40c39bc087845bfb27465a0df4ea419520094bc35842ff83dd8cbe6f9b6 AS dependencies
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

FROM node:24-bookworm-slim@sha256:0e0ff40c39bc087845bfb27465a0df4ea419520094bc35842ff83dd8cbe6f9b6 AS build
WORKDIR /app
ARG NEXT_PUBLIC_SERVICEMIND_API_URL=http://127.0.0.1:18080
ARG NEXT_PUBLIC_KEYCLOAK_URL=http://127.0.0.1:8090
ARG NEXT_PUBLIC_KEYCLOAK_REALM=servicemind
ARG NEXT_PUBLIC_KEYCLOAK_CLIENT_ID=servicemind-api
ENV NEXT_TELEMETRY_DISABLED=1
ENV NEXT_PUBLIC_SERVICEMIND_API_URL=$NEXT_PUBLIC_SERVICEMIND_API_URL
ENV NEXT_PUBLIC_KEYCLOAK_URL=$NEXT_PUBLIC_KEYCLOAK_URL
ENV NEXT_PUBLIC_KEYCLOAK_REALM=$NEXT_PUBLIC_KEYCLOAK_REALM
ENV NEXT_PUBLIC_KEYCLOAK_CLIENT_ID=$NEXT_PUBLIC_KEYCLOAK_CLIENT_ID
COPY --from=dependencies /app/node_modules ./node_modules
COPY . .
RUN npm run build

FROM gcr.io/distroless/nodejs24-debian13:nonroot@sha256:bb6b03d81066993293a10feda7250e8e1cc034035fe9b61cfceededa7c8bf04d AS runtime
WORKDIR /app
ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
ENV HOSTNAME=0.0.0.0
ENV PORT=3000
USER nonroot
COPY --from=build --chown=nonroot:nonroot /app/public ./public
COPY --from=build --chown=nonroot:nonroot /app/.next/standalone ./
COPY --from=build --chown=nonroot:nonroot /app/.next/static ./.next/static
EXPOSE 3000
CMD ["server.js"]
