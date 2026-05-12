FROM golang:1.24-alpine AS builder
WORKDIR /app
COPY go.mod go.sum ./
COPY vendor/ ./vendor/
COPY *.go ./
COPY cmd/ ./cmd/
COPY planning/ ./planning/
RUN CGO_ENABLED=0 go build -mod=vendor -o shark-rl ./cmd/

FROM alpine:3.20
RUN apk add --no-cache ca-certificates
COPY --from=builder /app/shark-rl /usr/local/bin/shark-rl
EXPOSE 8081
CMD ["shark-rl"]
