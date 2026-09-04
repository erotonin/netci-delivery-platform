FROM node:26.8.1-alpine3.24 AS build

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
RUN npm run build

FROM nginx:1.29.8-alpine3.23

COPY nginx.conf /etc/nginx/nginx.conf
COPY --from=build /app/dist /usr/share/nginx/html

# The custom config keeps its pid and temporary files under /tmp, so the
# production web tier never needs root after the image has been assembled.
USER nginx
EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
  CMD wget -q -O - http://127.0.0.1:8080/healthz || exit 1

CMD ["nginx", "-g", "daemon off;"]
