FROM node:22.23.2-alpine3.24 AS build

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
RUN npm run build

FROM nginx:1.29.8-alpine3.23

COPY nginx.conf /etc/nginx/nginx.conf
COPY nginx/api-upstream.conf.template /etc/nginx/templates/api-upstream.conf.template
COPY nginx/csp.conf.template /etc/nginx/templates/csp.conf.template
COPY --from=build /app/dist /usr/share/nginx/html

# Where the API is. compose names the service netci-api; the Helm chart sets this to the
# release's backend Service. The entrypoint renders the template into /tmp, which the
# non-root nginx user can write.
ENV NETCI_API_UPSTREAM=netci-api:8000 \
    NETCI_OIDC_ORIGIN="" \
    NGINX_ENVSUBST_OUTPUT_DIR=/tmp

# The custom config keeps its pid and temporary files under /tmp, so the
# production web tier never needs root after the image has been assembled.
USER nginx
EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
  CMD wget -q -O - http://127.0.0.1:8080/healthz || exit 1

CMD ["nginx", "-g", "daemon off;"]
